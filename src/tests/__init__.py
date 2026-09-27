"""auto_phigros 的自检包。

`src/selftest.py` 是薄入口（`python src/selftest.py`），东西都在这里：

    coverage.py    算法输出的判据（事件流、覆盖、在位时长、滑键起手）
    archive.py     .psap 往返与镜像
    runtime.py     闸门 / 时钟 / 坐标 / 播放器 / 缓存 / 日志文件
    console.py     控制台回显与命令解析
    liveness.py    存活探测与收工（含"我们自己正忙"的豁免）
    accounting.py  判决对账、结算、延迟自校准
    attach.py      附加目标的解析（含实机"进程名≠包名"那种）
    settings.py    config.json 的读写与落盘边界
    referee.py     裁判（algorithms/judging.py）的规则判据
    stubs.py       顶掉 frida 会话与 Controller 的替身
    cli.py         入口：把上面全部跑一遍并打印结果

`python src/judge.py` 是另一条线：把一份规划按游戏真实判定重放，用来验证算法本身；
`python src/judge.py --compare <日志>` 拿实机日志逐音符对裁判 —— 那条是验收线。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
"""项目根目录（`src/` 的上一层）。自检与 `judge.py` 都从这里找 `charts/`、`plans/`。"""

SOURCE_ROOT = ROOT / "src"
"""Python 的导入根。`runtime` / `formats` / `tools` / `planner` 这些顶层名字都在它下面。"""

if str(SOURCE_ROOT) not in sys.path:
    # 兜底：`python -m tests.cli`、`python src/tests/cli.py`、pytest 哪种跑法都能 import 到那些
    # 顶层包（正常跑 `python src/selftest.py` 时脚本目录本来就是 `src/`，这一句是白给的）。
    sys.path.insert(0, str(SOURCE_ROOT))

