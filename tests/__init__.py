"""auto_phigros 的自检包。

根目录的 `selftest.py` 是薄入口（`python selftest.py`），东西都在这里：

    coverage.py    算法输出的判据（事件流、覆盖、在位时长、滑键起手）
    archive.py     .psap 往返与镜像
    charts.py      每张谱 × 每个规划器的五项检查（驱动上面几个）
    runtime.py     闸门 / 时钟 / 坐标 / 播放器 / 缓存
    console.py     控制台回显与命令解析
    liveness.py    存活探测与收工
    accounting.py  判决对账与结算
    stubs.py       顶掉 frida 会话与 Controller 参数的替身
    cli.py         入口：把上面全部跑一遍并打印结果

`python judge.py` 是另一条线：把一份规划按游戏真实判定重放，用来验证算法本身。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
"""项目根目录。自检与 `judge.py` 都从这里找 `charts/`、`plans/`。"""

if str(ROOT) not in sys.path:
    # 让 `python -m tests.cli`、`python tests/cli.py`、pytest 三种跑法都能 import 到根目录
    # 那些模块（planner / storage / main …）
    sys.path.insert(0, str(ROOT))
