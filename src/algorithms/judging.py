"""游戏怎么判，以及"把一份规划按规则重放一遍"的裁判。

判定的唯一出处：数字全部来自 ``Phigros4.0-音游内核逆向报告.md``（每条都标出处），
自检、``judge.py`` 与规划器都从这里读，不许各写一份。

单位：几何与 `Note.offset` 都在 16 宽的虚拟屏上，游戏容差是谱面单位（Tap 1.9 / Drag 2.1），
乘 `CHART_TO_SCREEN` 得 1.71 / 1.89；窄窗 `HEAD_WINDOW` 之外、游戏扫描窗之内的命中就是蹭键。
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from enum import IntEnum

from .chart import Chart, Note, NoteType
from .geometry import place_note
from .utils import PlanResult, Touch

# --------------------------------------------------------------- 规则常量

CHART_TO_SCREEN = 0.9
"""谱面单位 → 虚拟屏单位：`CheckNote` 的 1.9 是谱面单位，虚拟屏宽 16，比例 0.9。"""

TAP_TOLERANCE = 1.9 * CHART_TO_SCREEN
"""Tap / Hold 头判的横向容差（`JudgeControl::CheckNote`，报告 §6.2），1.9 是谱面单位。
边界严格：`0x1d21104` 第 189 行是 `if (touchPos >= 1.9) goto skip`，比较必须用 `>=`。"""

EDGE_TOLERANCE = 0.9 * CHART_TO_SCREEN
"""边缘收窄的起点（第 199-201 行）：`badTime = 0.22 + (touchPos − 0.9) × 0.08 × (−0.5)`，
`touchPos = 1.9` 时 0.22 缩到 0.18，且只收紧**早**的那一侧（第 212 行）。"""

DRAG_TOLERANCE = 2.1 * CHART_TO_SCREEN
"""Drag / Flick 的横向容差（`DragControl::Judge` / `CheckFlick`），比 Tap 宽。"""

PERFECT_TIME = 0.08
GOOD_TIME = 0.18
BAD_TIME = 0.22
"""Tap 的三档窗口（报告 §6.7）：|Δ| < 0.08 → Perfect，< 0.18 → Good，否则 Bad。"""

SCAN_BACK = 0.18
SCAN_AHEAD = 0.22
"""判定扫描窗（报告 §6.3）：每帧只看 `realTime ∈ (nowTime − 0.18, nowTime + 0.22)` 的音符，
一次按下能碰到的 Δ = nowTime − realTime ∈ (−0.22, +0.18)：**早 220ms、晚 180ms，不对称**。"""

DRAG_WINDOW = 0.10
"""Drag 的判定窗：`|realTime − nowTime| <= 0.1` 时逐帧比手指（报告 §6.5）。"""

FLICK_WINDOW = 0.14
"""Flick 的候选窗：`CheckFlick` 用 `PerfectTimeRange × 1.75 = ±0.14s` 挑音符（报告 §6.6）。"""

FLICK_MISS = 0.22
"""Flick 迟到这么久还是没被点亮就是 Miss。"""

HOLD_SETTLE_LEAD = 0.22
"""Hold 在 `realTime + holdTime − 0.22` 就结算（报告 §6.6）—— 手指可以**早走 220ms**。"""

METRIC_EPSILON = 0.005
"""挑选度量差多少以内算**平局**（虚拟屏单位）：游戏比的是活 transform 的两个 `float`，本机
用插值出的线位置去比，误差就在千分之几，不设容差会分出假胜负 —— 平局归扫描顺序。"""

PROCESS_DELAY = 0.029
"""送出的触摸由游戏在之后某一帧处理并读 `nowTime`：`logs/2026-09-27_19-49-47.log` 里
1149 条同档判定的中位差 +28.9ms、分布 12~48ms（固定管线 + 0~1 帧量化，帧相位不可知）。"""

DELIVERED_LATENCY = PROCESS_DELAY
"""裁判重放时假设的送达补偿（秒），播放器那边就是 `options.latency`；计划里的落点是音符
自己那一时刻的判定点，所以按"送达准时"重放时它必须正好抵掉 PROCESS_DELAY。"""

HOLD_TIMEOUT = 0.25
"""Hold 的兜底超时：按住结束之后再过 0.25s 还没判就是 Miss。"""

HOLD_GRACE_FRAMES = 3
"""Hold 主体允许连续落空几帧：`_safeFrame = 2`，第 4 帧才判 Miss（报告 §6.6）。"""

HOLD_GRACE_MS = 67.0
"""Hold 主体的宽容（毫秒）：把 `_safeFrame = 2` 的 3 帧按 60fps 折算成 67ms，自检用它而不是
帧数。判据只能是"没有一段超过这个窗口完全落空"，不能要求每一刻都在容差内。"""

FRAME_RATE = 60.0
"""重放用的帧率。Tap / Hold 头判与帧率无关（判定就发生在发 DOWN 的那一帧），
Drag / Flick / Hold 主体要逐帧采样，帧相位是假设（实际是 60 或 2×刷新率）。"""

FRAME_MS = 1000.0 / FRAME_RATE
"""一帧（60fps）有多少毫秒。覆盖率判据里最要紧的一个数：见 `tests/coverage.py`。"""

RUN_STEP_MS = 1.0
"""扫"手指在位"的步长。1ms 的粒度对 16.7ms 的判据来说够细了。"""

HOLD_SAMPLE_MS = 8
"""扫 hold 主体时的步长，与规划器的采样间隔一致。"""

FLICK_JUMP = 1.0
"""多大的位移算一次"手指跳变"（虚拟屏单位）。它取**保守**的那一侧：只有跳变才一定够快、
让 ``FingerManagement::Update`` 置起 ``Fingers.isNewFlick``，所以它报问题才值得看一眼。"""

HEAD_WINDOW = (-0.02, 0.16)
"""规划这一层认为"这一次按下是**为这个音符**按的"的时间窗（与 `tests/coverage.py` 一致）；
落在游戏扫描窗里、却落在这个窄窗外的按下就是蹭键。"""


class Verdict(IntEnum):
    """一档判定。数值顺序即优劣，方便排序与统计。"""

    PERFECT = 0
    GOOD = 1
    BAD = 2
    MISS = 3

    @property
    def label(self) -> str:
        return ("Perfect", "Good", "Bad", "Miss")[int(self)]


def verdict_of(delta: float) -> Verdict:
    """Tap / Hold 头判：早晚量 → 判定（`ClickControl::Judge` 0x1d3060c，报告 §6.6）：
    `|Δ| < 0.08` → Perfect，`< 0.18` → Good，否则 Bad。**没有上界** —— 判档与"能不能
    判到"是两件事，后者由 :func:`tap_window` 管。"""
    if abs(delta) < PERFECT_TIME:
        return Verdict.PERFECT
    if abs(delta) < GOOD_TIME:
        return Verdict.GOOD
    return Verdict.BAD


def tap_window(delta: float, touch_pos: float) -> bool:
    """这一下按下能不能判到某个 Tap / Hold 头（`JudgeControl::CheckNote` 0x1d21104
    第 186-213 行）：横向 `touchPos >= 1.9` 严格跳过；早的那一侧由 `badTime` 兜底、晚的
    那一侧由扫描窗兜底；`minDeltaTime` 每帧被写成 10000.0f，那条早判守卫是死代码。"""
    if touch_pos >= TAP_TOLERANCE / CHART_TO_SCREEN:
        return False
    bad_time = BAD_TIME
    if touch_pos > EDGE_TOLERANCE / CHART_TO_SCREEN:
        bad_time += (touch_pos - EDGE_TOLERANCE / CHART_TO_SCREEN) * PERFECT_TIME * -0.5
    return -bad_time <= delta < SCAN_BACK


def score(perfect: int, good: int, notes: int, max_combo: int) -> tuple[int, float]:
    """分数与完成度（报告 §7.2）：`分数 = 900000 × acc + 100000 × maxCombo / N`，
    `acc = (Perfect + 0.65 × Good) / N`，完成度 = acc × 100。"""
    if notes <= 0:
        return 0, 0.0
    accuracy = (perfect + 0.65 * good) / notes
    total = 900000.0 * accuracy + 100000.0 * max_combo / notes
    return int(round(total)), accuracy * 100.0


# --------------------------------------------------------------- 手指时间轴


class Fingers:
    """把事件流按指针拆开，回答"某时刻哪些手指在屏幕上、在哪"：`down_at(t)` 取每根手指
    在 t 之前最后一次事件的位置，只要那一次不是 UP 就算还按着（手指在两次事件之间不动，
    判定线却在动，所以横向偏差是逐帧漂的）。"""

    def __init__(self, result: PlanResult) -> None:
        raw: dict[int, list[tuple[int, Touch, complex]]] = defaultdict(list)
        self.downs: list[tuple[int, int, complex]] = []
        for timestamp, events in result.frames:
            for event in events:
                position = complex(event.x, event.y)
                raw[event.pointer].append((timestamp, event.action, position))
                if event.action is Touch.DOWN:
                    self.downs.append((timestamp, event.pointer, position))
        self.items = raw
        self.timestamps = {pointer: [item[0] for item in items] for pointer, items in raw.items()}

    def down_at(self, moment: int) -> list[complex]:
        """`moment`（毫秒）那一刻屏幕上的手指位置。"""
        positions: list[complex] = []
        for pointer, items in self.items.items():
            index = bisect_right(self.timestamps[pointer], moment) - 1
            if index < 0:
                continue
            _, action, position = items[index]
            if action is not Touch.UP:
                positions.append(position)
        return positions

    def dwell_ms(self, pointer: int, moment: int) -> int:
        """这根手指在 `moment` 按下去之后待了多久（毫秒）：用来分辨这一次按下是 tap
        （按下即抬，本算法里 8ms）还是 drag / flick 的起手（按住 ≥24ms）—— 两者都会
        被 `CheckNote` 当成一次按下，起手会顺手把够得着的 TAP 判掉。"""
        items = self.items.get(pointer)
        if not items:
            return 0
        index = bisect_right(self.timestamps[pointer], moment) - 1
        for later, action, _ in items[index + 1 :]:
            if action is Touch.UP:
                return later - moment
        return 0

    def downs_between(self, lo_ms: float, hi_ms: float) -> list[tuple[int, complex]]:
        """时间窗里的所有按下（含指针号）。"""
        return [
            (timestamp, position)
            for timestamp, _, position in self.downs
            if lo_ms <= timestamp <= hi_ms
        ]


def deviation(line, note: Note, position: complex, seconds: float) -> float:
    """一根手指 `position` 在 `seconds` 那一刻对 `note` 的横向偏差（虚拟屏单位）：Phigros 的
    垂直判定就是 `(手指 − 判定线原点)·判定线朝向 − note.offset`。**不要绕 `Screen.remap`**
    （判定线跑到屏幕外时它退化成屏幕中心，横向信息就没了），且手指与判定线必须同刻、取精确秒数。"""
    lateral = ((position - line.position_at(seconds)) * line.rotation_at(seconds).conjugate()).real
    return abs(lateral - note.offset)


# --------------------------------------------------------------- 裁判


@dataclass(slots=True)
class Judgement:
    """一个音符的判定结果。"""

    line: int
    note: Note
    seconds: float
    """判定点对应的时刻（flick 可能被 `place_note` 微调过）。"""
    verdict: Verdict
    at: float
    """判定发生的时刻（秒）。Miss 用"游戏会判它的最晚时刻"。"""
    delta: float
    pointer: int
    """哪根手指按的；Miss 是 -1。"""
    above: bool = True
    index: int = 0
    """音符在线上的位置（上面还是下面、第几个）—— 设备的 ``noteCode`` 那套身份，用来把
    每条判定与日志里设备的判定逐音符对上（``judge.py --compare``）。"""
    grazed: bool = False
    """是不是"不是为它按的那一下"判掉的 —— 见 :class:`Graze`。"""

    @property
    def key(self) -> tuple[int, bool, int]:
        return (self.line, self.above, self.index)

    @property
    def kind(self) -> NoteType:
        return self.note.kind


@dataclass(slots=True)
class HoldHead:
    """一个 Hold 的**头判现场**：`HoldControl::Judge`（0x1d32668）里头部只写 `judged` 与
    `isPerfect`、记下 `this->_judgeTime = v8`，**分数到尾部结算才发**、报的是**头判的 Δ**
    （第 303-337 行）；头判之后手指没留住，身体宽限耗尽就判 Miss。"""

    slot: Slot
    at: float
    """头判发生在哪一刻（游戏处理那次按下的时刻）。"""
    delta: float
    """头判的早晚量 —— 收尾报的就是它。"""
    pointer: int
    is_perfect: bool
    aimed: bool
    """这一次按下是不是"计划本来瞄着它"的（不瞄就是蹭键）。"""


@dataclass(slots=True)
class Graze:
    """蹭键：一次**不是为这个音符按下**的动作，却把它判掉了。`stolen` 是分界线：
    True = 计划本来按对了却被这一下先碰掉，音符拿到更差的一档、计划自己那一次白发（纯丢分）；
    False = 计划本来就没按对（覆盖率判据也会报它），蹭键只是让结果更难看。"""

    line: int
    note: Note
    verdict: Verdict
    at: float
    delta: float
    pointer: int
    stolen: bool
    dwell_ms: int = 0
    """抢人的那一下按了多久（毫秒）：tap 是按下即抬（8ms 上下），**drag / flick 的起手**
    要按住一段时间（≥24ms），而两者在 `CheckNote` 眼里都只是一次"按下"。"""

    def text(self) -> str:
        tail = (
            "计划自己那一次按下白发（本来按对了）"
            if self.stolen
            else "计划本来也没按到它（覆盖判据同样会报）"
        )
        if self.dwell_ms >= 24:
            kind = f"drag / flick 的起手（按住 {self.dwell_ms}ms）"
        else:
            kind = f"一次点按（{self.dwell_ms}ms）"
        return (
            f"蹭键：{self.note.kind.name} @ {self.note.seconds:.3f}s（线 {self.line}）"
            f"被判成 {self.verdict.label}（{self.delta * 1000:+.0f}ms，指针 {self.pointer}）"
            f"—— 抢它的是{kind}；{tail}"
        )


@dataclass(slots=True)
class Report:
    """一份规划的体检报告。"""

    plan: str
    chart: str
    notes: int
    judgements: list[Judgement] = field(default_factory=list)
    grazes: list[Graze] = field(default_factory=list)

    @property
    def counts(self) -> dict[Verdict, int]:
        table = dict.fromkeys(Verdict, 0)
        for judgement in self.judgements:
            table[judgement.verdict] += 1
        return table

    @property
    def lost(self) -> list[Judgement]:
        """没被判到的音符（Miss）—— 裁判最该盯的就是这一列。"""
        return [j for j in self.judgements if j.verdict is Verdict.MISS]

    @property
    def max_combo(self) -> int:
        best = run = 0
        for judgement in sorted(self.judgements, key=lambda item: item.at):
            if judgement.verdict in (Verdict.BAD, Verdict.MISS):
                run = 0
            else:
                run += 1
                best = max(best, run)
        return best

    def score(self) -> tuple[int, float]:
        table = self.counts
        return score(table[Verdict.PERFECT], table[Verdict.GOOD], self.notes, self.max_combo)


# --------------------------------------------------------------- 谱面音符表


@dataclass(slots=True)
class Slot:
    """谱面里一个音符的判定现场：在哪条线上、判定点在哪一刻、在哪。

    同一 `(线, 时刻)` 上可能有不止一个音符叠着，所以"判过没有"不能用 `(线, 时刻)` 当身份，
    表里的下标才是身份。
    """

    index: int
    """**表内下标**（`note_table` 里的位置）：只用来判重，不是游戏的编号。"""
    line: int
    geometry: object
    note: Note
    seconds: float
    point: complex
    """判定点在虚拟屏上的位置（`place_note` 算出来的），多押里挑最近的那个要用它。"""
    above: bool = True
    """上/下侧 —— 游戏编号里的那一半。"""
    side_index: int = 0
    """**同侧**的第几个 —— 游戏编号里的另一半（``线 5 上 第 18 个`` 的那个 18）。"""

    @property
    def key(self) -> tuple[int, bool, int]:
        """游戏的音符身份：``(线, 上/下, 同侧第几个)``。"""
        return (self.line, self.above, self.side_index)

    @property
    def kind(self) -> NoteType:
        return self.note.kind


def note_table(chart: Chart) -> list[Slot]:
    """把整张谱摊成一张表，判定点用 `place_note` 算（flick 可能被微调）。"""
    slots: list[Slot] = []
    for line_index, line in enumerate(chart.lines):
        counted: dict[bool, int] = {True: 0, False: 0}
        for note in line.notes:
            placement = place_note(line, note, chart.screen, retime=note.kind is NoteType.FLICK)
            side_index = counted[note.above]
            counted[note.above] = side_index + 1
            slots.append(
                Slot(
                    index=len(slots),
                    line=line_index,
                    geometry=line,
                    note=note,
                    seconds=placement.seconds,
                    point=placement.position,
                    above=note.above,
                    side_index=side_index,
                )
            )
    return slots


def _by_line(slots: list[Slot]) -> list[tuple[list[Slot], list[float]]]:
    """每条线一张**按时间排好**的表（配合 bisect 只查扫描窗里的音符）。

    排序不能省：谱面 JSON 里一条线是 `notesAbove` 接着 `notesBelow`，不按时间排就拿到一堆
    错误区间，判据会报出成片的假丢音。
    """
    grouped: dict[int, list[Slot]] = defaultdict(list)
    for slot in slots:
        grouped[slot.line].append(slot)
    tables: list[tuple[list[Slot], list[float]]] = []
    for line in sorted(grouped):
        ordered = sorted(grouped[line], key=lambda slot: slot.seconds)
        tables.append((ordered, [slot.seconds for slot in ordered]))
    return tables


def scan_order(slots: list[Slot]) -> tuple[list[Slot], list[float]]:
    """**全局**按时间排好的扫描表 —— 谁是"最近的那一个"就是这么定的：游戏用
    `SortForNoteWithFloorPosition` 把音符按时间排成一条表（报告 §6.2），所以度量并列时
    胜出的是**时刻最早**的那个。排序键带上线号与上下，同一 `(线, 时刻)` 上叠着的音符才可复现。"""
    ordered = sorted(slots, key=lambda slot: (slot.seconds, slot.line, slot.above, slot.side_index))
    return ordered, [slot.seconds for slot in ordered]


# --------------------------------------------------------------- 裁判


def normal_offset(line, position: complex, seconds: float) -> float:
    """指尖到判定线的**法向**距离（判定线无限细，它只进"挑最近候选"的度量，不参与判定）。
    出处 `JudgeControl::GetFingerPosition`（报告 §6.3）：它给每根手指算横向与法向两个量，
    `CheckNote` 只用横向判。同一条线上的音符法向距离相同，所以它们必然并列、胜负交给扫描顺序。"""
    local = (position - line.position_at(seconds)) * line.rotation_at(seconds).conjugate()
    return abs(local.imag)


def selection_metric(slot: Slot, position: complex, seconds: float) -> float:
    """游戏在多押里挑"最近候选"用的度量（报告 §6.3 末尾）：`|Δx| + |Δy| / 2.2`，`|Δx|` 是
    `deviation()` 的横向偏差、`|Δy|` 是到判定线的法向距离（见 :func:`normal_offset`）。
    它只用来选一个、不构成判定条件；并列时 `judge_taps` 按时间序取值，时刻最早的胜出。"""
    return deviation(slot.geometry, slot.note, position, seconds) + normal_offset(
        slot.geometry, position, seconds
    ) / 2.2


def judge_taps(
    chart: Chart,
    result: PlanResult,
    slots: list[Slot],
    *,
    fingers: Fingers,
    heads: dict[int, "HoldHead"],
) -> tuple[list[Judgement], list[Graze], set[int]]:
    """Tap 与 Hold 的头判：**逐次按下**判，一次按下只判**一个**音符：`CheckNote` 在每个手指
    `phase == Began` 那一帧、于候选里挑 `selection_metric` 最小的那个（报告 §6.2/§6.3），
    候选须未判过、`Δ ∈ (−0.22, +0.18)`、横向 < 1.9（谱面单位）；Hold 只挂号，窄窗外算蹭键。"""
    scan, moments = scan_order(
        [slot for slot in slots if slot.kind in (NoteType.TAP, NoteType.HOLD)]
    )

    # 计划"本来按对了"的音符：窄窗里有一次横向够得着的按下（与覆盖率判据同一套窗口）
    proper: set[int] = set()
    down_times = [timestamp / 1000.0 for timestamp, _, _ in fingers.downs]
    for slot in scan:
        lo = bisect_right(down_times, slot.seconds + HEAD_WINDOW[0] - 0.001)
        hi = bisect_right(down_times, slot.seconds + HEAD_WINDOW[1] + 0.001)
        for index in range(lo, hi):
            timestamp, _, position = fingers.downs[index]
            if (
                deviation(slot.geometry, slot.note, position, timestamp / 1000.0)
                <= TAP_TOLERANCE
            ):
                proper.add(slot.index)
                break

    judged: set[int] = set()
    judgements: list[Judgement] = []
    grazes: list[Graze] = []
    for timestamp, pointer, position in fingers.downs:
        # 判定发生在游戏处理这一下按下的那一帧；播放器按 latency 提前发，正好抵掉（见 DELIVERED_LATENCY）
        moment = timestamp / 1000.0 + PROCESS_DELAY - DELIVERED_LATENCY
        best: tuple[float, Slot, Judgement] | None = None
        lo = bisect_right(moments, moment - SCAN_BACK)
        hi = bisect_right(moments, moment + SCAN_AHEAD)
        for offset in range(lo, hi):
            slot = scan[offset]
            if slot.index in judged:
                continue
            delta = moment - slot.seconds
            lateral = deviation(slot.geometry, slot.note, position, moment)
            # 三条过滤全在这里（含边缘收窄的 badTime）：见 tap_window 的反编译引用
            if not tap_window(delta, lateral / CHART_TO_SCREEN):
                continue
            if slot.kind is NoteType.HOLD and abs(delta) >= GOOD_TIME:
                # Hold 头判 |Δ| >= 0.18 不判档、清掉 isJudged 继续等（`HoldControl::Judge` 0x1d32668）
                continue
            verdict = verdict_of(delta)
            metric = selection_metric(slot, position, moment)
            if best is not None and metric >= best[0] - METRIC_EPSILON:
                # 差在容差以内算平局 → 保留**先遇到的**（时间序在前）那个，见 METRIC_EPSILON
                continue
            best = (
                metric,
                slot,
                Judgement(
                    line=slot.line,
                    note=slot.note,
                    seconds=slot.seconds,
                    verdict=verdict,
                    at=moment,
                    delta=delta,
                    pointer=pointer,
                    above=slot.above,
                    index=slot.side_index,
                ),
            )

        if best is None:
            continue
        _, slot, judgement = best
        judged.add(slot.index)
        if slot.kind is NoteType.HOLD:
            # Hold 头判当场不算分，只挂号；记下头判的 Δ —— 收尾报的就是它（`this->_judgeTime`，第 180 行）
            heads[slot.index] = HoldHead(
                slot=slot,
                at=judgement.at,
                delta=judgement.delta,
                pointer=pointer,
                is_perfect=abs(judgement.delta) < PERFECT_TIME,
                aimed=HEAD_WINDOW[0] <= judgement.delta <= HEAD_WINDOW[1],
            )
            if not heads[slot.index].aimed:
                grazes.append(
                    Graze(
                        line=slot.line,
                        note=slot.note,
                        verdict=judgement.verdict,
                        at=judgement.at,
                        delta=judgement.delta,
                        pointer=pointer,
                        stolen=slot.index in proper,
                        dwell_ms=fingers.dwell_ms(pointer, timestamp),
                    )
                )
            continue
        aimed = HEAD_WINDOW[0] <= judgement.delta <= HEAD_WINDOW[1]
        judgement.grazed = not aimed
        judgements.append(judgement)
        if aimed:
            continue
        grazes.append(
            Graze(
                line=slot.line,
                note=slot.note,
                verdict=judgement.verdict,
                at=judgement.at,
                delta=judgement.delta,
                pointer=pointer,
                stolen=slot.index in proper,
                dwell_ms=fingers.dwell_ms(pointer, timestamp),
            )
        )

    return judgements, grazes, judged


def judge_holds(
    slots: list[Slot], heads: dict[int, HoldHead], fingers: Fingers
) -> list[Judgement]:
    """Hold 的身体与收尾（`HoldControl::Judge` 0x1d32668）：身体**只在头判之后跑**（第 241-295 行）
    逐帧查**所有**按着的手指（`fingerPositionX` 数组，不看 phase），横向 < 1.9（谱面单位）就刷新
    `_safeFrame = 2`，连续落空 3 帧判 Miss；收尾在 `realTime + holdTime − 0.22` 报头判的 Δ。"""
    judgements: list[Judgement] = []
    step = FRAME_MS / 1000.0
    """一帧多少**秒**（`FRAME_MS` 是毫秒，不能直接拿它累加）。"""
    for slot in slots:
        if slot.kind is not NoteType.HOLD:
            continue
        head = heads.get(slot.index)
        if head is None:
            # 头部从没被任何按下选中 —— 走头部那条 Miss（比 realTime 晚 0.22，不会早）
            judgements.append(
                Judgement(
                    line=slot.line,
                    note=slot.note,
                    seconds=slot.seconds,
                    verdict=Verdict.MISS,
                    at=slot.seconds + BAD_TIME,
                    delta=BAD_TIME,
                    pointer=-1,
                    above=slot.above,
                    index=slot.side_index,
                )
            )
            continue

        settle = slot.seconds + slot.note.hold - HOLD_SETTLE_LEAD
        grace = HOLD_GRACE_FRAMES - 1
        """预制体里 `_safeFrame` 的初值 = 2（判据是 `if (--_safeFrame < 0)`）→ 忍 3 帧。"""
        moment = head.at
        while moment <= settle:
            on_line = any(
                deviation(slot.geometry, slot.note, position, moment) < TAP_TOLERANCE
                for position in fingers.down_at(round(moment * 1000.0))
            )
            if on_line:
                grace = HOLD_GRACE_FRAMES - 1
            elif grace < 0:
                judgements.append(
                    Judgement(
                        line=slot.line,
                        note=slot.note,
                        seconds=slot.seconds,
                        verdict=Verdict.MISS,
                        at=moment,
                        delta=moment - slot.seconds,
                        pointer=-1,
                        above=slot.above,
                        index=slot.side_index,
                        grazed=not head.aimed,
                    )
                )
                break
            else:
                grace -= 1
            moment += step
        else:
            # 撑到收尾：按头判的档，报**头判**的 Δ
            judgements.append(
                Judgement(
                    line=slot.line,
                    note=slot.note,
                    seconds=slot.seconds,
                    verdict=Verdict.PERFECT if head.is_perfect else Verdict.GOOD,
                    at=settle,
                    delta=head.delta,
                    pointer=head.pointer,
                    above=slot.above,
                    index=slot.side_index,
                    grazed=not head.aimed,
                )
            )
    return judgements


def judge_drags_and_flicks(result: PlanResult, slots: list[Slot]) -> list[Judgement]:
    """Drag / Flick：逐帧（`FRAME_RATE`）比位置，够到一次就算 Perfect，否则 Miss。
    帧相位是假设（设备帧率可能是 60 或 120），所以只报 Miss 与"什么时候够到的"；与相位
    无关的严格判据在 `tests/coverage.py`。"""
    fingers = Fingers(result)
    timer = Timer(result)
    judgements: list[Judgement] = []

    for slot in slots:
        if slot.kind not in (NoteType.DRAG, NoteType.FLICK):
            continue
        window = DRAG_WINDOW if slot.kind is NoteType.DRAG else FLICK_WINDOW
        verdict = Verdict.MISS
        at = slot.seconds + window
        delta = window
        pointer = -1
        for moment in timer.around(slot.seconds, window):
            for candidate in fingers.down_at(round(moment * 1000)):
                if deviation(slot.geometry, slot.note, candidate, moment) <= DRAG_TOLERANCE:
                    verdict = Verdict.PERFECT
                    at = moment
                    delta = moment - slot.seconds
                    break
            if verdict is Verdict.PERFECT:
                break
        judgements.append(
            Judgement(
                line=slot.line,
                note=slot.note,
                seconds=slot.seconds,
                verdict=verdict,
                at=at,
                delta=delta,
                pointer=pointer,
                above=slot.above,
                index=slot.side_index,
            )
        )
    return judgements


class Timer:
    """按帧率给出一段窗口里的采样时刻。"""

    def __init__(self, result: PlanResult, frame_rate: float = FRAME_RATE) -> None:
        self.step = 1.0 / frame_rate
        self.duration = (result.frames[-1][0] / 1000.0) if result.frames else 0.0

    def around(self, seconds: float, window: float) -> list[float]:
        lo = max(0.0, seconds - window)
        hi = min(self.duration, seconds + window)
        count = int(math.floor((hi - lo) / self.step)) + 1
        return [lo + i * self.step for i in range(max(0, count))]


def simulate(chart: Chart, result: PlanResult, *, plan: str = "", chart_name: str = "") -> Report:
    """把一份规划完整重放一遍：Tap / Hold 头判逐次按下判（一个音符只判一次、先到先得，
    与帧率无关）、Hold 身体与收尾（`judge_holds`）、Drag / Flick 逐帧比位置；
    没被任何判定碰到的音符算 Miss（`lost`）。"""
    slots = note_table(chart)
    fingers = Fingers(result)
    heads: dict[int, HoldHead] = {}
    taps, grazes, judged = judge_taps(
        chart, result, slots, fingers=fingers, heads=heads
    )
    holds = judge_holds(slots, heads, fingers)
    others = judge_drags_and_flicks(result, slots)
    judged |= {slot.index for slot in slots if slot.kind in (NoteType.DRAG, NoteType.FLICK)}
    judged |= {slot.index for slot in slots if slot.kind is NoteType.HOLD}

    missed = [
        Judgement(
            line=slot.line,
            note=slot.note,
            seconds=slot.seconds,
            verdict=Verdict.MISS,
            at=slot.seconds + FLICK_MISS,
            delta=FLICK_MISS,
            pointer=-1,
            above=slot.above,
            index=slot.side_index,
        )
        for slot in slots
        if slot.index not in judged
    ]

    return Report(
        plan=plan,
        chart=chart_name,
        notes=len(slots),
        judgements=taps + holds + others + missed,
        grazes=grazes,
    )
