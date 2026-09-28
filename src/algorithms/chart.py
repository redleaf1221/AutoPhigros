"""官谱模型与解析：只保留规划需要的三样 —— 音符**什么时候**被判定（``time`` 与判定线的
``bpm``）、在判定线上的**横向偏移**（``positionX``）、判定线在任意时刻的**位置与朝向**。

坐标：``LevelControl::SetInformation`` 折出 ``world = (raw − 0.5) × 10``（横向再乘
``A = min(宽高比, 16/9)``），``JudgeLineControl::UpdateInfo`` 把它与角度**原样**写进
``localPosition`` / ``Quaternion::AngleAxis``（不取反、不翻转），虚拟屏上是干净线性的映射。
"""

from __future__ import annotations

import bisect
import cmath
import json
import math
from enum import IntEnum
from typing import Any, NamedTuple

from .geometry import Screen
from .utils import Position, Vector

SECONDS_PER_TICK = 1.875
"""``time`` 字段的单位是 1/32 拍：32 个 tick 为一拍，而一分钟是 60 秒，
所以一个 tick 的秒数是 ``60 / 32 = 1.875``，即 ``time * 1.875 / bpm``。"""

NOTE_X_SCALE = 0.9
"""``positionX``（沿判定线的世界单位）到虚拟屏单位的缩放：``16 / (10 * 16/9) = 0.9``。"""

OFFICIAL_SCREEN = Screen(16.0, 9.0)
"""官谱（``formatVersion >= 2``）的虚拟屏幕尺寸。"""

_LEGACY_WIDTH = 880.0
_LEGACY_HEIGHT = 520.0


class ChartFormatError(ValueError):
    """谱面文本不符合官谱格式。"""


class NoteType(IntEnum):
    """官谱里 ``note.type`` 的取值。"""

    TAP = 1
    DRAG = 2
    HOLD = 3
    FLICK = 4


class Note(NamedTuple):
    kind: NoteType
    seconds: float
    """判定时刻，单位为秒，原点是谱面时间轴原点（不含 offset）。"""
    hold: float
    """按住时长，单位为秒；只有 HOLD 非零。"""
    offset: float
    """判定线上的横向偏移量。"""
    above: bool = True
    """在判定线上面（``notesAbove``）还是下面（``notesBelow``）。音符身份的一半：游戏编号是
    ``线号 × 1000000 + （上=0 / 下=100000） + 该侧第几个 × 10``，"第几个"按**同一侧**数。"""


class Segment(NamedTuple):
    start: float
    end: float
    begin: Any
    finish: Any


def _segment_start(segment: Segment) -> float:
    return segment.start


class Track:
    """分段线性插值的参数轨道：一条事件描述"从 ``startTime`` 到 ``endTime``，值从 ``start``
    线性变到 ``end``"。查询取 start 最晚且不晚于查询时刻的那一段，也就是后写入的覆盖先前的。"""

    __slots__ = ("segments",)

    def __init__(self) -> None:
        self.segments: list[Segment] = []

    def cut(self, start: float, end: float, begin: Any, finish: Any) -> None:
        bisect.insort_left(self.segments, Segment(start, end, begin, finish), key=_segment_start)

    def at(self, seconds: float) -> Any:
        segments = self.segments
        if not segments:
            return 0j

        right = bisect.bisect_left(segments, seconds, key=_segment_start)
        if right < len(segments) and math.isclose(segments[right].start, seconds):
            return segments[right].begin
        if right == 0:
            # 查询时刻早于第一条事件：判定线保持首条事件起点处的值
            return segments[0].begin

        segment = segments[right - 1]
        span = segment.end - segment.start
        ratio = 0.0 if span == 0 else (seconds - segment.start) / span
        return segment.begin + (segment.finish - segment.begin) * ratio

    def __len__(self) -> int:
        return len(self.segments)

    def __repr__(self) -> str:
        return f"Track({len(self.segments)} segments)"


class JudgeLine:
    __slots__ = ("bpm", "notes", "move", "rotate")

    def __init__(self, bpm: float, notes: list[Note], move: Track, rotate: Track) -> None:
        self.bpm = bpm
        self.notes = notes
        self.move = move
        self.rotate = rotate

    @property
    def beat_seconds(self) -> float:
        return SECONDS_PER_TICK / self.bpm

    def position_at(self, seconds: float) -> Position:
        return self.move.at(seconds)

    def rotation_at(self, seconds: float) -> Vector:
        return cmath.exp(self.rotate.at(seconds) * 1j)

    def point_at(self, seconds: float, offset: float) -> Position:
        """判定线上某个横向偏移处、某一时刻的位置。"""
        return self.position_at(seconds) + self.rotation_at(seconds) * offset


class Chart:
    __slots__ = ("format_version", "offset", "screen", "lines")

    def __init__(
        self, format_version: int, offset: float, screen: Screen, lines: list[JudgeLine]
    ) -> None:
        self.format_version = format_version
        self.offset = offset
        self.screen = screen
        self.lines = lines

    @property
    def note_count(self) -> int:
        return sum(len(line.notes) for line in self.lines)

    @classmethod
    def parse(cls, text: str) -> Chart:
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as error:
            raise ChartFormatError(f"不是合法 JSON：{error}") from error
        if not isinstance(raw, dict):
            raise ChartFormatError("谱面根节点不是对象")

        raw_lines = raw.get("judgeLineList")
        if not isinstance(raw_lines, list):
            raise ChartFormatError("缺少 judgeLineList，这不是官谱")

        version = int(raw.get("formatVersion") or 0)
        offset = float(raw.get("offset") or 0.0)
        lines = [_parse_judge_line(entry, version) for entry in raw_lines]
        return cls(version, offset, OFFICIAL_SCREEN, lines)


def _parse_judge_line(raw: Any, version: int) -> JudgeLine:
    if not isinstance(raw, dict):
        raise ChartFormatError("judgeLineList 的元素不是对象")

    bpm = float(raw.get("bpm") or 0.0)
    if bpm <= 0:
        raise ChartFormatError(f"判定线 bpm 非法：{bpm}")
    beat = SECONDS_PER_TICK / bpm

    rotate = Track()
    for event in raw.get("judgeLineRotateEvents") or ():
        # 官谱的旋转角以度为单位，逆时针为正；世界与虚拟屏的 y 都朝上，直接拿来用
        rotate.cut(
            float(event["startTime"]) * beat,
            float(event["endTime"]) * beat,
            math.radians(float(event["start"])),
            math.radians(float(event["end"])),
        )

    move = Track()
    for event in raw.get("judgeLineMoveEvents") or ():
        start_time = float(event["startTime"]) * beat
        end_time = float(event["endTime"]) * beat
        if version <= 1:
            begin = _legacy_position(float(event["start"]))
            finish = _legacy_position(float(event["end"]))
        else:
            if "start2" not in event or "end2" not in event:
                raise ChartFormatError("formatVersion >= 2 的移动事件缺少 start2 / end2")
            begin = _position(float(event["start"]), float(event["start2"]))
            finish = _position(float(event["end"]), float(event["end2"]))
        move.cut(start_time, end_time, begin, finish)

    notes = [
        _parse_note(entry, beat, above=above)
        for above, entries in ((True, raw.get("notesAbove") or ()), (False, raw.get("notesBelow") or ()))
        for entry in entries
    ]
    return JudgeLine(bpm, notes, move, rotate)


def _parse_note(raw: Any, beat: float, *, above: bool = True) -> Note:
    try:
        kind = NoteType(int(raw["type"]))
    except (KeyError, ValueError, TypeError) as error:
        raise ChartFormatError(f"未知的音符类型：{raw!r}") from error
    return Note(
        kind=kind,
        seconds=float(raw["time"]) * beat,
        hold=float(raw.get("holdTime") or 0.0) * beat,
        offset=float(raw["positionX"]) * NOTE_X_SCALE,
        above=above,
    )


def _position(x: float, y: float) -> Position:
    """官谱 v2+ 的移动事件：``start``/``end`` 是 0~1 的横向比例（0 = 左边缘，1 = 右边缘），
    ``start2``/``end2`` 是 0~1 的纵向比例（**0 = 下边缘，1 = 上边缘**）；换算到虚拟屏就是
    干干净净的 ``(16x, 9y)``。"""
    return Position(x * OFFICIAL_SCREEN.width, y * OFFICIAL_SCREEN.height)


def _legacy_position(value: float) -> Position:
    """官谱 v1 的移动事件：一个整数里打包了 880x520 坐标系下的 x 与 y —— 游戏侧对应
    ``world_x = (x//1000 - 440)/440*5*A``、``world_y = (y%1000 - 260)/52``，折到虚拟屏
    同样是线性的 ``x/880*16`` 与 ``y/520*9``，都不翻转。"""
    return Position(
        (value // 1000) / _LEGACY_WIDTH * OFFICIAL_SCREEN.width,
        (value % 1000) / _LEGACY_HEIGHT * OFFICIAL_SCREEN.height,
    )
