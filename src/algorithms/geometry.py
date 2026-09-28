"""几何：虚拟屏幕、音符摆位、判定区。

**垂直判定**：``JudgeControl::GetFingerPosition`` 只为每根手指、每条判定线算横向与法向
两个量，而 ``JudgeControl::CheckNote`` 只比横向的 ``touchPos >= 1.9``（横向偏得越多，
时间窗收得越紧），法向分量从没用过 —— 判定线"无限细"，触点离它多远都无所谓。于是把屏幕
外的音符沿**垂直于判定线**的方向拉回屏幕是安全的（横向分量不变），按判定区窄带的重心按
下去也一样。平面几何一律交给 shapely，本模块不手写求交与裁剪。
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
    """规划用的虚拟屏幕：官谱 ``formatVersion >= 2`` 的坐标系是 16x9，判定线横向跨度 16，
    音符的 ``positionX`` 乘缩放系数后作为线上的横向偏移量；y 轴向上，设备控制器再把它
    映射到真实分辨率。"""

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
        """把落在屏幕外的点沿垂直于判定线的方向拉回屏幕：过该点作垂线，取它与屏幕边框
        所有交点的重心 —— 垂直判定下沿这条法线平移不改变判定结果。探针长度取
        ``点到屏幕的距离 + 屏幕对角线``；够不着就会静默退回屏幕中心，那等于把音符判死。"""
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
    """算出音符的判定点：在屏幕内直接返回；屏幕外时 ``retime=True``（只对 flick 用）沿
    时间轴前后各找几拍，取第一个让判定点回到屏幕内的时刻（偏移只有一两拍，远小于 Perfect
    窗），否则交给 :meth:`Screen.remap` 拉回屏幕。"""
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
    """按判定线切向切出一条贯穿屏幕的窄带，作为 note 的判定区：同一条线上的音符判定区
    互相平行，只有不同判定线相交时它们的判定区才会真正重叠 —— 那正是几何算法要合并的。"""

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
