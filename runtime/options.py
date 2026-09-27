#!/usr/bin/env python3
"""运行时设置：控制台能在跑的时候改的那几个旋钮。

只有一份。``main.py`` 造一个、控制台改它、规划与播放**每次都读它** —— 于是"改了什么时候
生效"这个问题根本不存在：延迟改了下一个事件就按新值发，规划器改了下一关就按新的算。
反过来，如果让每个模块各存一份、改的时候挨个去同步，就一定会有改漏的那一处。

不进这里的东西有两类，各有各的理由：

* **一次性参数**（``--host`` / ``--device-id`` / ``--attach``）—— 它们是"怎么连上"的，
  连着的时候改没有意义；
* **每局才知道的参数**（谱面镜像开关、游戏自己的延迟）—— 它们由 agent 随 ``level-start``
  报上来，是**观测结果**不是设置项，主机只能照做。
"""

from __future__ import annotations

from dataclasses import dataclass

from algorithms import DEFAULT_PLANNER


@dataclass(slots=True)
class Options:
    """运行时可调设置。字段即语义，改它就是改行为。"""

    planner: str = DEFAULT_PLANNER
    """用哪个规划器。下一关生效（规划本来就在开谱那一刻做）。"""

    latency: float = 0.0
    """注入链路的手工补偿（秒），正数 = 提前发。**立即生效**。"""

    inject: bool = True
    """是否真的把触控发给设备。关掉 = 照常排期、照常记迟到，只是不碰设备。"""

    verbose: bool = False
    """是否把每一个 Perfect 也打出来。默认关：一局几百条，只在查判定时才要。"""

    def summary(self) -> str:
        return (
            f"规划器 {self.planner}，手工补偿 {self.latency * 1000:+.0f}ms，"
            f"触控注入 {'开' if self.inject else '关'}"
            f"{'，判定流水（含 Perfect）开' if self.verbose else ''}"
        )
