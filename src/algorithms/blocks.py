"""噪域：4.0.1 谱面里新增的 ``blockAreaList``（``GameInformation::BlockArea``）。

一句话规则（判定侧，逐帧、在判音符**之前**跑）：**触点落进生效中的噪域，这根手指就被从
`FingerManagement.fingers` 里摘掉** —— 当帧起它一个音符也判不到；而且 ``blockedFingerIds``
只在**手指抬起**时才清（``SyncFingers`` 里的 ``RemoveWhere(id 不在当前 touches)``）。
所以：**一进噪域，这根手指就废到抬起来为止**，中途挪出去也不算数。

反编译依据（Phigros 4.0.1 ``libil2cpp.so``，行号/地址见各函数注释）：

===========================  =========  ==========================================
``GameInformation::BlockArea``           拓扑：两个角百分比 + 四个时刻 + isSubtract
                                           + rotate/move/scale 三组**键帧**事件
``GameInformation::BlockArea::Mirror``     0x1cbe3e4  镜像（x 翻、角度取反）
``PreviewBlockControl::IsTimeValid``       0x1cdbaf4  ``appearTime <= t < disappearTime``
``PreviewBlockControl::IsActive``          0x1cdcab8  ``enableTime <= t < disableTime``
``PreviewBlockControl::GetBlockGeometry``  0x1cdbb38  百分比 → （尺寸，中心，锚点）
``PreviewBlockControl::UpdateBlocksTransform`` 0x1cdb5b0  基础位置与缩放；不在窗口里就
                                                          把位置甩到 x=1000（等于失效）
``PreviewBlockControl::UpdateBlockAnimations`` 0x1cdb864 再叠 scale → rotate → move 键帧
``PreviewBlockControl::SafeDiv``           0x1cdc518  自变量的绝对值过小就返回 **1.0**
``PreviewBlockControl::CalculateEasedProgress`` 0x1cdc850
``GetEase::Instantiation``                 0x1cc95e4  15 张 101 点缓动表
``GetEase::GetEaseWithProgress``           0x1cc92fc  查表 + 线性插值（下标截断）
``JudgeControl::TryGetBlockingBlock``      0x1d735d8  奇偶判定，见 :meth:`NoiseField.blocking`
``JudgeControl::IsPositionInsideBlock``    ``TryGetBlockTouchHalfSize`` 定 inset 后的矩形
``JudgeControl::IsPositionInsideOriginalBlock``        原矩形（``|local| <= 0.5``）
``JudgeControl::TryGetBlockTouchHalfSize`` 0x1cd...    inset = ``inset_ratio × 屏高 / 块尺寸``
===========================  =========  ==========================================

坐标：百分比 ``p`` 与虚拟屏的关系是干净的 ``(p.x × 宽, p.y × 高)``，与判定线的
``start/start2`` 同一套（``0 = 左/下边缘``，见 ``chart._position``）。世界坐标里块的
``localPosition = (p − 0.5) × (screenWidth, screenHeight)``、``localScale = |Δp| × 同款``，
而世界高恰好是相机的 10 个单位，所以虚屏单位下的位置/尺寸就是按比例换过去的同一个数 ——
``screenWidth / screenHeight`` 那份宽高比在两侧约掉，**规则本身与设备宽高比无关**。

一个前提（与项目其余部分同一个前提）：虚拟屏的宽高比就是游戏的 16:9。设备的实际宽高比
只在"虚拟屏 → 设备像素"那一步登场（``backends.utils.to_pixels`` 的 ``A``）；等比缩放
之下局部坐标不变，所以噪域这条链不需要知道设备分辨率。

**这个模块里只有机制**（噪域是什么、哪一点会被吃、会被吃到什么时候）。"遇到噪域怎么办" ——
禁区算多保守、手指往哪挪、将死的要不要提前松、收尾怎么补按 —— 是**各规划器自己的规划**：
四个规划器里各有一份自己的 ``NoiseGuard`` / ``finish_noise``。为什么这么分、代价是什么，
见 ``docs/impl.md`` 的「机制一份、策略四份」。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Iterable, NamedTuple


from shapely.geometry import Polygon
from shapely.ops import unary_union

from .geometry import Screen
from .utils import Position, Touch

# --------------------------------------------------------------- 缓动

EASE_KINDS = 15
"""``GetEase::Instantiation`` 建了 15 张表：0 线性，1~12 是 2/3/4/5 次幂的 in/out/inout，
13 恒 0，14 恒 1。"""

EASE_STEPS = 100
"""每张表 101 个点（下标 0..100），查表时按 ``floor(进度 × 100)`` 截断后线性插值。"""


def _ease_tables() -> tuple[tuple[float, ...], ...]:
    """把 ``GetEase::Instantiation``（0x1cc95e4）逐句抄成 Python。

    那三段循环的下标在反编译里是"字节偏移"（数组数据在 +32），换回元素下标就是：

    * ``in[i]  = (i/100)^p``
    * ``out[i] = 1 − (1 − i/100)^p``
    * ``inout[k] = in[2k] × 0.5           （k = 0..49）``
    * ``inout[50+k] = out[2k] × 0.5 + 0.5（k = 0..49）``
    * ``inout[100] = 1``
    """
    table = [[0.0] * (EASE_STEPS + 1) for _ in range(EASE_KINDS)]
    for i in range(EASE_STEPS + 1):
        table[0][i] = i / EASE_STEPS

    for power, base in ((2, 1), (3, 4), (4, 7), (5, 10)):
        ease_in, ease_out, ease_in_out = table[base], table[base + 1], table[base + 2]
        for i in range(EASE_STEPS + 1):
            t = i / EASE_STEPS
            ease_in[i] = t**power
            ease_out[i] = 1.0 - (1.0 - t) ** power
        for k in range(EASE_STEPS // 2):
            ease_in_out[k] = ease_in[2 * k] * 0.5
            ease_in_out[EASE_STEPS // 2 + k] = ease_out[2 * k] * 0.5 + 0.5
        ease_in_out[EASE_STEPS] = 1.0

    for i in range(EASE_STEPS + 1):
        table[13][i] = 0.0
        table[14][i] = 1.0
    return tuple(tuple(row) for row in table)


EASE_TABLES = _ease_tables()


def ease_progress(progress: float, kind: int) -> float:
    """``GetEase::GetEaseWithProgress``（0x1cc92fc）：查表 + 在 ``[i, i+1]`` 之间线性插值。

    ``kind`` 越界或进度是 NaN 时按游戏里那条"取第 0 个 / 第 100 个"的分支走 —— 别在这里
    抛异常：它是每帧每块都要跑的观测路径，游戏自己就是夹住的。
    """
    table = EASE_TABLES[kind] if 0 <= kind < EASE_KINDS else EASE_TABLES[0]
    scaled = progress * EASE_STEPS
    index = int(scaled)  # C 的截断：向零取整
    if index >= EASE_STEPS:
        return table[EASE_STEPS]
    if index < 0:
        return table[0]
    return table[index] + (scaled - index) * (table[index + 1] - table[index])


def _clamp01(value: float) -> float:
    """scale / move / rotate 三条路都在外面夹了 ``[0, 1]``（``vbic + vmin`` / 显式分支）。"""
    return 0.0 if value < 0.0 else (1.0 if value > 1.0 else value)


def _safe_div(numerator: float, denominator: float) -> float:
    """``PreviewBlockControl::SafeDiv``（0x1cdc518）：``|分母|`` 小到 0 附近就返回 **1.0**。

    这不是"防崩"的随手一写：缩放事件里 ``0`` 是合法值（块会缩成一条线），比值取 1.0
    意味着"这一对事件之间不缩放"。
    """
    threshold = max(abs(denominator), 0.0) * 1e-6
    if threshold <= 1.401298464324817e-45 * 8.0:
        threshold = 1.401298464324817e-45 * 8.0
    if abs(denominator) < threshold:
        return 1.0
    return numerator / denominator


# --------------------------------------------------------------- 键帧


class BlockShape(NamedTuple):
    """某一时刻这一块的形状（虚拟屏单位、y 轴向上、角度逆时针为正）。"""

    centre: Position
    width: float
    height: float
    degrees: float


@dataclass(frozen=True, slots=True)
class MoveKey:
    """``GameInformation::MoveEvent``：``endPosition`` 是**块的绝对中心**（百分比）。"""

    seconds: float
    x: float
    y: float
    ease_x: int
    ease_y: int


@dataclass(frozen=True, slots=True)
class ScaleKey:
    """``GameInformation::ScaleEvent``：``scale`` 是相对基础矩形的倍率，``anchor`` 是锚点
    （百分比，缩放时中心绕它走）。"""

    seconds: float
    x: float
    y: float
    anchor_x: float
    anchor_y: float
    ease_x: int
    ease_y: int


@dataclass(frozen=True, slots=True)
class RotateKey:
    """``GameInformation::RotateEvent``：``rotation`` 是**绝对角度**（度），``anchor`` 同样
    是"转的时候中心绕它走"的锚点，不是块的轴心。"""

    seconds: float
    degrees: float
    anchor_x: float
    anchor_y: float
    ease: int


def _current_index(keys: Iterable[object], seconds: float) -> int:
    """``FindCurrentEventIndex``（0x1fd9dd4）：最后一个 ``time <= seconds`` 的下标；一个都
    不满足就是 ``-1``，到尾部就钉在最后一个。"""
    index = -1
    for position, key in enumerate(keys):  # type: ignore[arg-type]
        if key.seconds > seconds:  # type: ignore[attr-defined]
            break
        index = position
    return index


def _interpolate(now: float, begin: float, end: float, kind: int) -> float:
    """两个事件之间的缓动进度（``CalculateEasedProgress``：先算线性进度再进缓动表）。"""
    if end == begin:
        return 1.0
    return ease_progress((now - begin) / (end - begin), kind)


def _scale_about(point: Position, anchor: Position, step_x: float, step_y: float) -> Position:
    """``ScaleAroundAnchor``：把 ``point`` 相对 ``anchor`` 的分量按两个方向缩放。"""
    return complex(
        anchor.real + (point.real - anchor.real) * step_x,
        anchor.imag + (point.imag - anchor.imag) * step_y,
    )


def _rotate_about(point: Position, anchor: Position, degrees: float) -> Position:
    """``RotateAroundAnchor``：绕锚点转 ``degrees`` 度（逆时针为正）。"""
    if degrees == 0.0:
        return point
    radians = math.radians(degrees)
    cos, sin = math.cos(radians), math.sin(radians)
    dx, dy = point.real - anchor.real, point.imag - anchor.imag
    return complex(anchor.real + dx * cos - dy * sin, anchor.imag + dx * sin + dy * cos)


# --------------------------------------------------------------- 一块噪域


@dataclass(frozen=True, slots=True)
class BlockArea:
    """谱面里的一块噪域。坐标一律存**百分比**（0~1，0 = 左下），到虚拟屏的换算在
    :meth:`shape` 里一次做完。"""

    top_right: tuple[float, float]
    bottom_left: tuple[float, float]
    appear: float
    enable: float
    disable: float
    disappear: float
    subtract: bool
    moves: tuple[MoveKey, ...]
    scales: tuple[ScaleKey, ...]
    rotates: tuple[RotateKey, ...]

    # ---- 时间窗 ----

    def is_active(self, seconds: float) -> bool:
        """``IsActive``（0x1cdcab8）：只有这个窗口里的块才参与判定。"""
        return self.enable <= seconds < self.disable

    def is_visible(self, seconds: float) -> bool:
        """``IsTimeValid``（0x1cdbaf4）：``UpdateBlockAnimations`` 只在窗口里改 transform，
        窗口外 ``UpdateBlocksTransform`` 把它甩到 x=1000（等于"这块不存在"）。"""
        return self.appear <= seconds < self.disappear

    # ---- 几何 ----

    @property
    def base_centre(self) -> tuple[float, float]:
        """基础矩形的中点（百分比）。"""
        return (
            (self.top_right[0] + self.bottom_left[0]) / 2,
            (self.top_right[1] + self.bottom_left[1]) / 2,
        )

    @property
    def base_span(self) -> tuple[float, float]:
        """基础矩形的**带符号**跨度（百分比）；反编译里 ``Item1 = topRight − bottomLeft``
        是不取绝对值的，符号只在最后 ``set_localScale`` 时被抹掉，中间不影响任何东西。"""
        return (
            self.top_right[0] - self.bottom_left[0],
            self.top_right[1] - self.bottom_left[1],
        )

    def shape(self, seconds: float, screen: Screen) -> BlockShape | None:
        """把 ``UpdateBlocksTransform``（0x1cdb5b0）与 ``UpdateBlockAnimations``（0x1cdb864）
        合起来算一遍：返回这一时刻的形状；不在 ``[appear, disappear)`` 里返回 ``None``。

        顺序（与 ``Update`` 里的调用顺序一致）：基础矩形 → 缩放键帧 → 旋转键帧 → 移动键帧。
        缩放/旋转的"中心绕锚点走"是**累积**的：第 0 到第 i−1 对事件的比值一路叠上去。
        """
        if not self.is_visible(seconds):
            return None

        centre = complex(self.base_centre[0] * screen.width, self.base_centre[1] * screen.height)
        span_x, span_y = self.base_span
        base = (abs(span_x) * screen.width, abs(span_y) * screen.height)
        size = base
        degrees = 0.0

        index = _current_index(self.scales, seconds)
        if index >= 0:
            for pair in range(index):
                previous, following = self.scales[pair], self.scales[pair + 1]
                anchor = complex(previous.anchor_x * screen.width, previous.anchor_y * screen.height)
                centre = _scale_about(
                    centre,
                    anchor,
                    _safe_div(following.x, previous.x),
                    _safe_div(following.y, previous.y),
                )
            current = self.scales[index]
            if index >= len(self.scales) - 1:
                value = (current.x, current.y)
            else:
                following = self.scales[index + 1]
                step_x = _clamp01(_interpolate(seconds, current.seconds, following.seconds, current.ease_x))
                step_y = _clamp01(_interpolate(seconds, current.seconds, following.seconds, current.ease_y))
                value = (
                    current.x + step_x * (following.x - current.x),
                    current.y + step_y * (following.y - current.y),
                )
                anchor = complex(current.anchor_x * screen.width, current.anchor_y * screen.height)
                centre = _scale_about(
                    centre,
                    anchor,
                    _safe_div(value[0], current.x),
                    _safe_div(value[1], current.y),
                )
            size = (value[0] * base[0], value[1] * base[1])

        index = _current_index(self.rotates, seconds)
        if index >= 0:
            for pair in range(index):
                previous, following = self.rotates[pair], self.rotates[pair + 1]
                anchor = complex(previous.anchor_x * screen.width, previous.anchor_y * screen.height)
                centre = _rotate_about(centre, anchor, following.degrees - previous.degrees)
            current = self.rotates[index]
            if index >= len(self.rotates) - 1:
                degrees = current.degrees
            else:
                following = self.rotates[index + 1]
                step = _clamp01(_interpolate(seconds, current.seconds, following.seconds, current.ease))
                delta = (following.degrees - current.degrees) * step
                degrees = current.degrees + delta
                anchor = complex(current.anchor_x * screen.width, current.anchor_y * screen.height)
                centre = _rotate_about(centre, anchor, delta)

        index = _current_index(self.moves, seconds)
        if index >= 0:
            current = self.moves[index]
            if index >= len(self.moves) - 1:
                target = (current.x, current.y)
            else:
                following = self.moves[index + 1]
                step_x = _clamp01(_interpolate(seconds, current.seconds, following.seconds, current.ease_x))
                step_y = _clamp01(_interpolate(seconds, current.seconds, following.seconds, current.ease_y))
                target = (
                    current.x + step_x * (following.x - current.x),
                    current.y + step_y * (following.y - current.y),
                )
            # 移动事件给的是**绝对中心**，落点是"相对基础中心的位移"（UpdateMovement）
            centre += complex(target[0] * screen.width, target[1] * screen.height) - complex(
                self.base_centre[0] * screen.width, self.base_centre[1] * screen.height
            )

        return BlockShape(centre, abs(size[0]), abs(size[1]), degrees)

    # ---- 命中判定 ----

    def to_local(self, shape: BlockShape, point: Position) -> Position | None:
        """把世界（虚拟屏）坐标的点换到**已知形状**的块的局部系：单位方块是 ``±0.5``。

        游戏走的是 ``Transform::InverseTransformPoint``：先平移、再反旋转、再除 ``lossyScale``
        （永远取正）。虚屏与世界之间是等比缩放，所以局部坐标两处一致。退化块返回 ``None``
        —— 对应游戏里 ``|lossyScale| < 0.0001f`` 那两个早退（此时它压根不算命中）。
        """
        if shape.width < 1e-4 or shape.height < 1e-4:
            return None
        relative = point - shape.centre
        if shape.degrees:
            radians = math.radians(-shape.degrees)
            cos, sin = math.cos(radians), math.sin(radians)
            relative = complex(relative.real * cos - relative.imag * sin,
                               relative.real * sin + relative.imag * cos)
        return complex(relative.real / shape.width, relative.imag / shape.height)

    def local(self, point: Position, seconds: float, screen: Screen) -> Position | None:
        """:meth:`to_local` 的便捷版（自己算形状）；不值得在同一帧里连算两次时用。"""
        shape = self.shape(seconds, screen)
        return None if shape is None else self.to_local(shape, point)

    def insets(
        self, shape: BlockShape, *, inset_ratio: float, inset_cap: float, screen: Screen
    ) -> tuple[float, float]:
        """局部系里 inset 的两个分量（``TryGetBlockTouchHalfSize`` 0x1d73d08）::

            v = inset_ratio × max(screenHeight, 0)              # 世界单位
            half.x = 0.5 ± min(v / |lossyScale.x|, inset_cap)   # 局部单位
            half.y = 0.5 ± min(v / |lossyScale.y|, inset_cap)

        * ``screenHeight`` 是**场景的世界高度**（``PreviewBlockControl.screenHeight``：
          ``AnchorToWorld`` 用它把百分比换成世界坐标），虚屏上就是 ``screen.height``；
        * ``lossyScale`` 是块的**当前**世界尺寸，也就是 ``shape.width`` / ``shape.height``
          —— 键帧缩放算完之后的值。**不是** ``base_span``：谱面里带 scale 键帧的块，
          尺寸会变，inset 也跟着变（早期版本按基础的 ``Δp`` 算，那样只在倍率为 1 时对）；
        * 两个方向共用同一个分子，宽高比是通过 ``|lossyScale.x|`` 自己进去的，不用另补；
        * 上限是**局部**单位，且游戏写的是 ``if (v >= cap) v = cap``，与 ``min`` 等价。
        """
        world = inset_ratio * max(screen.height, 0.0)
        span_x, span_y = abs(shape.width), abs(shape.height)
        horizontal = world / span_x if span_x >= 1e-4 else math.inf
        vertical = world / span_y if span_y >= 1e-4 else math.inf
        return (min(horizontal, inset_cap), min(vertical, inset_cap))

    def contains_original(self, point: Position, seconds: float, screen: Screen) -> bool:
        """``IsPositionInsideOriginalBlock``：原矩形（不看 inset），``|local| <= 0.5``。"""
        local = self.local(point, seconds, screen)
        return local is not None and abs(local.real) <= 0.5 and abs(local.imag) <= 0.5

    def contains_touch(
        self,
        point: Position,
        seconds: float,
        screen: Screen,
        *,
        inset_ratio: float,
        inset_cap: float,
    ) -> bool:
        """``IsPositionInsideBlock``：把 ``±0.5`` 按 ``isSubtract`` 往外扩或往里收一圈 inset。"""
        shape = self.shape(seconds, screen)
        if shape is None:
            return False
        local = self.to_local(shape, point)
        if local is None:
            return False
        inset_x, inset_y = self.insets(
            shape, inset_ratio=inset_ratio, inset_cap=inset_cap, screen=screen
        )
        sign = 1.0 if self.subtract else -1.0
        half_x, half_y = 0.5 + sign * inset_x, 0.5 + sign * inset_y
        return abs(local.real) <= half_x and abs(local.imag) <= half_y

    # ---- 镜像 ----

    def mirrored(self) -> BlockArea:
        """``GameInformation::BlockArea::Mirror``（0x1cbe3e4）：**只翻 x**。

        矩形：两个角的 x 互换并取 ``1 − x``；rotate：``anchor.x → 1 − anchor.x`` 且角度取反；
        move：``endPosition.x → 1 − endPosition.x``；scale：**只翻 anchor.x**（倍率不动）。
        每一条都照抄 —— 尤其 ``scale`` 那条看着别扭，但游戏就是这么写的。
        """
        return BlockArea(
            top_right=(1.0 - self.bottom_left[0], self.top_right[1]),
            bottom_left=(1.0 - self.top_right[0], self.bottom_left[1]),
            appear=self.appear,
            enable=self.enable,
            disable=self.disable,
            disappear=self.disappear,
            subtract=self.subtract,
            moves=tuple(MoveKey(k.seconds, 1.0 - k.x, k.y, k.ease_x, k.ease_y) for k in self.moves),
            scales=tuple(
                ScaleKey(k.seconds, k.x, k.y, 1.0 - k.anchor_x, k.anchor_y, k.ease_x, k.ease_y)
                for k in self.scales
            ),
            rotates=tuple(
                RotateKey(k.seconds, -k.degrees, 1.0 - k.anchor_x, k.anchor_y, k.ease)
                for k in self.rotates
            ),
        )


# --------------------------------------------------------------- 一整张谱面的噪域


INSET_RATIO = 0.05
"""``JudgeControl.blockTouchInsetScreenHeightRatio`` = ``0x3D4CCCCD`` = ``0.05f``。

出处是**构造函数**（``JudgeControl::JudgeControl`` @ ``0x1d73e88``，``_ZN12JudgeControlC1Ev``）里的一句
8 字节写：

.. code-block:: c

    *(_QWORD *)&this->blockTouchInsetScreenHeightRatio = 0x3E8000003D4CCCCDLL;

两个字段在类里紧挨着，编译器把两次 4 字节写合并成了一次 8 字节写 —— 所以第二个字段
在反编译里**看不到自己的赋值**：低 32 位 ``0x3D4CCCCD`` 落在 ``blockTouchInsetScreenHeightRatio``
（0.05），高 32 位 ``0x3E800000`` 落在紧跟其后的 :data:`INSET_CAP`（0.25）。想在 IDA 里核对，
就去 ``JudgeControl::JudgeControl`` 那一句；想核对字段顺序，就看 ``TryGetBlockTouchHalfSize``
里两个 ``this->`` 的取用顺序（同一台机器上编译出来的偏移是固定的）。
"""

INSET_CAP = 0.25
"""``JudgeControl.maxBlockTouchInsetLocal`` = ``0x3E800000`` = ``0.25f``（局部单位，1.0 = 一整个块）。

于是 ``half = 0.5 ± min(0.05 × screenHeight / |lossyScale|, 0.25)`` —— 命中矩形最窄收到
一半（``0.25``），最宽撑到 ``1.5`` 倍；`0.05 × 屏高` 是**世界**单位下的余量，除以块的
当前世界尺寸才换成局部单位，所以屏高在 5 个单位以上的块都被这个上限压住。
"""


@dataclass(frozen=True, slots=True)
class NoiseField:
    """一张谱面上的全部噪域，外加那两个 inset 参数（它们是关卡场景的，不是谱面的）。"""

    blocks: tuple[BlockArea, ...] = ()
    screen: Screen | None = None
    inset_ratio: float = INSET_RATIO
    inset_cap: float = INSET_CAP

    def __bool__(self) -> bool:
        return bool(self.blocks)

    @property
    def _screen(self) -> Screen:
        if self.screen is None:
            raise ValueError("NoiseField 缺 screen：噪域的百分比要落到某块虚拟屏上")
        return self.screen

    @property
    def span(self) -> tuple[float, float]:
        """所有噪域在时间上覆盖的范围（``appear`` 到 ``disappear``）；没有块就是 ``(0, 0)``。"""
        if not self.blocks:
            return (0.0, 0.0)
        return (
            min(block.appear for block in self.blocks),
            max(block.disappear for block in self.blocks),
        )

    @property
    def active_windows(self) -> tuple[tuple[float, float], ...]:
        """``[enable, disable)`` 的并集（合并相邻/重叠），给"只在有噪域的时段里采样"用。"""
        windows = sorted((block.enable, block.disable) for block in self.blocks)
        merged: list[tuple[float, float]] = []
        for begin, end in windows:
            if merged and begin <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], end))
            else:
                merged.append((begin, end))
        return tuple(merged)

    def blocking(self, point: Position, seconds: float) -> BlockArea | None:
        """``TryGetBlockingBlock``（0x1d735d8）：这一点在这一刻会不会被噪域吃掉。

        游戏的原样逻辑（**照抄，不解释**：它看着像"奇偶填充"，但两套矩形各算各的）：

        * 对每块**生效中**的块：``insideOriginal``（原矩形）累加 ``v23``（isSubtract 的真值）
          与 ``v24``（"有非 subtract 块"）；``insideTouch``（inset 后）数 ``v22`` 与记录
          第一个 subtract 块 ``v53`` / 第一个非 subtract 块 ``v25``；
        * ``(v24 ^ v23) & 1 == 0`` → 不吃；
        * ``(v22 & 1) == (v25 != null)`` → 不吃；
        * 否则返回 ``v25 ?? v53``。
        """
        screen = self._screen
        original_subtract = 0
        original_normal = 0
        touch_subtract = 0
        first_subtract: BlockArea | None = None
        first_normal: BlockArea | None = None

        for block in self.blocks:
            if not block.is_active(seconds):
                continue
            if block.contains_original(point, seconds, screen):
                if block.subtract:
                    original_subtract += 1
                else:
                    original_normal |= 1
            if block.contains_touch(
                point,
                seconds,
                screen,
                inset_ratio=self.inset_ratio,
                inset_cap=self.inset_cap,
            ):
                if block.subtract:
                    touch_subtract += 1
                    if first_subtract is None:
                        first_subtract = block
                elif first_normal is None:
                    first_normal = block

        if ((original_normal ^ original_subtract) & 1) == 0:
            return None
        if (touch_subtract & 1) == (1 if first_normal is not None else 0):
            return None
        return first_normal if first_normal is not None else first_subtract

    def blocks_at(self, point: Position, seconds: float) -> bool:
        """:meth:`blocking` 的布尔版（调用方只关心"废没废"时用它）。"""
        return self.blocking(point, seconds) is not None

    def blocking_area(self, seconds: float) -> Polygon | None:
        """这一时刻**真会被吃掉**的区域（:meth:`blocking` 的集合版）；没有就是 ``None``。

        逐点的两道奇偶门可以一字不差地写成集合运算 —— 把"命中某类矩形的块数"换成"奇覆盖"
        （被奇数个矩形盖住 = 逐个 ``symmetric_difference``）：

        .. code-block:: text

            gate1 = A Δ P      A = 非 subtract 块的**原矩形**并集
                               P = subtract 块原矩形的**奇覆盖**
            gate2 = N Δ Q      N = 非 subtract 块的 **inset 矩形**并集
                               Q = subtract 块 inset 矩形的奇覆盖
            命中  = gate1 ∩ gate2

        对得上 :meth:`blocking` 的逐点结果（``tests/noise.py`` 逐格对拍过），所以规划器可以拿它
        当**精确**禁区用 —— 不必再退回"所有块的原矩形并集"那种保守近似：那种近似在
        **subtract 块盖住全屏**的谱面（Ametrine）上会退化成"全屏都危险"，把能玩的 drag 判成
        "整块在噪域里"。

        窗口与余量不是这里的事：这里只回答"这一刻"，"手指要待多久、留多少安全边"是各规划器
        自己的策略。
        """
        screen = self._screen
        normal_original: list[Polygon] = []
        subtract_original: list[Polygon] = []
        normal_touch: list[Polygon] = []
        subtract_touch: list[Polygon] = []
        for block in self.blocks:
            if not block.is_active(seconds):
                continue
            shape = block.shape(seconds, screen)
            if shape is None or shape.width <= 0.0 or shape.height <= 0.0:
                continue
            inset_x, inset_y = block.insets(
                shape, inset_ratio=self.inset_ratio, inset_cap=self.inset_cap, screen=screen
            )
            sign = 1.0 if block.subtract else -1.0
            half_x, half_y = 0.5 + sign * inset_x, 0.5 + sign * inset_y
            if half_x <= 0.0 or half_y <= 0.0:
                continue
            original = _rect(shape, 0.5, 0.5)
            touch = _rect(shape, half_x, half_y)
            if block.subtract:
                subtract_original.append(original)
                subtract_touch.append(touch)
            else:
                normal_original.append(original)
                normal_touch.append(touch)

        if not normal_original and not subtract_original:
            return None
        gate_one = _union(normal_original).symmetric_difference(_odd_cover(subtract_original))
        gate_two = _union(normal_touch).symmetric_difference(_odd_cover(subtract_touch))
        return gate_one.intersection(gate_two)

    def hits(self, point: Position, lo: float, hi: float, *, step: float = 1 / 60) -> float | None:
        """一个**不动的**点从 ``lo`` 到 ``hi`` 之间第一次被吃掉的时刻（秒）；没有就是 ``None``。

        与裁判用的是同一个 :meth:`blocking`（精确的奇偶 + inset），所以"这根手指会不会死"
        问得准；只是按 ``step`` 采样 —— 与游戏逐帧问一次的粒度同级。
        """
        if not self.blocks or hi < lo:
            return None
        count = int((hi - lo) // step) + 1
        for index in range(max(0, count) + 1):
            seconds = lo + index * step
            if seconds > hi:
                seconds = hi
            if self.blocking(point, seconds) is not None:
                return seconds
            if seconds >= hi:
                break
        return None

    def active_near(self, lo: float, hi: float) -> bool:
        """``[lo, hi]`` 里有没有任何一块噪域可能生效（便宜的前置判断）。"""
        return any(
            block.enable < hi and block.disable > lo for block in self.blocks
        )

    def mirrored(self) -> NoiseField:
        return NoiseField(
            blocks=tuple(block.mirrored() for block in self.blocks),
            screen=self.screen,
            inset_ratio=self.inset_ratio,
            inset_cap=self.inset_cap,
        )

    # ----------------------------------------------------------- 给规划器用

def exposure(result: Any, field: NoiseField, *, step_ms: float = 8.0) -> dict[int, list[tuple[int, int]]]:
    """这份规划里每根手指**被噪域吃掉**的区间（指针 → ``[[开始, 抬起)`` 毫秒]）。

    与裁判共用同一个 :meth:`NoiseField.hits`：两次触控事件之间手指是不动的（游戏也是这么
    读的），所以按事件分段问"这一段里第一次被吃是什么时候"就够 —— 比按帧扫更准，也不必猜
    帧相位。没抬起就一直算到这一份事件的结尾。
    """
    if not field or not field.blocks or not result.frames:
        return {}
    items: dict[int, list[tuple[int, Any, Position]]] = {}
    for timestamp, batch in result.frames:
        for event in batch:
            items.setdefault(event.pointer, []).append(
                (timestamp, event.action, complex(event.x, event.y))
            )

    dead: dict[int, list[tuple[int, int]]] = {}
    for pointer, events in items.items():
        intervals: list[tuple[int, int]] = []
        death: int | None = None
        for index, (timestamp, action, position) in enumerate(events):
            if action is Touch.DOWN:
                death = None
            if death is None and action is not Touch.UP and index + 1 < len(events):
                following = events[index + 1][0]
                if following > timestamp:
                    hit = field.hits(
                        position, timestamp / 1000.0, following / 1000.0, step=step_ms / 1000.0
                    )
                    if hit is not None:
                        death = int(hit * 1000.0)
            if action is Touch.UP:
                if death is not None:
                    intervals.append((death, timestamp))
                death = None
        if death is not None:
            intervals.append((death, events[-1][0]))
        if intervals:
            dead[pointer] = intervals
    return dead


# --------------------------------------------------- 集合版的两道门（:meth:`NoiseField.blocking_area` 用）

EMPTY = Polygon()
"""空区域（shapely 的空多边形）—— 对称差/求交的零元。"""


def _rect(shape: BlockShape, half_x: float, half_y: float) -> Polygon:
    """块的矩形：局部半宽/半高（相对世界尺寸的倍数）→ 世界多边形。

    与游戏同一套旋转（``_corners`` 那种，正角度逆时针），``half = 0.5`` 是原矩形、
    ``0.5 ± inset`` 是 inset 矩形。
    """
    radians = math.radians(shape.degrees)
    cos, sin = math.cos(radians), math.sin(radians)
    points = []
    for sign_x, sign_y in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
        local_x = sign_x * half_x * shape.width
        local_y = sign_y * half_y * shape.height
        points.append(
            (
                shape.centre.real + local_x * cos - local_y * sin,
                shape.centre.imag + local_x * sin + local_y * cos,
            )
        )
    return Polygon(points)


def _union(rects: list[Polygon]) -> Polygon:
    return unary_union(rects) if rects else EMPTY


def _odd_cover(rects: list[Polygon]) -> Polygon:
    """**奇覆盖**：被奇数个矩形盖住的区域 —— 逐个 ``symmetric_difference`` 就是它。

    两个矩形重叠时重叠部分被减掉（偶数层），三个重叠时又算上（奇数层）—— 正好是游戏里
    ``v23 += isSubtract`` / ``v22 += isSubtract`` 累加后取 ``& 1`` 的那件事。
    """
    cover = EMPTY
    for item in rects:
        cover = cover.symmetric_difference(item)
    return cover
