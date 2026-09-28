#!/usr/bin/env python3
"""规划模块：把一份官谱变成一串触控事件。

``main.py``（frida 主干）拿到谱面就调它；它自己也能单独跑：

    python src/planner.py charts/Glaciaxion.SunsetRay.0_HD_93215ea2.npz
    python src/planner.py Chart.npz --planner radical --set flick_repeats=1
    python src/planner.py Chart.npz --options            # 看这个规划器能调什么
    python src/planner.py Chart.npz -o /tmp/out --no-cache

输入的谱面是 ``charts/`` 里那种 npz，原文与来源都在里面 —— 规划结果的文件名要靠它。

**落盘的规划结果就是缓存**：一张谱面 + 一个规划器对应一个文件，命中与否看它 meta 里的
``cache_key``（算法源码 + 规划器名 + 这一套参数）。**换了参数就是另一份结果**，同一张
谱面同一个规划器换了参数会重算并覆盖同一个文件；文件名里不带参数，免得变成一坨。
镜像、延迟这类运行时设置不进缓存，执行时由 ``touch.py`` 临时改。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

from tqdm import tqdm

from algorithms import (
    DEFAULT_PLANNER,
    catalog,
    create,
    options_from_args,
    parameters,
)
from algorithms.chart import Chart
from algorithms.utils import PlanResult, Progress, SilentProgress
from formats.storage import (
    ChartRef,
    NpzFormatError,
    load_chart,
    load_plan,
    plan_meta,
    plan_path_for,
    save_plan,
)

ROOT = Path(__file__).resolve().parents[1]
"""项目根目录 —— ``src/`` 的上一层。规划缓存 ``plans/`` 在它下面。"""
PLANS_DIR = ROOT / "plans"

SOURCE_ROOT = Path(__file__).resolve().parent
"""源码根（``src/``）。缓存指纹只认这儿：数据目录挪窝不算算法改过。"""


class TqdmProgress:
    """把规划器的进度汇报接到 tqdm 上。规划器只认 ``track`` 这一个方法。"""

    def track(self, iterable, description: str):
        return tqdm(iterable, desc=f"  {description}", leave=False)


def plan(
    text: str,
    *,
    planner: str = DEFAULT_PLANNER,
    options: Mapping[str, Any] | None = None,
    ref: ChartRef | None = None,
    cache: bool = True,
    directory: Path = PLANS_DIR,
    progress: Progress | None = None,
) -> PlanResult:
    """规划一份谱面 —— 落盘的规划结果同时就是缓存。

    规划结果是**谱面 + 规划器 + 参数**的纯函数，三者一起决定那份 ``.npz`` 里的内容。
    ``cache=True`` 时先看文件在不在、``cache_key`` 对不对（源码 / 规划器 / 参数都算进去了），
    不对就算一遍并覆盖写下去。镜像、延迟这些运行时设置一概不进缓存：存的是规范解，
    执行时由 ``touch.Player`` 临时改。``cache=False`` = 既不用也不写。
    """
    ref = ref or ChartRef()
    key = cache_key(planner, options)
    if cache:
        cached = load_cached(ref, planner, options, directory, key=key)
        if cached is not None:
            cached.stats["cached"] = True
            return cached

    chart = Chart.parse(text)
    result = create(planner, options).plan(chart, progress or SilentProgress())
    result.stats["cached"] = False
    if cache:
        save_plan(result, ref, directory, cache_key=key)
    return result


def cache_key(planner_name: str, options: Mapping[str, Any] | None = None) -> str:
    """这份结果由"哪一版算法 + 哪一套参数"算出来的。

    覆盖 ``algorithms/`` 下所有 ``.py`` 与本模块自己，再加上规划器名和参数 —— 参数是用户
    随时会改的东西，不写进指纹的话换了参数会悄悄命中旧结果。
    """
    digest = hashlib.sha1()
    for path in sorted((SOURCE_ROOT / "algorithms").rglob("*.py")) + [Path(__file__).resolve()]:
        digest.update(path.read_bytes())
    digest.update(json.dumps([planner_name, dict(options or {})], sort_keys=True).encode("utf-8"))
    return digest.hexdigest()[:16]


def load_cached(
    ref: ChartRef,
    planner_name: str,
    options: Mapping[str, Any] | None = None,
    directory: Path = PLANS_DIR,
    *,
    key: str | None = None,
) -> PlanResult | None:
    """命中就返回规划结果，否则 None。

    先只读 meta 那一小段：名字与指纹不对就没必要把事件流解出来。
    """
    path = plan_path_for(ref, planner_name, directory)
    try:
        meta = plan_meta(path)
    except (OSError, NpzFormatError):
        return None
    if meta.get("planner") != planner_name or meta.get("cache_key") != (key or cache_key(planner_name, options)):
        return None
    try:
        return load_plan(path)
    except (OSError, NpzFormatError):
        return None


def describe(name: str) -> str:
    """规划器的"名字：说明"，说明取自注册表的清单（那里是唯一的出处）。"""
    for info in catalog():
        if info.name == name:
            return f"{info.name}：{info.summary}"
    return name


def parameters_text(name: str) -> list[str]:
    """某个规划器的参数表：名字、默认值、说明。"""
    return [f"  {item.name:<22} {item.default!r:<8} {item.help}" for item in parameters(name)]


def summary(result: PlanResult) -> str:
    return (
        f"{result.planner}: {len(result.frames)} 帧 / {result.event_count} 个事件 / "
        f"{result.pointer_count} 指针 / {result.duration_ms / 1000:.1f}s"
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="planner.py", description="auto_phigros 规划模块：官谱 -> 触控事件序列"
    )
    parser.add_argument("chart", type=Path, help="采集下来的谱面 .npz")
    parser.add_argument(
        "--planner",
        default=DEFAULT_PLANNER,
        choices=[info.name for info in catalog()],
        help="用哪个规划器（默认 %(default)s）",
    )
    parser.add_argument(
        "--set",
        action="append",
        metavar="名字=值",
        help="改一个规划器参数（可重复）；不带值打 --options 看清单",
    )
    parser.add_argument("--options", action="store_true", help="列出这个规划器的参数与默认值")
    parser.add_argument("-o", "--out", type=Path, default=PLANS_DIR, help="输出目录（默认 %(default)s）")
    parser.add_argument(
        "--no-cache",
        action="store_true",
        help="既不用已有的规划结果、也不写盘（默认是把它当缓存用）",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.options:
        print(f"[planner] {describe(args.planner)} 的参数：")
        print("\n".join(parameters_text(args.planner)))
        return 0

    try:
        options = options_from_args(args.set)
        text, ref = load_chart(args.chart)
    except (OSError, NpzFormatError, ValueError) as error:
        print(f"[planner] 读不了 {args.chart}：{error}", file=sys.stderr)
        return 2

    print(f"[planner] {args.chart.name}  ({ref.stem})")
    print(f"[planner] 用 {describe(args.planner)}" + (f"，参数 {options}" if options else ""))
    try:
        result = plan(
            text,
            planner=args.planner,
            options=options,
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
        print(f"[planner] 用的就是 {plan_path_for(ref, result.planner, args.out)}")
    else:
        print(f"[planner] 已保存 {plan_path_for(ref, result.planner, args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
