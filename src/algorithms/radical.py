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
from .chart import Chart, NoteType
from .geometry import JudgeAreaBuilder, Screen, interior_of, place_note
from .judging import DRAG_TOLERANCE, NORMAL_WEIGHT, TAP_TOLERANCE
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


def _tolerance(kind: NoteType) -> float:
    """这一类音符游戏**判得到**的横向容差：drag 是 2.1 谱面单位（1.89），其余是 1.9（1.71）。"""
    return DRAG_TOLERANCE if kind is NoteType.DRAG else TAP_TOLERANCE


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
    noise_slack_ms: float = field(
        default=33.0,
        metadata={
            "help": "禁区窗口前后各让多少毫秒——游戏按 60fps 更新噪域变换，触控落进哪一帧有一两帧"
            "的余量（实测：只在按下那一刻问的话，落点在 t−33ms 正好压在边界上）"
        },
    )
    noise_weight: float = field(
        default=2.0,
        metadata={
            "help": "价值函数里「离噪域边界还差多少余量」的汇率：差一格（虚拟屏单位）扣多少判定权。"
            "默认 2.0：压线的代价是整个音符报 Miss，而判定权多花一点只在'同时刻有别的音符抢'时才要命"
        },
    )
    noise_clearance: float = field(
        default=0.25,
        metadata={
            "help": "落点想离噪域边界多远（虚拟屏单位）—— 给设备那边的误差留的：位置被量化到物理"
            "像素（16/1240≈0.013 一格）、到达晚 1~3 帧（噪域一直在动）、游戏用 float32 算同一个式子"
        },
    )
    avoid_noise: bool = field(
        default=True,
        metadata={"help": "按下时避开噪域（4.0.1 的 blockAreaList）：落进噪域的手指会被废掉"},
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
        "builder",
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
        self.builder = JudgeAreaBuilder(screen)
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
            tick_ms=1.0,
            span_ticks=MIN_DWELL_MS,
            slack_ms=config.noise_slack_ms,
            clearance=config.noise_clearance,
            weight=config.noise_weight,
            enabled=config.avoid_noise,
        )
        self.warnings = warnings if warnings is not None else []

    def _place(self, note: PlainNote, *, swipe: bool = False) -> Position:
        """落在噪域里就改到"这一下判得到的区域"里**度量最小**的那个点（见 :meth:`NoiseGuard.aim`）。

        ``swipe`` 给 flick 用：划动那一段（``flick_radius``）也得干净，否则落点刚躲开、
        一划又进去了。
        """
        area = self.builder.band(note.position, note.rotation, half_width=_tolerance(note.kind))
        place = self.guard.aim(
            area if area is not None else note.position,
            note.position,
            note.rotation,
            tick=self.now,
            extend=self.screen.flick_radius if swipe else 0.0,
            along=note.rotation * self.rotate_factor,
        )
        if place is None:
            if len(self.warnings) < WARNING_LIMIT:
                self.warnings.append(
                    f"{self.now / 1000.0:.3f}s 的 {note.kind.name} 这一刻的判定区整块都在噪域里、"
                    f"没有可玩的落点（这一下会丢）"
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
