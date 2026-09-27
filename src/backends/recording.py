"""干跑后端：不连设备，只把"什么时候发了什么"记下来。

看着像测试替身，其实是个正经后端：`touch.py --backend recording` 就是完整的调度器
演练 —— 时钟、排序、提前量全都跑一遍，只是最后一步不写进设备。没有设备时这是唯一
能验调度器精度的办法，`selftest.py` 也用它。
"""

from __future__ import annotations

import time
from typing import Sequence

from algorithms.geometry import Screen
from algorithms.utils import TouchEvent


class RecordingBackend:
    """把每一批事件连同发出的时刻记在 :attr:`calls` 里。"""

    def __init__(self, *, serial: str | None = None) -> None:
        self.serial = serial
        """本来要发给哪台设备。注册表把公共参数一视同仁地递给每个后端，收下记着就是。"""
        self.screen: Screen | None = None
        self.calls: list[tuple[float, tuple[TouchEvent, ...]]] = []
        """``(主机 monotonic 时刻, 这一批事件)``，按发出顺序。"""

    def open(self, screen: Screen) -> None:
        self.screen = screen

    def close(self) -> None:
        pass

    def send(self, events: Sequence[TouchEvent]) -> None:
        self.calls.append((time.monotonic(), tuple(events)))

    @property
    def events(self) -> list[TouchEvent]:
        return [event for _, batch in self.calls for event in batch]


BACKEND = RecordingBackend
