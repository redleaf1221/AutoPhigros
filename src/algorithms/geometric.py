"""几何算法（吸收自 phisap 的"极端/几何算法"）。

以固定 tick 把谱面切成互不影响的帧；给每个 note 算出一条贯穿屏幕的判定区窄带，同一帧里
与 tap 窄带相交的 drag 并进来、drag 之间相交的也并起来 —— 一次按下同时判掉一整片区域。
最省手指，代价是判定区宽度只是 phisap 的经验值。
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Mapping, NamedTuple

from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

from .blocks import (
    INSET_CAP,
    INSET_RATIO,
    NoiseField,
    exposure,
)
from .chart import Chart, JudgeLine, NoteType
from .geometry import JudgeAreaBuilder, Screen, interior_of, outside, place_note
from .judging import DRAG_TOLERANCE, SCAN_AHEAD, TAP_TOLERANCE, blocked_fingers, spine_offset
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


# ------------------------------------------------- 噪域：本规划器自己的策略
# 游戏机制（噪域是什么、哪一点会被吃、会被吃到什么时候）只有一份，在 `blocks.NoiseField` 里。
# **这一段是"遇到噪域怎么办"** —— 禁区算多保守、手指往哪儿挪、将死的要不要提前松、收尾怎么补按
# —— 它跟本规划器的时间栅格、手指大概要待多久、以及"这一下按下会顺手判掉谁"绑在一起，所以归
# 本规划器自己，不跟别的规划器共用：换规划器就是换这一整段。
#
# 四个规划器手上这一份**各自独立**（都是当初从 blocks.py 搬过来的），谁想换算法就改自己这份；
# `tests/noise.py` 会挨个跑四个规划器，谁跟机制对不上就会红。


NOISE_TICK_STRIDE = 4
"""噪域生效期间，就算没有音符也要每隔这么多 tick 走一遍分配器（4 tick = 32ms）。

分配器只在"有音符的帧"上跑，手指却一直按在屏幕上：两根音符之间只要空得久一点（Ametrine 里
那段空档 1.3 秒），就没人复查在屏手指，噪域扫过来把它吃掉，规划器却仍以为它按着 —— 后面
依赖它的音符一次都不按。空帧很便宜（只做"复查 + 退休"），所以这段窗口干脆按固定步长走。"""


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
class GeometricConfig:
    """几何算法的参数（控制台 ``option`` 或 config.json 的 ``planner_options`` 都能改）。"""

    flick_ticks: int = field(default=3, metadata={"help": "滑键手势的持续 tick 数"})
    flick_repeats: int = field(default=2, metadata={"help": "一次滑键手势重复划几下"})
    flick_direction: int = field(
        default=0, metadata={"help": "0 = 垂直判定线滑动，1 = 平行判定线滑动"}
    )
    max_pointers: int = field(default=10, metadata={"help": "最多几根手指"})
    down_lead_ticks: int = field(
        default=6,
        metadata={
            "help": "要新按手指、而目标点会顺手判掉别的音符时：先按到补集里的落脚点，"
            "隔多少 tick 再 MOVE 到目标（送达延迟的抖动有 1~3 帧，得留够）"
        },
    )
    merge_min_area: float = field(
        default=2.0,
        metadata={
            "help": "drag 并进 tap 那一组的最小重叠面积（虚拟屏平方单位）：并了就由 tap 自己"
            "那一下顺手判掉它，两块判定区只擦个边是不够的"
        },
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


CONFIG = GeometricConfig


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
    __slots__ = (
        "screen",
        "flick_ticks",
        "flick_repeats",
        "rotate_factor",
        "down_lead",
        "merge_min_area",
        "idle",
        "on_screen",
        "track",
        "now",
        "builder",
        "danger",
        "danger_seconds",
        "_strips",
        "guard",
        "warnings",
    )

    def __init__(
        self,
        screen: Screen,
        config: GeometricConfig,
        danger: list[tuple[float, JudgeLine, float]] | None = None,
        noise: NoiseField | None = None,
        warnings: list[str] | None = None,
    ) -> None:
        self.screen = screen
        self.flick_ticks = config.flick_ticks
        self.flick_repeats = max(1, config.flick_repeats)
        # 方向与另外两个算法相反：从 note 中心朝一侧划出去
        self.rotate_factor = -1j if config.flick_direction == 0 else 1
        self.down_lead = max(0, config.down_lead_ticks)
        self.merge_min_area = config.merge_min_area
        self.idle: set[int] = set(range(1000, 1000 + config.max_pointers))
        self.on_screen: list[Pointer] = []
        self.track = EventTrack()
        self.now = 0
        self.builder = JudgeAreaBuilder(screen)
        self.danger = danger or []
        """`(时刻, 判定线, 横向偏移)`：TAP 与 Hold 头的判定点，用来算"这一下按下会判到谁"。"""
        self.danger_seconds = [item[0] for item in self.danger]
        self._strips: dict[int, list[Polygon]] = {}
        self.guard = NoiseGuard(
            noise,
            screen,
            margin=config.noise_margin,
            tick_ms=FRAME_MS,
            dwell_ticks=DRAG_DWELL_TICKS,
            lead_ticks=self.down_lead,
            enabled=config.avoid_noise,
        )
        self.warnings = warnings if warnings is not None else []

    # ------------------------------------------------------------------ 分配

    def allocate(self, timestamp: int, frame: Frame) -> None:
        self.now = timestamp
        self._release_doomed()

        # 每个 tap 都得独立按一次，不能合并；drag 可以往 tap 或别的 drag 上并
        tap_areas: list[tuple[Polygon, Position]] = [
            (note.judge, note.position) for note in frame.taps
        ]
        drag_areas: list[Polygon] = []

        # 已经在屏幕上的手指落进 drag 判定区，就说明这个 drag 已经被顺手判掉了。
        # 这条捷径成立的前提是：`_release_doomed` 已经在本帧开头把"到 `expire` 之前会被噪域
        # 扫到"的手指摘掉了，而且**噪域生效期间会补走空帧**（见 `NOISE_TICK_STRIDE`）—— 不然
        # 两根音符之间的空档里手指数掉了没人发现，这里就会以为有人按着、一次也不按
        #（Ametrine 73.333s 那个 drag 就是这么丢的）。
        occupied = [pointer.position for pointer in self.on_screen if pointer.expire > timestamp]
        occupied.extend(note.position for note in frame.flicks)

        for note in frame.drags:
            if any(note.judge.contains(Point(point.real, point.imag)) for point in occupied):
                continue
            merged = False
            for index, (area, press) in enumerate(tap_areas):
                if not area.intersects(note.judge):
                    continue
                overlap = area.intersection(note.judge)
                # 并进 tap 那一组 = 这个 drag 由 tap 的**按下**顺手判掉（按在 tap 自己的判定点上），
                # 所以只有两块判定区真正叠在一起（面积够大）时那一下才够得着它
                if overlap.area < self.merge_min_area:
                    continue
                tap_areas[index] = (overlap, press)
                merged = True
                break
            if not merged:
                for index, area in enumerate(drag_areas):
                    if area.intersects(note.judge):
                        drag_areas[index] = area.intersection(note.judge)
                        merged = True
                        break
            if not merged:
                drag_areas.append(note.judge)

        for note in frame.taps:
            # 按在音符自己的判定点上（横向偏差 0，是度量的下界），不要按合并区域的中心。
            # 落在噪域里就沿法向挪（横向偏差不变）：不挪的话这一下当帧就被废掉了。
            position = self.guard.place(note.position, note.rotation, self.now)
            if position is None:
                self._warn(
                    f"TAP @ {note.timestamp * FRAME_MS / 1000.0:.3f}s 落在这张谱面保守判定的"
                    f"噪域禁区里、沿判定线法向也挪不出去（是否真丢由裁判说了算）"
                )
                position = note.position
            pointer = self._take_idle_or_resting(position)
            if isinstance(pointer, Pointer):
                self._insert(pointer.expire, pointer.position, Touch.UP, pointer.id)
            # 按下至少停一帧：这根手指还可能被当成"覆盖了某个 drag"来用
            bound = self._bind(pointer, position, DRAG_DWELL_TICKS)
            self._insert(timestamp, position, Touch.DOWN, bound.id)

        for note in frame.flicks:
            place = self.guard.place(note.position, note.rotation, self.now, extend=self.screen.flick_radius, along=note.rotation * self.rotate_factor)
            if place is None:
                self._warn(
                    f"FLICK @ {note.timestamp * FRAME_MS / 1000.0:.3f}s 落在噪域里、"
                    f"沿判定线法向也挪不出去（这一下会丢）"
                )
                place = note.position
            chosen = self._take_resting_or_idle(place)
            safe = self._safe_down(place, isinstance(chosen, int))
            lead = self.down_lead if safe is not None else 0
            # 整段（repeats 下）都占着这根手指，中途不松手
            bound = self._bind(
                chosen,
                place,
                (self.flick_ticks + 1) * self.flick_repeats + lead,
            )
            if safe is not None:
                self._insert(self.now, safe, Touch.DOWN, bound.id)

            swipe = place
            for repeat in range(self.flick_repeats):
                base = self.now + lead + repeat * (self.flick_ticks + 1)
                # 每一下都先蹦回判定点再划出去，那一下跳变就是游戏要找的"新起手"
                action = Touch.DOWN if isinstance(chosen, int) and repeat == 0 and not lead else Touch.MOVE
                self._insert(base, place, action, bound.id)
                for delta in range(1, self.flick_ticks + 1):
                    rate = delta / self.flick_ticks
                    swipe = (
                        place
                        + note.rotation * self.rotate_factor * rate * self.screen.flick_radius
                    )
                    self._insert(base + delta, swipe, Touch.MOVE, bound.id)
            bound.position = swipe

        noise_area = self.guard.area(self.now)
        for area in drag_areas:
            if noise_area is not None:
                free = area.difference(noise_area)
                if free.is_empty:
                    self._warn(
                        f"{self.now * FRAME_MS / 1000.0:.3f}s 的 DRAG 判定区整块都在噪域里"
                        f"（这一下会丢）"
                    )
                else:
                    area = free
            # 区域**内部**的一点：重心可能落在凹形区域外面，那样就够不着别的成员了
            position = interior_of(area)
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

        # 歇够了的手指抬起来，收回备用池
        for pointer in [item for item in self.on_screen if item.expire < timestamp]:
            self._insert(pointer.expire, pointer.position, Touch.UP, pointer.id)
            self.on_screen.remove(pointer)
            self.idle.add(pointer.id)

    def _safe_down(self, position: Position, fresh: bool) -> Position | None:
        """要**新按**一根手指时：按在 `position` 会顺手判掉别的音符的话，返回补集里的落脚点。

        老手指用 MOVE 就位，不触发 `CheckNote`，所以只有新按下才有这个问题。
        落脚点同时还必须避开噪域 —— 手指一落进去就当帧作废，连后面几 tick 都要重排。
        """
        if not fresh or self.down_lead <= 0:
            return None
        strips = self._danger_strips()
        area = self.guard.area(self.now)
        if area is not None:
            strips = [*strips, area]
        if not any(strip.contains(Point(position.real, position.imag)) for strip in strips):
            return None
        return outside(strips, position, self.screen)

    def _release_doomed(self) -> None:
        """把"接下来会被噪域扫到"的手指**在被扫到之前**松开，收回备用池。

        两件事缺一不可（Ametrine 73.333s 那个 drag 就是缺第二件丢的）：

        * **松手要排在被吃之前**：`blockedFingerIds` 是"抬起时清"，提前一条 UP 就把手指救回来了
          （代价只是下次重新按下）。判据用精确的 :meth:`NoiseGuard.doomed`（同一套奇偶 + inset），
          不是保守矩形；
        * **松开之后不能再算"覆盖了某个 drag"**：`allocate` 里"已经在屏的手指落进 drag 判定区
          就算它被判掉了"那条捷径只看在屏表 —— 留着一根马上要被抬起的手指，规划器就会以为
          那一下有人按着，于是一次也不按。

        "接下来"的窗口是**这一帧到它本来打算待到的时刻（`expire`）**；这个窗口能成立的前提是
        :data:`NOISE_TICK_STRIDE` 那条：噪域生效期间哪怕没有音符也要按步长补走空帧，否则两根
        音符之间的空档里没人复查，手指数掉了规划器也不知道。
        """
        if not self.guard.active or not self.on_screen:
            return
        for pointer in list(self.on_screen):
            until = max(pointer.expire, self.now)
            if until <= self.now:
                continue
            if not self.guard.doomed(pointer.position, self.now, until):
                continue
            self._insert(self.now, pointer.position, Touch.UP, pointer.id)
            self.on_screen.remove(pointer)
            self.idle.add(pointer.id)

    def _warn(self, message: str) -> None:
        if len(self.warnings) < WARNING_LIMIT:
            self.warnings.append(message)

    def _danger_strips(self) -> list[Polygon]:
        """这一刻按下会判到谁：把"还没按下过的" TAP / Hold 头的**容差带**取出来。

        带子按**按下那一刻**的判定线算（`CheckNote` 读的是当帧的 `fingerPositionX`），
        半宽 `TAP_TOLERANCE`（1.9 谱面单位折成虚拟屏 1.71）。同 tick 的 TAP 已经由它自己
        那一次按下判掉了（tap 循环排在 drag 前面），所以只看还在未来的。
        """
        cached = self._strips.get(self.now)
        if cached is not None:
            return cached
        seconds = self.now * FRAME_MS / 1000.0
        lo = bisect_left(self.danger_seconds, seconds + FRAME_MS / 2000.0)
        hi = bisect_right(self.danger_seconds, seconds + SCAN_AHEAD)
        strips: list[Polygon] = []
        for note_seconds, line, offset in self.danger[lo:hi]:
            strip = self.builder.band(
                line.point_at(seconds, offset),
                line.rotation_at(seconds),
                half_width=TAP_TOLERANCE,
            )
            if strip is not None:
                strips.append(strip)
        self._strips[self.now] = strips
        return strips

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

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        self.config = build_options(GeometricConfig, options)

    def plan(self, chart: Chart, progress: Progress) -> PlanResult:
        screen = chart.screen
        builder = JudgeAreaBuilder(screen)
        frames: defaultdict[int, Frame] = defaultdict(Frame)
        danger: list[tuple[float, JudgeLine, float]] = []
        """TAP 与 Hold 头的判定点：`CheckNote` 的候选只可能是这些（drag / flick 不进 tap 判定）。"""
        warnings: list[str] = []
        retimed = 0

        for line in progress.track(chart.lines, "统计判定区"):
            for note in line.notes:
                timestamp = round(note.seconds * 1000)
                tick = timestamp >> 3
                placement = place_note(line, note, screen, retime=note.kind is NoteType.FLICK)
                if note.kind in (NoteType.TAP, NoteType.HOLD):
                    # Hold 只有**头**进候选：身体是逐帧位置判的，不参与 CheckNote 的挑选
                    danger.append((note.seconds, line, note.offset))
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

        danger.sort(key=lambda item: item[0])
        field = chart.noise_field()
        note_frames = len(frames)
        allocator = PointerAllocator(screen, self.config, danger, field, warnings)
        # 有音符的帧当然要走；**噪域生效期间还要按固定步长补走一遍空帧** —— 否则两根音符
        # 之间（Ametrine 里那段空档有 1.3 秒）没人复查在屏手指，噪域扫过来把手指数掉，
        # 规划器却仍以为它按着，后面依赖它的音符一次都不按。
        ticks = set(frames)
        if field:
            for begin, end in field.active_windows:
                first, last = int(begin * 1000) >> 3, int(end * 1000) >> 3
                ticks.update(range(max(0, first), last + 1, NOISE_TICK_STRIDE))
        for tick in progress.track(sorted(ticks), "规划触控事件"):
            allocator.allocate(tick, frames[tick])

        frames_out = allocator.done()
        field = chart.noise_field()
        result = PlanResult(planner=self.name, screen=screen, frames=frames_out)
        result, _repaired = finish_noise(result, field, warnings)
        result.stats = {
            "notes": chart.note_count,
            "frames": note_frames,
            "retimed_notes": retimed,
            "noise_blocks": len(field.blocks),
        }
        return result


PLANNER = GeometricPlanner
