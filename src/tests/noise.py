"""噪域自检：4.0.1 的 ``blockAreaList`` —— 几何、inset/isSubtract 的奇偶、吃掉手指的判定。

每一条都对着反编译的某个分支（出处见 ``algorithms/blocks.py`` 顶部）。合成谱控得住
"位置相同、时刻不同"这类构造，所以这些判据都能**破坏验证**：改掉对应那一行就会红。
"""

from __future__ import annotations

import json
import math
from typing import Any

from algorithms import conservative, geometric, radical
from algorithms.blocks import (
    BlockArea,
    MoveKey,
    NoiseField,
    RotateKey,
    ScaleKey,
    ease_progress,
)
from algorithms.chart import Chart, JudgeLine, Note, NoteType, Track
from algorithms.geometry import JudgeAreaBuilder
from algorithms.judging import TAP_TOLERANCE, Verdict, simulate, spine_offset
from algorithms.utils import PlanResult, SilentProgress, Touch, TouchEvent

from .archive import mirror_chart
from .referee import PRESS_Y, SCREEN, TAP_X, _line

EPSILON = 1e-9


def _block(
    x0: float,
    x1: float,
    y0: float = 0.0,
    y1: float = 1.0,
    *,
    appear: float = 0.0,
    enable: float = 0.0,
    disable: float = 100.0,
    disappear: float = 100.0,
    subtract: bool = False,
    moves: tuple[MoveKey, ...] = (),
    scales: tuple[ScaleKey, ...] = (),
    rotates: tuple[RotateKey, ...] = (),
) -> BlockArea:
    return BlockArea(
        top_right=(x1, y1),
        bottom_left=(x0, y0),
        appear=appear,
        enable=enable,
        disable=disable,
        disappear=disappear,
        subtract=subtract,
        moves=moves,
        scales=scales,
        rotates=rotates,
    )


def _field(*blocks: BlockArea, ratio: float = 0.0, cap: float = 1.0) -> NoiseField:
    return NoiseField(blocks=blocks, screen=SCREEN, inset_ratio=ratio, inset_cap=cap)


def _close(got: float, want: float) -> bool:
    return abs(got - want) < 1e-6


PLANNERS = (geometric, radical, conservative)
"""四份噪域策略**各自独立**：游戏机制（哪一点会被吃）只有一份，在 :class:`~algorithms.blocks.NoiseField`
里；"禁区算多保守、往哪挪、收尾怎么补按"归各规划器自己。所以凡是对**策略**的判据，都要挨个跑
一遍这四份 —— 谁跟机制脱节，自检就红。"""


def _policies(field: NoiseField, **options: Any) -> list[tuple[str, Any]]:
    """``(规划器名, 它自己的 NoiseGuard)``：策略判据按这个列表挨个跑。"""
    return [
        (
            module.__name__.rsplit(".", 1)[-1],
            module.NoiseGuard(field, field.screen or SCREEN, **options),
        )
        for module in PLANNERS
    ]


def _plan(events: list[tuple[int, int, Touch, float, float]]) -> PlanResult:
    """``(毫秒, 指针, 动作, x, y)`` 列表 → 规划结果（同刻的事件仍是分开的帧）。"""
    frames: dict[int, list[TouchEvent]] = {}
    for stamp, pointer, action, x, y in events:
        frames.setdefault(stamp, []).append(TouchEvent(pointer, action, x, y))
    return PlanResult(
        planner="stub", screen=SCREEN, frames=[(stamp, tuple(items)) for stamp, items in sorted(frames.items())]
    )


def _verdict_of(report, seconds: float) -> Verdict | None:
    for judgement in report.judgements:
        if _close(judgement.seconds, seconds):
            return judgement.verdict
    return None


def _lateral_gap(moved: complex, origin: complex) -> float:
    """挪过去之后**横向**偏差的变化量（判定线沿 +x：横向就是 x 分量）。"""
    return spine_offset(moved, origin, 1 + 0j) - spine_offset(origin, origin, 1 + 0j)


def check_noise() -> list[str]:
    problems: list[str] = []

    def expect(condition: bool, complaint: str) -> None:
        if not condition:
            problems.append(complaint)

    # ---- 1) 缓动表：15 张、101 点，1/2/3 是 2 次幂、4/5/6 是 3 次幂……（GetEase::Instantiation）
    expect(
        all(
            _close(ease_progress(0.5, kind), want)
            for kind, want in ((1, 0.25), (2, 0.75), (3, 0.5), (4, 0.125), (5, 0.875), (6, 0.5))
        ),
        "缓动表在 0.5 处的值不对：1~6 应当是 2/3 次幂的 in/out/inout"
        f"（实际 {[round(ease_progress(0.5, k), 6) for k in range(1, 7)]}）",
    )
    expect(
        _close(ease_progress(0.25, 6), 0.0625) and _close(ease_progress(0.75, 6), 0.9375),
        "inout 的两半拼得不对（前半取 in、后半取 out 并加 0.5）",
    )
    expect(
        _close(ease_progress(0.37, 0), 0.37)
        and ease_progress(0.99, 13) == 0.0
        and ease_progress(0.01, 14) == 1.0
        and ease_progress(-0.5, 1) == 0.0
        and ease_progress(2.0, 1) == 1.0,
        "线性 / 恒 0 / 恒 1 / 越界夹到两端这几条不成立",
    )
    # 下标之间的**插值**：0.505 落在第 50、51 点中间（2 次 in：0.25 与 0.2601）
    expect(
        _close(ease_progress(0.505, 1), 0.25 + 0.5 * (0.2601 - 0.25)),
        f"取整下标之后必须在相邻两点之间线性插值，实际 {ease_progress(0.505, 1)}",
    )

    # ---- 2) 百分比 → 虚拟屏：就是 (x × 16, y × 9)，和判定线的 start/start2 同一套
    shape = _block(0.0, 1.0).shape(1.0, SCREEN)
    expect(
        shape is not None
        and _close(shape.centre.real, 8.0)
        and _close(shape.centre.imag, 4.5)
        and _close(shape.width, 16.0)
        and _close(shape.height, 9.0),
        f"整屏噪域的几何不对：{shape}",
    )
    shape = _block(0.5, 1.0, 0.0, 0.5).shape(1.0, SCREEN)
    expect(
        shape is not None
        and _close(shape.centre.real, 12.0)
        and _close(shape.centre.imag, 2.25)
        and _close(shape.width, 8.0)
        and _close(shape.height, 4.5),
        f"右下四分之一块的几何不对：{shape}（百分比到虚屏的换算或 y 的方向反了）",
    )

    # ---- 3) 两个时间窗：enable/disable 管判定、appear/disappear 管 transform
    window = _block(0.0, 1.0, appear=1.0, enable=2.0, disable=3.0, disappear=4.0)
    expect(
        not window.is_active(1.999)
        and window.is_active(2.0)
        and window.is_active(2.999)
        and not window.is_active(3.0),
        "生效窗不是 [enable, disable)：左闭右开、与 disable 相等时不该生效",
    )
    expect(
        not window.is_visible(0.999) and window.is_visible(1.0) and not window.is_visible(4.0),
        "可见窗不是 [appear, disappear)",
    )
    expect(
        window.shape(0.5, SCREEN) is None,
        "窗口外的噪域应当没有形状（游戏把它甩到 x=1000）",
    )

    # ---- 4) inset 与 isSubtract：非 subtract 往里收、subtract 往外扩（TryGetBlockTouchHalfSize）
    full = _block(0.0, 1.0)
    shrunk = _field(full, ratio=0.02, cap=1.0)
    expect(shrunk.blocks_at(complex(8.0, 4.5), 1.0), "整屏噪域的正中该被吃掉")
    expect(
        not shrunk.blocks_at(complex(8.0, 8.9), 1.0),
        "非 subtract 的命中范围该往里收一圈：贴着上边缘（局部 0.978 > 0.5 − 0.02）不该被吃",
    )
    expect(
        not shrunk.blocks_at(complex(8.0, 4.5), 100.0),
        "块窗口外（t=100 超过 disable）不该被吃掉",
    )

    # 非 subtract 的收缩量（TryGetBlockTouchHalfSize）：世界余量 v = ratio × 屏高，局部 inset
    # 是 v / |lossyScale|（x、y 各除各的）。换成"百分比写法"就是 ratio / (Δp × 当前倍率)。
    # 窄条 Δp.x = 0.1 → 世界宽 1.6（半宽 0.8），ratio=0.02 → 世界余量 0.18 → 局部 inset 0.1125
    # → 半宽 0.3875 局部 = 0.62 世界 → 虚屏 x ∈ [7.38, 8.62]。8.55 在里、8.7 在外。
    narrow = _block(0.45, 0.55)
    narrow_field = _field(narrow, ratio=0.02, cap=1.0)
    expect(
        narrow_field.blocks_at(complex(8.35, 4.5), 1.0)
        and narrow_field.blocks_at(complex(8.55, 4.5), 1.0)
        and not narrow_field.blocks_at(complex(8.7, 4.5), 1.0),
        "窄条的横向 inset 换算不对（局部 inset_x = ratio × 屏高 / 世界宽）",
    )

    # inset 除的是**当前**的 lossyScale，不是基础矩形：带 scale 键帧的块，倍率一变 inset 就跟着变。
    # 整屏块（Δp = 1×1 → 世界 16×9）缩到 0.5 倍 → 世界高 4.5，局部 inset_y = 0.2 × 9 / 4.5 = 0.4
    # → 半高 0.1 局部 = 0.45 世界；按基础 Δp 算的话是 0.2 → 半高 0.3 局部 = 1.35 世界，
    # y = 5.5（离中心 1.0）就会被错误地判成"吃手"。
    half_scale = _block(0.0, 1.0, scales=(ScaleKey(0.0, 0.5, 0.5, 0.5, 0.5, 0, 0),))
    scaled = _field(half_scale, ratio=0.2, cap=1.0)
    expect(
        scaled.blocks_at(complex(8.0, 4.8), 1.0),
        "0.5 倍缩放的整屏噪域，正中附近该照吃（局部 0.067 < 0.1）",
    )
    expect(
        not scaled.blocks_at(complex(8.0, 5.5), 1.0),
        "inset 该按 scale 键帧之后的 lossyScale 算（局部 0.222 > 0.1），按基础矩形算会把这里误判成吃手",
    )

    # ---- 5) subtract 打洞：非 subtract 的块 + 叠一个 subtract，点在"洞"里就不吃
    hole = _block(0.45, 0.55, subtract=True)
    cancelled = _field(full, hole, ratio=0.05, cap=1.0)
    expect(
        not cancelled.blocks_at(complex(8.0, 4.5), 1.0),
        "subtract 块盖住的地方（奇偶抵消）不该被吃掉",
    )
    expect(
        cancelled.blocks_at(complex(4.0, 4.5), 1.0),
        "洞外的整屏噪域照吃：奇偶判定只对重叠处生效",
    )
    # 洞自己带一圈"往外扩"的余量：扩出来的那一圈也抵消（v22 用的是 inset 后的矩形）
    expect(
        not cancelled.blocks_at(complex(9.0, 4.5), 1.0),
        "subtract 的命中矩形该往外扩一圈，扩出来的部分同样抵消（isSubtract 的符号反了就会在这里露出来）",
    )
    # 两个 subtract 叠起来是偶数的洞 → 又变回危险区
    two_holes = _field(full, hole, _block(0.4, 0.6, subtract=True), ratio=0.05, cap=1.0)
    expect(
        two_holes.blocks_at(complex(8.0, 4.5), 1.0),
        "两个 subtract 叠在同一个点上，奇偶回到「危险」——不能简化成「有 subtract 就安全」",
    )
    # 只有 subtract 一块时，它自己往外扩出来的那一圈**不**吃人（第一条奇偶门要求原矩形也命中）
    lone_ring = _field(hole, ratio=0.05, cap=1.0)
    expect(
        lone_ring.blocks_at(complex(8.0, 4.5), 1.0) and not lone_ring.blocks_at(complex(9.0, 4.5), 1.0),
        "光一块 subtract：原矩形里吃、扩出来的那一圈不吃（v24 ^ v23 那道门就是干这个的）",
    )

    # ---- 6) 键帧：move 是绝对中心 + 缓动；scale/rotate 的中心绕锚点走
    linear = _block(
        0.4,
        0.6,
        moves=(
            MoveKey(0.0, 0.0, 0.5, 0, 0),
            MoveKey(2.0, 1.0, 0.5, 0, 0),
        ),
    )
    shape = linear.shape(1.0, SCREEN)
    expect(
        shape is not None and _close(shape.centre.real, 8.0),
        f"线性的移动事件在中点应当正好在 x=8：{shape}",
    )
    eased = _block(
        0.4,
        0.6,
        moves=(
            MoveKey(0.0, 0.0, 0.5, 1, 0),
            MoveKey(2.0, 1.0, 0.5, 1, 0),
        ),
    )
    shape = eased.shape(1.0, SCREEN)
    expect(
        shape is not None and _close(shape.centre.real, 4.0),
        f"缓动为 2 次 in 时，中点应当在 x=4（0.25 × 16）：{shape}（缓动没进几何？）",
    )

    anchored = _block(
        0.4,
        0.6,
        scales=(
            ScaleKey(0.0, 1.0, 1.0, 0.0, 0.5, 0, 0),
            ScaleKey(2.0, 2.0, 1.0, 0.0, 0.5, 0, 0),
        ),
    )
    shape = anchored.shape(1.0, SCREEN)
    expect(
        shape is not None
        and _close(shape.width, 4.8)
        and _close(shape.centre.real, 12.0),
        f"缩放要绕锚点走：尺寸 ×1.5、中心从 8 挪到 12，实际 {shape}",
    )

    turned = _block(
        0.45,
        0.55,
        rotates=(
            RotateKey(0.0, 0.0, 0.5, 0.5, 0),
            RotateKey(2.0, 90.0, 0.5, 0.5, 0),
        ),
    )
    expect(
        not turned.contains_original(complex(12.4, 4.5), 1.0, SCREEN)
        and turned.contains_original(complex(12.4, 4.5), 2.0, SCREEN),
        "转了 90° 之后，(12.4, 4.5) 应当落进块里（局部坐标要用角度反变换）",
    )
    expect(
        _close(turned.shape(2.0, SCREEN).degrees, 90.0),
        "角度是绝对角度、不是增量",
    )

    # ---- 7) 镜像：x 翻、角度取反；谓词意义上与原图对称
    subject = _field(
        _block(
            0.3,
            0.7,
            0.2,
            0.9,
            subtract=True,
            moves=(MoveKey(0.0, 0.2, 0.4, 0, 0), MoveKey(2.0, 0.8, 0.6, 0, 0)),
            scales=(ScaleKey(0.0, 1.0, 1.0, 0.3, 0.2, 0, 0), ScaleKey(2.0, 2.0, 1.0, 0.7, 0.9, 0, 0)),
            rotates=(RotateKey(0.0, 0.0, 0.4, 0.3, 0), RotateKey(2.0, 33.0, 0.2, 0.4, 0)),
        ),
        ratio=0.03,
    )
    flipped = subject.mirrored()
    symmetric = True
    for seconds in (0.2, 0.5, 1.0, 1.5, 1.9):
        for x in (1.0, 4.0, 7.0, 8.0, 9.0, 12.0, 15.0):
            for y in (0.5, 2.0, 4.5, 7.0, 8.5):
                point = complex(x, y)
                if subject.blocks_at(point, seconds) != flipped.blocks_at(
                    complex(16.0 - x, y), seconds
                ):
                    symmetric = False
    expect(
        symmetric,
        "镜像后 (x, y) 与 (16 − x, y) 的命中应当一一对应（角度没取反 / anchor.x 没翻就会不对称）",
    )

    # 同一条规则的另一份实现（测试里的 JSON 镜像）必须与 BlockArea.mirrored 一致
    text = json.dumps(
        {
            "formatVersion": 3,
            "offset": 0.0,
            "judgeLineList": [{"bpm": 120.0, "notesAbove": [], "notesBelow": []}],
            "blockAreaList": [
                {
                    "topRightPercentage": {"x": 0.7, "y": 0.9},
                    "bottomLeftPercentage": {"x": 0.3, "y": 0.2},
                    "appearTime": 0.0,
                    "enableTime": 0.5,
                    "disableTime": 2.0,
                    "disappearTime": 2.5,
                    "isSubtract": True,
                    "moveEvents": [
                        {"endPosition": {"x": 0.2, "y": 0.4}, "time": 0.0, "easeTypeX": 0, "easeTypeY": 1},
                        {"endPosition": {"x": 0.8, "y": 0.6}, "time": 2.0, "easeTypeX": 2, "easeTypeY": 0},
                    ],
                    "scaleEvents": [
                        {"anchor": {"x": 0.3, "y": 0.2}, "time": 0.0, "easeTypeX": 0, "easeTypeY": 0,
                         "scale": {"x": 1.0, "y": 1.0}},
                        {"anchor": {"x": 0.7, "y": 0.9}, "time": 2.0, "easeTypeX": 3, "easeTypeY": 3,
                         "scale": {"x": 2.0, "y": 0.5}},
                    ],
                    "rotateEvents": [
                        {"anchor": {"x": 0.4, "y": 0.3}, "time": 0.0, "easeType": 0, "rotation": 0.0},
                        {"anchor": {"x": 0.2, "y": 0.4}, "time": 2.0, "easeType": 4, "rotation": 33.0},
                    ],
                }
            ],
        }
    )
    parsed = Chart.parse(text)
    expect(len(parsed.blocks) == 1, f"blockAreaList 没解析出来：{len(parsed.blocks)} 块")
    parsed_field = parsed.noise_field(inset_ratio=0.03, inset_cap=1.0)
    same = all(
        parsed_field.blocks_at(complex(x, y), seconds)
        == Chart.parse(mirror_chart(text)).noise_field(inset_ratio=0.03, inset_cap=1.0).blocks_at(
            complex(16.0 - x, y), seconds
        )
        for seconds in (0.6, 1.0, 1.8)
        for x in (2.0, 6.0, 8.0, 11.0, 14.0)
        for y in (1.0, 4.5, 8.0)
    )
    expect(same, "JSON 镜像（mirror_chart）与 BlockArea.mirrored 的命中判定对不上")
    expect(
        parsed.format_version == 3 and not Chart.parse('{"judgeLineList": []}').blocks,
        "没有 blockAreaList 的谱面（4.0.0）应当解析出空的噪域",
    )

    # ---- 8) 判定：落进噪域的手指当帧就被摘掉，而且**废到抬起为止**
    eaten = _block(0.0, 1.0, enable=0.9, disable=1.6, appear=0.9, disappear=1.6)
    plain = Chart(3, 0.0, SCREEN, [_line(TAP_X, 4.5, [Note(NoteType.TAP, 1.0, 0.0, 0.0)])])
    noisy = Chart(
        3,
        0.0,
        SCREEN,
        [_line(TAP_X, 4.5, [Note(NoteType.TAP, 1.0, 0.0, 0.0)])],
        (eaten,),
    )
    tap = _plan([(1000, 7, Touch.DOWN, TAP_X, PRESS_Y), (1008, 7, Touch.UP, TAP_X, PRESS_Y)])
    expect(
        _verdict_of(simulate(plain, tap), 1.0) is Verdict.PERFECT,
        "没有噪域时这一下按在音符上应当是 Perfect（后面几条的对照）",
    )
    blocked = simulate(noisy, tap)
    expect(
        _verdict_of(blocked, 1.0) is Verdict.MISS,
        "按在生效噪域里的那一下不该判到音符（手指要在判音符之前被摘掉）",
    )
    expect(
        blocked.blocked.get(7) == [(1000, 1008)],
        f"被噪域吃掉的手指区间不对：{blocked.blocked}（应当是按下那一刻到抬起）",
    )

    # 中途挪出噪域也没用：抬起来之前一直是废的。这里用 drag 音符 —— 它是逐帧比位置的，
    # 正好能问"手指在不在"，而 tap 只认按下那一下（那样就没法把"挪出去"造出来）。
    two_lines = [
        _line(4.0, 4.5, [Note(NoteType.TAP, 1.0, 0.0, 0.0)]),
        _line(12.0, 4.5, [Note(NoteType.DRAG, 1.3, 0.0, 0.0)]),
    ]
    left_eater = _block(0.0, 0.5, enable=0.9, disable=1.6, appear=0.9, disappear=1.6)
    drag_out = _plan(
        [
            (1000, 3, Touch.DOWN, 4.0, PRESS_Y),
            (1150, 3, Touch.MOVE, 12.0, PRESS_Y),
            (1500, 3, Touch.UP, 12.0, PRESS_Y),
        ]
    )
    report = simulate(Chart(3, 0.0, SCREEN, two_lines, (left_eater,)), drag_out)
    expect(
        _verdict_of(report, 1.3) is Verdict.MISS,
        "手指出噪域之后、抬起之前仍然不能判音符（blockedFingerIds 只在抬起时清）",
    )
    report = simulate(Chart(3, 0.0, SCREEN, two_lines), drag_out)
    expect(
        _verdict_of(report, 1.3) is Verdict.PERFECT,
        "同一个手势在没有噪域的谱面上应当判到 1.3s 那个音符（证明上一条不是别的毛病）",
    )

    # 抬起来再按就复活：同一个指针的第二下在窗口外
    revive = _plan(
        [
            (1000, 7, Touch.DOWN, TAP_X, PRESS_Y),
            (1008, 7, Touch.UP, TAP_X, PRESS_Y),
            (2000, 7, Touch.DOWN, TAP_X, PRESS_Y),
            (2008, 7, Touch.UP, TAP_X, PRESS_Y),
        ]
    )
    report = simulate(
        Chart(
            3,
            0.0,
            SCREEN,
            [
                _line(
                    TAP_X,
                    4.5,
                    [Note(NoteType.TAP, 1.0, 0.0, 0.0), Note(NoteType.TAP, 2.0, 0.0, 0.0)],
                )
            ],
            (eaten,),
        ),
        revive,
    )
    expect(
        _verdict_of(report, 2.0) is Verdict.PERFECT,
        "第二下是抬起之后重新按的，手指应当复活、正常判到 2.0s 那个音符",
    )
    expect(
        report.blocked.get(7) == [(1000, 1008)],
        f"死亡区间该在抬起处结束：{report.blocked}",
    )

    # 手指全程在噪声窗口里、但位置在噪域外 → 一次都不该被吃
    outside = _plan(
        [
            (1000, 5, Touch.DOWN, 12.0, PRESS_Y),
            (1008, 5, Touch.MOVE, 12.0, PRESS_Y),
            (1500, 5, Touch.UP, 12.0, PRESS_Y),
        ]
    )
    report = simulate(
        Chart(3, 0.0, SCREEN, [_line(12.0, 4.5, [Note(NoteType.DRAG, 1.3, 0.0, 0.0)])], (left_eater,)),
        outside,
    )
    expect(not report.blocked, f"点在噪域外不该被吃：{report.blocked}")
    expect(
        _verdict_of(report, 1.3) is Verdict.PERFECT,
        "噪域生效期间、手指在噪域外的音符照常判到",
    )

    # 手指先按在那儿、噪域**后来才生效** —— 按住期间必须持续采样（不是只看按下那一瞬）
    late = _block(0.0, 1.0, appear=1.2, enable=1.2, disable=2.0, disappear=2.0)
    held = _plan([(1000, 9, Touch.DOWN, TAP_X, PRESS_Y), (2200, 9, Touch.UP, TAP_X, PRESS_Y)])
    drag_note = [Note(NoteType.DRAG, 1.5, 0.0, 0.0)]
    report = simulate(Chart(3, 0.0, SCREEN, [_line(TAP_X, 4.5, drag_note)], (late,)), held)
    intervals = report.blocked.get(9, [])
    expect(
        _verdict_of(report, 1.5) is Verdict.MISS
        and len(intervals) == 1
        and abs(intervals[0][0] - 1200) <= 2
        and intervals[0][1] == 2200,
        f"噪域在手指按住期间生效时，那根手指当帧就废（区间应当在 1200ms 起、2200ms 抬起结束）：{report.blocked}",
    )
    expect(
        _verdict_of(simulate(Chart(3, 0.0, SCREEN, [_line(TAP_X, 4.5, drag_note)]), held), 1.5)
        is Verdict.PERFECT,
        "同一个按住不动的手势在没有噪域的谱面上应当判到（证明上一条不是采样没扫到别的毛病）",
    )

    # ---- 9) 规划器：按下点落进噪域时，沿判定线法向挪出去（横向偏差不变 → 判定不受影响）
    from algorithms import create  # 规划器按名字现 import，这里跟着 registry 走

    covered = Chart(
        3,
        0.0,
        SCREEN,
        [_line(TAP_X, 4.5, [Note(NoteType.TAP, 1.0, 0.0, 0.0)])],
        (_block(0.45, 0.55, 0.3, 0.7, enable=0.0, disable=100.0),),
    )
    expect(
        covered.blocks[0].contains_original(complex(TAP_X, 4.5), 0.0, SCREEN),
        "合成谱没造对：判定点应当正好落在噪域中心",
    )
    avoiding = create("geometric", None).plan(covered, SilentProgress())
    report = simulate(covered, avoiding)
    expect(
        _verdict_of(report, 1.0) is Verdict.PERFECT and not report.blocked,
        f"规划器该把这一下从噪域里挪出去：判定 {_verdict_of(report, 1.0)}，暴露 {report.blocked}",
    )
    # 反面：关掉规避（配置项）就必然丢 —— 证明上面那条是"躲"救回来的
    blind = create("geometric", {"avoid_noise": False}).plan(covered, SilentProgress())
    report = simulate(covered, blind)
    expect(
        _verdict_of(report, 1.0) is Verdict.MISS and bool(report.blocked),
        f"关掉 avoid_noise 之后这一下应当被噪域吃掉（上面那条的对照）："
        f"{_verdict_of(report, 1.0)}，暴露 {report.blocked}",
    )

    # 同一条链走 Petrichor 的形状：屏幕左半边被盖住、判定点压在盖住的边上。判定线是横的，
    # 法向（竖直）整条都在噪域里 —— 只搜法向的实现会把这 20 个 TAP 全按在原处、被吃掉；
    # 沿判定线挪出去就好。落点还必须**落在判定线上**（法向偏差 0 = 判定权最小）。
    dodged = Chart(
        3,
        0.0,
        SCREEN,
        [_line(TAP_X, PRESS_Y, [Note(NoteType.TAP, 1.0, 0.0, 0.0)])],
        (_block(0.0, 0.55, 0.0, 1.0, enable=0.0, disable=100.0),),
    )
    expect(
        dodged.noise_field().blocking(complex(TAP_X, PRESS_Y), 0.0) is not None,
        "合成谱没造对：判定点应当落在「左半边」那块噪域的命中矩形里（inset 之后）",
    )
    plan = create("geometric", None).plan(dodged, SilentProgress())
    report = simulate(dodged, plan)
    downs = [
        (stamp, event)
        for stamp, batch in plan.frames
        for event in batch
        if event.action is Touch.DOWN
    ]
    expect(
        _verdict_of(report, 1.0) is Verdict.PERFECT and not report.blocked,
        f"沿判定线挪出去就干净了：判定 {_verdict_of(report, 1.0)}，暴露 {report.blocked}",
    )
    expect(
        bool(downs)
        and abs(downs[0][1].y - PRESS_Y) < 1e-6
        and 0.3 < abs(downs[0][1].x - TAP_X) < 0.45,
        f"这一下的落点该落在判定线上、横向**只让出必要的那一点**（真实需要 0.35）：{downs[:2]}",
    )

    # 低层一点：aim() 挑出来的点必须（a）不在噪域里、（b）判定线还有干净的部分时就落在线上
    #（法向偏差 0 是"判定权"最小的位置）、（c）横向让出的量不超过游戏判得到的容差。
    #
    # 这一条构造的是"**法向被堵死、只有沿判定线**能出去"的那种几何（判定线横着、左半边被一块
    # 噪域整条盖住）。它和老 `clear` 的差别很直接：老的那份只沿法向探，这种形状它必然报
    # "挪不出去"。Petrichor 里那 14 个 TAP 是**反过来**的那一半（判定线竖直、法向能出去），
    # 见下面 §9b。
    origin = complex(TAP_X, PRESS_Y)
    edge = _field(_block(0.0, 0.55, 0.0, 1.0, enable=0.0, disable=100.0), ratio=0.0, cap=0.0)
    for who, policy in _policies(edge):
        band = JudgeAreaBuilder(SCREEN).band(origin, 1 + 0j, half_width=TAP_TOLERANCE)
        moved = policy.aim(band, origin, 1 + 0j, tick=0, span_ticks=1)
        expect(
            moved is not None and policy.clean(moved, 0.0, 0.05),
            f"[{who}] 法向整条都在噪域里、沿判定线挪一下就好：aim 该给出一个干净的点：{moved}",
        )
        expect(
            moved is not None
            and abs(moved.imag - PRESS_Y) < 1e-6
            and 0.7 < _lateral_gap(moved, origin) < 0.9,
            f"[{who}] 挪出去的点该落在判定线上（法向 0）、横向**只让出必要的那一点**"
            f"（真实需要 0.8；带上保险余量就会更大）：{moved}",
        )
    # ---- 9b) Petrichor 那 14 个 TAP：**法向**是唯一出路，而且出路在 4.2~4.9 那么远
    # 实测（真谱面，`_re/dbg_pick.py`）：判定线**竖直**（所以法向是水平的），质点 (3.2 或
    # 12.8, 4.5)，沿法向要走 4.22~4.93 才出噪域，沿判定线 ±4 之内一个干净点都没有。老
    # `NoiseGuard.clear` 方向没找错，是**两个自设的常量**把它卡住的：探针上限 `reach = 4.5`
    # （出路正好骑在这个数上下）和 0.3 的保险余量（把出路又推远 0.3）—— 余量一开，14 个**全**
    # 报"挪不出去"；余量关掉也有 5 个够不着（`_re/dbg_oldclear.py` 的重放）。
    # 所以这里两头都要立判据：落点必须**只要必要的那一点**（不许被余量推远），而且**不许有
    # "探到 4.5 就放弃"**这种上限。
    def _vertical(notes: list[Note], y: float = PRESS_Y) -> JudgeLine:
        """竖着的判定线：``rotation = e^{iπ/2} = i``，法向是 ±x（和 Petrichor 那几条一样）。"""
        move, rotate = Track(), Track()
        move.cut(0.0, 1000.0, complex(TAP_X, y), complex(TAP_X, y))
        rotate.cut(0.0, 1000.0, math.pi / 2, math.pi / 2)
        return JudgeLine(bpm=120.0, notes=notes, move=move, rotate=rotate)

    # 原矩形 x ∈ [0, 13.2]（屏宽 16 → 82.5%）：inset 之后**命中矩形**的右边缘落在 12.75，
    # 离判定点 4.75 —— 正好越过老代码那个 4.5 的探针上限。
    far = Chart(
        3,
        0.0,
        SCREEN,
        [_vertical([Note(NoteType.TAP, 1.0, 0.0, 0.0)])],
        (_block(0.0, 0.825, 0.0, 1.0, enable=0.0, disable=100.0),),
    )
    expect(
        far.noise_field().blocking(complex(TAP_X, PRESS_Y), 1.0) is not None,
        "合成谱没造对：判定点应当落在噪域的命中矩形里",
    )
    far_plan = create("geometric", None).plan(far, SilentProgress())
    far_report = simulate(far, far_plan)
    far_downs = [
        (stamp, event)
        for stamp, batch in far_plan.frames
        for event in batch
        if event.action is Touch.DOWN
    ]
    expect(
        _verdict_of(far_report, 1.0) is Verdict.PERFECT and not far_report.blocked,
        f"沿法向挪出去就干净了（判定不受影响）：判定 {_verdict_of(far_report, 1.0)}，"
        f"暴露 {far_report.blocked}",
    )
    expect(
        bool(far_downs)
        and abs(far_downs[0][1].y - PRESS_Y) < 1e-6
        and 4.6 < abs(far_downs[0][1].x - TAP_X) < 5.0,
        f"沿法向挪：横向偏差必须一点不变，而且**只挪必要的那一点**（真实需要 4.75；"
        f"带保险余量就会到 5.05）：{far_downs[:2]}",
    )

    # 整屏都被盖住时没有落点 → None（不许"照原样按下去"）
    everything = _field(_block(0.0, 1.0, enable=0.0, disable=100.0), ratio=0.0, cap=0.0)
    for who, policy in _policies(everything):
        band = JudgeAreaBuilder(SCREEN).band(origin, 1 + 0j, half_width=TAP_TOLERANCE)
        expect(
            policy.aim(band, origin, 1 + 0j, tick=0, span_ticks=1) is None,
            f"[{who}] 整屏都被噪域盖住时应当返回 None（这一下真没救，交给裁判报 Miss）",
        )
    # flick 的划动那一段也得干净：两段块中间留一条 0.9 宽的缝，按下点能躲进缝里，
    # 但一划（半径 1.6）就进了另一段 —— 这时候必须判定"没处躲"，而不是躲进缝里就算完
    two_bands = _field(
        _block(0.0, 1.0, 0.0, 0.45, enable=0.0, disable=100.0),
        _block(0.0, 1.0, 0.55, 1.0, enable=0.0, disable=100.0),
        ratio=0.0,
        cap=0.0,
    )
    press = complex(8.0, 4.0)
    for who, policy in _policies(two_bands):
        band = JudgeAreaBuilder(SCREEN).band(press, 1 + 0j, half_width=TAP_TOLERANCE)
        parked = policy.aim(band, press, 1 + 0j, tick=0, span_ticks=1)
        expect(
            parked is not None and abs(parked.imag - 4.0) < 0.2,
            f"[{who}] 不管划动时，缝里贴着边的那点就该被选中（法向只让出必要的一点）：{parked}",
        )
        expect(
            policy.aim(band, press, 1 + 0j, tick=0, span_ticks=1, extend=1.6, along=1j) is None,
            f"[{who}] 把划动半径算进去之后，缝里那点（一划就进另一段）不该再算数 —— 应当返回 None",
        )

    # ---- 10) 兜底修复（repair）：按住期间被噪域扫到 → 提前松开、出来之后再按一条 DOWN。
    # 这一条是"规划阶段没预知到"的那种case（手指先按在别处、噪域后来才罩住它）。
    from algorithms.blocks import exposure

    bar = _block(0.7, 0.8, enable=1.0, disable=2.0, appear=1.0, disappear=2.0)
    field = _field(bar, ratio=0.0, cap=0.0)
    held = _plan(
        [
            (200, 4, Touch.DOWN, 12.0, PRESS_Y),
            (1400, 4, Touch.MOVE, 4.0, PRESS_Y),
            (2500, 4, Touch.UP, 4.0, PRESS_Y),
        ]
    )
    before = exposure(held, field)
    expect(
        before.get(4) == [(1000, 2500)],
        f"按住不动、噪域在 1.0s 罩上来：死亡区间应当是 [1.0s, 抬起)：{before}",
    )
    # 修复与收尾都是**各规划器自己**的一份（不再是共用入口），所以四份挨个验
    repaired_plans: dict[str, Any] = {}
    for who, module in ((m.__name__.rsplit(".", 1)[-1], m) for m in PLANNERS):
        fixed, repaired = module.repair_noise(held, field)
        repaired_plans[who] = fixed
        expect(repaired >= 1, f"[{who}] 这一处应当被修复（实际修了 {repaired} 处）")
        expect(
            not exposure(fixed, field),
            f"[{who}] 修完之后不该还有暴露：{exposure(fixed, field)}"
            f"（提前 UP + 出来再 DOWN，手指就复活了）",
        )
        expect(
            any(
                event.action is Touch.DOWN and event.pointer == 4
                for _, batch in fixed.frames
                for event in batch
            ),
            f"[{who}] 修完之后应当多出一条按下",
        )
        done, saved = module.finish_noise(held, field, [])
        expect(
            saved >= 1
            and not exposure(done, field)
            and any("噪域" in line for line in done.warnings),
            f"[{who}] finish 该做同样的事并把结果写进警告：救回 {saved} 段，警告 {done.warnings}",
        )
        quiet, none_saved = module.finish_noise(
            _plan([(200, 4, Touch.UP, 4.0, PRESS_Y)]), field, []
        )
        expect(
            none_saved == 0 and not quiet.warnings,
            f"[{who}] 没有暴露时 finish 应当是纯粹的空操作",
        )

    # 修复不许造出"没按下就抬起"：这一下**按下本身**就在禁区里，没有"提前松开"可言
    from .coverage import check_stream

    inside = _plan(
        [
            (1200, 4, Touch.DOWN, 12.0, PRESS_Y),
            (1400, 4, Touch.MOVE, 4.0, PRESS_Y),
            (2500, 4, Touch.UP, 4.0, PRESS_Y),
        ]
    )
    for who, module in ((m.__name__.rsplit(".", 1)[-1], m) for m in PLANNERS):
        mended, count = module.repair_noise(inside, field)
        expect(
            not check_stream(mended),
            f"[{who}] 按下就在禁区里时不该硬修（会在按下之前插一条 UP）：{check_stream(mended)}",
        )
        expect(count == 0, f"[{who}] 这一种情况救不了，不该记成救回：{count}")
        expect(
            not check_stream(repaired_plans[who]),
            f"[{who}] 修好的那一份也要过流自检：{check_stream(repaired_plans[who])}",
        )

    # ---- 11) 禁区 ⊇ 真命中集：一个点都不能漏
    # `forbidden()` 给的是"这一段里每一刻的命中区域的并集"，必须**包住**裁判会吃人的每一个点，
    # 否则规划器会心安理得地把落点放进一个真会被吃掉的位置。网格上把两个谓词对一遍：
    # 凡是 `blocks_at` 说"吃"的点，都得落在 `forbidden(那么一刻)` 里。
    from shapely.geometry import Point

    moving = _block(
        0.1,
        0.9,
        0.15,
        0.75,
        enable=0.0,
        disable=3.0,
        appear=0.0,
        disappear=3.0,
        moves=(MoveKey(0.0, 0.3, 0.5, 0, 0), MoveKey(3.0, 0.65, 0.45, 0, 0)),
        scales=(ScaleKey(0.0, 1.0, 1.0, 0.5, 0.5, 0, 0), ScaleKey(3.0, 0.7, 1.3, 0.5, 0.5, 0, 0)),
        rotates=(RotateKey(0.0, 0.0, 0.5, 0.5, 0), RotateKey(3.0, 22.0, 0.5, 0.5, 0)),
    )
    # 单独一块 subtract：它**自己就能吃人**（第一道奇偶门在 v24=0、v23=1 时放行），
    # 所以禁区里不能把 subtract 的块当成"洞"跳过去。
    lone = _block(0.1, 0.3, 0.55, 0.75, enable=0.0, disable=3.0, appear=0.0, disappear=3.0,
                  subtract=True)
    mixed = _field(moving, lone, ratio=0.05)
    eaten_points = leaked = 0
    samples: list[str] = []
    for who, policy in _policies(mixed):
        for index in range(16):
            seconds = index * 0.2
            # 窗口是**半开**的 `[lo, hi)`，`hi` 要给一点余量，否则 `enable == lo == hi` 会被当成空窗
            area = policy.forbidden(seconds, seconds + 1e-6)
            for xi in range(41):
                for yi in range(23):
                    point = complex(xi * 0.4, yi * 0.4)
                    if mixed.blocking(point, seconds) is None:
                        continue
                    eaten_points += 1
                    if area is None or not area.covers(Point(point.real, point.imag)):
                        leaked += 1
                        if len(samples) < 4:
                            samples.append(
                                f"[{who}] ({point.real:.1f},{point.imag:.1f}) @ {seconds:.1f}s"
                            )
    expect(
        eaten_points > 200 and leaked == 0,
        f"禁区必须包住真命中集：{leaked} 个吃手点漏在禁区外"
        f"（四份策略共采样 {eaten_points} 个），例如 {samples}",
    )

    # ---- 13) 手指**全程**不许进噪域：歇着的手指也要在噪域扫到它之前松开
    # Ametrine 73.333s 那个 drag 就是这么丢的：两根音符之间空了 1.3 秒，没人复查在屏手指，
    # 噪域扫过来把手指数掉、规划器却仍以为它按着，于是那一下一次都没按（`repair` 事后把
    # 手指补成"提前松开 + 稍后再按"，但那时 drag 的窗口已经过了）。
    # 这里把场景压到最小：先按一下 tap（留下一根歇着的手指），隔一段空档，噪域罩住那个位置，
    # 最后来一个 drag —— 它必须被判掉，而且**全程一个手指都不许被吃**。
    from algorithms.blocks import exposure as exposure_of

    spot = _block(0.45, 0.55, 0.4, 0.6, enable=0.5, disable=1.5, appear=0.5, disappear=1.5)
    gap = Chart(
        3,
        0.0,
        SCREEN,
        [_line(TAP_X, 4.5, [Note(NoteType.TAP, 0.05, 0.0, 0.0), Note(NoteType.DRAG, 1.0, 0.0, 0.0)])],
        (spot,),
    )
    expect(
        gap.blocks[0].contains_original(complex(TAP_X, PRESS_Y), 1.0, SCREEN),
        "合成谱没造对：噪域该罩住那根歇着的手指所在的位置",
    )
    lazy = geometric.PLANNER().plan(gap, SilentProgress())
    expect(
        not exposure_of(lazy, gap.noise_field()),
        f"歇着的手指被噪域吃掉了：{exposure_of(lazy, gap.noise_field())}"
        f"（歇着的要提前松开，`_release_doomed` 的前瞻窗口 + 噪域生效期间按 tick 补走空帧）",
    )
    lazy_report = simulate(gap, lazy)
    expect(
        not lazy_report.lost,
        f"空档之后那个 drag 丢了：{[item.seconds for item in lazy_report.lost]}"
        f"（规划器以为歇着的手指还按着 → 一次也不按）",
    )

    # ---- 12) 精确禁区：两道奇偶门的**集合版**（`blocking_area`）必须与逐点 `blocking` 一致
    from shapely.geometry import Point

    compared = 0
    # 第二个场专门盯"奇覆盖"：**两块重叠的 subtract**（单块时 奇覆盖 == 并集，退化了也看不出来）
    twin = _field(
        moving,
        lone,
        _block(0.15, 0.35, 0.5, 0.7, enable=0.0, disable=3.0, appear=0.0, disappear=3.0,
               subtract=True),
        ratio=0.05,
    )
    for field_under_test in (mixed, twin):
        for moment in (0.4, 1.1, 2.2):
            area = field_under_test.blocking_area(moment)
            mismatch: list[tuple[float, float]] = []
            for xi in range(32):
                for yi in range(18):
                    # 网格错开一点：正好压在块边上时，游戏用 `<= 0.5`、shapely 的 `covers` 认闭边界，
                    # 会差 1 ULP —— 那种点跳过（下面还有样本数下限兜着）
                    point = complex((xi + 0.37) * 16 / 32, (yi + 0.41) * 9 / 18)
                    spot = Point(point.real, point.imag)
                    if area is not None and area.boundary.distance(spot) < 1e-6:
                        continue
                    compared += 1
                    if (area is not None and area.covers(spot)) != field_under_test.blocks_at(
                        point, moment
                    ):
                        mismatch.append((round(point.real, 3), round(point.imag, 3)))
            expect(
                not mismatch,
                f"blocking_area 与逐点 blocking 对不上：{mismatch[:4]}（t={moment}s）",
            )
    expect(compared > 1500, f"对拍样本太少（{compared}）—— 边界跳得太多就不是判据了")

    # Ametrine 那种形状：**一块盖满全屏的 subtract** + 一个小 normal 块。奇偶之下
    # "小 normal 块里安全、其余全危险" —— 而旧禁区（所有块原矩形的并集）会把整屏都算成危险，
    # 于是每个 drag 都被误报成"判定区整块在噪域里"（还会照着把触点放进真危险的地方）。
    sheet = _field(
        _block(0.0, 1.0, 0.0, 1.0, enable=0.0, disable=10.0, subtract=True),
        _block(0.4, 0.6, 0.4, 0.6, enable=0.0, disable=10.0),
        ratio=0.05,
    )
    hole = complex(8.0, 4.5)
    """小 normal 块正中：它有 inset 矩形，与 subtract 的奇偶抵消 → 安全。"""
    outside = complex(1.0, 1.0)
    expect(
        sheet.blocking(hole, 1.0) is None,
        "非 subtract 块抵消掉 subtract 的地方应当安全（这就是 Ametrine 的可玩空间）",
    )
    expect(
        sheet.blocking(outside, 1.0) is not None,
        "盖满全屏的 subtract 单独在的地方应当吃人（否则这块谱面就没有危险区了）",
    )
    for who, policy in _policies(sheet):
        area = policy.forbidden(1.0, 1.0 + 1e-6)
        expect(
            area is not None and not area.covers(Point(hole.real, hole.imag)),
            f"[{who}] 安全点被算进了禁区（整屏 subtract 被当成整屏危险）—— 这就是 Ametrine 那条误报",
        )
        expect(
            area is not None and area.covers(Point(outside.real, outside.imag)),
            f"[{who}] 该危险的点没被算进禁区（禁区不能是空的）",
        )

    return problems
