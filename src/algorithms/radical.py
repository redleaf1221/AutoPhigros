"""激进算法（吸收自 phisap 的"激进算法"）。

把 hold 拆成"开头一次 tap + 之后每毫秒一个 drag"，全曲只剩 tap / drag / flick 三种瞬时
动作；然后在 1 毫秒的时间栅格上贪心分配手指 —— 优先复用刚闲下来、一条 MOVE 就能就位的
手指，没有才按下新的。
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, NamedTuple

from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from .blocks import (
    INSET_CAP,
    INSET_RATIO,
    NoiseField,
    exposure,
)
from .chart import Chart, NoteType
from .geometry import Screen, place_note
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


# ------------------------------------------------- 噪域：本规划器自己的策略
#
# 游戏机制（噪域是什么、哪一点会被吃、会被吃到什么时候）只有一份，在 `blocks.NoiseField` 里。
# **这一段是"遇到噪域怎么办"** —— 禁区算多保守、手指往哪儿挪、将死的要不要提前松、收尾怎么补按
# —— 它跟本规划器的时间栅格、手指大概要待多久、以及"这一下按下会顺手判掉谁"绑在一起，所以归
# 本规划器自己，不跟别的规划器共用：换规划器就是换这一整段。
#
# 四个规划器手上这一份**各自独立**（都是当初从 blocks.py 搬过来的），谁想换算法就改自己这份；
# `tests/noise.py` 会挨个跑四个规划器，谁跟机制对不上就会红。


class NoiseGuard:
    """本规划器自己的"噪域门房"：**禁区、落点、接下来会不会死**这三件事归它管。

    它拿到的是一张谱面的 :class:`~algorithms.blocks.NoiseField`（机制那一份），再加上本规划器
    的时间栅格与"手指大概要待多久"；``forbidden`` / ``clear`` 原来是 ``NoiseField`` 上的方法，
    现在已经归到这里 —— 因为"禁区算多保守、往哪挪"是**规划**，不是游戏的规则。
    """

    def __init__(
        self,
        field: NoiseField | None,
        screen: Screen,
        *,
        margin: float = 0.0,
        tick_ms: float = 8.0,
        dwell_ticks: int = 0,
        lead_ticks: int = 0,
        enabled: bool = True,
    ) -> None:
        self.field = field if (enabled and field is not None and field.blocks) else None
        self.screen = screen
        self.margin = margin
        self.tick_ms = tick_ms
        self.span_ticks = max(1, dwell_ticks + lead_ticks + 1)
        self._cache: dict[int, Any] = {}
        # 禁区/落点要用的几何（原来长在 NoiseField 上）
        self.blocks = tuple(field.blocks) if field is not None else ()
        self.inset_ratio = field.inset_ratio if field is not None else INSET_RATIO
        self.inset_cap = field.inset_cap if field is not None else INSET_CAP

    @property
    def _screen(self) -> Screen:
        """禁区算的是"虚拟屏上的哪一片"，就是构造时给的那块屏。"""
        return self.screen

    def forbidden(self, lo: float, hi: float, *, margin: float = 0.0, step: float = 1 / 60) -> Any:
        """``[lo, hi)`` 里"任何时刻踩进去都会被吃掉"的区域（虚拟屏上的多边形）。

        用的是 :meth:`blocks.NoiseField.blocking_area` —— **游戏自己的那两道奇偶门**（原矩形走
        第一道、inset 矩形走第二道），不是"所有块原矩形的并集"那种保守近似。这一条是要紧的：
        Ametrine 的 subtract 块（对角可玩空间之外的"洞"）会盖住**全屏**，按原矩形并集算出来的
        禁区就是全屏，于是每个 drag 都被判成"整块在噪域里" —— 那是**误报**，而且是会害人的误报
        （照它办就会把触点放进真会被吃的地方）。

        这一层只做两件**策略**决定：

        * **窗口**：手指在这一段里得一直站着，所以把 ``[lo, hi)`` 按 ``step`` 采样、把每一刻的
          禁区**并起来** —— 并集只会多禁、不会少禁；
        * **余量**：把并集整体往外 ``buffer(margin)``（洞会被缩小，同样是"多禁"那一侧）。

        没有生效的块就返回 ``None``。
        """
        if self.field is None:
            return None
        moments: set[float] = set()
        for block in self.field.blocks:
            if block.disable <= lo or block.enable >= hi:
                continue
            begin, end = max(lo, block.enable), min(hi, block.disable)
            moments.add(begin)
            moments.add(end)
            index = 0
            while begin + index * step <= end:
                moments.add(begin + index * step)
                index += 1
        shapes: list[Polygon] = []
        for seconds in moments:
            area = self.field.blocking_area(seconds)
            if area is None or area.is_empty:
                continue
            shapes.append(area if margin == 0.0 else area.buffer(margin))
        return unary_union(shapes) if shapes else None

    def clear(
        self,
        point: Position,
        rotation: Any,
        screen: Screen,
        *,
        lo: float,
        hi: float,
        margin: float = 0.0,
        reach: float = 4.5,
        samples: int = 48,
        area: Any = None,
        extend: float = 0.0,
        along: Any = None,
    ) -> Position | None:
        """把一次按下从噪域里挪出来：**沿判定线的法向**平移。

        垂直判定下唯一进判定的是**横向**偏差（``judging.spine_offset``），沿法向平移一点都
        不改变它 —— 这是"白拿"的自由度：既不损失判定，也不用牺牲落点（只需还在屏上）。
        返回挪过去的位置；整条法向都被禁（或都出屏）就返回 ``None``，那说明这一下本来就没救，
        交给裁判去报 Miss。

        ``reach`` 是往两侧各探多远（虚拟屏单位，默认半个屏高）。
        ``extend`` / ``along``：按下之后手指还要沿 ``along`` 走 ``extend`` 那么远（flick 的
        划动半径）—— 那一段也得是干净的，否则落点刚躲开、一划又进去了。
        """
        if not self.blocks:
            return point
        if area is None:
            area = self.forbidden(lo, hi, margin=margin)
        # 用 covers 而不是 contains：游戏的 `|local| <= half` 是**闭**区间，落在边界上也算被吃
        if area is None or not area.covers(Point(point.real, point.imag)):
            return point
        normal = rotation * 1j
        if normal == 0:
            return None
        for index in range(1, samples + 1):
            offset = reach * index / samples
            for sign in (1.0, -1.0):
                candidate = point + normal * (sign * offset)
                if not screen.visible(candidate):
                    continue
                if area.covers(Point(candidate.real, candidate.imag)):
                    continue
                if extend > 0.0 and not _ray_clear(
                    area, screen, candidate, along if along is not None else normal * sign, extend
                ):
                    continue
                return candidate
        return None

    @property
    def active(self) -> bool:
        return self.field is not None

    def _seconds(self, tick: int) -> float:
        return tick * self.tick_ms / 1000.0

    def area(self, tick: int) -> Any:
        """这个 tick 起、这根手指可能待着的那一段里的禁区（同一 tick 只算一次）。"""
        if self.field is None:
            return None
        if tick in self._cache:
            return self._cache[tick]
        value = self.forbidden(
            self._seconds(tick), self._seconds(tick + self.span_ticks), margin=self.margin
        )
        self._cache[tick] = value
        return value

    def place(
        self,
        point: Position,
        rotation: Any,
        tick: int,
        *,
        span_ticks: int | None = None,
        extend: float = 0.0,
        along: Any = None,
    ) -> Position | None:
        """按下点落在禁区里就沿判定线的法向挪出去；挪不出去返回 ``None``。

        ``extend`` / ``along`` 给 flick 用：划动那一段也得干净（见 :meth:`NoiseField.clear`）。
        """
        if self.field is None:
            return point
        span = self.span_ticks if span_ticks is None else max(1, span_ticks)
        return self.clear(
            point,
            rotation,
            self.screen,
            lo=self._seconds(tick),
            hi=self._seconds(tick + span),
            margin=self.margin,
            area=self.area(tick),
            extend=extend,
            along=along,
        )

    def doomed(self, position: Position, tick: int, until_tick: int) -> bool:
        """这根手指停在 ``position`` 不动、一直占到这个 tick，会不会被噪域吃掉。"""
        if self.field is None or until_tick <= tick:
            return False
        return (
            self.field.hits(
                position,
                self._seconds(tick),
                self._seconds(until_tick) + self.tick_ms / 1000.0,
                step=self.tick_ms / 1000.0,
            )
            is not None
        )


def finish_noise(result: Any, field: NoiseField, warnings: list[str]) -> tuple[Any, int]:
    """规划器收尾：**修一遍**被噪域吃掉的手指，再把剩下的暴露写成警告。

    四个规划器都调这一个 —— "规划时避开噪域"的兜底逻辑只有一份。
    """
    if not field or not field.blocks:
        return result, 0
    result, repaired = repair_noise(result, field)
    if repaired:
        warnings.append(f"噪域：提前松开 / 重新按下救回了 {repaired} 段手指")
    for pointer, intervals in sorted(exposure(result, field).items()):
        for begin, _end in intervals:
            if len(warnings) >= WARNING_LIMIT:
                break
            warnings.append(
                f"手指 {pointer} 在 {begin / 1000.0:.3f}s 划进了噪域"
                f"（规划没能绕开，这根手指到抬起前都判不到音符）"
            )
    result.warnings = warnings
    return result, repaired


def repair_noise(
    result: Any, field: NoiseField, *, rounds: int = 4, step_ms: float = 8.0
) -> tuple[Any, int]:
    """把"会被噪域吃掉的手指"改成"提前松开、出来之后再按下去"，返回（新规划，救回几段）。

    为什么要这一遍：规划器把一根手指留在屏幕上（按住、或者"歇"在某个位置等着被征用），
    噪域可能在**它按住期间**扫过来 —— 游戏当帧就把它从 `fingers` 里摘掉，而且直到抬起都
    不会还给它。规划阶段没法预知每一处，但事后可以补救：**在死之前一条 UP、出来之后按
    一条 DOWN**，`blockedFingerIds` 是"抬起时清"的，于是这根手指又活了。

    安全性：手指在 ``[死, 抬起)`` 本来什么都判不到，所以"提前松开"不会丢掉任何**本来能判到**
    的东西（它只可能多出一条按下 —— 蹭不蹭键由裁判去看）。只在**重新按下去的那一点不会被
    吃**时才动手（否则白折腾）；改完再验一遍，最多 ``rounds`` 轮。
    """
    if not field or not field.blocks or not result.frames:
        return result, 0

    events: list[list[Any]] = [
        [timestamp, event.pointer, event.action, event.x, event.y]
        for timestamp, batch in result.frames
        for event in batch
    ]
    by_pointer: dict[int, list[list[Any]]] = {}
    for event in events:
        by_pointer.setdefault(event[1], []).append(event)
    for items in by_pointer.values():
        items.sort(key=lambda item: item[0])

    repaired = 0
    for _ in range(rounds):
        changed = False
        for items in by_pointer.values():
            pressed_at: int | None = None
            """这根手指最近一次按下的时刻（毫秒）；没有就是 None。"""
            for index in range(len(items) - 1):
                moment, _, action, x, y = items[index]
                if action is Touch.DOWN:
                    pressed_at = moment
                if action is Touch.UP:
                    pressed_at = None
                    continue
                begin = moment / 1000.0
                end = items[index + 1][0] / 1000.0
                hit = field.hits(complex(x, y), begin, end, step=step_ms / 1000.0)
                if hit is None:
                    continue
                # 死在 [hit, 下一个事件)：只要"重新按下去"那一点是干净的，就松开再按
                follow = items[index + 1]
                if follow[2] is Touch.UP:
                    continue
                # 提前松开必须**在按下之后**：这一下本身按在禁区里（或者按下与死亡同刻）时，
                # 插不进一条合法的 UP —— 硬插会造出"没按下就抬起"，跳过（这一下本来也没救）
                release_at = int(hit * 1000.0) - 1
                if pressed_at is None or release_at <= pressed_at:
                    continue
                if field.blocking(complex(follow[3], follow[4]), follow[0] / 1000.0) is not None:
                    continue
                events.append([release_at, items[index][1], Touch.UP, x, y])
                follow[2] = Touch.DOWN
                repaired += 1
                changed = True
                break
        events.sort(key=lambda item: item[0])
        by_pointer = {}
        for event in events:
            by_pointer.setdefault(event[1], []).append(event)
        for items in by_pointer.values():
            items.sort(key=lambda item: item[0])
        if not changed:
            break

    if not repaired:
        return result, 0

    grouped: dict[int, list[TouchEvent]] = {}
    for timestamp, pointer, action, x, y in events:
        grouped.setdefault(int(timestamp), []).append(TouchEvent(pointer, action, x, y))
    frames = [(timestamp, tuple(grouped[timestamp])) for timestamp in sorted(grouped)]
    return (
        type(result)(
            planner=result.planner,
            screen=result.screen,
            frames=frames,
            stats=dict(result.stats),
            warnings=list(result.warnings),
        ),
        repaired,
    )


def _ray_clear(area: Any, screen: Screen, start: Position, along: Any, length: float) -> bool:
    """从 ``start`` 沿 ``along`` 走 ``length`` 这一条线段是不是整段都在禁区外、且都在屏上。

    沿线采样（禁区是若干矩形的并集，可能是凹的，只看终点不够）。
    """
    steps = max(2, int(length / 0.25) + 1)
    for index in range(1, steps + 1):
        probe = start + along * (length * index / steps)
        if not screen.visible(probe):
            return False
        if area.covers(Point(probe.real, probe.imag)):
            return False
    return True


@dataclass(slots=True)
class RadicalConfig:
    """激进算法的参数（控制台 ``option`` 或 config.json 的 ``planner_options`` 都能改）。"""

    flick_start: int = field(
        default=-17, metadata={"help": "滑键手势相对判定时刻的起点偏移（毫秒）"}
    )
    flick_end: int = field(default=17, metadata={"help": "滑键手势的终点偏移（毫秒）"})
    flick_repeats: int = field(default=2, metadata={"help": "一次滑键手势重复划几下"})
    flick_sample_ms: int = field(
        default=2, metadata={"help": "滑键手势里隔多少毫秒补一个触点"}
    )
    flick_direction: int = field(
        default=0,
        metadata={
            "help": "0 = 垂直判定线滑动（横向不变，判定线动了也不会甩出容差），"
            "1 = 平行判定线滑动（甩出去那一下的横向偏差就是半径）"
        },
    )
    recycle_scope_ratio: float = field(
        default=0.05, metadata={"help": "可以顺手征用的手指与目标的横向距离阈值（占对角线比例）"}
    )
    max_pointers: int = field(default=10, metadata={"help": "最多几根手指"})
    ignore_allocation_errors: bool = field(
        default=False, metadata={"help": "手指不够时跳过这一笔，而不是报错"}
    )
    avoid_noise: bool = field(
        default=True,
        metadata={"help": "按下时避开噪域（4.0.1 的 blockAreaList）：落进噪域的手指会被废掉"},
    )
    noise_margin: float = field(
        default=0.3,
        metadata={
            "help": "避噪域时把区域往外扩这么多（虚拟屏单位）：inset 的真值已经有了（0.05 / 0.25，见 "
            "blocks.INSET_*），这一圈余量现在只是保险（采样步长 + 我的几何与游戏之间的误差）"
        },
    )


CONFIG = RadicalConfig


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
        "guard",
        "warnings",
        "ignore_allocation_errors",
        "pointers",
        "track",
        "now",
        "last_timestamp",
    )

    def __init__(
        self,
        screen: Screen,
        config: RadicalConfig,
        noise: NoiseField | None = None,
        warnings: list[str] | None = None,
    ) -> None:
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
        # 这个算法的时间栅格是 1ms
        self.guard = NoiseGuard(
            noise,
            screen,
            margin=config.noise_margin,
            tick_ms=1.0,
            dwell_ticks=MIN_DWELL_MS,
            enabled=config.avoid_noise,
        )
        self.warnings = warnings if warnings is not None else []

    def _place(self, note: PlainNote, *, swipe: bool = False) -> Position:
        """按下点落在噪域里就沿判定线的法向挪出去（横向偏差不变，判定不受影响）。

        ``swipe`` 给 flick 用：划动那一段（``flick_radius``）也得干净，否则落点刚躲开、
        一划又进去了。
        """
        place = self.guard.place(
            note.position,
            note.rotation,
            self.now,
            extend=self.screen.flick_radius if swipe else 0.0,
            along=note.rotation * self.rotate_factor,
        )
        if place is None:
            if len(self.warnings) < 20:
                self.warnings.append(
                    f"{self.now / 1000.0:.3f}s 的 {note.kind.name} 落在这张谱面保守判定的噪域"
                    f"禁区里、沿判定线法向也挪不出去（是否真丢由裁判说了算）"
                )
            return note.position
        return place

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
            # 空闲的，或者**已经摆够一帧**的（见 MIN_DWELL_MS）：刚摆下去就征走，
            # 等于把它在原来那个位置的覆盖砍成不到一帧
            if pointer.note is None or pointer.age >= MIN_DWELL_MS
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
        # 按下至少停一帧：这根手指接着可能被 drag 征用，也可能被当成"覆盖了某个 drag"来用
        pointer.age = -MIN_DWELL_MS
        self._insert(self.now, self._place(note), Touch.DOWN, pointer.id)

    def _flick(self, pointer: Pointer, note: PlainNote) -> None:
        action = Touch.DOWN if pointer.note is None else Touch.MOVE
        place = self._place(note, swipe=True)
        self._insert(self.now, place, action, pointer.id)

        swipe = place
        for repeat in range(self.flick_repeats):
            # 每一下都从 +半径 跳到 一侧再划到 -半径（见 RadicalConfig.flick_repeats）
            base = self.now + repeat * self.flick_duration
            for delta in range(0, self.flick_duration, self.flick_sample_ms):
                rate = 1 - 2 * delta / self.flick_duration
                swipe = (
                    place
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
        place = self._place(note)
        self._insert(self.now, place, action, pointer.id)
        # 同一毫秒补发一条 MOVE：部分设备会丢掉紧随 DOWN 之后的第一条移动
        self._insert(self.now, place, Touch.MOVE, pointer.id)
        pointer.note = note._replace(position=place)
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

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        self.config = build_options(RadicalConfig, options)

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

        allocator = PointerAllocator(screen, self.config, chart.noise_field(), warnings)
        for frame in progress.track(frames, "规划触控事件"):
            allocator.allocate(frame)

        result = PlanResult(
            planner=self.name,
            screen=screen,
            frames=allocator.done(),
            stats={
                "notes": chart.note_count,
                "frames": len(frames),
                "retimed_notes": retimed,
                "noise_blocks": len(chart.blocks),
            },
            warnings=warnings,
        )
        result, _repaired = finish_noise(result, chart.noise_field(), warnings)
        return result


PLANNER = RadicalPlanner
