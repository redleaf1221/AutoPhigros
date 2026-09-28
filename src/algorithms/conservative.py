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
    Vector,
    build_options,
)


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


@dataclass(slots=True)
class _Record:
    pointer: int
    position: Position
    used_at: int
    """这根指针最后一次被摆放的时刻。抬起事件总是排在它**尽可能晚**的位置。"""


class PointerPool:
    """几押就备几根指针：用完不立刻抬起，先"晾"在屏幕上（``released``）等下一个 note 征用
    —— 复用一根已经在屏幕上的手指只要一条 MOVE，比先 UP 再 DOWN 更省事。规矩是别在手指
    还没被游戏看见时就挪走它（游戏逐帧读位置，见 :data:`~algorithms.utils.MIN_DWELL_MS`）。"""

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
        """挑一根回收来的手指：优先挑**已经摆够一帧**的（见 :data:`~algorithms.utils.MIN_DWELL_MS`），
        刚摆下去就被征走等于把它在原来那个位置的覆盖砍到不足一帧；都没摆够就按距离挑最近的。"""
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
        """这一帧要补发的抬起事件排在哪个时刻：压在这一帧的前一毫秒（再不发就赶不上重新按下），
        早抬一毫秒就等于把上一个位置的覆盖砍掉一毫秒，而游戏是逐帧读位置的。"""
        return self.now - 1


class ConservativePlanner:
    name = "conservative"

    def __init__(self, options: Mapping[str, Any] | None = None) -> None:
        self.config = build_options(ConservativeConfig, options)

    def plan(self, chart: Chart, progress: Progress) -> PlanResult:
        config = self.config
        screen = chart.screen
        warnings: list[str] = []
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
            },
            warnings=warnings,
        )
        return result

    # ------------------------------------------------------------------ 内部

    def _emit(
        self,
        track: EventTrack,
        timestamp: int,
        note: SemiNote,
        pool: PointerPool,
    ) -> None:
        match note.kind:
            case SemiKind.TAP:
                pointer, _ = pool.alloc(note)
                track.push(timestamp, note.position, Touch.DOWN, pointer)
                pool.release(note)
            case SemiKind.DRAG:
                # drag 不需要新的按下，正在屏幕上的手指移过去就行
                pointer, fresh = pool.alloc(note, fresh=False)
                track.push(
                    timestamp, note.position, Touch.DOWN if fresh else Touch.MOVE, pointer
                )
                pool.release(note)
            case SemiKind.FLICK_HEAD:
                pointer, fresh = pool.alloc(note, fresh=False)
                track.push(
                    timestamp, note.position, Touch.DOWN if fresh else Touch.MOVE, pointer
                )
            case SemiKind.HOLD_HEAD:
                pointer, _ = pool.alloc(note)
                track.push(timestamp, note.position, Touch.DOWN, pointer)
            case SemiKind.FLICK | SemiKind.HOLD:
                pointer, _ = pool.alloc(note)
                track.push(timestamp, note.position, Touch.MOVE, pointer)
            case SemiKind.FLICK_TAIL | SemiKind.HOLD_TAIL:
                pointer, _ = pool.alloc(note)
                track.push(timestamp, note.position, Touch.MOVE, pointer)
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
                            SemiNote(SemiKind.TAP, screen.remap(position, rotation), note_id)
                        )
                    case NoteType.DRAG:
                        frames[timestamp].append(
                            SemiNote(SemiKind.DRAG, screen.remap(position, rotation), note_id)
                        )
                    case NoteType.FLICK:
                        # 每一下都从 +半径跳回再划到 −半径，那个跳变就是一次"新起手"（见 flick_repeats）
                        first = -(config.flick_repeats * duration) // 2
                        for repeat in range(config.flick_repeats):
                            base = timestamp + first + repeat * duration
                            kind = SemiKind.FLICK_HEAD if repeat == 0 else SemiKind.FLICK
                            frames[base].append(
                                SemiNote(
                                    kind,
                                    _sway(screen, position, rotation, sway, 0, 0, duration),
                                    note_id,
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
                                    )
                                )
                    case NoteType.HOLD:
                        frames[timestamp].append(
                            SemiNote(SemiKind.HOLD_HEAD, screen.remap(position, rotation), note_id)
                        )
                        hold_ms = math.ceil(note.hold * 1000)
                        for offset in range(1, hold_ms, config.sample_delay):
                            moment = (timestamp + offset) / 1000
                            frames[timestamp + offset].append(
                                SemiNote(
                                    SemiKind.HOLD,
                                    screen.remap(
                                        line.point_at(moment, note.offset), line.rotation_at(moment)
                                    ),
                                    note_id,
                                )
                            )
                        end = (timestamp + hold_ms) / 1000
                        frames[timestamp + hold_ms].append(
                            SemiNote(
                                SemiKind.HOLD_TAIL,
                                screen.remap(line.point_at(end, note.offset), line.rotation_at(end)),
                                note_id,
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
    """滑键手势在**这一段划动**第 ``offset`` 毫秒时的触点：``rate`` 从 +1 线性走到 −1，即沿
    ``sway`` 从 +半径划到 −半径；下一段又从 +1 开始，那个跳变就是游戏要找的"新起手"。"""
    rate = 1 - 2 * (offset - flick_start) / duration
    return screen.remap(position + rotation * sway * screen.flick_radius * rate, rotation)


def _warn(warnings: list[str], message: str) -> None:
    if len(warnings) < WARNING_LIMIT:
        warnings.append(message)


PLANNER = ConservativePlanner
