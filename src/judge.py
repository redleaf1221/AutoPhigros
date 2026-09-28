#!/usr/bin/env python3
"""算法体检：把规划按**游戏真实的判定规则**重放一遍，报丢音、蹭键与判定分布。

`selftest.py` 查代码有没有 bug，很快；本模块查算法有没有问题，慢，且越全越慢。
判定窗口、容差、扫描窗、分数公式全部来自 `algorithms/judging.py`，出处见那里的注释与
`Phigros4.0-音游内核逆向报告.md`。

用法（conda 环境 auto_phigros）：

    python src/judge.py                          # 所有谱面 × 所有规划器（吃 plans/ 里的缓存）
    python src/judge.py --planner geometric      # 只体检一个规划器
    python src/judge.py --chart Dlyrotz          # 名字里带 Dlyrotz 的都跑（子串匹配）
    python src/judge.py --plan plans/xxx.npz     # 直接体检一份已有的规划（不再重算）
    python src/judge.py --no-cache               # 不吃也不写缓存，全部现算
    python src/judge.py --json out.json          # 另存一份机器可读的结果
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

import planner
from algorithms import catalog, options_from_args
from algorithms.chart import Chart
from algorithms.judging import Report, Verdict, simulate
from formats.storage import (
    CHART_SUFFIX,
    ChartRef,
    NpzFormatError,
    load_chart,
    load_plan,
    plan_meta,
    plan_path_for,
)
from tests.archive import check_mirror, check_storage
from tests.coverage import check_coverage, check_flick, check_stream

ROOT = Path(__file__).resolve().parents[1]
"""项目根目录 —— ``src/`` 的上一层。谱面与规划缓存都在那儿，不在源码树里。"""
CHARTS_DIR = ROOT / "charts"
PLANS_DIR = ROOT / "plans"


class QuietProgress:
    """什么都不打印的进度条 —— 报告本身就是输出，进度条会把两件事混在一起。"""

    def track(self, iterable, description: str):
        return iter(iterable)


def charts_named(needle: str | None) -> list[Path]:
    paths = sorted(CHARTS_DIR.glob(f"*{CHART_SUFFIX}"))
    if needle:
        paths = [path for path in paths if needle.lower() in path.name.lower()]
    return paths


def chart_for(path: Path) -> tuple[Chart, str, ChartRef]:
    """读一张谱面：原文、解析结果、以及它的身份（身份取自文件自己，不另算一个）。"""
    text, ref = load_chart(path)
    return Chart.parse(text), text, ref


def plan_for(
    chart: Chart,
    text: str,
    ref: ChartRef,
    name: str,
    *,
    cache: bool,
    options: Mapping[str, Any] | None = None,
) -> object:
    return planner.plan(
        text,
        planner=name,
        options=options,
        ref=ref,
        cache=cache,
        directory=PLANS_DIR,
        progress=QuietProgress(),
    )


def report_plan(chart: Chart, result, *, chart_name: str, plan_name: str) -> Report:
    return simulate(chart, result, plan=plan_name, chart_name=chart_name)


def print_report(report: Report, *, problems: list[str], verbose: bool) -> bool:
    """打印一份体检报告；返回"这份规划是否干净"。"""
    table = report.counts
    total = sum(table.values())
    score, percent = report.score()
    print(
        f"  {report.plan:<28} {table[Verdict.PERFECT]:>5}P {table[Verdict.GOOD]:>4}G "
        f"{table[Verdict.BAD]:>3}B {table[Verdict.MISS]:>4}M  "
        f"（判到 {total}/{report.notes}）  估算 {score} 分 / {percent:.2f}%  "
        f"最大连击 {report.max_combo}"
    )
    for judgement in report.lost[:8]:
        print(
            f"      ✗ 丢音 {judgement.kind.name} @ {judgement.seconds:.3f}s"
            f"（线 {judgement.line}）"
        )
    if len(report.lost) > 8:
        print(f"      ✗ 还有 {len(report.lost) - 8} 个丢音")
    for graze in report.grazes[:8]:
        print(f"      ~ {graze.text()}")
    if len(report.grazes) > 8:
        print(f"      ~ 还有 {len(report.grazes) - 8} 处蹭键")
    if verbose:
        for problem in problems:
            print(f"      ! {problem}")
    else:
        for problem in problems[:3]:
            print(f"      ! {problem}")
        if len(problems) > 3:
            print(f"      ! 还有 {len(problems) - 3} 条覆盖 / 镜像问题（-v 全看）")
    return not (problems or report.lost or report.grazes)


def judge_one(
    chart: Chart,
    text: str,
    ref: ChartRef,
    name: str,
    *,
    cache: bool,
    verbose: bool,
    options: Mapping[str, Any] | None = None,
) -> tuple[bool, Report | None, list[str]]:
    try:
        result = plan_for(chart, text, ref, name, cache=cache, options=options)
    except Exception as error:  # noqa: BLE001 - 一个规划器失败不该带走整轮体检
        print(f"  {name:<28} 规划失败：{type(error).__name__}: {error}")
        return False, None, []

    problems = check_stream(result)
    total, misses, _ = check_coverage(chart, result)
    problems += [f"覆盖：{miss}" for miss in misses]
    problems += check_storage(text, name, result, options)
    problems += check_flick(chart, result)
    _, mirror_misses, _ = check_mirror(text, result)
    problems += [f"镜像后：{miss}" for miss in mirror_misses]
    _ = total

    report = report_plan(chart, result, chart_name=ref.digest, plan_name=name)
    clean = print_report(report, problems=problems, verbose=verbose)
    return clean, report, problems


def judge_plan(path: Path, *, verbose: bool) -> tuple[bool, Report | None]:
    """直接体检一份已有的规划（不再重算）：谱面的身份就在规划结果自己的 meta 里。"""
    try:
        meta = plan_meta(path)
        result = load_plan(path)
    except (OSError, NpzFormatError) as error:
        print(f"  {path.name} 读不了：{error}")
        return False, None

    chart_meta = meta.get("chart") or {}
    ref = ChartRef(
        seq=int(chart_meta.get("seq") or 0),
        context=chart_meta.get("context") or {},
        digest=str(chart_meta.get("hash") or "unknown"),
    )
    matches = [chart for chart in charts_named(None) if chart.stem == ref.stem]
    if not matches:
        print(f"  {path.name} 对应的谱面（{ref.stem}）不在 charts/ 里")
        return False, None
    plan_chart, _, _ = chart_for(matches[0])
    problems = check_stream(result)
    _, misses, _ = check_coverage(plan_chart, result)
    problems += [f"覆盖：{miss}" for miss in misses]
    problems += check_flick(plan_chart, result)
    report = report_plan(plan_chart, result, chart_name=matches[0].name, plan_name=path.stem)
    clean = print_report(report, problems=problems, verbose=verbose)
    return clean, report


def compare(log_path: Path, *, verbose: bool, limit: int = 40) -> int:
    """把裁判和**设备那一次运行**逐音符对上 —— 不一致的地方就是要修裁判的地方。

    身份是游戏自己的 `noteCode` 那套（线 / 上或下 / 同侧第几个），所以两边能一条条对起来
    —— 光比总数只知道"大概哪里不对"，说不出是哪几个音符、差在哪。这个模式**不改任何
    东西**，只报事实：哪些音符两边判得不一样、各自判成了什么。
    """
    from tools import device_log

    run = device_log.read(log_path)
    print(f"{log_path.name}：{len(run.judges)} 条判定，设备自己的账目是 {run.label}")
    if run.latency is not None:
        # 用这一局真实的送达补偿重放：补偿没抵掉处理延迟时，计划里的落点本身就是偏的
        import algorithms.judging as judging

        judging.DELIVERED_LATENCY = run.latency
        print(f"  按这一局的送达补偿重放：手工补偿 {run.latency * 1000:+.0f}ms")
    if not run.judges:
        print("  这份日志里一条判定都没有 —— 没什么可比的")
        return 1
    if run.chart is None or run.planner is None:
        print("  日志里没写清是哪张谱面 / 哪个规划器（要 `[chart …] 已保存 …` 与 `[plan …]`）")
        return 1

    # 按 stem 找：日志记的是存成 npz 之前的文件名（`.json`），身份是 stem。
    stem = Path(run.chart).stem
    charts = [path for path in charts_named(stem) if path.stem == stem]
    if not charts:
        print(f"  charts/ 里没有 {stem} —— 采集时没开 save-chart？")
        return 1
    chart, _, ref = chart_for(charts[0])
    plan_path = plan_path_for(ref, run.planner, PLANS_DIR)
    try:
        result = load_plan(plan_path)
    except (OSError, NpzFormatError):
        print(f"  plans/ 里没有可用的 {plan_path.name} —— 先跑一次规划（或 --no-cache 现算）")
        return 1
    report = report_plan(chart, result, chart_name=charts[0].name, plan_name=plan_path.stem)
    mine = report.counts
    score, percent = report.score()
    mine_text = (
        f"{mine[Verdict.PERFECT]}P  {mine[Verdict.GOOD]}G  "
        f"{mine[Verdict.BAD]}B  {mine[Verdict.MISS]}M"
    )
    device_score = run.result.get("score")
    device_text = (
        f"{run.result['score']} 分 / {run.result['percent']}%"
        if device_score is not None
        else "（日志里没有 result 行）"
    )
    print(f"  设备（日志）：{run.label:<24} {device_text}  最大连击 {run.result.get('maxCombo', '?')}")
    print(f"  裁判（本机）：{mine_text:<24} {score:.0f} 分 / {percent:.2f}%  最大连击 {report.max_combo}")

    # 逐音符对。设备那边可能少几条（比如日志被截断），少的那边不算"不一致"但要报出来
    pairs: list[tuple[str, object, object]] = []
    compared = 0
    only_mine = 0
    for judgement in report.judgements:
        device = run.judges.get(judgement.key)
        if device is None:
            only_mine += 1
            continue
        compared += 1
        if device.kind != judgement.verdict.label:
            pairs.append(("不同", judgement, device))
    only_device = len(set(run.judges) - {judgement.key for judgement in report.judgements})

    print(
        f"  逐音符比出 {compared} 条：一致 {compared - len(pairs)}，"
        f"不一致 {len(pairs)}"
        + (f"，裁判多出 {only_mine} 条" if only_mine else "")
        + (f"，设备多出 {only_device} 条" if only_device else "")
    )
    if not pairs:
        return 0

    # 先把"哪一类不一致"归堆：同类问题会成片出现，归堆之后一眼看出是不是一个模型错误
    summary: dict[str, int] = {}
    for _, judgement, device in pairs:
        key = f"设备 {device.kind} / 裁判 {judgement.verdict.label}"
        summary[key] = summary.get(key, 0) + 1
    print("  ── 不一致的类别 ──")
    for key, count in sorted(summary.items(), key=lambda item: -item[1]):
        print(f"     {count:>4} 条：{key}")

    print("  ── 逐条（按时间）──")
    pairs.sort(key=lambda item: item[1].seconds)
    for _, judgement, device in pairs[:limit]:
        note = judgement.note
        mine_note = (
            f"{note.kind.name:<4} {judgement.seconds:8.3f}s "
            f"（线 {judgement.line} {'上' if judgement.above else '下'} 第 {judgement.index}）"
        )
        graze = "（裁判记成蹭键）" if judgement.grazed else ""
        print(f"     {mine_note}：设备 {device.kind:<7} 裁判 {judgement.verdict.label}{graze}")
    if len(pairs) > limit:
        print(f"     … 还有 {len(pairs) - limit} 条（-v 看全部）")
    if verbose:
        _ = verbose
    return 1 if pairs else 0


def serialise(report: Report, problems: list[str]) -> dict:
    table = report.counts
    score, percent = report.score()
    return {
        "chart": report.chart,
        "plan": report.plan,
        "notes": report.notes,
        "counts": {verdict.label: table[verdict] for verdict in Verdict},
        "score": score,
        "percent": percent,
        "max_combo": report.max_combo,
        "lost": [
            {"kind": j.kind.name, "seconds": j.seconds, "line": j.line} for j in report.lost
        ],
        "grazes": [
            {
                "kind": g.note.kind.name,
                "seconds": g.note.seconds,
                "line": g.line,
                "verdict": g.verdict.label,
                "delta_ms": g.delta * 1000.0,
                "pointer": g.pointer,
                "stolen": g.stolen,
            }
            for g in report.grazes
        ],
        "problems": problems,
    }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="judge.py", description="算法体检：按游戏真实判定重放规划，报丢音与蹭键"
    )
    parser.add_argument("--chart", default=None, help="只跑名字里带这个子串的谱面")
    parser.add_argument(
        "--planner",
        default=None,
        choices=[info.name for info in catalog()],
        help="只体检这个规划器（默认全部）",
    )
    parser.add_argument("--plan", type=Path, default=None, help="直接体检这份 .npz")
    parser.add_argument(
        "--set",
        action="append",
        metavar="名字=值",
        help="改一个规划器参数（要跟 --planner 一起用，可重复）",
    )
    parser.add_argument(
        "--cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=f"吃 {PLANS_DIR.name}/ 里的规划缓存（默认开；--no-cache 全部现算）",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="把每条覆盖问题都打出来")
    parser.add_argument(
        "--compare",
        type=Path,
        default=None,
        metavar="日志",
        help="拿一次运行的日志（logs/*.log）逐音符对裁判：不一致的地方就是要修的地方",
    )
    parser.add_argument("--json", type=Path, default=None, help="把结果另存成 JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.compare is not None:
        if not args.compare.is_file():
            print(f"没有这个文件：{args.compare}", file=sys.stderr)
            return 2
        return compare(args.compare, verbose=args.verbose)

    try:
        options = options_from_args(args.set)
    except ValueError as error:
        print(f"参数写错了：{error}", file=sys.stderr)
        return 2
    if options and not args.planner:
        print("--set 要跟 --planner 一起用（参数是某个规划器的）", file=sys.stderr)
        return 2

    entries: list[dict] = []
    clean = True

    if args.plan is not None:
        if not args.plan.is_file():
            print(f"没有这个文件：{args.plan}", file=sys.stderr)
            return 2
        ok, report = judge_plan(args.plan, verbose=args.verbose)
        clean = clean and ok
        if report is not None:
            entries.append(serialise(report, []))
    else:
        names = [args.planner] if args.planner else [info.name for info in catalog()]
        paths = charts_named(args.chart)
        if not paths:
            print("charts/ 里没有匹配的谱面。先用 main.py --save-chart 采一张。", file=sys.stderr)
            return 2
        for path in paths:
            chart, text, ref = chart_for(path)
            print(f"\n{path.name}（{chart.note_count} 音符 / {len(chart.lines)} 判定线）")
            for name in names:
                ok, report, problems = judge_one(
                    chart,
                    text,
                    ref,
                    name,
                    cache=args.cache,
                    verbose=args.verbose,
                    options=options,
                )
                clean = clean and ok
                if report is not None:
                    entries.append(serialise(report, problems))

    print("\n" + ("没有发现问题" if clean else "有项目需要看一眼"))
    if args.json is not None:
        args.json.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"结果已写入 {args.json}")
    return 0 if clean else 1


if __name__ == "__main__":
    sys.exit(main())
