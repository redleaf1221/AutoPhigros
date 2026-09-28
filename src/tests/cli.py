"""自检入口：**只跑与代码有关的那几项**，不逐张谱做规划。

这里捕捉真正的 bug（闸门放行、时钟、坐标、播放器排期、控制台、探活、收工、结算、
判决对账、在位时长、规划缓存），与 `charts/` 里有几张谱无关；逐张谱 × 规划器的算法体检
在 `judge.py`。唯一用到谱面的是缓存自检（要真的规划一次），所以只挑**最小**的那张。
"""

from __future__ import annotations

if __package__ in (None, ""):
    # `python src/tests/cli.py` 跑法：脚本目录是 src/tests/、顶层包在 src/ 里，自己挂上源码根再以包的身份跑一遍。
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests.cli import main as _main

    raise SystemExit(_main())

from algorithms import catalog

from formats.storage import CHART_SUFFIX, load_chart
from . import ROOT
from .accounting import check_calibration, check_device_log, check_judge, check_result
from .attach import check_attach
from .console import check_console
from .coverage import TOLERANCE, check_dwell
from .liveness import check_liveness, check_shutdown
from .planner import check_planner
from .referee import check_referee
from .pipeline import check_cache, check_clock, check_gate, check_log_file, check_pixels, check_player
from .settings import check_config


def _charts() -> list:
    return sorted((ROOT / "charts").glob(f"*{CHART_SUFFIX}"))


def main() -> int:
    charts = _charts()

    print(f"垂直判定横向容差 {TOLERANCE:.3f}（CheckNote 的 1.9 × 屏幕缩放 0.9）")
    print("可用规划器：")
    for info in catalog():
        print(f"  {info.name:<14} {info.summary}")

    failed = False

    gate_problems, gate_output = check_gate()
    print("\n闸门自检：" + ("通过" if not gate_problems else "有问题"))
    for problem in gate_problems:
        print(f"  ! {problem}")
    if gate_problems and gate_output:
        print("  —— agent 当时的输出 ——")
        print("\n".join(f"  | {line}" for line in gate_output.splitlines()))
    failed = failed or bool(gate_problems)

    for label, problems in (
        ("时钟自检", check_clock()),
        ("坐标换算自检", check_pixels()),
        ("播放器自检", check_player()),
        ("控制台自检", check_console()),
        ("附加目标自检", check_attach()),
        ("存活探测自检", check_liveness()),
        ("收工自检", check_shutdown()),
        ("结算自检", check_result()),
        ("延迟自校准自检", check_calibration()),
        ("判定对账自检", check_judge()),
        ("日志对账自检", check_device_log()),
        ("裁判自检", check_referee()),
        ("规划器自检", check_planner()),
        ("在位时长自检", check_dwell()),
        ("配置自检", check_config()),
        ("日志自检", check_log_file()),
    ):
        print(f"{label}：" + ("通过" if not problems else "有问题"))
        for problem in problems:
            print(f"  ! {problem}")
        failed = failed or bool(problems)

    if charts:
        smallest = min(charts, key=lambda path: path.stat().st_size)
        problems = check_cache(load_chart(smallest)[0])
        print(f"缓存自检（最小的 {smallest.name}）：" + ("通过" if not problems else "有问题"))
        for problem in problems:
            print(f"  ! {problem}")
        failed = failed or bool(problems)
    else:
        print("\ncharts/ 里没有谱面，跳过缓存自检（算法体检在 judge.py，那边也得先有谱面）。")

    print("\n" + ("全部通过" if not failed else "有项目未通过"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
