"""激进算法（吸收自 phisap 的"激进算法"）。

思路：把 hold 拆成"开头一次 tap + 之后每毫秒一个 drag"，于是全曲只剩 tap / drag / flick
三种瞬时动作；然后在 1 毫秒的时间栅格上贪心分配手指 —— 优先复用"还停在屏幕上但已经
完成使命"的手指（一条 MOVE 就能就位），实在没有才按下新的。手指固定十根，没有就报错
（``ignore_allocation_errors`` 打开时改为跳过这一笔）。

与 phisap 版本的差别：原实现在调用 ``Screen.remap`` 时把"旋转向量"当成了"弧度"又取了
一次 ``exp``（``remap(pos, cmath.exp(angle * 1j))``，而 ``angle`` 本身已经是
``cmath.exp(...)`` 的结果），这里按本意传入旋转向量。
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import Iterator, NamedTuple

from .chart import Chart, NoteType
from .geometry import Screen, place_note
from .track import EventTrack
from .utils import PlanResult, PlanningError, Position, Progress, Touch, TouchEvent, Vector


@dataclass(slots=True)
class RadicalConfig:
    flick_start: int = -17
    """滑键手势相对判定时刻的起点偏移（毫秒）。"""
    flick_end: int = 17
    flick_repeats: int = 2
    """一次滑键手势重复划几下。**别调回 1。**

    游戏那边一次"新起手"（``Fingers.isNewFlick``）只够点亮一个 flick，而且会被判定窗口里
    更早的音符抢走 —— 一个音符只划一下就是一次机会，漏了就是漏了（``conservative`` 那边
    同样的理由与测算写在 ``ConservativeConfig.flick_repeats``）。这里每多划一下，
    就多一次独立机会：第二下从 ``+半径`` **跳**回去重新划，跳变本身就是一次新起手。
    """
    flick_sample_ms: int = 2
    """滑键手势里隔多少毫秒补一个触点。

    本来是 1ms（这个算法的时间栅格就是 1ms），但重复两下之后事件数直接翻倍：实测
    ``Eradication Catastrophe`` IN 的峰值冲到 **419 个/秒** —— 而 362 个/秒正是本项目
    量出来"会把注入链和游戏主线程一起拖住"的那个量级（见 ``ConservativeConfig.sample_delay``）。
    改成 2ms 之后每个 flick 的事件数与改之前一样多（34 个），峰值回到 200 出头，
    而每 2ms 走完 ``2/34`` 个半径的位移，比游戏的速度阈值宽得多。
    """
    flick_direction: int = 1
    """0 = 垂直于判定线滑动，1 = 平行于判定线滑动。"""
    recycle_scope_ratio: float = 0.05
    """可以顺手征用的手指与目标的横向距离阈值，取屏幕对角线的这个比例。"""
    max_pointers: int = 10
    ignore_allocation_errors: bool = False


class PlainNote(NamedTuple):
    kind: NoteType
    timestamp: int
    position: Position
    rotation: Vector


class Frame:
    """1 毫秒的操作帧。"""

    __slots__ = ("timestamp", "pending")

    def __init__(self, timestamp: int) -> None:
        self.timestamp = timestamp
        self.pending: dict[NoteType, list[PlainNote]] = defaultdict(list)

    def add(self, note: PlainNote) -> None:
        self.pending[note.kind].append(note)

    def take(self, kind: NoteType) -> list[PlainNote]:
        return self.pending.pop(kind, [])


class FrameSet:
    __slots__ = ("frames",)

    def __init__(self) -> None:
        self.frames: dict[int, Frame] = {}

    def __getitem__(self, timestamp: int) -> Frame:
        frame = self.frames.get(timestamp)
        if frame is None:
            frame = Frame(timestamp)
            self.frames[timestamp] = frame
        return frame

    def __iter__(self) -> Iterator[Frame]:
        return iter(sorted(self.frames.values(), key=lambda frame: frame.timestamp))

    def __len__(self) -> int:
        return len(self.frames)


@dataclass(slots=True)
class Pointer:
    id: int
    note: PlainNote | None = None
    age: int = 0
    """这根手指离上一次被摆动过去了多久（毫秒）。负数表示它正忙着划一个 flick。"""


class PointerAllocator:
    __slots__ = (
        "screen",
        "flick_start",
        "flick_duration",
        "flick_repeats",
        "flick_sample_ms",
        "rotate_factor",
        "recycle_scope",
        "ignore_allocation_errors",
        "pointers",
        "track",
        "now",
        "last_timestamp",
    )

    def __init__(self, screen: Screen, config: RadicalConfig) -> None:
        self.screen = screen
        self.flick_start = config.flick_start
        self.flick_duration = config.flick_end - config.flick_start
        if self.flick_duration <= 0:
            raise PlanningError(f"flick 手势时长非正：{config.flick_start} -> {config.flick_end}")
        self.flick_repeats = max(1, config.flick_repeats)
        if config.flick_sample_ms <= 0:
            raise PlanningError(f"flick 采样间隔非正：{config.flick_sample_ms}")
        self.flick_sample_ms = config.flick_sample_ms
        self.rotate_factor = 1j if config.flick_direction == 0 else 1
        self.recycle_scope = (screen.width + screen.height) * config.recycle_scope_ratio
        self.ignore_allocation_errors = config.ignore_allocation_errors
        self.pointers = [Pointer(1000 + index) for index in range(config.max_pointers)]
        self.track = EventTrack()
        self.now = 0
        self.last_timestamp: int | None = None

    # ------------------------------------------------------------------ 分配

    def allocate(self, frame: Frame) -> None:
        assert self.last_timestamp is None or frame.timestamp >= self.last_timestamp
        self.now = frame.timestamp
        if self.last_timestamp is not None:
            elapsed = self.now - self.last_timestamp
            for pointer in self.pointers:
                pointer.age += elapsed

        # 顺序有讲究：tap 占下的手指待会儿还能被 flick / drag 征用
        for note in frame.take(NoteType.TAP):
            self._tap(self._alloc(note), note)
        for note in frame.take(NoteType.FLICK):
            self._flick(self._alloc(note), note)
        for note in frame.take(NoteType.DRAG):
            self._drag(note)

        self.last_timestamp = frame.timestamp

    def _alloc(self, note: PlainNote) -> Pointer:
        candidates = [
            pointer
            for pointer in self.pointers
            if pointer.note is None or pointer.age > 0  # 空闲的，或者歇了一会儿的
        ]
        if not candidates:
            raise PlanningError(f"{self.now}ms 处没有可用手指")
        return min(candidates, key=lambda pointer: _distance(pointer.note, note))

    def _reusable(self, note: PlainNote) -> Pointer | None:
        """找一根手上还按着、但已经闲下来的手指，且它与目标在同一条判定线方向附近。"""
        for pointer in self.pointers:
            if pointer.note is None or pointer.age <= 0:
                continue
            lateral = ((pointer.note.position - note.position) * note.rotation.conjugate()).real
            if abs(lateral) < self.recycle_scope:
                return pointer
        return None

    # ------------------------------------------------------------------ 动作

    def _tap(self, pointer: Pointer, note: PlainNote) -> None:
        if pointer.note is not None:
            # 征用一根还按着的手指：抬起排在**最后一刻**（这一帧的前一毫秒）。
            # 别排成"上次用完的下一毫秒" —— 手指摆在哪儿就是判定依据，早抬一毫秒
            # 就等于把上一个音符的覆盖砍成一个时间点，而游戏是逐帧读位置的
            # （见 algorithms.utils.MIN_DWELL_MS）。
            self._insert(self.now - 1, pointer.note.position, Touch.UP, pointer.id)
        pointer.note = note
        pointer.age = 0
        self._insert(self.now, note.position, Touch.DOWN, pointer.id)

    def _flick(self, pointer: Pointer, note: PlainNote) -> None:
        action = Touch.DOWN if pointer.note is None else Touch.MOVE
        self._insert(self.now, note.position, action, pointer.id)

        swipe = note.position
        for repeat in range(self.flick_repeats):
            # 每一下都从 +半径 跳到 一侧再划到 -半径（见 RadicalConfig.flick_repeats）
            base = self.now + repeat * self.flick_duration
            for delta in range(0, self.flick_duration, self.flick_sample_ms):
                rate = 1 - 2 * delta / self.flick_duration
                swipe = (
                    note.position
                    + note.rotation * self.rotate_factor * rate * self.screen.flick_radius
                )
                self._insert(base + delta, swipe, Touch.MOVE, pointer.id)

        pointer.note = note._replace(position=swipe)
        pointer.age = -self.flick_duration * self.flick_repeats

    def _drag(self, note: PlainNote) -> None:
        pointer = self._reusable(note)
        if pointer is not None:
            # 手指本来就在判定区里，什么都不用做
            pointer.age = 0
            return
        try:
            pointer = self._alloc(note)
        except PlanningError:
            if self.ignore_allocation_errors:
                return
            raise
        action = Touch.DOWN if pointer.note is None else Touch.MOVE
        self._insert(self.now, note.position, action, pointer.id)
        # 同一毫秒补发一条 MOVE：部分设备会丢掉紧随 DOWN 之后的第一条移动
        self._insert(self.now, note.position, Touch.MOVE, pointer.id)
        pointer.note = note
        pointer.age = 0

    # ------------------------------------------------------------------ 收尾

    def _insert(self, timestamp: int, position: Position, action: Touch, pointer: int) -> None:
        self.track.push(timestamp, position, action, pointer)

    def done(self) -> list[tuple[int, tuple[TouchEvent, ...]]]:
        if self.last_timestamp is not None:
            # 抬起时刻要同时盖过两件事：
            #   * flick 的滑动事件会一直铺到当前帧之后若干毫秒；
            #   * 没有事件产生的帧（drag 被"已经按住的手指"顺手判掉时就是这样）
            #     手指依然按在屏幕上，不能因为它没说话就把它当成已经抬起。
            latest = self.track.latest(self.last_timestamp)
            final = max(self.last_timestamp, latest) + 1
            for pointer in self.pointers:
                if pointer.note is not None:
                    self._insert(final, pointer.note.position, Touch.UP, pointer.id)
        return self.track.frames()


def _distance(from_note: PlainNote | None, to_note: PlainNote) -> float:
    if from_note is None:
        return math.inf
    return abs(to_note.position - from_note.position)


class RadicalPlanner:
    name = "radical"

    def __init__(self, config: RadicalConfig | None = None) -> None:
        self.config = config or RadicalConfig()

    def plan(self, chart: Chart, progress: Progress) -> PlanResult:
        screen = chart.screen
        warnings: list[str] = []
        frames = FrameSet()
        retimed = 0

        for line in progress.track(chart.lines, "统计操作帧"):
            for note in line.notes:
                timestamp = round(note.seconds * 1000)
                placement = place_note(line, note, screen, retime=note.kind is NoteType.FLICK)
                if placement.retimed:
                    retimed += 1
                    warnings.append(
                        f"{note.kind.name} @ {note.seconds:.3f}s 判定点在屏幕外，"
                        f"已微调至 {placement.seconds:.3f}s"
                    )
                elif placement.remapped and len(warnings) < 20:
                    warnings.append(
                        f"{note.kind.name} @ {note.seconds:.3f}s 判定点在屏幕外，已沿判定线拉回屏幕"
                    )

                if note.kind is NoteType.HOLD:
                    # hold = 开头一次 tap + 按住期间每毫秒一个 drag
                    frames[timestamp].add(PlainNote(NoteType.TAP, timestamp, screen.remap(placement.position, placement.rotation), placement.rotation))
                    hold_ms = math.ceil(note.hold * 1000)
                    for offset in range(1, hold_ms + 1):
                        moment = (timestamp + offset) / 1000
                        position = line.point_at(moment, note.offset)
                        rotation = line.rotation_at(moment)
                        frames[timestamp + offset].add(
                            PlainNote(NoteType.DRAG, timestamp + offset, screen.remap(position, rotation), rotation)
                        )
                elif note.kind is NoteType.FLICK:
                    # 起手时间提前，后面 _flick 会自己补上整段滑动
                    moment = timestamp + self.config.flick_start
                    frames[moment].add(
                        PlainNote(NoteType.FLICK, moment, screen.remap(placement.position, placement.rotation), placement.rotation)
                    )
                else:
                    frames[timestamp].add(
                        PlainNote(note.kind, timestamp, screen.remap(placement.position, placement.rotation), placement.rotation)
                    )

        allocator = PointerAllocator(screen, self.config)
        for frame in progress.track(frames, "规划触控事件"):
            allocator.allocate(frame)

        return PlanResult(
            planner=self.name,
            screen=screen,
            frames=allocator.done(),
            stats={
                "notes": chart.note_count,
                "frames": len(frames),
                "retimed_notes": retimed,
            },
            warnings=warnings,
        )


PLANNER = RadicalPlanner
