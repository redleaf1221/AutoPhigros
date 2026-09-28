"""算法输出的判据：事件流自洽、覆盖完整、在位时长、滑键起手。

这一堆回答"这份规划**打得过去**吗"；`judge.py` 回答的是另一件事 ——"游戏会把这一次按
判成什么、顺带判了谁"。两边用的是同一套窗口常量（`algorithms/judging.py`）。"""

from __future__ import annotations

import math
from collections import defaultdict

from algorithms.chart import Chart, NoteType
from algorithms.geometry import Screen, place_note
from algorithms.judging import (
    DRAG_TOLERANCE,
    DRAG_WINDOW,
    FLICK_WINDOW,
    FRAME_MS,
    HOLD_GRACE_MS,
    HOLD_SAMPLE_MS,
    RUN_STEP_MS,
    TAP_TOLERANCE as TOLERANCE,
    flick_candidate,
    flick_edges,
    lit_flicks,
    note_table,
)
from algorithms.judging import Fingers as Tracks
from algorithms.judging import deviation as _deviation
from algorithms.utils import PlanResult, Touch

DRAG_WINDOW_MS = DRAG_WINDOW * 1000.0
"""Drag 判定窗，毫秒版 —— 这个文件里的扫描都按毫秒走。"""

FLICK_WINDOW_MS = FLICK_WINDOW * 1000.0
"""Flick 候选窗，毫秒版。"""


def check_stream(result) -> list[str]:
    problems: list[str] = []
    state: dict[int, bool] = defaultdict(bool)
    last_timestamp = None
    for timestamp, events in result.frames:
        if last_timestamp is not None and timestamp < last_timestamp:
            problems.append(f"时间戳倒序：{last_timestamp} -> {timestamp}")
        last_timestamp = timestamp
        for event in events:
            down = state[event.pointer]
            if event.action is Touch.DOWN:
                if down:
                    problems.append(f"{timestamp}ms 指针 {event.pointer} 重复 DOWN")
                state[event.pointer] = True
            elif event.action is Touch.UP:
                if not down:
                    problems.append(f"{timestamp}ms 指针 {event.pointer} 未按下就 UP")
                state[event.pointer] = False
            elif event.action is Touch.MOVE and not down:
                problems.append(f"{timestamp}ms 指针 {event.pointer} 未按下就 MOVE")
    for pointer, down in state.items():
        if down:
            problems.append(f"指针 {pointer} 到结尾都没有抬起")
    return problems


def _lateral(tracks, line, note, seconds: float) -> float:
    """`seconds` 那一刻，离目标最近的那根手指的横向偏差；屏幕上一根手指都没有就是 inf。"""
    fingers = tracks.down_at(round(seconds * 1000))
    if not fingers:
        return math.inf
    return min(_deviation(line, note, finger, seconds) for finger in fingers)


def check_coverage(chart: Chart, result) -> tuple[int, list[str], float]:
    tracks = Tracks(result)
    screen = chart.screen
    misses: list[str] = []
    worst = 0.0
    total = 0

    for line in chart.lines:
        for note in line.notes:
            total += 1
            placement = place_note(line, note, screen, retime=note.kind is NoteType.FLICK)

            # Drag / Flick 逐帧比手指位置：窗口里要存在整整一帧都在容差内（Flick 另要 isNewFlick）。
            if note.kind in (NoteType.DRAG, NoteType.FLICK):
                window = DRAG_WINDOW_MS if note.kind is NoteType.DRAG else FLICK_WINDOW_MS
                run, best = _longest_run(tracks, line, note, placement.seconds, window)
                worst = max(worst, min(best, 99.0))
                if run < FRAME_MS:
                    where = "从没手指" if math.isinf(best) else f"最近偏差 {best:.3f}"
                    misses.append(
                        f"{note.kind.name} @ {placement.seconds:.3f}s 判定窗里最长只有 "
                        f"{run:.0f}ms 有手指在位（不足一帧 {FRAME_MS:.1f}ms；{where}）"
                    )
                continue

            # Tap / Hold 头判：只在"按下那一帧"判定，所以既要在点上，也得真的按下去了
            ok, best = _head(tracks, line, note, placement.seconds)
            worst = max(worst, min(best, 99.0))
            if not ok:
                what = "无穷" if math.isinf(best) else f"{best:.3f}"
                misses.append(f"{note.kind.name}头 @ {placement.seconds:.3f}s 横向偏差 {what}")
                continue

            # hold 主体：扫全程，只要求"没有一段超过宽容窗口完全落空"（见 HOLD_GRACE_MS）
            if note.kind is not NoteType.HOLD:
                continue
            end = placement.seconds + note.hold
            moment = placement.seconds
            gap = 0.0
            while moment <= end:
                lateral = _lateral(tracks, line, note, moment)
                if lateral <= TOLERANCE:
                    gap = 0.0
                else:
                    gap += HOLD_SAMPLE_MS / 1000.0
                    if gap > HOLD_GRACE_MS / 1000.0:
                        worst = max(worst, min(lateral, 99.0))
                        near = "没手指" if math.isinf(lateral) else f"最近 {lateral:.3f}"
                        misses.append(
                            f"HOLD主体 @ {moment:.3f}s 起连续 {gap * 1000:.0f}ms 落空"
                            f"（{near}）"
                        )
                        break
                moment += HOLD_SAMPLE_MS / 1000.0

    return total, misses, worst


def _head(tracks: Tracks, line, note, seconds: float) -> tuple[bool, float]:
    """Tap / Hold 的头判：窗口里有没有**一次合格的按下**，以及最小偏差。

    `CheckNote` 只为 `phase == Began` 的手指调用（`JudgeControl::Update` 里那一句），
    所以被 MOVE 过来的手指判不到；接受窗是 `realTime − 0.01 ~ realTime + 0.22`。
    """
    best = math.inf
    for timestamp, position in tracks.downs_between(
        seconds * 1000.0 - 20.0, seconds * 1000.0 + 160.0
    ):
        best = min(best, _deviation(line, note, position, timestamp / 1000.0))
    return best <= TOLERANCE, best


def _longest_run(
    tracks: Tracks, line, note, seconds: float, window_ms: float
) -> tuple[float, float]:
    """判定窗口里"有手指落在容差内"最长的一段（毫秒），以及全窗口的最小偏差。

    要求"最长的一段够一帧"而不是"整段不许有空洞"：帧等间隔，一段不短于一帧的连续覆盖里
    必然含有一帧，任何帧相位下都至少有一帧判得到；短于一帧就是掷硬币。
    """
    lo = seconds * 1000.0 - window_ms
    hi = seconds * 1000.0 + window_ms
    best = math.inf
    run = 0.0
    longest = 0.0
    moment = lo
    while moment <= hi:
        lateral = _lateral(tracks, line, note, moment / 1000.0)
        best = min(best, lateral)
        if lateral <= DRAG_TOLERANCE:
            run += RUN_STEP_MS
            longest = max(longest, run)
        else:
            run = 0.0
        moment += RUN_STEP_MS
    return longest, best


def check_dwell() -> list[str]:
    """覆盖率判据自己的回归用例：手指在位多久才算"够一帧"。

    钉住的是 `check_coverage` 报不报，不是 `_longest_run` 这个函数：手指到位后停一整帧
    以上不该报，只停 5ms 就抬起必须报 —— 防的是判据被"简化"回"那一瞬间在不在点上"。
    """
    from algorithms.chart import JudgeLine, Note, NoteType, Track
    from algorithms.geometry import Screen
    from algorithms.utils import PlanResult, TouchEvent
    from algorithms.utils import Touch as Action

    note = Note(kind=NoteType.DRAG, seconds=1.0, hold=0.0, offset=8.0)
    line = JudgeLine(bpm=120.0, notes=[note], move=Track(), rotate=Track())
    chart = Chart(3, 0.0, Screen(16.0, 9.0), [line])

    def coverage(dwell_ms: int) -> list[str]:
        frames = [
            (1000, (TouchEvent(1000, Action.DOWN, 8.0, 4.5),)),
            (1000 + dwell_ms, (TouchEvent(1000, Action.UP, 8.0, 4.5),)),
        ]
        result = PlanResult("stub", Screen(16.0, 9.0), frames)
        total, misses, _ = check_coverage(chart, result)
        if total != 1:
            return [f"合成谱面应当只有 1 个音符，实际 {total}"]
        return misses

    problems: list[str] = []
    if coverage(100):
        problems.append(f"停 100ms 的摆放被报了：{coverage(100)[0]}")
    if not coverage(5):
        problems.append("只停 5ms 的摆放没被报出来 —— 判据退化成「那一瞬间在不在点上」了")
    return problems


def check_flick(chart: Chart, result: PlanResult) -> list[str]:
    """滑键手势自检：每个 flick 都得点得亮。

    模型只有一份，在 `algorithms/judging.py`（`flick_edges` + `lit_flicks`）：`CheckFlick`
    只在手指 `isNewFlick` 那一帧跑，在 `nowTime ± 0.14s` 里挑第一个还没判过、横向 < 2.1 的
    flick 点亮，一次起手只点亮一个 —— 只划一下就必然漏。这里只报"谁没被点亮"。
    """
    flicks = [slot for slot in note_table(chart) if slot.kind is NoteType.FLICK]
    if not flicks:
        return []

    edges = flick_edges(result)
    lit = lit_flicks(flicks, edges)
    missing = [slot for slot in flicks if slot.index not in lit]
    if not missing:
        return []
    chances = min(
        sum(1 for moment, position in edges if flick_candidate(slot, moment, position))
        for slot in missing
    )
    return [
        f"有 {len(missing)} 个 flick 点不亮（最少的那个只有 {chances} 次起手机会）："
        + "、".join(f"{slot.seconds:.3f}s" for slot in missing[:5])
    ]
