"""几何：虚拟屏幕、音符摆位、判定区。

**垂直判定** —— Phigros 的判定特色，本模块的一切都建立在它上面。
``JudgeControl::GetFingerPosition`` 为每根手指、每条判定线只算两个量：判定线局部
坐标下的横向分量与法向分量；而 ``JudgeControl::CheckNote`` 里只把横向分量拿去比
``touchPos >= 1.9``（横向偏得越多，时间窗还收得越紧），法向分量算出来了但从来没用过。
也就是说判定线"无限细"：触点离判定线多远都无所谓，只看它投到线上落在哪儿。

于是：

* 把屏幕外的音符沿**垂直于判定线**的方向拉回屏幕是安全的 —— 横向分量不变；
* 几何算法可以放心按判定区窄带的重心按下去，哪怕重心离判定线很远。

平面几何一律交给 shapely（它的 2.x 接口本身就是 numpy 驱动的：``get_coordinates``
返回 ndarray，求个重心一行就够），本模块不手写直线求交、多边形裁剪之类的东西。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING

import shapely
from shapely.geometry import LinearRing, LineString, Point, Polygon, box

from .utils import Position, Vector

if TYPE_CHECKING:
    from .chart import JudgeLine, Note

JUDGE_AREA_WIDTH_RATIO = 1 / 16
"""判定区宽度相对屏幕宽度的比例（沿用 phisap 的经验值，未经严格验证）。"""


@dataclass(frozen=True, slots=True)
class Screen:
    """规划用的虚拟屏幕。

    官谱 ``formatVersion >= 2`` 的坐标系是 16x9：判定线横向跨度 16，
    音符的 ``positionX`` 乘上缩放系数后，作为判定线上的横向偏移量。
    设备控制器负责再把这块虚拟屏幕映射到真实分辨率。y 轴向上。
    """

    width: float
    height: float

    @property
    def flick_radius(self) -> float:
        """滑键手势的滑动半径，取屏幕宽度的十分之一。"""
        return self.width * 0.1

    @property
    def diagonal(self) -> float:
        return math.hypot(self.width, self.height)

    @property
    def center(self) -> Position:
        return Position(self.width / 2, self.height / 2)

    @property
    def bounds(self) -> Polygon:
        return _screen_box(self.width, self.height)

    def visible(self, point: Position) -> bool:
        return 0 <= point.real <= self.width and 0 <= point.imag <= self.height

    def remap(self, point: Position, rotation: Vector) -> Position:
        """把落在屏幕外的点沿垂直于判定线的方向拉回屏幕。

        过该点作一条垂直于判定线的直线，取它与屏幕边框的所有交点，返回其重心。
        因为垂直判定，沿这条法线平移不改变判定结果 —— 判定线整体跑到屏幕外时，只有
        这么摆才既落在屏幕里、又保持那个横向分量。

        **探针要够长。** 够得着的点可能离得很远：谱面里判定线会在一瞬间整体平移几十个
        单位（`Eradication Catastrophe` IN 的线 10 在 119.250s 从 y=1.8 跳到 y=90），
        那是谱师故意把判定线藏到屏幕外、让音符只靠横向分量判。探针只铺一条对角线的话，
        够不着的时候会**静默退回屏幕中心** —— 而屏幕中心的横向分量跟目标根本不一样，
        等于把这个音符判死（实测偏差 2.1，容差 1.9，必 Miss）。

        长度取 ``点到屏幕的距离 + 屏幕对角线``：交点若存在，它一定在屏幕里，
        设它到屏幕最近点的距离不超过对角线，于是它到该点的距离不超过这个和。
        """
        if self.visible(point):
            return point

        direction = rotation * 1j
        if direction == 0:
            return self.center

        probe_point = Point(point.real, point.imag)
        reach = _screen_box(self.width, self.height).distance(probe_point) + self.diagonal
        probe = LineString(
            [
                (point.real - direction.real * reach, point.imag - direction.imag * reach),
                (point.real + direction.real * reach, point.imag + direction.imag * reach),
            ]
        )
        hits = shapely.get_coordinates(shapely.intersection(probe, _screen_ring(self.width, self.height)))
        if len(hits) == 0:
            return self.center
        return Position(hits[:, 0].mean(), hits[:, 1].mean())


@lru_cache(maxsize=8)
def _screen_box(width: float, height: float) -> Polygon:
    return box(0.0, 0.0, width, height)


@lru_cache(maxsize=8)
def _screen_ring(width: float, height: float) -> LinearRing:
    return _screen_box(width, height).boundary


@dataclass(slots=True)
class Placement:
    """一个音符最终被判定的时刻，以及那一刻它在屏幕上的位置与判定线朝向。"""

    seconds: float
    position: Position
    rotation: Vector
    retimed: bool = False
    remapped: bool = False


def place_note(
    line: JudgeLine,
    note: Note,
    screen: Screen,
    *,
    retime: bool = False,
    max_beat_search: int = 10,
) -> Placement:
    """算出音符的判定点。

    位置在屏幕内时直接返回。落在屏幕外时分两种处理：

    * ``retime=True``（只对 flick 用）：沿时间轴前后各找若干拍，取第一个能让判定点
      回到屏幕内的时间。phisap 用它打补丁修 DESTRUCTION 3,2,1 的最后一个 flick ——
      那个 flick 的判定点在屏幕外，但游戏有"垂直判定"兜底，人类实际是在屏幕中心
      触发的。偏移量只有一两拍（138bpm 下一拍 13.6ms），远小于 Perfect 判定窗。
    * 否则：交给 :meth:`Screen.remap` 拉回屏幕。
    """
    position = line.point_at(note.seconds, note.offset)
    rotation = line.rotation_at(note.seconds)
    if screen.visible(position):
        return Placement(note.seconds, position, rotation)

    if retime:
        beat = line.beat_seconds
        for step in range(1, max_beat_search):
            for sign in (1, -1):
                seconds = note.seconds + step * sign * beat
                candidate = line.point_at(seconds, note.offset)
                if screen.visible(candidate):
                    return Placement(seconds, candidate, line.rotation_at(seconds), retimed=True)

    return Placement(note.seconds, screen.remap(position, rotation), rotation, remapped=True)


class JudgeAreaBuilder:
    """按判定线切向切出一条贯穿屏幕的窄带，作为 note 的判定区。

    一条判定线上的所有音符共用同一个方向，所以它们的判定区是互相平行的长条；
    两条判定线相交时，来自不同判定线的音符判定区才可能真正重叠 ——
    这正是几何算法要合并的东西。
    """

    __slots__ = ("screen", "half_width", "_box")

    def __init__(self, screen: Screen) -> None:
        self.screen = screen
        self.half_width = screen.width * JUDGE_AREA_WIDTH_RATIO / 2
        self._box = screen.bounds

    def band(self, position: Position, rotation: Vector) -> Polygon | None:
        direction = rotation * 1j
        if direction == 0:
            return None
        direction /= abs(direction)

        reach = self.screen.diagonal
        start = position - direction * reach
        end = position + direction * reach
        spine = LineString([(start.real, start.imag), (end.real, end.imag)])
        clipped = spine.buffer(self.half_width, cap_style="flat").intersection(self._box)
        if clipped.is_empty or clipped.area <= 0:
            return None
        return clipped


def centre_of(geometry: Polygon | LineString) -> Position:
    """几何体的重心，作为触点位置。"""
    point = shapely.centroid(geometry)
    return Position(point.x, point.y)
