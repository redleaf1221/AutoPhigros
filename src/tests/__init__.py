"""auto_phigros 的自检包：`src/selftest.py` 是薄入口（`python src/selftest.py`）。

各组判据都在本包：coverage / archive / pipeline / console / liveness / accounting / attach /
settings / referee / stubs，`cli.py` 把它们跑一遍并打印结果。
`python src/judge.py` 是另一条线（`--compare <日志>` 拿实机日志逐音符对裁判，那是验收线）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
"""项目根目录（`src/` 的上一层）。自检与 `judge.py` 都从这里找 `charts/`、`plans/`。"""

SOURCE_ROOT = ROOT / "src"
"""Python 的导入根。`runtime` / `formats` / `tools` / `planner` 这些顶层名字都在它下面。"""

if str(SOURCE_ROOT) not in sys.path:
    # 兜底：`python -m tests.cli`、`python src/tests/cli.py`、pytest 哪种跑法都能 import 到顶层包。
    sys.path.insert(0, str(SOURCE_ROOT))
