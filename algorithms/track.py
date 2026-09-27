"""触控事件时间轴：规划器往这儿放事件，它负责拼成 ``PlanResult.frames``。

存在的理由只有一个：**别把"手指没动"的 MOVE 放出去**。

规划器按毫秒给手指位置采样（hold 全程、flick 摆动全程），但真正改变位置的只是其中
一部分；同一个坐标再报一遍，到了设备上仍然是一次完整的输入事件
（INJECT → 输入分发 → 应用输入队列）。实测默认规划器每秒 362 个事件里有 66.6% 是
这种空报 —— 足以把游戏主线程拖住：主线程一停，游戏时钟的采样就断了，触控跟着停，
恢复时 ``nowTime`` 按音频往前跳一大截，整个时间轴就和谱面错开了。

真实手指不动时本来就不会产生事件，所以丢掉空报**更接近真实输入**，不是取巧。
"""

from __future__ import annotations

from collections import defaultdict

from .utils import Position, Touch, TouchEvent


class EventTrack:
    """按毫秒收集触控事件，顺手压掉原地不动的 MOVE。"""

    def __init__(self) -> None:
        self._events: defaultdict[int, list[TouchEvent]] = defaultdict(list)
        self._last: dict[int, Position] = {}
        self.dropped = 0
        """压掉了多少个空报（同一指针、同一坐标的 MOVE）。"""

    def push(
        self, timestamp: int, position: Position, action: Touch, pointer: int
    ) -> None:
        """放一个事件。坐标没变的 MOVE 会被丢掉。"""
        if action is Touch.MOVE and self._last.get(pointer) == position:
            self.dropped += 1
            return

        if action is Touch.UP or action is Touch.CANCEL:
            self._last.pop(pointer, None)
        else:
            self._last[pointer] = position
        self._events[timestamp].append(
            TouchEvent(pointer, action, position.real, position.imag)
        )

    def frames(self) -> list[tuple[int, tuple[TouchEvent, ...]]]:
        """排好序的帧表。空帧不会存在 —— 没放过事件的时刻根本没建过键。"""
        return [(timestamp, tuple(items)) for timestamp, items in sorted(self._events.items())]

    def latest(self, fallback: int) -> int:
        """最后一个事件的时刻；一个事件都没有就是 ``fallback``。"""
        return max(self._events, default=fallback)

    @property
    def total(self) -> int:
        return sum(len(items) for items in self._events.values())
