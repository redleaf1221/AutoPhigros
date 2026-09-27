"""保守算法（吸收自 phisap 的"保守算法"）。

思路：把每个 note 当作不可分割的整体，需要几押就分配几根手指；flick 与 hold 被拆成
"起始 + 若干中间采样 + 结束"，同一根手指从头按到尾。宁可多占手指，也绝不合并不同
note 的判定区 —— 所以它的结果最稳，代价是谱面难到十指不够时会直接失败。

与 phisap 版本的差别：指针数量由谱面实际需要的最大押数决定（而不是写死十根），
进度与告警改为通过注入的 ``Progress`` / ``PlanResult.warnings`` 汇报，规划器自身不打印。
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from enum import IntEnum
from typing import Iterator, NamedTuple

from .chart import Chart, NoteType
from .geometry import Screen, place_note
from .track import EventTrack
from .utils import (
    MIN_DWELL_MS,
    PlanResult,
    PlanningError,
    Position,
    Progress,
    Touch,
    Vector,
)

WARNING_LIMIT = 20


@dataclass(slots=True)
class ConservativeConfig:
    flick_start: int = -17
    """滑键手势相对判定时刻的起点偏移（毫秒，负值表示提前起手）。"""
    flick_end: int = 17
    """滑键手势的终点偏移（毫秒）。"""
    sample_delay: int = 8
    """手势中间采样间隔（毫秒），8ms = 125Hz。

    别往小里调。这个数直接决定**发到设备上的事件数**：hold 全程、flick 全程都按它采样，
    而每一个采样到了设备上都是一次完整的输入事件（INJECT → 输入分发 → 应用输入队列）。
    取 1ms 时实测密集段每毫秒一个事件、平均 362 个/秒，足以把 adb/scrcpy 那条注入链和
    游戏主线程一起拖住 —— 主线程一停，游戏时钟的采样就断，触控跟着停，恢复时
    ``nowTime`` 按音频往前跳一大截，整个时间轴就和谱面错开了。

    8ms 不是拍脑袋：``geometric`` 一直用 8ms 的 tick，过的是同一个覆盖率自检，
    48 个事件/秒就把 393 个音符全覆盖了。设备那边一帧最多消费一个位置，比 125Hz 更密
    只是白灌。
    """
    flick_direction: int = 0
    """0 = 垂直于判定线滑动，1 = 平行于判定线滑动。"""
    flick_repeats: int = 2
    """一次滑键手势重复划几下。**别调回 1。**

    游戏判 flick 用的是 ``JudgeControl::CheckFlick``（``0x1d21828``）：只有手指那一帧带
    ``Fingers.isNewFlick``（一次"新起手"，由 ``FingerManagement::Update`` 按手指的瞬时
    速度算出来，阈值 ``flickJudgeSpeed`` 在 ``Start`` 里按 dpi 归一）时它才跑；跑的时候在
    ``nowTime ± 0.14s`` 里挑**时刻最早**的那个还没判过的 flick，只要
    ``|positionX − fingerPositionX| < 2.1`` 就把它标记成已划中。也就是说：**一次"新起手"
    只能点亮一个音符**，而且会被窗口里更早的音符抢走。

    原先每个 flick 只划一下（一个来回），于是那个音符的命全押在**唯一那一次**起手上：
    实测 70 个 flick 里随机漏掉一个，每次漏的还不一样（抢不抢得着取决于那一帧落在哪儿）。
    重复两下就是给每个音符两次独立的机会：前面被抢了，后面还有。

    取 2 是量出来的，不是拍的：离线按游戏那套规则模拟"哪些 flick 会被点亮"，只划一下时
    Dlyrotz HD 上有 **7 个 flick 永远点不亮**（`CheckFlick` 的窗口被更早的音符抢走），
    划两下之后**所有谱面都归零**。三下能再多一点余量，但峰值注入率从 167 涨到 197 个/秒
    （Eradication Catastrophe IN），而注入链的承受力正是这条链上最紧的一环 —— 划两下够用，
    真碰上偶发漏音再往上调。
    """
    max_pointers: int = 10


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

    def __init__(self, config: ConservativeConfig | None = None) -> None:
        self.config = config or ConservativeConfig()

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
