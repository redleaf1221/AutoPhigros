"""几何算法（纯版）：**在交集内部挑落点，能吸附到判定线就吸附上去**。

与 `geometric` 的差别只有一处：`geometric` 是"drag 并进 tap 那一组，由 tap 自己的按下顺手判掉"
（合并判据 + 面积门槛），落点在交集之外（tap 的判定点或区域内部点）；纯版不并，**所有 drag 只跟
drag 求交**，然后在那块交集**内部**调整落点：

* 能落在某个 TAP 的**判定线**上（它判定点 ± 1.71 那一段与交集相交）就落上去 —— 那一点的法向偏差
  是 0，这一发按下去就可能顺手把这个 TAP 判掉；落在它判定点上时**一定**判掉它，于是它那一次按下
  可以省掉（**省手指**才是几何算法的目的）；
* 吸附不到就取交集内部一点（`point_on_surface`，重心可能落在凹形区域外面）；
* 挑之前先把"会蹭键的部分"从交集里扣掉（别人的容差带），扣没了才退回原交集。

Hold 不参与吸附：它的**身体**要手指一直待着，被一次 drag 的按下"顺手判掉"反而会让身体落空。
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Mapping, NamedTuple

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import nearest_points

from .chart import Chart, JudgeLine, NoteType
from .geometry import JudgeAreaBuilder, Screen, clear_of, interior_of, outside, place_note
from .judging import (
    DRAG_TOLERANCE,
    HEAD_WINDOW,
    PERFECT_TIME,
    SCAN_AHEAD,
    TAP_TOLERANCE,
    spine_offset,
)
from .track import EventTrack
from .utils import (
    MIN_DWELL_MS,
    WARNING_LIMIT,
    PlanResult,
    PlanningError,
    Position,
    Progress,
    Touch,
    TouchEvent,
    Vector,
    build_options,
)


FRAME_MS = 8
"""时间栅格：1 tick = 8ms（125Hz）。算法内部一律以 tick 计时，所以它是结构不是旋钮。"""

DRAG_DWELL_TICKS = math.ceil(MIN_DWELL_MS / FRAME_MS)
"""drag / flick 占住一根手指的 tick 数：把 :data:`~algorithms.utils.MIN_DWELL_MS` 折算成 tick。"""


@dataclass(slots=True)
class GeometricPureConfig:
    """几何算法（纯版）的参数（控制台 ``option`` 或 config.json 的 ``planner_options`` 都能改）。"""

    flick_ticks: int = field(default=3, metadata={"help": "滑键手势的持续 tick 数"})
    flick_repeats: int = field(default=2, metadata={"help": "一次滑键手势重复划几下"})
    flick_direction: int = field(
        default=0, metadata={"help": "0 = 垂直判定线滑动，1 = 平行判定线滑动"}
    )
    max_pointers: int = field(default=10, metadata={"help": "最多几根手指"})
    press_margin: float = field(
        default=0.5,
        metadata={
            "help": "落点离每个 drag 的脊线至少留这么多（虚拟屏单位）：判定线在窗口里会动，"
            "贴到容差边上（1.89）一晃就出界"
        },
    )
    down_lead_ticks: int = field(
        default=6,
        metadata={
            "help": "要新按手指、而目标点会顺手判掉别的音符时：先按到补集里的落脚点，"
            "隔多少 tick 再 MOVE 到目标（送达延迟的抖动有 1~3 帧，得留够）"
        },
    )


CONFIG = GeometricPureConfig


class PlainNote(NamedTuple):
    kind: NoteType
    timestamp: int
    position: Position
    rotation: Vector
    judge: Polygon
    key: tuple[int, int, float]
    """(毫秒, 判定线序号, 横向偏移)：`CheckNote` 候选的身份，用来把"目标自己"从 veto 里排除。"""


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
    __slots__ = (
        "screen",
        "flick_ticks",
        "flick_repeats",
        "rotate_factor",
        "down_lead",
        "press_margin",
        "idle",
        "on_screen",
        "track",
        "now",
        "builder",
        "danger",
        "danger_seconds",
        "_strips",
    )

    def __init__(
        self,
        screen: Screen,
        config: GeometricPureConfig,
        danger: list[tuple[float, JudgeLine, float]] | None = None,
    ) -> None:
        self.screen = screen
        self.flick_ticks = config.flick_ticks
        self.flick_repeats = max(1, config.flick_repeats)
        # 方向与另外两个算法相反：从 note 中心朝一侧划出去
        self.rotate_factor = -1j if config.flick_direction == 0 else 1
        self.down_lead = max(0, config.down_lead_ticks)
        self.press_margin = max(0.0, config.press_margin)
        self.idle: set[int] = set(range(1000, 1000 + config.max_pointers))
        self.on_screen: list[Pointer] = []
        self.track = EventTrack()
        self.now = 0
        self.builder = JudgeAreaBuilder(screen)
        self.danger = danger or []
        """`(时刻, 判定线, 横向偏移, 身份)`：TAP 与 Hold 头的判定点，算"这一下按下会判到谁"。"""
        self.danger_seconds = [item[0] for item in self.danger]
        self._strips: dict[int, list[tuple[tuple[int, int, float], Polygon]]] = {}

    # ------------------------------------------------------------------ 分配

    def allocate(self, timestamp: int, frame: Frame) -> None:
        self.now = timestamp
        seconds = timestamp * FRAME_MS / 1000.0

        # 已经在屏幕上的手指落进 drag 判定区，就说明这个 drag 已经被顺手判掉了
        occupied = [pointer.position for pointer in self.on_screen if pointer.expire > timestamp]
        occupied.extend(note.position for note in frame.flicks)

        # drag **只跟 drag** 并：并出来的那块交集 = "一次按下够得着的一整片"
        drag_areas: list[Polygon] = []
        for note in frame.drags:
            if any(note.judge.contains(Point(point.real, point.imag)) for point in occupied):
                continue
            for index, area in enumerate(drag_areas):
                if area.intersects(note.judge):
                    drag_areas[index] = area.intersection(note.judge)
                    break
            else:
                drag_areas.append(note.judge)

        # 这一帧**值得顺手判掉**的 TAP：时机得落在"瞄准它"的那个窗里（Perfect 且报告认作冲它去的）
        targets = [
            note
            for note in frame.taps
            if note.kind is NoteType.TAP
            and HEAD_WINDOW[0] <= seconds - note.timestamp * FRAME_MS / 1000.0 <= PERFECT_TIME
        ]

        covered: set[int] = set()
        for area in drag_areas:
            position, hit = self._press_point(area, targets)
            chosen = self._take_resting_or_idle(position)
            safe = self._safe_down(position, isinstance(chosen, int))
            lead = self.down_lead if safe is not None else 0
            bound = self._bind(chosen, position, DRAG_DWELL_TICKS + lead)
            if safe is None:
                self._insert(
                    self.now, position, Touch.DOWN if isinstance(chosen, int) else Touch.MOVE, bound.id
                )
            else:
                # 按下落在补集里的落脚点，隔 lead 个 tick 再用一条 MOVE 挪到真正要按的地方；
                # MOVE 不触发 `CheckNote`，所以那一帧不会判掉谁
                self._insert(self.now, safe, Touch.DOWN, bound.id)
                self._insert(self.now + lead, position, Touch.MOVE, bound.id)
            # 顺手判掉了一个 tap：只有"新按下 + 落点是吸附点"才算数（MOVE 不触发 `CheckNote`）
            if hit is not None and safe is None and isinstance(chosen, int):
                covered.add(id(hit))

        for note in frame.taps:
            if id(note) in covered:
                continue  # 这一发已经由某个 drag 组的按下顺手判掉了 —— 省下一根手指
            # 按在音符自己的判定点上（横向偏差 0，是度量的下界），不要按合并区域的中心
            position = note.position
            pointer = self._take_idle_or_resting(position)
            if isinstance(pointer, Pointer):
                self._insert(pointer.expire, pointer.position, Touch.UP, pointer.id)
            # 按下至少停一帧：这根手指还可能被当成"覆盖了某个 drag"来用
            bound = self._bind(pointer, position, DRAG_DWELL_TICKS)
            self._insert(timestamp, position, Touch.DOWN, bound.id)

        for note in frame.flicks:
            chosen = self._take_resting_or_idle(note.position)
            safe = self._safe_down(note.position, isinstance(chosen, int))
            lead = self.down_lead if safe is not None else 0
            # 整段（repeats 下）都占着这根手指，中途不松手
            bound = self._bind(
                chosen,
                note.position,
                (self.flick_ticks + 1) * self.flick_repeats + lead,
            )
            if safe is not None:
                self._insert(self.now, safe, Touch.DOWN, bound.id)

            swipe = note.position
            for repeat in range(self.flick_repeats):
                base = self.now + lead + repeat * (self.flick_ticks + 1)
                # 每一下都先蹦回判定点再划出去，那一下跳变就是游戏要找的"新起手"
                action = Touch.DOWN if isinstance(chosen, int) and repeat == 0 and not lead else Touch.MOVE
                self._insert(base, note.position, action, bound.id)
                for delta in range(1, self.flick_ticks + 1):
                    rate = delta / self.flick_ticks
                    swipe = (
                        note.position
                        + note.rotation * self.rotate_factor * rate * self.screen.flick_radius
                    )
                    self._insert(base + delta, swipe, Touch.MOVE, bound.id)
            bound.position = swipe

        # 歇够了的手指抬起来，收回备用池
        for pointer in [item for item in self.on_screen if item.expire < timestamp]:
            self._insert(pointer.expire, pointer.position, Touch.UP, pointer.id)
            self.on_screen.remove(pointer)
            self.idle.add(pointer.id)

    def _press_point(
        self, area: Polygon, targets: list[PlainNote]
    ) -> tuple[Position, PlainNote | None]:
        """在交集内部挑落点：**能吸附到某条判定线上就吸附**，吸附不到才取区域内部一点。

        吸附不到才退回"交集扣掉所有蹭键带"的内部点。
        """
        best: tuple[float, Position, PlainNote] | None = None
        for note in targets:
            snapped = self._snap(note, area)
            if snapped is None:
                continue
            if self._strays_into(snapped, note):
                continue
            gap = abs(spine_offset(snapped, note.position, note.rotation))
            if gap > TAP_TOLERANCE - self.press_margin:
                continue  # 贴到容差边上：判定线一晃就判不到了，宁可不吸附
            if best is None or gap < best[0]:
                best = (gap, snapped, note)
        if best is None:
            strips = [strip for _, strip in self._danger_pairs()]
            return interior_of(clear_of(area, strips, self.screen)), None
        return best[1], best[2]

    def _snap(self, note: PlainNote, region: Polygon) -> Position | None:
        """把落点吸附到 `note` 的判定线上：取它"判定点 ± 1.71"那一段与区域相交的部分里、
        离判定点最近的点（越近越"瞄准"，落在判定点上时横向偏差 0）。"""
        along = note.rotation
        anchor = note.position
        segment = LineString(
            [
                (anchor.real - along.real * TAP_TOLERANCE, anchor.imag - along.imag * TAP_TOLERANCE),
                (anchor.real + along.real * TAP_TOLERANCE, anchor.imag + along.imag * TAP_TOLERANCE),
            ]
        )
        clipped = segment.intersection(region)
        if clipped.is_empty:
            return None
        here, _ = nearest_points(clipped, Point(anchor.real, anchor.imag))
        if not note.judge.contains(here):
            return None  # 落点得真在它判定区里（判定区是按它自己的容差切的）
        return Position(here.x, here.y)

    def _strays_into(self, point: Position, target: PlainNote) -> bool:
        """这一点是不是落在**别人**（还没按过的 TAP / Hold 头）的容差带里。

        "别人"按身份的 key 区分 —— 只比时刻的话，同一时刻的邻居会被当成"自己"放过。
        """
        for key, strip in self._danger_pairs():
            if key == target.key:
                continue
            if strip.contains(Point(point.real, point.imag)):
                return True
        return False

    def _danger_pairs(self) -> list[tuple[tuple[int, int, float], Polygon]]:
        """这一刻按下会判到谁：窗口里那些**还没按过**的 TAP / Hold 头的容差带。

        带子按**按下那一刻**的判定线算（`CheckNote` 读的是当帧的 `fingerPositionX`），
        半宽 `TAP_TOLERANCE`（1.9 谱面单位折成虚拟屏 1.71）。同 tick 的 TAP 已经由它自己
        那一次按下判掉了（tap 循环排在 drag 前面），所以只看还在未来的。
        """
        cached = self._strips.get(self.now)
        if cached is None:
            seconds = self.now * FRAME_MS / 1000.0
            lo = bisect_left(self.danger_seconds, seconds + FRAME_MS / 2000.0)
            hi = bisect_right(self.danger_seconds, seconds + SCAN_AHEAD)
            cached = []
            for _note_seconds, line, offset, key in self.danger[lo:hi]:
                strip = self.builder.band(
                    line.point_at(seconds, offset),
                    line.rotation_at(seconds),
                    half_width=TAP_TOLERANCE,
                )
                if strip is not None:
                    cached.append((key, strip))
            self._strips[self.now] = cached
        return cached

    def _danger_strips(self) -> list[Polygon]:
        """补集落脚点用的那一份（只要带子，不要时刻）。"""
        return [strip for _, strip in self._danger_pairs()]

    def _safe_down(self, position: Position, fresh: bool) -> Position | None:
        """要**新按**一根手指时：按在 `position` 会顺手判掉别的音符的话，返回补集里的落脚点。

        老手指用 MOVE 就位，不触发 `CheckNote`，所以只有新按下才有这个问题。
        """
        if not fresh or self.down_lead <= 0:
            return None
        strips = self._danger_strips()
        if not any(strip.contains(Point(position.real, position.imag)) for strip in strips):
            return None
        return outside(strips, position, self.screen)

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
    builder: JudgeAreaBuilder,
    position: Position,
    rotation: Vector,
    label: str,
    half_width: float,
) -> Polygon:
    """判定区必须按"实际要按的点"来切 —— 判定点被拉回屏幕后，判定区也得跟着回来。"""
    area = builder.band(position, rotation, half_width=half_width)
    if area is None:
        raise PlanningError(f"{label} 算不出判定区：判定点 {position} 不在任何一条判定线上")
    return area


def _on_screen(screen: Screen, line: JudgeLine, moment: float, offset: float) -> tuple[Position, Vector]:
    position = line.point_at(moment, offset)
    rotation = line.rotation_at(moment)
    if not screen.visible(position):
        position = screen.remap(position, rotation)
    return position, rotation


class GeometricPurePlanner:
    name = "geometric_pure"

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        self.config = build_options(GeometricPureConfig, options)

    def plan(self, chart: Chart, progress: Progress) -> PlanResult:
        screen = chart.screen
        builder = JudgeAreaBuilder(screen)
        frames: defaultdict[int, Frame] = defaultdict(Frame)
        danger: list[tuple[float, JudgeLine, float]] = []
        """TAP 与 Hold 头的判定点：`CheckNote` 的候选只可能是这些（drag / flick 不进 tap 判定）。"""
        warnings: list[str] = []
        retimed = 0

        for line_index, line in enumerate(progress.track(chart.lines, "统计判定区")):
            for note in line.notes:
                timestamp = round(note.seconds * 1000)
                tick = timestamp >> 3
                key = (timestamp, line_index, round(note.offset, 6))
                placement = place_note(line, note, screen, retime=note.kind is NoteType.FLICK)
                if note.kind in (NoteType.TAP, NoteType.HOLD):
                    # Hold 只有**头**进候选：身体是逐帧位置判的，不参与 CheckNote 的挑选
                    danger.append((note.seconds, line, note.offset, key))
                if placement.retimed:
                    retimed += 1
                    if len(warnings) < WARNING_LIMIT:
                        warnings.append(
                            f"{note.kind.name} @ {note.seconds:.3f}s 判定点在屏幕外，"
                            f"已微调至 {placement.seconds:.3f}s"
                        )

                # 判定区按**游戏真正的容差**切（Tap / Hold 1.9、Drag / Flick 2.1，谱面单位）——
                # 纯版的"交集"就是"一次按下够得着的一整片"，用窄带（phisap 的经验值 1/16）
                # 会把能一起判掉的音符挡在外面
                reach = (
                    TAP_TOLERANCE
                    if note.kind in (NoteType.TAP, NoteType.HOLD)
                    else DRAG_TOLERANCE - self.config.press_margin
                )
                entry = PlainNote(
                    note.kind,
                    tick,
                    placement.position,
                    placement.rotation,
                    _band(
                        builder,
                        placement.position,
                        placement.rotation,
                        f"{note.kind.name} @ {note.seconds:.3f}s",
                        reach,
                    ),
                    key,
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
                                NoteType.DRAG,
                                extra,
                                position,
                                rotation,
                                _band(
                                    builder,
                                    position,
                                    rotation,
                                    f"HOLD @ {moment:.3f}s",
                                    DRAG_TOLERANCE - self.config.press_margin,
                                ),
                                key,
                            )
                        )
                elif note.kind is NoteType.FLICK:
                    frames[tick].flicks.append(entry)
                elif note.kind is NoteType.TAP:
                    frames[tick].taps.append(entry)
                else:
                    frames[tick].drags.append(entry)

        danger.sort(key=lambda item: item[0])
        allocator = PointerAllocator(screen, self.config, danger)
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


PLANNER = GeometricPurePlanner
