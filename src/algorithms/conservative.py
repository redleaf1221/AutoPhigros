"""保守算法（吸收自 phisap 的"保守算法"）。

每个 note 当不可分割的整体，需要几押就分配几根手指；flick / hold 拆成"起始 + 若干中间
采样 + 结束"，同一根手指从头按到尾。绝不合并不同 note 的判定区 —— 最稳，代价是难谱吃手指。
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from enum import IntEnum
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
class ConservativeConfig:
    """保守算法的参数（控制台 ``option`` 或 config.json 的 ``planner_options`` 都能改）。"""

    flick_start: int = field(
        default=-17, metadata={"help": "滑键手势相对判定时刻的起点偏移（毫秒）"}
    )
    flick_end: int = field(default=17, metadata={"help": "滑键手势的终点偏移（毫秒）"})
    sample_delay: int = field(
        default=8, metadata={"help": "手势中间采样间隔（毫秒）；8ms = 125Hz"}
    )
    flick_direction: int = field(
        default=0, metadata={"help": "0 = 垂直判定线滑动，1 = 平行判定线滑动"}
    )
    flick_repeats: int = field(default=2, metadata={"help": "一次滑键手势重复划几下"})
    max_pointers: int = field(default=10, metadata={"help": "最多几根手指"})
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


CONFIG = ConservativeConfig


class SemiKind(IntEnum):
    """把 note 拆开之后的中间形态。"""

    TAP = 0
    DRAG = 1
    FLICK_HEAD = 2
    FLICK = 3
    FLICK_TAIL = 4
    HOLD_HEAD = 5
    HOLD = 6
    HOLD_TAIL = 7


class SemiNote(NamedTuple):
    kind: SemiKind
    position: Position
    note_id: int
    rotation: Vector | None = None
    """判定线朝向；``None`` = 这一笔不参与避噪域（老调用点没填，就按原样落）"""


@dataclass(slots=True)
class _Record:
    pointer: int
    position: Position
    used_at: int
    """这根指针最后一次被摆放的时刻。抬起事件总是排在它**尽可能晚**的位置。"""


class PointerPool:
    """几押就备几根指针。

    指针用完后不立刻抬起，而是先"晾"在屏幕上（``released``），随时可以被下一个
    note 征用 —— 复用一根已经在屏幕上的手指只需一条 MOVE，比先 UP 再 DOWN 更省事，
    也更不容易丢判定。

    有一条**必须守住的规矩**（见 :data:`~algorithms.utils.MIN_DWELL_MS`）：一根手指摆到
    一个位置之后，别在它还没被游戏看见的时候就把它挪走。游戏逐帧读手指位置，摆放下
    一毫秒就走，等于这个位置只在时间轴上占了一个点 —— drag 的 ±0.1s 窗口里能不能撞上
    一帧纯看运气。所以：

    * 抬起排在**最后一刻**（重新按下之前的那一毫秒），而不是"用完的下一毫秒"；
    * 征用别人时优先挑已经摆够一帧的。
    """

    def __init__(self, count: int, base: int = 1000) -> None:
        self.idle: set[int] = set(range(base, base + count))
        self.in_use: dict[int, _Record] = {}
        self.released: dict[int, _Record] = {}
        self.pending_lift: list[_Record] = []
        self.to_release: set[int] = set()
        self.now = 0

    def alloc(self, note: SemiNote, *, fresh: bool = True) -> tuple[int, bool]:
        """给 note 分配一根指针，返回 ``(指针号, 是否需要先按下)``。"""
        existing = self.in_use.get(note.note_id)
        if existing is not None:
            # 同一个 flick / hold 的后续采样，沿用原来那根手指
            self.in_use[note.note_id] = _Record(existing.pointer, note.position, self.now)
            return existing.pointer, False

        if not fresh and self.released:
            record = self._nearest(note.position)
            del self.released[record.pointer]
            self._ensure_reusable(record)
            self.in_use[note.note_id] = _Record(record.pointer, note.position, self.now)
            return record.pointer, False

        if self.idle:
            pointer = self.idle.pop()
            self.in_use[note.note_id] = _Record(pointer, note.position, self.now)
            return pointer, True

        if self.released:
            record = self._nearest(note.position)
            del self.released[record.pointer]
            self._ensure_reusable(record)
            # 征用一根还按在屏幕上的手指：得先把它抬起来
            self.pending_lift.append(record)
            self.in_use[note.note_id] = _Record(record.pointer, note.position, self.now)
            return record.pointer, True

        raise PlanningError("需要同时按下的音符超过了十指上限")

    def _nearest(self, position: Position) -> _Record:
        """挑一根回收来的手指。

        优先挑**已经摆够一帧**的（见 :data:`~algorithms.utils.MIN_DWELL_MS`）：手指摆在哪
        儿就是判定依据，刚摆下去就被征走，等于把它在原来那个位置上的覆盖砍到不足一帧。
        全都还没摆够就只好按距离挑最近的那根 —— 位置总得摆对。
        """
        return min(
            self.released.values(),
            key=lambda record: (
                self.now - record.used_at < MIN_DWELL_MS,
                abs(position - record.position),
            ),
        )

    def _ensure_reusable(self, record: _Record) -> None:
        if record.used_at + 1 >= self.now:
            raise PlanningError("指针回收冲突：抬起与重新按下之间不足 1 毫秒")

    def release(self, note: SemiNote) -> None:
        """标记这根手指用完了，但先别抬 —— 说不定下一个 note 还要接着用。"""
        self.to_release.add(note.note_id)

    def recycle(self) -> Iterator[_Record]:
        """一帧结束：把用完的手指挪进 ``released``，并交出需要补发抬起事件的指针。"""
        for note_id in self.to_release:
            record = self.in_use.pop(note_id, None)
            if record is not None:
                self.released[record.pointer] = record
        self.to_release.clear()
        yield from self.pending_lift
        self.pending_lift.clear()

    def finish(self) -> Iterator[_Record]:
        yield from self.released.values()
        yield from self.in_use.values()

    def lift_at(self) -> int:
        """这一帧要补发的抬起事件排在哪个时刻。

        尽量晚 —— 压在这一帧的前一毫秒，也就是"再不发就赶不上重新按下"的那一刻。
        早抬一毫秒，等于把那根手指上一个位置的覆盖时间砍掉一毫秒：那个位置就只剩
        一个时间点，而游戏是逐帧读位置的。
        """
        return self.now - 1


class ConservativePlanner:
    name = "conservative"

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        self.config = build_options(ConservativeConfig, options)

    def plan(self, chart: Chart, progress: Progress) -> PlanResult:
        config = self.config
        screen = chart.screen
        warnings: list[str] = []
        self.warnings = warnings
        # 这个算法的时间栅格是 1ms（帧表就是毫秒）
        self.guard = NoiseGuard(
            chart.noise_field(),
            screen,
            margin=config.noise_margin,
            tick_ms=1.0,
            dwell_ticks=MIN_DWELL_MS,
            enabled=config.avoid_noise,
        )
        frames, retimed = self._build_frames(chart, screen, progress, warnings)

        if not frames:
            return PlanResult(self.name, screen, [], {"frames": 0, "notes": chart.note_count}, warnings)

        max_concurrent = max(len(frame) for frame in frames.values())
        if max_concurrent > config.max_pointers:
            raise PlanningError(
                f"最多需要同时按下 {max_concurrent} 个音符，超过 {config.max_pointers} 指上限；"
                f"换 radical 或 geometric 规划器再试"
            )

        pool = PointerPool(max_concurrent)
        track = EventTrack()

        for timestamp in progress.track(sorted(frames), "规划触控事件"):
            pool.now = timestamp
            for note in frames[timestamp]:
                self._emit(track, timestamp, note, pool)
            for record in pool.recycle():
                track.push(pool.lift_at(), record.position, Touch.UP, record.pointer)

        for record in pool.finish():
            track.push(record.used_at + 1, record.position, Touch.UP, record.pointer)

        result = PlanResult(
            planner=self.name,
            screen=screen,
            frames=track.frames(),
            stats={
                "notes": chart.note_count,
                "frames": len(frames),
                "max_concurrent": max_concurrent,
                "retimed_notes": retimed,
                "dropped_moves": track.dropped,
                "noise_blocks": len(chart.blocks),
            },
            warnings=warnings,
        )
        result, _repaired = finish_noise(result, chart.noise_field(), warnings)
        return result

    # ------------------------------------------------------------------ 内部

    def _place(self, note: SemiNote, timestamp: int) -> Position:
        """按下点落在噪域里就沿判定线的法向挪出去（横向偏差不变，判定不受影响）。

        ``note.rotation`` 为 ``None`` 的（没填朝向的老调用点）不参与避噪域，按原样落。
        """
        if note.rotation is None:
            return note.position
        place = self.guard.place(note.position, note.rotation, timestamp)
        if place is None:
            _warn(
                self.warnings,
                f"{timestamp / 1000.0:.3f}s 的 {note.kind.name} 落在这张谱面保守判定的噪域"
                f"禁区里、沿判定线法向也挪不出去（是否真丢由裁判说了算）",
            )
            return note.position
        return place

    def _emit(
        self,
        track: EventTrack,
        timestamp: int,
        note: SemiNote,
        pool: PointerPool,
    ) -> None:
        position = self._place(note, timestamp)
        match note.kind:
            case SemiKind.TAP:
                pointer, _ = pool.alloc(note)
                track.push(timestamp, position, Touch.DOWN, pointer)
                pool.release(note)
            case SemiKind.DRAG:
                # drag 不需要新的按下，正在屏幕上的手指移过去就行
                pointer, fresh = pool.alloc(note, fresh=False)
                track.push(
                    timestamp, position, Touch.DOWN if fresh else Touch.MOVE, pointer
                )
                pool.release(note)
            case SemiKind.FLICK_HEAD:
                pointer, fresh = pool.alloc(note, fresh=False)
                track.push(
                    timestamp, position, Touch.DOWN if fresh else Touch.MOVE, pointer
                )
            case SemiKind.HOLD_HEAD:
                pointer, _ = pool.alloc(note)
                track.push(timestamp, position, Touch.DOWN, pointer)
            case SemiKind.FLICK | SemiKind.HOLD:
                pointer, _ = pool.alloc(note)
                track.push(timestamp, position, Touch.MOVE, pointer)
            case SemiKind.FLICK_TAIL | SemiKind.HOLD_TAIL:
                pointer, _ = pool.alloc(note)
                track.push(timestamp, position, Touch.MOVE, pointer)
                pool.release(note)

    def _build_frames(
        self,
        chart: Chart,
        screen: Screen,
        progress: Progress,
        warnings: list[str],
    ) -> tuple[dict[int, list[SemiNote]], int]:
        config = self.config
        duration = config.flick_end - config.flick_start
        if duration <= 0:
            raise PlanningError(f"flick 手势时长非正：{config.flick_start} -> {config.flick_end}")

        sway = 1j if config.flick_direction == 0 else 1
        frames: defaultdict[int, list[SemiNote]] = defaultdict(list)
        retimed = 0
        note_id = 0

        for line in progress.track(chart.lines, "统计音符帧"):
            for note in line.notes:
                timestamp = round(note.seconds * 1000)
                placement = place_note(line, note, screen, retime=note.kind is NoteType.FLICK)
                if placement.retimed:
                    retimed += 1
                    _warn(
                        warnings,
                        f"{note.kind.name} @ {note.seconds:.3f}s 判定点在屏幕外，"
                        f"已微调至 {placement.seconds:.3f}s",
                    )
                elif placement.remapped:
                    _warn(
                        warnings,
                        f"{note.kind.name} @ {note.seconds:.3f}s 判定点在屏幕外，已沿判定线拉回屏幕",
                    )

                position = placement.position
                rotation = placement.rotation

                match note.kind:
                    case NoteType.TAP:
                        frames[timestamp].append(
                            SemiNote(
                                SemiKind.TAP,
                                screen.remap(position, rotation),
                                note_id,
                                rotation,
                            )
                        )
                    case NoteType.DRAG:
                        frames[timestamp].append(
                            SemiNote(
                                SemiKind.DRAG,
                                screen.remap(position, rotation),
                                note_id,
                                rotation,
                            )
                        )
                    case NoteType.FLICK:
                        # 同一次手势划几下（见 ConservativeConfig.flick_repeats）：
                        # 每一下都从 +半径**跳**回 +半径一侧再划到 -半径，于是每一下
                        # 都是一次"新起手"，游戏那边就是一次独立的判定机会。
                        # 整段关于判定时刻对称；只有最后一下收手（其它几下中途就跳回去）。
                        first = -(config.flick_repeats * duration) // 2
                        for repeat in range(config.flick_repeats):
                            base = timestamp + first + repeat * duration
                            kind = SemiKind.FLICK_HEAD if repeat == 0 else SemiKind.FLICK
                            frames[base].append(
                                SemiNote(
                                    kind,
                                    _sway(screen, position, rotation, sway, 0, 0, duration),
                                    note_id,
                                    rotation,
                                )
                            )
                            for offset in range(
                                config.sample_delay, duration, config.sample_delay
                            ):
                                frames[base + offset].append(
                                    SemiNote(
                                        SemiKind.FLICK,
                                        _sway(screen, position, rotation, sway, offset, 0, duration),
                                        note_id,
                                        rotation,
                                    )
                                )
                            if repeat == config.flick_repeats - 1:
                                frames[base + duration].append(
                                    SemiNote(
                                        SemiKind.FLICK_TAIL,
                                        _sway(
                                            screen, position, rotation, sway, duration, 0, duration
                                        ),
                                        note_id,
                                        rotation,
                                    )
                                )
                    case NoteType.HOLD:
                        frames[timestamp].append(
                            SemiNote(
                                SemiKind.HOLD_HEAD,
                                screen.remap(position, rotation),
                                note_id,
                                rotation,
                            )
                        )
                        hold_ms = math.ceil(note.hold * 1000)
                        for offset in range(1, hold_ms, config.sample_delay):
                            moment = (timestamp + offset) / 1000
                            body_rotation = line.rotation_at(moment)
                            body_position = screen.remap(
                                line.point_at(moment, note.offset), body_rotation
                            )
                            frames[timestamp + offset].append(
                                SemiNote(
                                    SemiKind.HOLD,
                                    body_position,
                                    note_id,
                                    body_rotation,
                                )
                            )
                        end = (timestamp + hold_ms) / 1000
                        tail_rotation = line.rotation_at(end)
                        frames[timestamp + hold_ms].append(
                            SemiNote(
                                SemiKind.HOLD_TAIL,
                                screen.remap(line.point_at(end, note.offset), tail_rotation),
                                note_id,
                                tail_rotation,
                            )
                        )

                note_id += 1

        return frames, retimed


def _sway(
    screen: Screen,
    position: Position,
    rotation: Vector,
    sway: Vector,
    offset: int,
    flick_start: int,
    duration: int,
) -> Position:
    """滑键手势在**这一段划动**第 ``offset`` 毫秒时的触点：沿 ``sway`` 从 +半径划到 -半径。

    ``offset`` 是这一段内的相对时刻（0 = 起手、``duration`` = 收尾），所以 ``rate`` 从 +1
    线性走到 -1。**下一段又从 +1 开始** —— 那个跳变就是游戏要找的"新起手"。
    """
    rate = 1 - 2 * (offset - flick_start) / duration
    return screen.remap(position + rotation * sway * screen.flick_radius * rate, rotation)


def _warn(warnings: list[str], message: str) -> None:
    if len(warnings) < WARNING_LIMIT:
        warnings.append(message)


PLANNER = ConservativePlanner
