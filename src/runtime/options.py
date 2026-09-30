#!/usr/bin/env python3
"""运行时设置：控制台能在跑的时候改的那几个旋钮。

只有一份。``main.py`` 造一个、控制台改它、规划与播放每次都读它 —— 于是"改了什么时候生效"
这个问题根本不存在。反过来，每个模块各存一份、改的时候挨个同步，就一定会有改漏的那一处。

不进来的东西：连设备用的一次性参数（``--host`` / ``--attach``），以及每局才知道的观测值
（谱面镜像开关、游戏自己的延迟）—— 后者由 agent 随 ``level-start`` 报上来，主机只能照做。
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

    lead_in: float = 3.0
    """单机跑（``python src/touch.py``）时留的起跑线：几秒后开始发。"""

    late_warn: float = 0.02
    """迟到超过这么多秒就记一笔。20ms = Perfect 窗口（80ms）的四分之一，够早才值得追查。"""

    late_skip: float = 0.15
    """迟到超过这么多秒的事件干脆不发：Good 窗口是 180ms，越过 150ms 基本已经判没了，
    补发救不回来，还会在游戏刚从卡顿里缓过来时再灌它一管子输入。"""

    inject: bool = True
    """是否真的把触控发给设备。关掉 = 照常排期、照常记迟到，只是不碰设备。"""

    gate: bool = True
    """开谱时要不要把游戏**拦在闸门上**等我们规划完（agent 侧阻塞 Unity 主线程）。

    拦着的好处是这一局从第一个音符起就按我们的排期走；关掉的好处是游戏主线程一秒都不停 ——
    查"游戏在某个时刻卡住"这类问题时先关它，谱面有缓存时关掉也照样满分。**立即生效**。
    """

    verbose: bool = False
    """是否把每一个 Perfect 也打出来。默认关：一局几百条，只在查判定时才要。"""

    def summary(self) -> str:
        return (
            f"规划器 {self.planner}，手工补偿 {self.latency * 1000:+.0f}ms，"
            f"触控注入 {'开' if self.inject else '关'}"
            f"{'，闸门关' if not self.gate else ''}"
            f"{'，判定流水（含 Perfect）开' if self.verbose else ''}"
        )
