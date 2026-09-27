#!/usr/bin/env python3
"""算法体检：把规划按**游戏真实的判定规则**重放一遍，报丢音、蹭键与判定分布。

和 `selftest.py` 的分工：

    python selftest.py     ← 代码有没有 bug（闸门、时钟、播放器、控制台、收工…），很快
    python judge.py        ← 算法有没有问题（丢音、蹭键、分布、分数），慢，且越全越慢

用法（conda 环境 auto_phigros）：

    python judge.py                          # 所有谱面 × 所有规划器（吃 plans/ 里的缓存）
    python judge.py --planner geometric      # 只体检一个规划器
    python judge.py --chart Dlyrotz          # 名字里带 Dlyrotz 的都跑（子串匹配）
    python judge.py --plan plans/xxx.psap    # 直接体检一份已有的规划（不再重算）
    python judge.py --no-cache               # 不吃也不写缓存，全部现算
    python judge.py --json out.json          # 另存一份机器可读的结果

报告的每一行都对应一件能被验证的事：判定窗口、容差、扫描窗、分数公式全部来自
`algorithms/judging.py`（出处见那里的注释与 `Phigros4.0-音游内核逆向报告.md`）。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

import planner
from algorithms import catalog, create
from algorithms.chart import Chart
from algorithms.judging import Report, Verdict, simulate
from storage import ChartRef, decode_plan
from tests.archive import check_mirror, check_storage
from tests.coverage import check_coverage, check_flick, check_stream

ROOT = Path(__file__).resolve().parent
CHARTS_DIR = ROOT / "charts"
PLANS_DIR = ROOT / "plans"


class QuietProgress:
    """什么都不打印的进度条 —— 报告本身就是输出，进度条会把两件事混在一起。"""

    def track(self, iterable, description: str):
        return iter(iterable)


def charts_named(needle: str | None) -> list[Path]:
    paths = sorted(
        path for path in CHARTS_DIR.glob("*.json") if not path.name.endswith(".meta.json")
    )
    if needle:
        paths = [path for path in paths if needle.lower() in path.name.lower()]
    return paths


def chart_for(path: Path) -> tuple[Chart, str]:
    text = path.read_text(encoding="utf-8")
    return Chart.parse(text), text


def plan_for(chart: Chart, text: str, ref: ChartRef, name: str, *, cache: bool) -> object:
    return planner.plan(
        text,
        planner=name,
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
    chart: Chart, text: str, ref: ChartRef, name: str, *, cache: bool, verbose: bool
) -> tuple[bool, Report | None, list[str]]:
    try:
        result = plan_for(chart, text, ref, name, cache=cache)
    except Exception as error:  # noqa: BLE001 - 一个规划器失败不该带走整轮体检
        print(f"  {name:<28} 规划失败：{type(error).__name__}: {error}")
        return False, None, []

    problems = check_stream(result)
    total, misses, _ = check_coverage(chart, result)
    problems += [f"覆盖：{miss}" for miss in misses]
    problems += check_storage(text, name, result)
    problems += check_flick(chart, result)
    _, mirror_misses, _ = check_mirror(text, result)
    problems += [f"镜像后：{miss}" for miss in mirror_misses]
    _ = total

    report = report_plan(chart, result, chart_name=ref.digest, plan_name=name)
    clean = print_report(report, problems=problems, verbose=verbose)
    return clean, report, problems


def judge_plan(path: Path, *, verbose: bool) -> tuple[bool, Report | None]:
    """直接体检一份已有的 `.psap`：从旁边的 meta 里找回谱面。"""
    result = decode_plan(path.read_bytes())
    meta_path = path.with_suffix(".meta.json")
    if not meta_path.is_file():
        print(f"  {path.name} 旁边没有 meta.json，找不到它对应的谱面")
        return False, None
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    digest = meta.get("digest") or meta.get("hash")
    matches = [p for p in charts_named(None) if digest and digest in p.name]
    if not matches:
        print(f"  {path.name} 对应的谱面不在 charts/ 里（digest={digest}）")
        return False, None
    chart, text = chart_for(matches[0])
    problems = check_stream(result)
    _, misses, _ = check_coverage(chart, result)
    problems += [f"覆盖：{miss}" for miss in misses]
    problems += check_flick(chart, result)
    report = report_plan(chart, result, chart_name=matches[0].name, plan_name=path.stem)
    clean = print_report(report, problems=problems, verbose=verbose)
    _ = text
    return clean, report


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
                "target": g.target,
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
    parser.add_argument("--plan", type=Path, default=None, help="直接体检这份 .psap")
    parser.add_argument(
        "--cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=f"吃 {PLANS_DIR.name}/ 里的规划缓存（默认开；--no-cache 全部现算）",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="把每条覆盖问题都打出来")
    parser.add_argument("--json", type=Path, default=None, help="把结果另存成 JSON")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
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
            chart, text = chart_for(path)
            ref = ChartRef.of_text(text, seq=0, context={"songsId": path.stem})
            print(f"\n{path.name}（{chart.note_count} 音符 / {len(chart.lines)} 判定线）")
            for name in names:
                ok, report, problems = judge_one(
                    chart, text, ref, name, cache=args.cache, verbose=args.verbose
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
