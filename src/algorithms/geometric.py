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

from shapely.affinity import affine_transform
from shapely.geometry import Point, Polygon
from shapely.ops import nearest_points, unary_union

from .blocks import (
    INSET_CAP,
    INSET_RATIO,
    SAMPLE_MS,
    NoiseField,
    exposure,
)
from .chart import Chart, JudgeLine, NoteType
from .geometry import SAFE_MARGIN, JudgeAreaBuilder, Screen, interior_of, place_note
from .judging import (
    DRAG_TOLERANCE,
    NORMAL_WEIGHT,
    SCAN_AHEAD,
    TAP_TOLERANCE,
    blocked_fingers,
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

EPSILON = 1e-4
"""挑落点时把可玩区往内收的**数值**余量（虚拟屏单位，万分之一个格）。

``area.difference(噪域)`` 出来的边界**正压在**噪域边上，而游戏的判据是闭区间
（``|local| <= half``，边界也算被吃）；"多边形上离目标最近的点"于是常常正落在边界上。往里收
这一丝是为了让最近点落在里面 —— 它跟"保险余量"不是一回事：按经验加的那圈
``buffer(noise_margin)`` 已经删掉了（它会把游戏认得的落点也一起禁掉），落点收不收最后由
:meth:`NoiseGuard.clean`（游戏自己的逐点判据）说了算，这里只是让候选落在里面。"""


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
    的时间栅格与"手指大概要待多久"；``forbidden`` / ``aim`` 原来是 ``NoiseField`` 上的方法，
    现在已经归到这里 —— 因为"手指要待多久、落点挑哪一个"是**规划**，不是游戏的规则。

    **这里没有"保守禁区"**：游戏吃不吃一个点，``NoiseField.blocking`` / ``blocking_area`` 已经
    逐点逐格对拍过了（``tests/noise.py``），禁区就是那个集合本身。唯一还算策略的是这两件：

    * **窗口**：手指不是一瞬间，而是从 ``tick`` 待到 ``tick + span_ticks`` —— 它在这段时间里每
      一帧都得干净，所以把这段里每一刻的禁区**并起来**（并集只会多禁、不会少禁）；
    * **落点**：在"这一下判得到的区域"里挑，优先挑判定权（``CheckNote`` 的度量）最小的那个点，
      见 :meth:`aim`。

    以前那圈 ``buffer(noise_margin)`` 是拿"采样步长 / 我的几何与游戏的误差"当借口加的保险，
    代价是**把游戏认的落点也一起禁掉**：Petrichor 那 14 个 TAP 就是这么丢的 —— 左半边被一块
    噪域盖住、音符正好压在盖住的边上，往外挪 0.2 就干净了，被撑大的禁区却报"挪不出去"，
    于是照样按在会被吃掉的点上。现在改成**候选点由几何提，收不收由游戏自己的逐点判据说了算**
    （:meth:`clean`），边界上那点浮点误差就自然被否掉，不需要任何外扩。
    """

    def __init__(
        self,
        field: NoiseField | None,
        screen: Screen,
        *,
        tick_ms: float = 8.0,
        span_ticks: int = 1,
        slack_ms: float = 0.0,
        clearance: float = 0.0,
        weight: float = 1.0,
        enabled: bool = True,
    ) -> None:
        self.field = field if (enabled and field is not None and field.blocks) else None
        self.screen = screen
        self.tick_ms = tick_ms
        self.step = max(tick_ms, SAMPLE_MS) / 1000.0
        """问"会不会被吃"的步长（秒）：不比 :data:`~algorithms.blocks.SAMPLE_MS` 更细。"""
        self.span_ticks = max(1, span_ticks)
        self.slack = max(0.0, slack_ms) / 1000.0
        """禁区窗口往**两边**各让这么多秒 —— 游戏是按帧（60fps）更新噪域变换的，触控落进哪一帧
        有一两帧的余量；只问 [tick, tick+span] 会漏掉"相邻那一帧边界还在原点"这种情况。"""
        self.clearance = max(0.0, clearance)
        """落点想**离噪域边界多远**（虚拟屏单位）—— 价值函数里的那一段。"""
        self.noise_weight = max(0.0, weight)
        """余量不够时的"汇率"：差一格余量扣多少判定权（1.0 = 一格换一格）。"""
        self._cache: dict[int, Any] = {}
        self._moment: dict[float, Any] = {}
        # 禁区/落点要用的几何（原来长在 NoiseField 上）
        self.blocks = tuple(field.blocks) if field is not None else ()
        self.inset_ratio = field.inset_ratio if field is not None else INSET_RATIO
        self.inset_cap = field.inset_cap if field is not None else INSET_CAP

    @property
    def _screen(self) -> Screen:
        """禁区算的是"虚拟屏上的哪一片"，就是构造时给的那块屏。"""
        return self.screen

    def moment(self, seconds: float) -> Any:
        """``seconds`` 那一刻的禁区（``blocking_area``）：同一条时间栅格只算一次。

        Petrichor 一张谱面有 427 块噪域、手指要问的时刻几十万个，而相邻窗口的时刻是重合的
        （对齐之后重合 4/5），这一层把重复计算砍掉一个量级。
        """
        key = round(seconds, 6)
        if key in self._moment:
            return self._moment[key]
        value = self.field.blocking_area(key) if self.field is not None else None
        self._moment[key] = value
        return value

    def clean(self, point: Position, lo: float, hi: float) -> bool:
        """一个**不动的**点从 ``lo`` 待到 ``hi`` 会不会被吃掉 —— 游戏自己的逐点判据。

        这是"候选落点收不收"的**唯一**判据（几何只负责提出候选）。``blocking`` 里的
        ``|local| <= half`` 是闭区间：落在边界上也算被吃，所以"多边形上离目标最近的点"这种
        落在噪域边上的候选会被这里否掉 —— 不需要先用 ``buffer`` 把禁区撑大一圈。
        """
        if self.field is None:
            return True
        return self.field.hits(complex(point.real, point.imag), lo, hi, step=self.step) is None

    def forbidden(self, lo: float, hi: float, *, step: float = 1 / 60) -> Any:
        """``[lo, hi)`` 里"任何一帧踩进去都会被吃掉"的区域（虚拟屏上的多边形）。

        用的是 :meth:`blocks.NoiseField.blocking_area` —— **游戏自己的那两道奇偶门**（原矩形走
        第一道、inset 矩形走第二道），不是"所有块原矩形的并集"那种保守近似。这一条是要紧的：
        Ametrine 的 subtract 块（对角可玩空间之外的"洞"）会盖住**全屏**，按原矩形并集算出来的
        禁区就是全屏，于是每个 drag 都被判成"整块在噪域里" —— 那是**误报**，而且是会害人的误报。

        **这里没有余量**：站得离边界多远是"落点值多少"的事，归 :meth:`aim` 的价值函数，不归禁区。

        采样时刻是"``[lo, hi)`` 按 ``step`` **对齐到绝对时间栅格**"的那些点，外加两端。对齐是为了
        让相邻 tick（窗口重合 4/5）共享同一次计算（见 :meth:`moment`）—— Petrichor 那张 427 块
        的谱面，光这一步就值二十几秒。

        没有生效的块就返回 ``None``。
        """
        if self.field is None:
            return None
        moments: set[float] = {lo, hi}
        if hi > lo:
            index = math.ceil(lo / step)
            while index * step < hi:
                moments.add(index * step)
                index += 1
        for block in self.blocks:
            if block.disable <= lo or block.enable >= hi:
                continue
            moments.add(max(lo, block.enable))
            moments.add(min(hi, block.disable))
        shapes = [
            area
            for area in (self.moment(seconds) for seconds in moments)
            if area is not None and not area.is_empty
        ]
        return unary_union(shapes) if shapes else None

    def aim(
        self,
        area: Any,
        point: Position,
        rotation: Any,
        *,
        tick: int,
        span_ticks: int | None = None,
        avoid: Any = None,
        extend: float = 0.0,
        along: Any = None,
    ) -> Position | None:
        """在"这一下判得到的区域"里挑一个落点；挑不出来返回 ``None``。

        ``area`` 是**这一下（这一组）判得到的区域** —— 交集思想就在这儿：一次按下要判掉一整组，
        落点就必须落在这组每个成员都够得着的地方，所以调用方给的是交集。

        落点值多少，看的是**两个量**（不是只看判定权）：

        .. code-block:: text

            价值(p) = 判定权(p) + noise_weight × max(0, noise_clearance − 离噪声边界的距离(p))

        * **判定权**（`CheckNote` 的度量 `|Δx| + |Δy| / 2.2`）：挑候选时用的那个数。音符自己那个点
          （横向、法向偏差都是 0）取到 0，同一刻/邻近的别的音符再近也顶不掉它 —— 这就是"点在
          判定线上"的**唯一**作用，不是一条额外要求；它同时还落在窄带里，于是串在后面的 drag
          会被它"顺手判掉"，手指不用重新按。
        * **离噪声边界的距离**：算得再准，落点也只是"刚好在边界外"（差一万分之一格），而设备那边
          还有三道误差叠上来 —— 送过去的位置被量化到物理像素（16 / 1240 ≈ 0.013 一格）、到达时刻
          晚一到三帧（噪域那块**一直在动**）、游戏用 float32 算同一个式子。2026-10-15 那一局就是这么
          丢的：14 个 TAP 的落点离边界 **0.000**，手确实挪出去了、挪到"压线"上，于是当帧被吃掉。

        两项都是**虚拟屏单位**，所以 `noise_weight = 1` 读作"宁可多花一格判定权，也不愿少站一格
        余量"。权重大了就往远处站（判定权可以多花），小了就往判得准的地方站 —— 这一条是**代价**，
        不是**禁令**：余量不够只是扣分，绝不让这一下变得"没有落点"（那才是丢音符）。

        ``avoid`` 是"这一下按下会顺手判掉别的音符"的**区域**（风格约束：蹭一下换来的是别人一个
        Bad）。它是在 ``area`` **里面**再切一刀 —— 落点仍旧必须落在这组人的交集里（交集思想），
        只是尽量躲开别人。躲不开也不能因此丢掉这一下，所以它是两遍里的第一遍，第二遍放开它。

        一个候选都不行 = 这几帧真的没有落点，返回 ``None``（调用方据此报账）。
        """
        span = self.span_ticks if span_ticks is None else max(1, span_ticks)
        lo = max(0.0, self._seconds(tick) - self.slack)
        hi = self._seconds(tick + span) + self.slack
        direction = along if along is not None else rotation * 1j
        if span == self.span_ticks:
            noise = self.area(tick)
        else:
            noise = self.forbidden(lo, hi)

        def usable(candidate: Position) -> bool:
            if noise is not None and not _ray_clear(noise, self.screen, candidate, direction, extend):
                return False
            return self.clean(candidate, lo, hi)

        # 1) 音符自己那个点：离禁区还有余量、也不蹭着别人 —— 就用它（度量 0）
        if (
            (noise is None or not noise.covers(Point(point.real, point.imag)))
            and (avoid is None or avoid.is_empty or not avoid.covers(Point(point.real, point.imag)))
            and self.clean(point, lo, hi)
        ):
            return point
        free = area if noise is None else area.difference(noise)
        if free.is_empty:
            return None
        # 数值上的一层：`difference` 的边界**正压在**噪域边上，而游戏的判据是闭区间，所以把
        # 可玩区往内收一丝（1e-4 虚拟屏单位），让"最近点"落在里面；收空了就退回不收的版本，
        # 由 `clean` 去判。这是万分之一格的数值处理，不是策略。
        inner = free.buffer(-EPSILON)
        if not inner.is_empty:
            free = inner
        # 两片候选面：贴着边界的那一片（判定权最小），和"多让出 clearance 之后"的那一片。
        # 站远一点要花判定权，值不值由价值函数 + `noise_weight` 说了算 —— 这就是
        # "不以 metric 为唯一标准，而是加上噪域距离"。
        roomy = None
        if noise is not None and self.clearance > 0.0:
            roomier = free.difference(noise.buffer(self.clearance))
            if not roomier.is_empty:
                roomy = roomier
        plain = [item for item in (free, roomy) if item is not None and not item.is_empty]
        if not plain:
            return None
        dodged: list[Any] = []
        if avoid is not None and not avoid.is_empty:
            dodged = [item for item in (r.difference(avoid) for r in plain) if not item.is_empty]
        own = point if self.clean(point, lo, hi) else None

        def strays(candidate: Position) -> bool:
            return (
                avoid is not None
                and not avoid.is_empty
                and avoid.covers(Point(candidate.real, candidate.imag))
            )

        def candidates(strict: bool) -> list[Position]:
            """这一遍的候选：音符自己那个点 + 各片可玩区的"度量最小点"与代表点。"""
            found: list[Position] = []
            if own is not None and not (strict and strays(own)):
                found.append(own)
            for candidate_area in (dodged if strict else plain):
                found.append(self._cheapest(candidate_area, point, rotation))
                found.append(interior_of(candidate_area))
            return found

        # 第一遍只收"不蹭别人"的；一个都没有时第二遍放开那一条（丢音符是实效问题，蹭键是风格问题）
        for strict in (True, False):
            best: Position | None = None
            best_value = math.inf
            for candidate in candidates(strict):
                if not usable(candidate):
                    continue
                value = self._value(candidate, point, rotation, noise)
                if value < best_value:
                    best, best_value = candidate, value
            if best is not None:
                return best
        return None

    def _metric(self, candidate: Position, point: Position, rotation: Any) -> float:
        """`CheckNote` 挑候选的度量 `|Δx| + |Δy| / 2.2`（横向 1 倍、法向 1/2.2）。"""
        local = (candidate - point) * rotation.conjugate()
        return abs(local.real) + abs(local.imag) / NORMAL_WEIGHT

    def _value(self, candidate: Position, point: Position, rotation: Any, noise: Any) -> float:
        """这个落点值多少：判定权 + `noise_weight` × 还差多少余量（见 :meth:`aim`）。"""
        value = self._metric(candidate, point, rotation)
        if noise is not None and self.clearance > 0.0 and self.noise_weight > 0.0:
            short = self.clearance - noise.distance(Point(candidate.real, candidate.imag))
            if short > 0.0:
                value += self.noise_weight * short
        return value

    def _cheapest(self, free: Any, point: Position, rotation: Any) -> Position:
        """可玩区里**度量最小**的那个点：`CheckNote` 的度量是 `|Δx| + |Δy| / 2.2`。

        把法向压 2.2 倍（:data:`~algorithms.judging.NORMAL_WEIGHT`）之后它就是欧氏距离，于是
        "最近点"就是这个度量的最小点 —— 判定线还有干净的部分时结果自然落在线（Δy = 0）上，
        躲不开时贴着噪域边（横向刚好让出必要的量、法向不浪费）。度量与游戏是同一个数，
        方向也就不用再定：横向 1 倍、法向 1/2.2 倍。
        """
        if rotation == 0:
            return interior_of(free)
        inverse = rotation.conjugate()
        weight = NORMAL_WEIGHT
        x0, y0 = point.real, point.imag
        # [Δx, Δy / 2.2]，其中 [Δx, Δy] = conj(rotation) · (p − point)
        matrix = [
            inverse.real,
            -inverse.imag,
            inverse.imag / weight,
            inverse.real / weight,
            -(inverse.real * x0 - inverse.imag * y0),
            -(inverse.imag / weight * x0 + inverse.real / weight * y0),
        ]
        here = nearest_points(affine_transform(free, matrix), Point(0.0, 0.0))[0]
        local = complex(here.x, here.y * weight) * rotation
        return Position(point.real + local.real, point.imag + local.imag)

    @property
    def active(self) -> bool:
        return self.field is not None

    def _seconds(self, tick: int) -> float:
        return tick * self.tick_ms / 1000.0

    def area(self, tick: int) -> Any:
        """这个 tick 起、这根手指可能待着的那一段里的禁区，**已经撑过 clearance**（同一 tick 只算一次）。"""
        if self.field is None:
            return None
        if tick in self._cache:
            return self._cache[tick]
        value = self.forbidden(
            max(0.0, self._seconds(tick) - self.slack),
            self._seconds(tick + self.span_ticks) + self.slack,
        )
        self._cache[tick] = value
        return value

    def doomed(self, position: Position, tick: int, until_tick: int) -> bool:
        """这根手指停在 ``position`` 不动、一直占到这个 tick，会不会被噪域吃掉。"""
        if self.field is None or until_tick <= tick:
            return False
        return (
            self.field.hits(
                position,
                self._seconds(tick),
                self._seconds(until_tick) + self.step,
                step=self.step,
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
    noise_slack_ms: float = field(
        default=33.0,
        metadata={
            "help": "禁区窗口前后各让多少毫秒——游戏按 60fps 更新噪域变换，触控落进哪一帧有一两帧"
            "的余量（实测：只在 [t, t+按住时长] 里问的话，落点在 t−33ms 正好压在边界上）"
        },
    )
    noise_weight: float = field(
        default=2.0,
        metadata={
            "help": "价值函数里「离噪域边界还差多少余量」的汇率：差一格（虚拟屏单位）扣多少判定权。"
            "默认 2.0：压线的代价是**整个音符报 Miss**，而判定权多花一点只在'同时刻有别的音符"
            "抢'时才要命，所以一格余量值两格判定权。调小就更贴着判定点站"
        },
    )
    noise_clearance: float = field(
        default=0.25,
        metadata={
            "help": "落点想离噪域边界多远（虚拟屏单位）—— 这一段是给设备那边的误差留的："
            "送过去的位置被量化到物理像素（16/1240≈0.013 一格）、到达晚 1~3 帧（噪域一直在动）、"
            "游戏用 float32 算同一个式子。算得再准也只是「刚好在边界外」，那时手会压线被吃掉"
        },
    )
    avoid_grazes: bool = field(
        default=True,
        metadata={
            "help": "新按下的落点尽量不落进别的 TAP / Hold 头的容差带（蹭键）：蹭一下换来的是"
            "别人一个 Bad，是风格问题；躲不开也照样按（丢音符是实效问题）"
        },
    )
    down_lead_ticks: int = field(
        default=6,
        metadata={
            "help": "新按一根手指、而落点会顺手判掉别的音符时：先按到交集里躲开别人带子的那一点，"
            "隔多少 tick 再 MOVE 到真正的落点（MOVE 不触发 CheckNote；送达延迟的抖动有 1~3 帧）"
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
        "merge_min_area",
        "avoid_grazes",
        "down_lead",
        "idle",
        "on_screen",
        "track",
        "now",
        "builder",
        "danger",
        "danger_seconds",
        "_future",
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
        self.merge_min_area = config.merge_min_area
        self.down_lead = max(0, config.down_lead_ticks)
        self.avoid_grazes = config.avoid_grazes
        self.idle: set[int] = set(range(1000, 1000 + config.max_pointers))
        self.on_screen: list[Pointer] = []
        self.track = EventTrack()
        self.now = 0
        self.builder = JudgeAreaBuilder(screen)
        self.danger = danger or []
        """`(时刻, 判定线, 横向偏移)`：TAP 与 Hold 头的判定点，用来算"这一下按下会判到谁"。"""
        self.danger_seconds = [item[0] for item in self.danger]
        self._future: dict[int, list[tuple[float, JudgeLine, float]]] = {}
        self.guard = NoiseGuard(
            noise,
            screen,
            tick_ms=FRAME_MS,
            span_ticks=DRAG_DWELL_TICKS,
            slack_ms=config.noise_slack_ms,
            clearance=config.noise_clearance,
            weight=config.noise_weight,
            enabled=config.avoid_noise,
        )
        self.warnings = warnings if warnings is not None else []

    # ------------------------------------------------------------------ 分配

    def _reach(self, note: PlainNote, tolerance: float) -> Polygon:
        """游戏**真正**判得到的区域：`TAP_TOLERANCE`(1.71) / `DRAG_TOLERANCE`(1.89)。

        窄带（``screen.width / 16 / 2``，规划自己的经验宽度）只用来判"两块判定区算不算够得着
        同一个落点"，落点本身从**容差带**里挑 —— 容差带更宽，可玩区更大，能躲开的东西更多。
        """
        area = self.builder.band(note.position, note.rotation, half_width=tolerance)
        return note.judge if area is None else area

    def allocate(self, timestamp: int, frame: Frame) -> None:
        self.now = timestamp
        self._release_doomed()

        # 每个 tap 都得独立按一次（`CheckNote` 一次按下只判一个音符），drag 可以并进 tap 或别的
        # drag 那一组 —— 并进去之后由**同一次按下**判掉，所以落点取的是这一组所有人的交集。
        # 每一项：[窄带交集（判"够不够得着同一处"）, 容差交集（挑落点用）, 判定点, 朝向]。
        tap_groups: list[list[Any]] = [
            [
                note.judge,
                self._reach(note, TAP_TOLERANCE),
                note.position,
                note.rotation,
            ]
            for note in frame.taps
        ]
        drag_groups: list[list[Any]] = []

        # 已经在屏幕上的手指落进 drag 判定区，就说明这个 drag 已经被顺手判掉了。
        # 这条捷径成立的前提是：`_release_doomed` 已经在本帧开头把"到 `expire` 之前会被噪域
        # 扫到"的手指摘掉了，而且**噪域生效期间会补走空帧**（见 `NOISE_TICK_STRIDE`）—— 不然
        # 两根音符之间的空档里手指数掉了没人发现，这里就会以为有人按着、一次也不按
        #（Ametrine 73.333s 那个 drag 就是这么丢的）。
        occupied = [pointer.position for pointer in self.on_screen if pointer.expire > timestamp]
        occupied.extend(note.position for note in frame.flicks)

        for note in frame.drags:
            reach = self._reach(note, DRAG_TOLERANCE)
            if any(reach.contains(Point(point.real, point.imag)) for point in occupied):
                continue
            merged = False
            for group in tap_groups:
                if not group[0].intersects(note.judge):
                    continue
                overlap = group[0].intersection(note.judge)
                # 并进 tap 那一组 = 这个 drag 由 tap 的**按下**顺手判掉（按在 tap 自己的判定点上），
                # 所以只有两块判定区真正叠在一起（面积够大）时那一下才够得着它
                if overlap.area < self.merge_min_area:
                    continue
                group[0] = overlap
                group[1] = group[1].intersection(reach)
                merged = True
                break
            if not merged:
                for group in drag_groups:
                    if group[0].intersects(note.judge):
                        group[0] = group[0].intersection(note.judge)
                        group[1] = group[1].intersection(reach)
                        merged = True
                        break
            if not merged:
                drag_groups.append([note.judge, reach, note.position, note.rotation])

        avoid = self._strips() if self.danger else None
        for group in tap_groups:
            # TAP 不躲蹭键：它那一下的落点**就是**音符自己那个点（度量 0）—— `CheckNote` 一次
            # 按下只判一个音符，度量 0 保证先判到的是它自己，于是根本蹭不到别人。反过来，为了
            # 躲开同位置的"别人"而横向挪开，挪出的是**它自己的**容差带（两条带子一样宽），
            # 那一下就当帧废了（C9S 上 34 个丢音就是这么来的）。
            aimed = self.guard.aim(group[1], group[2], group[3], tick=self.now)
            if aimed is None:
                self._warn(
                    f"TAP @ {group[2]} 这一刻的判定区整块都在噪域里、没有可玩的落点（这一下会丢）"
                )
                position = group[2]
            else:
                position = aimed
            pointer = self._take_idle_or_resting(position)
            if isinstance(pointer, Pointer):
                self._insert(pointer.expire, pointer.position, Touch.UP, pointer.id)
            # 按下至少停一帧：这根手指还可能被当成"覆盖了某个 drag"来用
            bound = self._bind(pointer, position, DRAG_DWELL_TICKS)
            self._insert(timestamp, position, Touch.DOWN, bound.id)

        for note in frame.flicks:
            area = self._reach(note, TAP_TOLERANCE)
            span = (self.flick_ticks + 1) * self.flick_repeats
            aimed = self.guard.aim(
                area,
                note.position,
                note.rotation,
                tick=self.now,
                span_ticks=span,
                extend=self.screen.flick_radius,
                along=note.rotation * self.rotate_factor,
            )
            if aimed is None:
                self._warn(
                    f"FLICK @ {self.now * FRAME_MS / 1000.0:.3f}s 落点连同划动那一段都在噪域里"
                    f"（这一下会丢）"
                )
                place = note.position
            else:
                place = aimed
            chosen = self._take_resting_or_idle(place)
            # 新按一根手指才谈得上蹭键（MOVE 不触发 CheckNote）：按下先落在交集里躲开别人带子的
            # 那一点，隔 lead 个 tick 再 MOVE 到真正的落点
            lead = 0
            landing = place
            if isinstance(chosen, int):
                other = self.guard.aim(
                    area,
                    note.position,
                    note.rotation,
                    tick=self.now,
                    span_ticks=span,
                    avoid=avoid,
                    extend=self.screen.flick_radius,
                    along=note.rotation * self.rotate_factor,
                )
                if other is not None and other != place:
                    landing = other
                    lead = self.down_lead
            bound = self._bind(chosen, place, span + lead)
            if isinstance(chosen, int):
                self._insert(self.now, landing, Touch.DOWN, bound.id)

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

        for group in drag_groups:
            aimed = self.guard.aim(group[1], group[2], group[3], tick=self.now)
            if aimed is None:
                self._warn(
                    f"{self.now * FRAME_MS / 1000.0:.3f}s 的 DRAG 判定区整块都在噪域里"
                    f"（这一下会丢）"
                )
                position = group[2]
            else:
                position = aimed
            chosen = self._take_resting_or_idle(position)
            lead = 0
            landing = position
            if isinstance(chosen, int) and avoid is not None:
                other = self.guard.aim(group[1], group[2], group[3], tick=self.now, avoid=avoid)
                if other is not None and other != position:
                    landing = other
                    lead = self.down_lead
            bound = self._bind(chosen, position, DRAG_DWELL_TICKS + lead)
            if isinstance(chosen, int):
                self._insert(self.now, landing, Touch.DOWN, bound.id)
                if lead:
                    self._insert(self.now + lead, position, Touch.MOVE, bound.id)
            else:
                self._insert(self.now, position, Touch.MOVE, bound.id)

        # 歇够了的手指抬起来，收回备用池
        for pointer in [item for item in self.on_screen if item.expire < timestamp]:
            self._insert(pointer.expire, pointer.position, Touch.UP, pointer.id)
            self.on_screen.remove(pointer)
            self.idle.add(pointer.id)

    def _strips(self) -> Any:
        """这一刻按下会顺手判到谁：还没按过的 TAP / Hold 头的**容差带**（并起来、外扩一丝）。

        带子按**按下那一刻**的判定线算（`CheckNote` 读的是当帧的 `fingerPositionX`），半宽
        `TAP_TOLERANCE`（1.9 谱面单位折成虚拟屏 1.71）。同 tick 的 TAP 由它自己那一次按下判掉，
        所以只看还在未来的。外扩 :data:`~algorithms.geometry.SAFE_MARGIN` 是因为带子边界上
        `|横向| == 1.9` 那一侧游戏是**跳过**的，浮点一抖就成了"判到了"。

        它只用来在 ``area`` 里面再切一刀（见 :meth:`NoiseGuard.aim`），不是用来绕开音符的：
        蹭键是风格问题，丢音符是实效问题。
        """
        cached = self._future.get(self.now)
        if cached is not None:
            return cached
        if not self.avoid_grazes:
            self._future[self.now] = None
            return None
        seconds = self.now * FRAME_MS / 1000.0
        lo = bisect_left(self.danger_seconds, seconds + FRAME_MS / 2000.0)
        hi = bisect_right(self.danger_seconds, seconds + SCAN_AHEAD)
        strips: list[Polygon] = []
        for _note_seconds, line, offset in self.danger[lo:hi]:
            strip = self.builder.band(
                line.point_at(seconds, offset),
                line.rotation_at(seconds),
                half_width=TAP_TOLERANCE,
            )
            if strip is not None:
                strips.append(strip)
        value: Any = unary_union(strips).buffer(SAFE_MARGIN) if strips else None
        self._future[self.now] = value
        return value

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
