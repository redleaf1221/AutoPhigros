"""游戏怎么判，以及"把一份规划按规则重放一遍"的裁判。

判定的唯一出处：数字全部来自 ``Phigros4.0-音游内核逆向报告.md``（每条都标出处），
自检、``judge.py`` 与规划器都从这里读，不许各写一份。

单位：几何与 `Note.offset` 都在 16 宽的虚拟屏上，游戏容差是谱面单位（Tap 1.9 / Drag 2.1），
乘 `CHART_TO_SCREEN` 得 1.71 / 1.89；窄窗 `HEAD_WINDOW` 之外、游戏扫描窗之内的命中就是蹭键。
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
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
"""Tap 的三档窗口（`ClickControl::Judge` 0x1d3060c）：|Δ| < 0.08 → Perfect，< 0.18 → Good，
否则 Bad。三个数就是 `JudgeControl` 静态字段里的 `PerfectTimeRange` / `GoodTimeRange` /
`BadTimeRange`，**别的判定也读它们**。"""

SCAN_BACK = 0.18
SCAN_AHEAD = 0.22
"""判定扫描窗（`CheckNote` 0x1d21104 第 68-126 行）：每帧只看
`realTime ∈ (nowTime − 0.18, nowTime + 0.22)` 的音符 —— 下界取 `GoodTimeRange`、上界取
`BadTimeRange`，两端都严格。反过来 Δ = nowTime − realTime ∈ (−0.22, +0.18)：**早 220ms、
晚 180ms，不对称**。"""

NEAR_TIME = 0.01
"""`CheckNote` / `CheckFlick` 里那条 10ms：`minDeltaTime` 守卫（`>= minDeltaTime + 0.01`）与
"只有 10ms 内的候选才比度量"（`fabsf(realTime 差) > 0.01`）用的是同一个数。"""

FUTURE = 10000.0
"""那两个函数开头把 `minDeltaTime` 写成 `10000.0f`（`0x461C4000`）—— 初值只为了让**第一个**
过门的候选无条件通过；每接受一个候选就把它改成那个候选的 `|realTime − nowTime|`。"""

DRAG_WINDOW = 0.10
"""Drag 的判定窗（`DragControl::Judge` 0x1d313f0）：`|realTime − nowTime| <= 0.1` 时逐帧比手指；
Miss 也在这条线上（`Δ < −0.1`）。"""

FLICK_WINDOW = 0.14
"""Flick 的候选窗与 Miss 线：`CheckFlick` 0x1d21828 用 `PerfectTimeRange × 1.75` 挑音符，
`FlickControl::Judge` 0x1d319e4 也用它判 Miss（`Δ < −0.14`）。"""

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


def spine_offset(position: complex, spine: complex, rotation: Vector) -> float:
    """`position` 相对"过 `spine`、沿 `rotation` 的那条无限长的线"的**横向**偏移。

    `deviation` 量的是同一个东西（`(手指 − 判定线原点)·朝向 − 音符偏移`），只是那条线的原点
    取判定线自己的原点；规划器手上只有"判定点"没有判定线对象，就直说这个量 —— "够不够得着"
    全用它，别再写第二份。
    """
    return ((position - spine) * rotation.conjugate()).real


def deviation(line, note: Note, position: complex, seconds: float) -> float:
    """一根手指 `position` 在 `seconds` 那一刻对 `note` 的横向偏差（虚拟屏单位）：Phigros 的
    垂直判定就是 `(手指 − 判定线原点)·判定线朝向 − note.offset`。**不要绕 `Screen.remap`**
    （判定线跑到屏幕外时它退化成屏幕中心，横向信息就没了），且手指与判定线必须同刻、取精确秒数。"""
    return abs(
        spine_offset(
            position, line.point_at(seconds, note.offset), line.rotation_at(seconds)
        )
    )


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
    """在候选之间挑"最近的那个"用的度量（`CheckNote` 第 405-409 行）：`|Δx| + |Δy| / 2.2`，
    `|Δx|` 是 :func:`deviation` 的横向偏差、`|Δy|` 是到判定线的法向距离（:func:`normal_offset`）。
    它只在"时间上挨着（差 ≤ 10ms）、类型也允许"的候选之间比 —— 不是全局最小，
    见 :func:`judge_taps` 与 :func:`lit_flicks`。"""
    return deviation(slot.geometry, slot.note, position, seconds) + normal_offset(
        slot.geometry, position, seconds
    ) / 2.2


def candidates_in_window(
    ordered: list[Slot], moments: list[float], moment: float, *, back: float, ahead: float
) -> list[Slot]:
    """`CheckNote` / `CheckFlick` 开头那两段游标扫出来的候选：`realTime ∈ (now − back, now + ahead)`。

    两端都是**严格**的（反编译里是 `if (realTime >= now + ahead) break` / `<= now − back`），
    所以 `ahead` 那一侧用 `bisect_left` 把相等剔掉。
    """
    lo = bisect_right(moments, moment - back)
    hi = bisect_left(moments, moment + ahead)
    return ordered[lo:hi]


def judge_taps(
    slots: list[Slot],
    *,
    drags: list[Judgement],
    lit: dict[int, float],
    fingers: Fingers,
    heads: dict[int, "HoldHead"],
) -> tuple[list[Judgement], list[Graze], set[int]]:
    """Tap 与 Hold 的头判：**逐次按下**判，一次按下只判**一个**音符。

    `CheckNote`（0x1d21104）的挑法比"取度量最小"绕得多，四类音符都进候选（Flick 只挑不判）：

    * 每个候选过三道门：已判过 / 横向 `>= 1.9` / 早侧收窄的 `badTime`（见 :func:`tap_window`）；
    * 还没有 best 就**直接接受**；
    * best 是 Drag / Flick → 下一个过门的候选**无条件**顶掉它；
    * best 是 Tap / Hold → 候选也得是 Tap / Hold、且两者 `realTime` 差 `<= 10ms`，才比度量
      （度量严格更小才顶替）；
    * 每次接受都把 `minDeltaTime` 写成 `|realTime − nowTime|`，往后 `realTime − nowTime >=
      minDeltaTime + 0.01` 的候选**看都不看** —— 这才是"一次按下够不到太远的音符"的真正原因。

    Hold 只挂号、当场不算分（`HoldControl::Judge`）；`|Δ| >= 0.18` 时游戏会把标记清掉继续等。
    """
    ordered, moments = scan_order(slots)
    lit_drags = {judgement.index: judgement.at for judgement in drags if judgement.verdict is Verdict.PERFECT}

    # 计划"本来按对了"的音符：窄窗里有一次横向够得着的按下（与覆盖率判据同一套窗口）
    proper: set[int] = set()
    down_times = [timestamp / 1000.0 for timestamp, _, _ in fingers.downs]
    for slot in ordered:
        if slot.kind not in (NoteType.TAP, NoteType.HOLD):
            continue
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
        best: Slot | None = None
        best_metric = 0.0
        min_delta = FUTURE
        for slot in candidates_in_window(
            ordered, moments, moment, back=SCAN_BACK, ahead=SCAN_AHEAD
        ):
            if slot.index in judged:
                continue
            if slot.kind is NoteType.DRAG and lit_drags.get(slot.index, math.inf) <= moment:
                continue  # drag 的位置判据已经把它点亮了（组件 +0x28）
            if slot.kind is NoteType.FLICK and lit.get(slot.index, math.inf) <= moment:
                continue  # flick 已被 CheckFlick 点亮
            if slot.seconds - moment >= min_delta + NEAR_TIME:
                continue
            delta = moment - slot.seconds
            lateral = deviation(slot.geometry, slot.note, position, moment)
            # 三条过滤全在这里（含边缘收窄的 badTime）：见 tap_window 的反编译引用
            if not tap_window(delta, lateral / CHART_TO_SCREEN):
                continue
            metric = selection_metric(slot, position, moment)
            if best is not None:
                if best.kind not in (NoteType.DRAG, NoteType.FLICK):
                    if slot.kind not in (NoteType.TAP, NoteType.HOLD):
                        continue  # best 是 Tap / Hold 时，Drag / Flick 顶不掉它
                    if abs(best.seconds - slot.seconds) > NEAR_TIME:
                        continue  # 也只在 10ms 内才比度量
                    if metric >= best_metric - METRIC_EPSILON:
                        # 差在容差以内算平局 → 保留**先遇到的**（时间序在前）那个，见 METRIC_EPSILON
                        continue
            best, best_metric = slot, metric
            min_delta = abs(slot.seconds - moment)

        if best is None or best.kind is NoteType.FLICK:
            continue  # Flick 由 CheckFlick 点灯，CheckNote 挑到它也不标
        delta = moment - best.seconds
        if best.kind is NoteType.DRAG:
            judged.add(best.index)  # 标在 drag 组件上（它由逐帧位置自己判档）
            continue
        if best.kind is NoteType.HOLD and abs(delta) >= GOOD_TIME:
            # 头判 |Δ| >= 0.18：HoldControl::Judge 会把标记清掉继续等，下一次按下还能判它
            continue
        judged.add(best.index)
        aimed = HEAD_WINDOW[0] <= delta <= HEAD_WINDOW[1]
        judgement = Judgement(
            line=best.line,
            note=best.note,
            seconds=best.seconds,
            verdict=verdict_of(delta),
            at=moment,
            delta=delta,
            pointer=pointer,
            above=best.above,
            index=best.side_index,
        )
        if best.kind is NoteType.HOLD:
            # Hold 头判当场不算分，只挂号；记下头判的 Δ —— 收尾报的就是它（`this->_judgeTime`，第 180 行）
            heads[best.index] = HoldHead(
                slot=best,
                at=moment,
                delta=delta,
                pointer=pointer,
                is_perfect=abs(delta) < PERFECT_TIME,
                aimed=aimed,
            )
        else:
            judgement.grazed = not aimed
            judgements.append(judgement)
        if not aimed:
            grazes.append(
                Graze(
                    line=best.line,
                    note=best.note,
                    verdict=judgement.verdict,
                    at=moment,
                    delta=delta,
                    pointer=pointer,
                    stolen=best.index in proper,
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
            # 头部从没被任何按下选中 —— 走头部那条 Miss：`HoldControl::Judge` 读的是静态字段
            # +8（`GoodTimeRange`），所以是 realTime + 0.18，不是 +0.22
            judgements.append(
                Judgement(
                    line=slot.line,
                    note=slot.note,
                    seconds=slot.seconds,
                    verdict=Verdict.MISS,
                    at=slot.seconds + GOOD_TIME,
                    delta=GOOD_TIME,
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


def flick_edges(result: PlanResult) -> list[tuple[float, complex]]:
    """每一次"新起手"（`Fingers.isNewFlick`）：同一根手指相邻两个事件之间的一次位置跳变。

    `FingerManagement::Update` 是按瞬时速度算的（`|nowMove| / deltaTime` 过
    `flickJudgeSpeed × 5`），阈值随 dpi 归一、拿不到，所以只认**跳变**（`FLICK_JUMP`）——
    跳变才一定够快。手指从上次抬起的旧位置"出现在"新位置也算一次，这也是悲观的那一侧。
    """
    edges: list[tuple[float, complex]] = []
    for items in Fingers(result).items.values():
        previous: complex | None = None
        for timestamp, action, position in items:
            if (
                previous is not None
                and action is not Touch.UP
                and abs(position - previous) >= FLICK_JUMP
            ):
                edges.append((timestamp / 1000.0, position))
            previous = position
    edges.sort(key=lambda edge: edge[0])
    return edges


def flick_candidate(slot: Slot, moment: float, position: complex) -> bool:
    """这一次"新起手"够不够得着这个 flick：`CheckFlick` 的候选判据 —— 时间在
    `nowTime ± 0.14s` 里（两端严格）、横向 < 2.1（谱面单位）。"""
    return (
        abs(moment - slot.seconds) < FLICK_WINDOW
        and deviation(slot.geometry, slot.note, position, moment) < DRAG_TOLERANCE
    )


def lit_flicks(slots: list[Slot], edges: list[tuple[float, complex]]) -> dict[int, float]:
    """`CheckFlick`（0x1d21828）点亮了哪些 flick。

    候选只有 Flick（`type != 4` 直接跳过），窗口 `realTime ∈ (now − 0.14, now + 0.14)` 两端严格、
    已点亮的跳过；挑法与 `CheckNote` 同源：第一个过门的直接接受，之后每次接受都把
    `minDeltaTime` 记成 `|realTime − now|`（后面的候选远过 10ms 就不看），有 best 时还要
    `|两者 realTime 差| <= 10ms` 且度量严格更小才顶替。每次起手只点亮一个，点完清 `isNewFlick`。
    """
    flicks, moments = scan_order([slot for slot in slots if slot.kind is NoteType.FLICK])
    lit: dict[int, float] = {}
    for moment, position in edges:
        best: Slot | None = None
        best_metric = 0.0
        min_delta = FUTURE
        for slot in candidates_in_window(
            flicks, moments, moment, back=FLICK_WINDOW, ahead=FLICK_WINDOW
        ):
            if slot.index in lit:
                continue
            if slot.seconds - moment >= min_delta + NEAR_TIME:
                continue
            lateral = deviation(slot.geometry, slot.note, position, moment)
            if lateral >= DRAG_TOLERANCE:
                continue
            metric = selection_metric(slot, position, moment)
            if best is not None:
                if abs(best.seconds - slot.seconds) > NEAR_TIME:
                    continue
                if metric >= best_metric - METRIC_EPSILON:
                    continue
            best, best_metric = slot, metric
            min_delta = abs(slot.seconds - moment)
        if best is not None:
            lit[best.index] = moment
    return lit


def judge_drags(slots: list[Slot], result: PlanResult) -> list[Judgement]:
    """Drag：逐帧（`FRAME_RATE`）比位置，够到一次就算 Perfect，否则 Miss。不看手指的
    `phase`（`DragControl::Judge` 读的是每帧的位置）。帧相位是假设（设备帧率可能是 60 或
    120），所以只报 Miss 与"什么时候够到的"；与相位无关的严格判据在 `tests/coverage.py`。"""
    fingers = Fingers(result)
    timer = Timer(result)
    judgements: list[Judgement] = []

    for slot in slots:
        if slot.kind is not NoteType.DRAG:
            continue
        window = DRAG_WINDOW
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


def judge_flicks(slots: list[Slot], lit: dict[int, float]) -> list[Judgement]:
    """Flick：光"摆在那儿"不算 —— 得有一次新起手把它点亮（`CheckFlick`）。点亮了就是
    Perfect（`at` 取点亮那一刻），没点亮就是 Miss（`FlickControl::Judge` 迟到 `FLICK_WINDOW`
    就判 Miss）。"""
    judgements: list[Judgement] = []
    for slot in slots:
        if slot.kind is not NoteType.FLICK:
            continue
        moment = lit.get(slot.index)
        judgements.append(
            Judgement(
                line=slot.line,
                note=slot.note,
                seconds=slot.seconds,
                verdict=Verdict.PERFECT if moment is not None else Verdict.MISS,
                at=moment if moment is not None else slot.seconds + FLICK_WINDOW,
                delta=moment - slot.seconds if moment is not None else FLICK_WINDOW,
                pointer=-1,
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
    与帧率无关）、Hold 身体与收尾（`judge_holds`）、Drag 逐帧比位置、Flick 看有没有被
    新起手点亮；没被任何判定碰到的音符算 Miss（`lost`）。

    顺序有讲究：`CheckNote` 挑候选时要知道某根 drag 有没有已经被**位置判据**点亮、
    某个 flick 有没有已经被**起手**点亮，所以 drag / flick 先算。
    """
    slots = note_table(chart)
    fingers = Fingers(result)
    drags = judge_drags(slots, result)
    lit = lit_flicks(slots, flick_edges(result))
    heads: dict[int, HoldHead] = {}
    taps, grazes, judged = judge_taps(
        slots, drags=drags, lit=lit, fingers=fingers, heads=heads
    )
    holds = judge_holds(slots, heads, fingers)
    others = drags + judge_flicks(slots, lit)
    judged |= {slot.index for slot in slots if slot.kind in (NoteType.DRAG, NoteType.FLICK)}
    judged |= {slot.index for slot in slots if slot.kind is NoteType.HOLD}

    missed = [
        # 只剩 Tap 会走到这里：它的 Miss 线也是 `GoodTimeRange`（`ClickControl::Judge`）
        Judgement(
            line=slot.line,
            note=slot.note,
            seconds=slot.seconds,
            verdict=Verdict.MISS,
            at=slot.seconds + GOOD_TIME,
            delta=GOOD_TIME,
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
