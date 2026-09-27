#!/usr/bin/env python3
"""规划结果可视化：把 ``.psap`` 渲染成视频。

手指用点表示，运动轨迹用线表示。主要用途是**叠到录屏上做对照**，所以默认输出带 alpha 的
``.mov``（QuickTime Animation），能直接拖进 Premiere / AE；不想要透明就 ``--background``
换个底色（绿幕用 ``chroma``）。

    python render.py plans/xxx.psap
    python render.py plans/xxx.psap --size 1280x720 --fps 30
    python render.py plans/xxx.psap --background chroma
    python render.py plans/xxx.psap --no-paths --point-color "#00ff88" --point-radius 10
    python render.py plans/xxx.psap --path-color blue --path-width 5 --path-window 800

关于抗锯齿，有个坑值得写下来：OpenCV 的 ``LINE_AA`` **只有在单通道图上才给出正确的覆盖率**
（白 255 叠在黑 0 上，结果就是覆盖率本身）。直接往 RGBA 上画是不行的 —— 透明像素会被当成
黑色参与混合，边缘立刻出现一圈暗边。所以这里是先出覆盖率掩膜（轨迹一张、手指一张），
再用 numpy 自己做直通 alpha 的合成。

另外，笔画只在各自的包围盒里光栅化与合成：一帧通常只有几个点加几条短线，
按整屏算 1920x1080 的浮点合成会白白慢上两个数量级。
"""

from __future__ import annotations

import argparse
import math
import sys
from bisect import bisect_left, bisect_right
from collections import defaultdict
from pathlib import Path
from typing import Sequence

import cv2
import imageio_ffmpeg
import numpy as np
from tqdm import tqdm

from algorithms.geometry import Screen
from algorithms.utils import PlanResult, Touch
from formats.storage import decode_plan

ROOT = Path(__file__).resolve().parent
RENDERS_DIR = ROOT / "renders"

DEFAULT_SIZE = (1920, 1080)
DEFAULT_FPS = 60

Color = tuple[int, int, int]

NAMED_COLORS: dict[str, str] = {
    "red": "#ff3030",
    "green": "#00ff00",
    "chroma": "#00b140",
    """广播标准的绿幕绿，抠像比纯绿好用。"""
    "blue": "#3060ff",
    "white": "#ffffff",
    "black": "#000000",
    "gray": "#808080",
    "grey": "#808080",
    "yellow": "#ffff30",
    "cyan": "#30ffff",
    "magenta": "#ff30ff",
    "orange": "#ffa030",
}

TRANSPARENT = {"none", "transparent", "alpha"}

# 容器 -> (编码器, 输出像素格式)。只有前两个带 alpha 通道。
CONTAINERS: dict[str, tuple[str, str]] = {
    ".mov": ("qtrle", "argb"),
    ".webm": ("libvpx-vp9", "yuva420p"),
    ".mp4": ("libx264", "yuv420p"),
    ".mkv": ("libx264", "yuv420p"),
}


# --------------------------------------------------------------- 参数解析


def parse_color(text: str) -> Color | None:
    """``#rrggbb`` / ``rrggbb`` / ``#rgb`` / ``r,g,b`` / 颜色名 / ``none``。"""
    value = text.strip().lower()
    if value in TRANSPARENT:
        return None
    value = NAMED_COLORS.get(value, value)

    if "," in value:
        parts = [part.strip() for part in value.split(",")]
        if len(parts) != 3:
            raise ValueError(f"颜色写不对：{text!r}")
        channels = tuple(int(part) for part in parts)
    else:
        value = value.lstrip("#")
        if len(value) == 3:
            value = "".join(char * 2 for char in value)
        if len(value) != 6 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError(f"颜色写不对：{text!r}")
        channels = tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))

    if any(not 0 <= channel <= 255 for channel in channels):
        raise ValueError(f"颜色分量超范围：{text!r}")
    return channels  # type: ignore[return-value]


def as_color(value: str | Color | None) -> Color | None:
    """既能收命令行传的颜色名/十六进制串，也能直接收 ``(r, g, b)``。"""
    return parse_color(value) if isinstance(value, str) else value


def parse_size(text: str) -> tuple[int, int]:
    for separator in ("x", "X", "*", ",", "："):
        if separator in text:
            left, _, right = text.partition(separator)
            return int(left), int(right)
    raise ValueError(f"尺寸写不对：{text!r}（应是 1920x1080 这样）")


def align_size(size: tuple[int, int]) -> tuple[int, int]:
    """宽高各向上取到 8 的倍数 —— 视频编码器（尤其 yuv420p）要求偶数对齐。"""
    return tuple(max(8, (value + 7) // 8 * 8) for value in size)  # type: ignore[return-value]


# --------------------------------------------------------------- 时间轴


class Motion:
    """把事件流按指针拆开，回答"某一时刻哪些手指在屏幕上、各自身后拖了多长的尾巴"。"""

    def __init__(self, plan: PlanResult, window_ms: int) -> None:
        tracks: dict[int, list[tuple[int, Touch, complex]]] = defaultdict(list)
        for timestamp, events in plan.frames:
            for event in events:
                tracks[event.pointer].append((timestamp, event.action, complex(event.x, event.y)))
        self.tracks = tracks
        self.stamps = {pointer: [item[0] for item in items] for pointer, items in tracks.items()}
        self.window = window_ms

    def at(self, moment: float) -> tuple[list[list[complex]], list[complex]]:
        """返回 (轨迹们, 手指们)，坐标都是虚拟屏幕坐标。"""
        trails: list[list[complex]] = []
        heads: list[complex] = []

        for pointer, items in self.tracks.items():
            stamps = self.stamps[pointer]
            index = bisect_right(stamps, moment) - 1
            if index < 0 or items[index][1] is Touch.UP:
                continue

            heads.append(items[index][2])

            start = 0 if self.window <= 0 else bisect_left(stamps, moment - self.window)
            # 窗口里可能有"抬起又按下"，轨迹只从最后一次按下算起
            for cursor in range(index, start - 1, -1):
                if items[cursor][1] is Touch.UP:
                    start = cursor + 1
                    break
            # 手指最后一次动过已经比窗口还早时，bisect 会越过 index，
            # 但手指现在还在屏幕上，轨迹至少要有它当前所在的那一点
            start = min(start, index)
            trails.append([items[cursor][2] for cursor in range(start, index + 1)])

        return trails, heads


# --------------------------------------------------------------- 绘制


def to_pixel(point: complex, screen: Screen, size: tuple[int, int]) -> tuple[float, float]:
    """虚拟屏幕坐标（y 轴向上）-> 像素坐标（y 轴向下）。"""
    width, height = size
    return (point.real * width / screen.width, (screen.height - point.imag) * height / screen.height)


def to_pixels(stroke: Sequence[complex], screen: Screen, size: tuple[int, int]) -> list[tuple[float, float]]:
    return [to_pixel(point, screen, size) for point in stroke]


def strokes_box(
    strokes: Sequence[Sequence[tuple[float, float]]], padding: float, size: tuple[int, int]
) -> tuple[int, int, int, int] | None:
    """所有笔画的外接矩形（含线宽/半径的余量），并夹到画面内。"""
    if not strokes:
        return None
    xs = [point[0] for stroke in strokes for point in stroke]
    ys = [point[1] for stroke in strokes for point in stroke]
    width, height = size
    x0 = max(0, int(math.floor(min(xs) - padding)))
    y0 = max(0, int(math.floor(min(ys) - padding)))
    x1 = min(width, int(math.ceil(max(xs) + padding)) + 1)
    y1 = min(height, int(math.ceil(max(ys) + padding)) + 1)
    if x1 <= x0 or y1 <= y0:
        return None
    return x0, y0, x1, y1


def rasterize(
    strokes: Sequence[Sequence[tuple[float, float]]],
    thickness: int,
    box: tuple[int, int, int, int],
) -> np.ndarray:
    """把笔画画进单通道掩膜。白色 255 叠在黑色 0 上，得到的正是抗锯齿覆盖率。"""
    x0, y0, x1, y1 = box
    mask = np.zeros((y1 - y0, x1 - x0), np.uint8)

    for stroke in strokes:
        if not stroke:
            continue
        points = [(int(round(x - x0)), int(round(y - y0))) for x, y in stroke]
        if len(points) >= 2:
            cv2.polylines(mask, [np.asarray(points, np.int32)], False, 255, thickness, cv2.LINE_AA)
        else:
            cv2.circle(mask, points[0], max(1, thickness // 2), 255, -1, cv2.LINE_AA)
    return mask


def blend(frame: np.ndarray, box: tuple[int, int, int, int], coverage: np.ndarray, color: Color) -> None:
    """把一层纯色按覆盖率合成进 frame（直通 alpha 的 over）。frame 是 RGBA。"""
    x0, y0, x1, y1 = box
    region = frame[y0:y1, x0:x1]

    alpha = coverage.astype(np.float32) * (1.0 / 255.0)
    source = np.asarray(color, np.float32)

    under_alpha = region[..., 3].astype(np.float32) * (1.0 / 255.0)
    under_rgb = region[..., :3].astype(np.float32)

    out_alpha = alpha + under_alpha * (1.0 - alpha)
    weights = under_alpha * (1.0 - alpha)
    rgb = source * alpha[..., None] + under_rgb * weights[..., None]
    rgb /= np.maximum(out_alpha, 1e-6)[..., None]
    np.clip(rgb, 0.0, 255.0, out=rgb)

    region[..., :3] = rgb
    region[..., 3] = np.clip(out_alpha * 255.0, 0.0, 255.0)


def draw(
    screen: Screen,
    size: tuple[int, int],
    trails: Sequence[Sequence[complex]],
    heads: Sequence[complex],
    *,
    show_paths: bool,
    path_width: float,
    path_color: Color,
    show_points: bool,
    point_radius: float,
    point_color: Color,
    background: Color | None,
) -> np.ndarray:
    width, height = size
    frame = np.zeros((height, width, 4), np.uint8)
    if background is not None:
        frame[..., 0] = background[0]
        frame[..., 1] = background[1]
        frame[..., 2] = background[2]
        frame[..., 3] = 255

    layers: list[tuple[Sequence[Sequence[tuple[float, float]]], int, Color]] = []
    if show_paths and trails:
        layers.append(([to_pixels(trail, screen, size) for trail in trails],
                       max(1, round(path_width)), path_color))
    if show_points and heads:
        layers.append(
            ([[to_pixel(head, screen, size)] for head in heads],
             max(2, round(point_radius * 2)), point_color)
        )

    for strokes, thickness, color in layers:
        box = strokes_box(strokes, thickness / 2 + 1, size)
        if box is None:
            continue
        blend(frame, box, rasterize(strokes, thickness, box), color)

    return frame


# --------------------------------------------------------------- 编码


class VideoWriter:
    """把 RGBA 帧喂给 ffmpeg。``.mov`` 走 qtrle（带 alpha），``.webm`` 走 VP9（带 alpha）。"""

    def __init__(self, path: Path, size: tuple[int, int], fps: int, has_alpha: bool) -> None:
        suffix = path.suffix.lower()
        if suffix not in CONTAINERS:
            supported = ", ".join(sorted(CONTAINERS))
            raise ValueError(f"不认识的容器 {suffix!r}；支持：{supported}")
        codec, pixel_format = CONTAINERS[suffix]
        if not has_alpha and suffix in (".mov", ".webm"):
            # 不透明时没必要用带 alpha 的编码器，体积还更小
            codec, pixel_format = ("libx264", "yuv420p") if suffix == ".mov" else ("libvpx-vp9", "yuv420p")

        path.parent.mkdir(parents=True, exist_ok=True)
        self.generator = imageio_ffmpeg.write_frames(
            str(path),
            size,
            fps=fps,
            codec=codec,
            pix_fmt_in="rgba",
            pix_fmt_out=pixel_format,
            macro_block_size=8,
            ffmpeg_log_level="error",
        )
        self.generator.send(None)

    def __enter__(self) -> VideoWriter:
        return self

    def __exit__(self, *_: object) -> None:
        self.generator.close()

    def write(self, frame: np.ndarray) -> None:
        self.generator.send(frame.tobytes())


# --------------------------------------------------------------- 入口


def render(
    plan: PlanResult | Path,
    output: Path | None = None,
    *,
    size: tuple[int, int] = DEFAULT_SIZE,
    fps: int = DEFAULT_FPS,
    background: str | Color = "none",
    show_points: bool = True,
    point_radius: float = 8.0,
    point_color: str | Color = "#ff3030",
    show_paths: bool = True,
    path_width: float = 3.0,
    path_color: str | Color = "#ff3030",
    path_window_ms: int = 400,
) -> Path:
    """把一份规划结果渲染成视频，返回输出路径。

    ``background="none"`` 输出带 alpha 的视频（只有 .mov / .webm 能做到）；
    给了颜色就铺满底色（绿幕抠像用 ``"chroma"``）。
    """
    if not isinstance(plan, PlanResult):
        plan = decode_plan(Path(plan).read_bytes())

    output = Path(output) if output is not None else RENDERS_DIR / "plan.mov"
    size = align_size(size)
    background_rgb = as_color(background)
    path_rgb = as_color(path_color) or (255, 255, 255)
    point_rgb = as_color(point_color) or (255, 255, 255)

    suffix = output.suffix.lower()
    if background_rgb is None and suffix not in (".mov", ".webm"):
        raise ValueError(
            f"{suffix} 装不了透明通道（H.264 没有 alpha）。"
            f"要么输出 .mov / .webm，要么给个底色，比如 --background chroma"
        )

    motion = Motion(plan, path_window_ms)
    total = int(plan.duration_ms * fps / 1000) + 1

    with VideoWriter(output, size, fps, background_rgb is None) as writer:
        for index in tqdm(range(total), desc="  渲染", unit="帧"):
            moment = index * 1000.0 / fps
            trails, heads = motion.at(moment)
            writer.write(
                draw(
                    plan.screen,
                    size,
                    trails,
                    heads,
                    show_paths=show_paths,
                    path_width=path_width,
                    path_color=path_rgb,
                    show_points=show_points,
                    point_radius=point_radius,
                    point_color=point_rgb,
                    background=background_rgb,
                )
            )
    return output


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="render.py", description="auto_phigros 可视化：把规划结果渲染成视频"
    )
    parser.add_argument("plan", type=Path, help=".psap 文件")
    parser.add_argument("-o", "--output", type=Path, default=None, help="输出文件（默认 renders/<名字>.mov）")
    parser.add_argument("--size", default="1920x1080", help="分辨率（默认 %(default)s）")
    parser.add_argument("--fps", type=int, default=DEFAULT_FPS, help="帧率（默认 %(default)s）")
    parser.add_argument(
        "--background",
        default="none",
        help="底色：none 透明（默认），也可给颜色名 / #rrggbb；绿幕用 chroma",
    )

    points = parser.add_argument_group("点（手指）")
    points.add_argument("--no-points", action="store_true", help="不画手指")
    points.add_argument("--point-radius", type=float, default=8.0, help="半径，单位像素（默认 %(default)s）")
    points.add_argument("--point-color", default="#ff3030", help="颜色（默认 %(default)s）")

    paths = parser.add_argument_group("路径（轨迹）")
    paths.add_argument("--no-paths", action="store_true", help="不画轨迹")
    paths.add_argument("--path-width", type=float, default=3.0, help="粗细，单位像素（默认 %(default)s）")
    paths.add_argument("--path-color", default="#ff3030", help="颜色（默认 %(default)s）")
    paths.add_argument(
        "--path-window",
        type=int,
        default=400,
        metavar="MS",
        help="轨迹最多回溯多久；0 表示从按下画到当下（默认 %(default)s）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.plan.is_file():
        print(f"[render] 找不到 {args.plan}", file=sys.stderr)
        return 2
    try:
        size = parse_size(args.size)
        output = args.output or RENDERS_DIR / f"{args.plan.stem}.mov"
        aligned = align_size(size)
        note = "" if aligned == size else f"（{size[0]}x{size[1]} 已对齐到 8 的倍数）"
        print(f"[render] {args.plan.name} -> {output}")
        print(f"[render] {aligned[0]}x{aligned[1]} @ {args.fps}fps{note}")
        path = render(
            args.plan,
            output,
            size=size,
            fps=args.fps,
            background=args.background,
            show_points=not args.no_points,
            point_radius=args.point_radius,
            point_color=args.point_color,
            show_paths=not args.no_paths,
            path_width=args.path_width,
            path_color=args.path_color,
            path_window_ms=args.path_window,
        )
    except (ValueError, OSError) as error:
        print(f"[render] {error}", file=sys.stderr)
        return 1

    mebibytes = path.stat().st_size / 1024 / 1024
    print(f"[render] 已保存 {path}（{mebibytes:.1f} MiB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
