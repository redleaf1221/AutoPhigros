"""几何算法（吸收自 phisap 的"极端/几何算法"）。

思路：以 125Hz（8ms）为基准把谱面切成互不影响的帧，一个 tick 就是一帧。给每个 note
算出一条贯穿屏幕的判定区窄带；同一帧里，与 tap 判定区相交的 drag 就并进去，drag 之间
相交的也并起来 —— 一次按下可以同时判掉一整片区域里的音符。这是三个算法里最省手指的，
代价是判定区宽度只是 phisap 的经验值，精度不如保守算法。

平面几何全部交给 shapely：窄带是 ``LineString.buffer`` 与屏幕矩形求交，合并是
``Polygon.intersection``，落点取 ``shapely.centroid``。
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from typing import NamedTuple

from shapely.geometry import Point, Polygon

from .chart import Chart, JudgeLine, NoteType
from .geometry import JudgeAreaBuilder, Screen, centre_of, place_note
from .track import EventTrack
from .utils import (
    MIN_DWELL_MS,
    PlanResult,
    PlanningError,
    Position,
    Progress,
    Touch,
    TouchEvent,
    Vector,
)

FRAME_MS = 8
"""125Hz 的时间栅格：1 tick = 8 毫秒。算法内部一律以 tick 计时。"""

DRAG_DWELL_TICKS = math.ceil(MIN_DWELL_MS / FRAME_MS)
"""drag / flick 占住一根手指的 tick 数。

本来是 1 tick（8ms）—— 按"判定区已经合并好了，一帧就够"的想法。但游戏是**逐帧**读手指
位置的：手指只在那个位置停 8 毫秒的话，60fps（16.7ms 一帧）下能不能撞上一帧全看运气。
所以按 :data:`~algorithms.utils.MIN_DWELL_MS` 折算成 tick 数，让手指在一个位置待够一帧。
"""

WARNING_LIMIT = 20


@dataclass(slots=True)
class GeometricConfig:
    flick_ticks: int = 3
    """滑键手势的持续 tick 数（3 tick = 24 毫秒）。"""
    flick_repeats: int = 2
    """一次滑键手势重复划几下。**别调回 1。**

    游戏那边一次"新起手"（``Fingers.isNewFlick``）只够点亮一个 flick，而且会被判定窗口
    里更早的音符抢走 —— 只划一下就是一个音符只有一次机会（``conservative`` 那边同样的
    理由与测算写在 ``ConservativeConfig.flick_repeats``）。每多划一下，多一次独立机会。
    """
    flick_direction: int = 0
    """0 = 垂直于判定线滑动，1 = 平行于判定线滑动。"""
    max_pointers: int = 10


class PlainNote(NamedTuple):
    timestamp: int
    position: Position
    rotation: Vector
    judge: Polygon


class Frame:
    __slots__ = ("taps", "drags", "flicks")

    def __init__(self) -> None:
        self.taps: list[PlainNote] = []
        self.drags: list[PlainNote] = []
        self.flicks: list[PlainNote] = []


@dataclass(slots=True)
class Pointer:
    id: int
    position: Position
    expire: int
    """这根手指在这之前都还占着（tick）。到点之后它只是"歇"在屏幕上，可以随手征用。"""


class PointerAllocator:
    __slots__ = ("screen", "flick_ticks", "flick_repeats", "rotate_factor", "idle", "on_screen", "track", "now")

    def __init__(self, screen: Screen, config: GeometricConfig) -> None:
        self.screen = screen
        self.flick_ticks = config.flick_ticks
        self.flick_repeats = max(1, config.flick_repeats)
        # 方向与另外两个算法相反：从 note 中心朝一侧划出去
        self.rotate_factor = -1j if config.flick_direction == 0 else 1
        self.idle: set[int] = set(range(1000, 1000 + config.max_pointers))
        self.on_screen: list[Pointer] = []
        self.track = EventTrack()
        self.now = 0

    # ------------------------------------------------------------------ 分配

    def allocate(self, timestamp: int, frame: Frame) -> None:
        self.now = timestamp

        # 每个 tap 都得独立按一次，不能合并；drag 可以往 tap 或别的 drag 上并
        tap_areas: list[Polygon] = [note.judge for note in frame.taps]
        drag_areas: list[Polygon] = []

        # 已经在屏幕上的手指落进 drag 判定区，就说明这个 drag 已经被顺手判掉了
        occupied = [pointer.position for pointer in self.on_screen if pointer.expire > timestamp]
        occupied.extend(centre_of(note.judge) for note in frame.flicks)

        for note in frame.drags:
            if any(note.judge.contains(Point(point.real, point.imag)) for point in occupied):
                continue
            merged = False
            for pool in (tap_areas, drag_areas):
                for index, area in enumerate(pool):
                    if area.intersects(note.judge):
                        pool[index] = area.intersection(note.judge)
                        merged = True
                        break
                if merged:
                    break
            if not merged:
                drag_areas.append(note.judge)

        for note in frame.taps:
            # **按在音符自己的判定点上**，不要按"合并区域"的中心。
            # 踩过（Credits IN 上 11 个丢音）：drag 并进来之后，按下点被拉到了两个窄带的
            # 中间，于是横向偏差比隔壁那个 TAP 大一点点（0.011），而游戏挑的是**度量最小的
            # 那个候选** —— 这一下就被隔壁抢走、它自己去判了隔壁的音符（Good −139ms），
            # 原来瞄的那个音符什么都没得到。
            # 自己的判定点上横向偏差是 0（度量的下界），别的音符除非位置完全相同比不过它；
            # 合并仍然保留在两件事上：drag 不再单独占一根手指、以及"已经在屏幕上的手指
            # 落进并后的区域也算它被顺手判掉"。
            position = note.position
            pointer = self._take_idle_or_resting(position)
            if isinstance(pointer, Pointer):
                self._insert(pointer.expire, pointer.position, Touch.UP, pointer.id)
            bound = self._bind(pointer, position, 1)
            self._insert(timestamp, position, Touch.DOWN, bound.id)

        for note in frame.flicks:
            chosen = self._take_resting_or_idle(note.position)
            action = Touch.DOWN if isinstance(chosen, int) else Touch.MOVE
            # 整段（repeats 下）都占着这根手指，中途不松手
            bound = self._bind(
                chosen, note.position, (self.flick_ticks + 1) * self.flick_repeats
            )

            swipe = note.position
            for repeat in range(self.flick_repeats):
                base = self.now + repeat * (self.flick_ticks + 1)
                # 每一下都先**蹦回**判定点再划出去（见 GeometricConfig.flick_repeats），
                # 那一下跳变就是游戏要找的"新起手"
                self._insert(
                    base, note.position, action if repeat == 0 else Touch.MOVE, bound.id
                )
                for delta in range(1, self.flick_ticks + 1):
                    rate = delta / self.flick_ticks
                    swipe = (
                        note.position
                        + note.rotation * self.rotate_factor * rate * self.screen.flick_radius
                    )
                    self._insert(base + delta, swipe, Touch.MOVE, bound.id)
            bound.position = swipe

        for area in drag_areas:
            position = centre_of(area)
            chosen = self._take_resting_or_idle(position)
            action = Touch.DOWN if isinstance(chosen, int) else Touch.MOVE
            bound = self._bind(chosen, position, DRAG_DWELL_TICKS)
            self._insert(self.now, position, action, bound.id)

        # 歇够了的手指抬起来，收回备用池
        for pointer in [item for item in self.on_screen if item.expire < timestamp]:
            self._insert(pointer.expire, pointer.position, Touch.UP, pointer.id)
            self.on_screen.remove(pointer)
            self.idle.add(pointer.id)

    def _take_idle_or_resting(self, position: Position) -> Pointer | int:
        """tap 反正要按下去，优先用真正空闲的手指。"""
        if self.idle:
            return self.idle.pop()
        resting = [pointer for pointer in self.on_screen if pointer.expire < self.now]
        if not resting:
            raise PlanningError(f"{self.now} tick 处没有可用手指")
        return min(resting, key=lambda pointer: abs(pointer.position - position))

    def _take_resting_or_idle(self, position: Position) -> Pointer | int:
        """flick / drag 只要手指已经在屏幕上，一条 MOVE 就能就位，优先征用。"""
        resting = [pointer for pointer in self.on_screen if pointer.expire <= self.now]
        if resting:
            return min(resting, key=lambda pointer: abs(pointer.position - position))
        if self.idle:
            return self.idle.pop()
        raise PlanningError(f"{self.now} tick 处没有可用手指")

    def _bind(self, pointer: Pointer | int, position: Position, age: int) -> Pointer:
        if isinstance(pointer, int):
            bound = Pointer(pointer, position, self.now + age)
            self.on_screen.append(bound)
            return bound
        pointer.expire = self.now + age
        pointer.position = position
        return pointer

    def _insert(self, timestamp: int, position: Position, action: Touch, pointer: int) -> None:
        self.track.push(timestamp, position, action, pointer)

    # ------------------------------------------------------------------ 收尾

    def done(self) -> list[tuple[int, tuple[TouchEvent, ...]]]:
        for pointer in self.on_screen:
            self._insert(pointer.expire, pointer.position, Touch.UP, pointer.id)
        # track 里的键是 tick（8ms 一格），出门前换算成毫秒
        return [(tick << 3, items) for tick, items in self.track.frames()]


def _band(
    builder: JudgeAreaBuilder, position: Position, rotation: Vector, label: str
) -> Polygon:
    """判定区必须按"实际要按的点"来切 —— 判定点被拉回屏幕后，判定区也得跟着回来。"""
    area = builder.band(position, rotation)
    if area is None:
        raise PlanningError(f"{label} 算不出判定区：判定点 {position} 不在任何一条判定线上")
    return area


def _on_screen(screen: Screen, line: JudgeLine, moment: float, offset: float) -> tuple[Position, Vector]:
    position = line.point_at(moment, offset)
    rotation = line.rotation_at(moment)
    if not screen.visible(position):
        position = screen.remap(position, rotation)
    return position, rotation


class GeometricPlanner:
    name = "geometric"

    def __init__(self, config: GeometricConfig | None = None) -> None:
        self.config = config or GeometricConfig()

    def plan(self, chart: Chart, progress: Progress) -> PlanResult:
        screen = chart.screen
        builder = JudgeAreaBuilder(screen)
        frames: defaultdict[int, Frame] = defaultdict(Frame)
        warnings: list[str] = []
        retimed = 0

        for line in progress.track(chart.lines, "统计判定区"):
            for note in line.notes:
                timestamp = round(note.seconds * 1000)
                tick = timestamp >> 3
                placement = place_note(line, note, screen, retime=note.kind is NoteType.FLICK)
                if placement.retimed:
                    retimed += 1
                    if len(warnings) < WARNING_LIMIT:
                        warnings.append(
                            f"{note.kind.name} @ {note.seconds:.3f}s 判定点在屏幕外，"
                            f"已微调至 {placement.seconds:.3f}s"
                        )

                entry = PlainNote(
                    tick,
                    placement.position,
                    placement.rotation,
                    _band(builder, placement.position, placement.rotation, f"{note.kind.name} @ {note.seconds:.3f}s"),
                )

                if note.kind is NoteType.HOLD:
                    # hold 的开头当 tap 按，按住期间每 tick 一个 drag
                    frames[tick].taps.append(entry)
                    end = (timestamp + math.ceil(note.hold * 1000)) >> 3
                    for extra in range(tick + 1, end):
                        moment = (extra << 3) / 1000
                        position, rotation = _on_screen(screen, line, moment, note.offset)
                        frames[extra].drags.append(
                            PlainNote(
                                extra,
                                position,
                                rotation,
                                _band(builder, position, rotation, f"HOLD @ {moment:.3f}s"),
                            )
                        )
                elif note.kind is NoteType.FLICK:
                    frames[tick].flicks.append(entry)
                elif note.kind is NoteType.TAP:
                    frames[tick].taps.append(entry)
                else:
                    frames[tick].drags.append(entry)

        allocator = PointerAllocator(screen, self.config)
        for tick in progress.track(sorted(frames), "规划触控事件"):
            allocator.allocate(tick, frames[tick])

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


PLANNER = GeometricPlanner
