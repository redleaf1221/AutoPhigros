"""自检入口：**只跑与代码有关的那几项**，不逐张谱做规划。

分工是这样的（`python src/selftest.py` 与 `python src/judge.py` 各管一头）：

* 这里 —— 捕捉**真正的 bug**：闸门放不放行、时钟会不会偏早、坐标换算、播放器排期、
  控制台回显、存活探测、收工、结算、判决对账、在位时长、规划缓存。都很快，
  与 `charts/` 里有几张谱无关；
* `judge.py` —— 揪**算法问题**：逐张谱 × 每个规划器跑一遍，报丢音、蹭键、判定分布。
  那个慢，而且慢得有用（谱面越多越慢）。

唯一用到谱面的是缓存自检（它得真的规划一次才能验编解码往返），所以只挑**最小**的那张。
"""

from __future__ import annotations

if __package__ in (None, ""):
    # `python src/tests/cli.py` 这种跑法：进 sys.path 的是脚本目录 `src/tests/`，而顶层包在它
    # 上一层的 `src/` 里，于是 `import algorithms` 立刻失败（相对 import 更是连父包都没有）。
    # 既然 `tests/__init__.py` 里承诺了"哪种跑法都能跑"，这里就自己把源码根挂上、再以包的身份
    # 跑一遍。
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests.cli import main as _main

    raise SystemExit(_main())

from algorithms import catalog

from . import ROOT
from .accounting import check_calibration, check_judge, check_result
from .attach import check_attach
from .console import check_console
from .coverage import TOLERANCE, check_dwell
from .liveness import check_liveness, check_shutdown
from .referee import check_referee
from .runtime import check_cache, check_clock, check_gate, check_log_file, check_pixels, check_player
from .settings import check_config


def _charts() -> list:
    return sorted(
        path for path in (ROOT / "charts").glob("*.json") if not path.name.endswith(".meta.json")
    )


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
        ("裁判自检", check_referee()),
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
        problems = check_cache(smallest.read_text(encoding="utf-8"))
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
