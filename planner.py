#!/usr/bin/env python3
"""规划模块：把一份官谱 JSON 变成一串触控事件。

``main.py``（frida 主干）拿到谱面就调它；它自己也能单独跑：

    python planner.py charts/0002_Glaciaxion.SunsetRay.0_HD_93215ea2.json
    python planner.py Chart.json --no-cache         # 不吃也不写缓存
    python planner.py Chart.json --planner radical
    python planner.py Chart.json -o /tmp/out

单独跑时会先找同目录下的 ``<名字>.meta.json``（采集时留下的那份），从里面恢复序号、
来源上下文与内容哈希；找不到就退回用文件名当来源、现算哈希。

**落盘的规划结果就是缓存**（默认打开）：一张谱面 + 一个规划器对应一个 ``.psap``，
下次同样的组合直接读回来；算法源码动过自动失效（见 :func:`cache_key`）。
镜像、延迟这类运行时设置不进缓存，执行时由 ``touch.py`` 临时改。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from tqdm import tqdm

from algorithms import DEFAULT_PLANNER, catalog, create
from algorithms.chart import Chart
from algorithms.utils import PlanResult, Progress, SilentProgress
from formats.storage import ChartRef, decode_plan, load_plan_meta, plan_path, plan_path_for, save_plan

ROOT = Path(__file__).resolve().parent
PLANS_DIR = ROOT / "plans"


class TqdmProgress:
    """把规划器的进度汇报接到 tqdm 上。规划器只认 ``track`` 这一个方法。"""

    def track(self, iterable, description: str):
        return tqdm(iterable, desc=f"  {description}", leave=False)


def plan(
    text: str,
    *,
    planner: str = DEFAULT_PLANNER,
    ref: ChartRef | None = None,
    cache: bool = True,
    directory: Path = PLANS_DIR,
    progress: Progress | None = None,
) -> PlanResult:
    """规划一份谱面 —— 落盘的规划结果同时就是缓存。

    规划结果是**谱面的纯函数**：一张谱面 + 一个规划器 ⟺ 一个 ``.psap`` 文件
    （``storage.plan_path_for`` 拼出来的那个名字）。所以 ``cache=True`` 时先看这个文件
    在不在、是不是这一版算法算出来的（meta 里的 ``cache_key``）：在就直接读回来，
    不在就算一遍并写下去。没有第二份"缓存格式"。

    镜像、延迟这些**运行时**的东西一概不进缓存：``.psap`` 存的是规范解（不镜像、不偏移），
    执行时由 ``touch.Player`` 临时改。于是一张谱面的缓存在任何局面下都能用，
    也不用为"开着镜像再存一份"。

    ``cache=False`` = 既不用也不写（只想要一份临时结果时用）。
    """
    ref = ref or ChartRef()
    if cache:
        cached = load_cached(ref, planner, directory)
        if cached is not None:
            cached.stats["cached"] = True
            return cached

    chart = Chart.parse(text)
    result = create(planner).plan(chart, progress or SilentProgress())
    result.stats["cached"] = False
    if cache:
        save_plan(result, ref, directory, cache_key=cache_key())
    return result


def cache_key() -> str:
    """规划管线的指纹：算法源码动过，缓存就该失效。

    覆盖 ``algorithms/`` 下所有 ``.py`` 与本模块自己 —— 改了算法、改了谱面解析、
    改了坐标换算，算出来的东西都可能不一样，宁可重算一遍。
    """
    digest = hashlib.sha1()
    for path in sorted((ROOT / "algorithms").rglob("*.py")) + [Path(__file__).resolve()]:
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def load_cached(
    ref: ChartRef, planner_name: str, directory: Path = PLANS_DIR
) -> PlanResult | None:
    """命中就返回规划结果，否则 None。

    **统计与警告要从 meta 里补回来**：``.psap`` 是二进制，里面只有事件流（那是规划的本体）；
    ``stats`` / ``warnings`` 存在旁边的 ``.meta.json``。不补的话，缓存命中时
    ``stats["notes"]`` 就是 None —— 主机会拿它跟游戏报的音符数核对，于是打出
    "游戏 372，JSON None -> 不一致！"这种假警报（真踩过）。
    """
    meta = load_plan_meta(ref, planner_name, directory)
    if meta is None or meta.get("planner") != planner_name:
        return None
    if meta.get("cache_key") != cache_key():
        return None
    try:
        result = decode_plan(plan_path_for(ref, planner_name, directory).read_bytes())
    except (OSError, ValueError):
        return None
    stats = meta.get("stats")
    if isinstance(stats, dict):
        result.stats.update(stats)
    warnings = meta.get("warnings")
    if isinstance(warnings, list):
        result.warnings = [str(item) for item in warnings]
    return result


def describe(name: str) -> str:
    """规划器的"名字：说明"，说明取自注册表的清单（那里是唯一的出处）。"""
    for info in catalog():
        if info.name == name:
            return f"{info.name}：{info.summary}"
    return name


def summary(result: PlanResult) -> str:
    return (
        f"{result.planner}: {len(result.frames)} 帧 / {result.event_count} 个事件 / "
        f"{result.pointer_count} 指针 / {result.duration_ms / 1000:.1f}s"
    )


def ref_from_path(path: Path, text: str) -> ChartRef:
    """尽量恢复这张谱面的来源。"""
    sidecar = path.with_suffix(".meta.json")
    if sidecar.is_file():
        try:
            meta = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        if meta:
            return ChartRef(
                seq=int(meta.get("seq") or 0),
                context=meta.get("context") or {},
                digest=str(meta.get("hash") or "") or "unknown",
            )
    return ChartRef.of_text(text, context={"songsId": path.stem})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="planner.py", description="auto_phigros 规划模块：官谱 JSON -> 触控事件序列"
    )
    parser.add_argument("chart", type=Path, help="官谱 JSON 文件")
    parser.add_argument(
        "--planner",
        default=DEFAULT_PLANNER,
        choices=[info.name for info in catalog()],
        help="用哪个规划器（默认 %(default)s）",
    )
    parser.add_argument("-o", "--out", type=Path, default=PLANS_DIR, help="输出目录（默认 %(default)s）")
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="既不用已有的规划结果、也不写盘（默认是把它当缓存用）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        text = args.chart.read_text(encoding="utf-8")
    except OSError as error:
        print(f"[planner] 读不了 {args.chart}：{error}", file=sys.stderr)
        return 2

    ref = ref_from_path(args.chart, text)

    print(f"[planner] {args.chart.name}  ({ref.stem})")
    print(f"[planner] 用 {describe(args.planner)}")
    try:
        result = plan(
            text,
            planner=args.planner,
            ref=ref,
            cache=not args.no_cache,
            directory=args.out,
            progress=TqdmProgress(),
        )
    except Exception as error:  # noqa: BLE001 - 命令行入口，报清楚就行
        print(f"[planner] 规划失败：{type(error).__name__}: {error}", file=sys.stderr)
        return 1

    cached = bool(result.stats.get("cached"))
    print(f"[planner] {summary(result)}" + ("（缓存）" if cached else ""))
    if "notes" in result.stats:
        print(f"[planner] 谱面 {result.stats['notes']} 个音符")
    for warning in result.warnings:
        print(f"          ~ {warning}")
    if args.no_cache:
        pass
    elif cached:
        print(f"[planner] 用的就是 {plan_path(result, ref, args.out)}")
    else:
        print(f"[planner] 已保存 {plan_path(result, ref, args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
