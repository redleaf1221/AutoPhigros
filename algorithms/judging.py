"""游戏怎么判，以及"把一份规划按规则重放一遍"的裁判。

这是判定规则的**唯一出处**：数字全部来自
``Phigros4.0-音游内核逆向报告.md``（每条都标了出处），自检、``judge.py``、以及将来做
蹭键回避的规划器都从这里读，不许各写一份。

单位
----
`Note.offset` 与判定线几何都在 16 宽的虚拟屏上；游戏的容差写的是**谱面单位**
（Tap 1.9 / Drag 2.1），乘 `CHART_TO_SCREEN` 换成虚拟屏单位（1.71 / 1.89）。
`deviation()` 返回的也是虚拟屏单位，可以直接和容差比。

为什么要一个"裁判"而不是只看覆盖率
----------------------------------
覆盖率问的是"**该按下的时候**按下了没有"，用的是规划的窄窗（`HEAD_WINDOW`：−20ms ~
+160ms）；游戏判定的扫描窗却宽得多（`realTime ∈ (nowTime − 0.18, nowTime + 0.22)`，见报告
§6.3）。两者之差就是**蹭键**的生存空间：一次为别的音符按下的手指，只要落在某个 tap 的
扫描窗里、横向又在容差内，就把它判掉了 —— 而且是 Perfect/Good/Bad 里更差的那一档
（实测：早 156ms / 165ms 判成 Good，早 204ms 判成 Bad）。覆盖率完全看不见这件事，
裁判的职责就是把它揪出来。
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
"""谱面单位 → 虚拟屏单位。`CheckNote` 的 1.9 是谱面单位，虚拟屏宽 16，比例就是 0.9。"""

TAP_TOLERANCE = 1.9 * CHART_TO_SCREEN
"""Tap / Hold 头判的横向容差（`JudgeControl::CheckNote`，报告 §6.2）。"""

DRAG_TOLERANCE = 2.1 * CHART_TO_SCREEN
"""Drag / Flick 的横向容差（`DragControl::Judge` / `CheckFlick`），比 Tap 宽。"""

PERFECT_TIME = 0.08
GOOD_TIME = 0.18
BAD_TIME = 0.22
"""Tap 的三档窗口（报告 §6.7）：|Δ| < 0.08 → Perfect，< 0.18 → Good，否则 Bad。"""

SCAN_BACK = 0.18
SCAN_AHEAD = 0.22
"""判定扫描窗：每帧只看 `realTime ∈ (nowTime − 0.18, nowTime + 0.22)` 的音符（报告 §6.3）。

也就是说一次按下能碰到的 tap 满足 `Δ = nowTime − realTime ∈ (−0.22, +0.18)`：
**早**最多 220ms、**晚**最多 180ms。这个不对称就是"被判早"的那些蹭键的来源。
"""

DRAG_WINDOW = 0.10
"""Drag 的判定窗：`|realTime − nowTime| <= 0.1` 时逐帧比手指（报告 §6.5）。"""

FLICK_WINDOW = 0.14
"""Flick 的候选窗：`CheckFlick` 用 `PerfectTimeRange × 1.75 = ±0.14s` 挑音符（报告 §6.6）。"""

FLICK_MISS = 0.22
"""Flick 迟到这么久还是没被点亮就是 Miss。"""

HOLD_SETTLE_LEAD = 0.22
"""Hold 在 `realTime + holdTime − 0.22` 就结算（报告 §6.6）—— 手指可以**早走 220ms**。"""

HOLD_TIMEOUT = 0.25
"""Hold 的兜底超时：按住结束之后再过 0.25s 还没判就是 Miss。"""

HOLD_GRACE_FRAMES = 3
"""Hold 主体允许连续落空几帧：`_safeFrame = 2`，第 4 帧才判 Miss（报告 §6.6）。"""

HOLD_GRACE_MS = 67.0
"""上一条在 60fps 下的**实测**宽容（毫秒），自检用的是这个而不是帧数。

为什么不用帧数直接算（3 帧 ≈ 50ms）：这条宽容是实测出来的 —— Glaciaxion IN 有一条判定线
每 53ms 在两个位置之间跳一次（归一化 0.2 ↔ 0.8），手指按住其中一个位置，另一个相位必然
落空；`geometric` 就是这么打的，实机 All Perfect。所以判据只能是"没有一段超过这个窗口
完全落空"，而不是"每一刻都在容差内"：后者在闪烁目标上是掷硬币，报不报错取决于那 8ms
落在哪个相位。写成 67ms 是把"到底算 3 帧还是 4 帧"这件事也一并钉住（实测值说了算）。
"""

FRAME_RATE = 60.0
"""重放用的帧率。判定是逐帧做的，而实际帧率取决于设备（60 或 2×刷新率）。

**Tap / Hold 头判与帧率无关**（判定发生在"按下那一帧"，那一帧就是我们发 DOWN 的时刻），
所以那部分结论是硬的；Drag / Flick / Hold 主体要按帧采样，帧相位是假设 —— 这也是为什么
覆盖率判据用的是"窗口里够一整帧"这种与相位无关的说法，而不是直接按 60fps 重放。
"""

FRAME_MS = 1000.0 / FRAME_RATE
"""一帧（60fps）有多少毫秒。覆盖率判据里最要紧的一个数：见 `tests/coverage.py`。"""

RUN_STEP_MS = 1.0
"""扫"手指在位"的步长。1ms 的粒度对 16.7ms 的判据来说够细了。"""

HOLD_SAMPLE_MS = 8
"""扫 hold 主体时的步长，与规划器的采样间隔一致。"""

FLICK_JUMP = 1.0
"""多大的位移算一次"手指跳变"（虚拟屏单位）。

这判据是**保守**的那一侧：只有跳变才一定够快、让 ``FingerManagement::Update`` 把
``Fingers.isNewFlick`` 置起来。真机上慢一点的划动有时也算数 —— 所以它说没问题就是真没问题
（那时机会只会更多），它报问题就值得看一眼。
"""

HEAD_WINDOW = (-0.02, 0.16)
"""规划这一层认为"这一次按下是**为这个音符**按的"的时间窗（与 `tests/coverage.py` 一致）。

落在扫描窗里、却落在这个窄窗外的按下，就是蹭键。"""


class Verdict(IntEnum):
    """一档判定。数值顺序即优劣，方便排序与统计。"""

    PERFECT = 0
    GOOD = 1
    BAD = 2
    MISS = 3

    @property
    def label(self) -> str:
        return ("Perfect", "Good", "Bad", "Miss")[int(self)]


def verdict_of(delta: float) -> Verdict | None:
    """Tap / Hold 头判：早晚量 → 判定；落在扫描窗之外表示"这一次按下根本碰不到它"。

    `delta = nowTime − realTime`，正数 = 晚（与游戏传给 `ScoreControl` 的 `−Δ` 同源）。
    """
    if not (-BAD_TIME < delta < SCAN_BACK):
        return None
    if abs(delta) < PERFECT_TIME:
        return Verdict.PERFECT
    if abs(delta) < GOOD_TIME:
        return Verdict.GOOD
    return Verdict.BAD


def score(perfect: int, good: int, notes: int, max_combo: int) -> tuple[int, float]:
    """分数与完成度（报告 §7.2）：

        分数   = 900000 × acc + 100000 × maxCombo / N
        acc    = (Perfect + 0.65 × Good) / N
        完成度 = acc × 100

    拿实机那条对过：1156 音符、1152 Perfect、2 Good、最大连击 613 → 950926 分，与游戏
    报的一分不差。所以裁判给的分可以直接拿来跟实机对账。
    """
    if notes <= 0:
        return 0, 0.0
    accuracy = (perfect + 0.65 * good) / notes
    total = 900000.0 * accuracy + 100000.0 * max_combo / notes
    return int(round(total)), accuracy * 100.0


# --------------------------------------------------------------- 手指时间轴


class Fingers:
    """把事件流按指针拆开，回答"某时刻哪些手指在屏幕上、在哪"。

    "在位"的判据就是这条时间轴：`down_at(t)` 取每根手指在 t 之前最后一次事件的位置，
    只要那一次不是 UP 就算还按着。判定线一直在动、手指在两次事件之间不动，所以游戏每帧
    读到的横向偏差是逐帧漂的 —— 所有逐帧判据都建立在这上面。
    """

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

    def downs_between(self, lo_ms: float, hi_ms: float) -> list[tuple[int, complex]]:
        """时间窗里的所有按下（含指针号）。"""
        return [
            (timestamp, position)
            for timestamp, _, position in self.downs
            if lo_ms <= timestamp <= hi_ms
        ]


def deviation(line, note: Note, position: complex, seconds: float) -> float:
    """一根手指 `position` 在 `seconds` 那一刻对 `note` 的横向偏差。

    判据就是 Phigros 的垂直判定本身：`fingerPositionX = (手指 − 判定线原点)·判定线朝向`，
    音符要的是它等于 `positionX`（乘 0.9 折成虚拟屏单位）。**直接用这个式子算，不要绕
    `Screen.remap`** —— remap 是给规划器找"够得着的落点"用的，它在判定线整体跑到屏幕外
    （过该点的探针线交不到屏幕）时会退化成屏幕中心，横向信息就丢了，量出来的偏差是假的。
    踩过：Glaciaxion 125.143s 那条线在 tick 起点处 y=90，remap 退化成 (8, 4.5)，把一个
    偏差 0.000 的 tap 头判冤枉成 2.625。

    手指与判定线必须取**同一时刻** —— 判定线会在两帧之间整体平移，取两个不同时刻去量，
    量到的东西没有可比性（踩过：尾判用尾时刻算目标、用尾前 8ms 查手指，那条闪烁的线刚好
    在这 8ms 里跳了 9.6 个世界单位，凭空报了 9.6 的偏差）。

    也该用谱面的**精确时刻**（`placement.seconds`），不要用取整后的毫秒：有的判定线会在
    一瞬间整体平移，而音符的判定时刻恰好压在那一瞬间上；游戏是在平移"之后"的那一帧判的，
    `Track.at` 在恰好落在事件起点时也取新事件的值，两边才一致。
    """
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
    grazed: bool = False
    """是不是"不是为它按的那一下"判掉的 —— 见 :class:`Graze`。"""

    @property
    def kind(self) -> NoteType:
        return self.note.kind


@dataclass(slots=True)
class Graze:
    """蹭键：一次**不是为这个音符按下**的动作，却把它判掉了。

    两种后果要分开看，`stolen` 就是分界线：

    * `stolen=True` —— 计划本来按对了（窄窗里有一次够得着的按下），却被这一下先碰掉，
      于是那个音符拿到更差的一档、而计划自己那一次按下白发。**这是纯丢分**；
    * `stolen=False` —— 计划本来就没按对（覆盖率那个判据也会报它），蹭键只是让结果更难看。
    """

    line: int
    note: Note
    verdict: Verdict
    at: float
    delta: float
    pointer: int
    stolen: bool

    def text(self) -> str:
        tail = (
            "计划自己那一次按下白发（本来按对了）"
            if self.stolen
            else "计划本来也没按到它（覆盖判据同样会报）"
        )
        return (
            f"蹭键：{self.note.kind.name} @ {self.note.seconds:.3f}s（线 {self.line}）"
            f"被判成 {self.verdict.label}（{self.delta * 1000:+.0f}ms，指针 {self.pointer}）"
            f"—— {tail}"
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

    为什么要有一张表：**同一个 `(线, 时刻)` 上可能有不止一个音符**（实测 Credits IN 就有
    7 个这种叠着的），所以"判过没有"不能用 `(线, 时刻)` 当身份 —— 那会把叠着的另一个
    音符悄悄丢掉。表里的下标才是身份。
    """

    index: int
    line: int
    geometry: object
    note: Note
    seconds: float
    point: complex
    """判定点在虚拟屏上的位置（`place_note` 算出来的），多押里挑最近的那个要用它。"""

    @property
    def kind(self) -> NoteType:
        return self.note.kind


def note_table(chart: Chart) -> list[Slot]:
    """把整张谱摊成一张表，判定点用 `place_note` 算（flick 可能被微调）。"""
    slots: list[Slot] = []
    for line_index, line in enumerate(chart.lines):
        for note in line.notes:
            placement = place_note(line, note, chart.screen, retime=note.kind is NoteType.FLICK)
            slots.append(
                Slot(
                    index=len(slots),
                    line=line_index,
                    geometry=line,
                    note=note,
                    seconds=placement.seconds,
                    point=placement.position,
                )
            )
    return slots


def _by_line(slots: list[Slot]) -> list[tuple[list[Slot], list[float]]]:
    """每条线一张**按时间排好**的表（配合 bisect 只查扫描窗里的音符）。

    排序这一步不能省：谱面 JSON 里一条线的音符是 `notesAbove` 接着 `notesBelow`，**不是**
    按时间排的，拿它直接 bisect 会得到一堆错误的区间 —— 表现是"明明有一次零偏差的按下，
    这个音符却被判成丢音"（踩过，Credits IN 上 104 个假丢音）。
    """
    grouped: dict[int, list[Slot]] = defaultdict(list)
    for slot in slots:
        grouped[slot.line].append(slot)
    tables: list[tuple[list[Slot], list[float]]] = []
    for line in sorted(grouped):
        ordered = sorted(grouped[line], key=lambda slot: slot.seconds)
        tables.append((ordered, [slot.seconds for slot in ordered]))
    return tables


# --------------------------------------------------------------- 裁判


def selection_metric(slot: Slot, position: complex, seconds: float) -> float:
    """游戏在多押里挑"最近候选"用的度量（报告 §6.3 末尾）。

        |Δx| + |Δy| / 2.2

    `|Δx|` 就是 `deviation()` 那个横向偏差；`|Δy|` 是指尖离判定点的纵向距离。它**只用来
    在多押里选一个**，不构成判定条件 —— 选出来之后仍然只看 `|Δx| < 1.9` 和早晚量。

    2.2 是 Y 方向的归一化系数（报告里写死的那个值）。我们这边的坐标是 0.9 倍的虚拟屏单位，
    同一个量级，比较候选之间的大小不受影响。
    """
    return deviation(slot.geometry, slot.note, position, seconds) + abs(
        position.imag - slot.point.imag
    ) / 2.2


def judge_taps(chart: Chart, result: PlanResult, slots: list[Slot]) -> tuple[list[Judgement], list[Graze]]:
    """Tap 与 Hold 的头判：**逐次按下**判，而且一次按下只判**一个**音符。

    这是这个模块里最容易搞错、也最要紧的一条：`JudgeControl::CheckNote` 是"在每个手指的
    `phase == Began` 那一帧，于判定线局部坐标里**挑最近的那个候选**"（报告 §6.2/§6.3），
    不是"把所有够得着的音符都判掉"。我们第一版按后者写，结果是 Credits IN 上凭空多出
    80 个 Good —— 而实机上那张谱是 All Perfect 级别的。一次按下一个音符，才解释得通。

    候选的判据（三条都要满足）：

    * 还没判过（一个音符只判一次，先到先得）；
    * `Δ = nowTime − realTime ∈ (−0.22, +0.18)`（判定扫描窗，报告 §6.3）；
    * `|Δx| < 1.9`（谱面单位 = 1.71 虚拟屏单位）。

    然后按 `selection_metric` 取最小的那个判掉。**它如果不是"计划本来瞄的那个"就是蹭键** ——
    判据是"这一次按下的时刻落在音符窄窗 `HEAD_WINDOW` 之外"：计划的窄窗只有 −20ms~+160ms，
    游戏的扫描窗却有 −220ms~+180ms，两者之差就是蹭键能活下来的缝。
    """
    fingers = Fingers(result)
    tables = _by_line([slot for slot in slots if slot.kind in (NoteType.TAP, NoteType.HOLD)])

    # 计划"本来按对了"的那些音符：窄窗里有一次横向够得着的按下（与覆盖率判据同一套窗口）。
    # 用来区分两种后果：计划按对了却被别人先碰掉（丢分）vs 计划根本没按（漏音）。
    proper: set[int] = set()
    down_times = [timestamp / 1000.0 for timestamp, _, _ in fingers.downs]
    for group, seconds in tables:
        for slot in group:
            lo = bisect_right(down_times, slot.seconds + HEAD_WINDOW[0] - 0.001)
            hi = bisect_right(down_times, slot.seconds + HEAD_WINDOW[1] + 0.001)
            for index in range(lo, hi):
                timestamp, _, position = fingers.downs[index]
                if deviation(slot.geometry, slot.note, position, slot.seconds) <= TAP_TOLERANCE:
                    proper.add(slot.index)
                    break

    judged: set[int] = set()
    judgements: list[Judgement] = []
    grazes: list[Graze] = []
    for timestamp, pointer, position in fingers.downs:
        moment = timestamp / 1000.0
        best: tuple[float, Slot, Judgement] | None = None
        for group, seconds in tables:
            lo = bisect_right(seconds, moment - SCAN_AHEAD)
            hi = bisect_right(seconds, moment + SCAN_BACK)
            for offset in range(lo, hi):
                slot = group[offset]
                if slot.index in judged:
                    continue
                delta = moment - slot.seconds
                verdict = verdict_of(delta)
                if verdict is None:
                    continue
                if deviation(slot.geometry, slot.note, position, slot.seconds) > TAP_TOLERANCE:
                    continue
                metric = selection_metric(slot, position, slot.seconds)
                if best is not None and metric >= best[0]:
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
                    ),
                )

        if best is None:
            continue
        _, slot, judgement = best
        judged.add(slot.index)
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
            )
        )

    return judgements, grazes, judged


def judge_drags_and_flicks(result: PlanResult, slots: list[Slot]) -> list[Judgement]:
    """Drag / Flick：逐帧（`FRAME_RATE`）比位置，够到一次就算 Perfect，否则 Miss。

    帧相位是假设（见 `FRAME_RATE`）—— 真机 60/120fps 都可能，所以这里**只报 Miss 与
    "什么时候够到的"**，别拿它去和实机的 Perfect 个数精确对账；与相位无关的严格判据在
    `tests/coverage.py`。
    """
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
    """把一份规划完整重放一遍。

    现在做实的

    * **Tap / Hold 头判**：逐次按下判（判定只发生在按下那一帧），一个音符只判一次、
      先到先得，并给出蹭键归因。这部分与帧率无关，结论是硬的；
    * **Drag / Flick**：逐帧比位置（帧相位是假设，见 `FRAME_RATE`）；
    * 没被任何一次判定碰到的音符 → **Miss**（`lost`）。

    还没进来的（它们在 `tests/coverage.py` 里有对应判据，补进来之后两边该收敛成一套）

    * Hold 主体的 `_safeFrame` 与"尾部抱的是头判的早晚量"：所以这里只有头判结果，
      "中途松手被判 Miss"还看不见；
    * Hold 主体的横向判据用的是**静态** `positionX`（报告 §6.6），与 Drag/Flick 用的
      "判定线当前时刻的位置"不是一回事 —— 这一条可能会解释实机上某些 hold 的 Miss。
    """
    slots = note_table(chart)
    taps, grazes, judged = judge_taps(chart, result, slots)
    others = judge_drags_and_flicks(result, slots)
    judged |= {slot.index for slot in slots if slot.kind in (NoteType.DRAG, NoteType.FLICK)}

    missed = [
        Judgement(
            line=slot.line,
            note=slot.note,
            seconds=slot.seconds,
            verdict=Verdict.MISS,
            at=slot.seconds + FLICK_MISS,
            delta=FLICK_MISS,
            pointer=-1,
        )
        for slot in slots
        if slot.index not in judged
    ]

    return Report(
        plan=plan,
        chart=chart_name,
        notes=len(slots),
        judgements=taps + others + missed,
        grazes=grazes,
    )