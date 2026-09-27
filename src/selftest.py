#!/usr/bin/env python3
"""自检入口：``python src/selftest.py``。

东西都在 ``tests/`` 包里，这里只做一件事 —— 把入口留在老地方（文档、习惯、脚本都在用
``python src/selftest.py``）。想分开跑或者把某组当库用：

    python -m tests.cli                  # 同一件事
    python src/judge.py plans/xxx.psap       # 另一条线：按游戏真实判定验证算法

退出码：0 = 全绿，1 = 有项目未通过，2 = ``charts/`` 是空的（跳过了规划相关的那部分）。
"""

from __future__ import annotations

import sys

from tests.cli import main

if __name__ == "__main__":
    sys.exit(main())
