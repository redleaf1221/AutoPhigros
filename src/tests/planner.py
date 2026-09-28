"""规划器自检：按下落点不许"顺手判掉"别的音符（`geometric` 的补集落脚点）。

判据是**风格约束**，不是判定：每一发 `DOWN` 若落在某个 TAP / Hold 头的容差带里，那一下就必须
是"冲着它去的"（Δ 在 `HEAD_WINDOW` 里）；否则这一发就是计划自己埋的蹭键。
"""

from __future__ import annotations

import math

from algorithms.chart import Chart, JudgeLine, Note, NoteType, Track
from algorithms.geometry import Screen
from algorithms.judging import (
    HEAD_WINDOW,
    SCAN_AHEAD,
    SCAN_BACK,
    TAP_TOLERANCE,
    deviation,
    note_table,
)
from algorithms.utils import Touch

SCREEN = Screen(16.0, 9.0)
TAP_X = 8.0


def _line(y: float, notes: list[Note], *, angle: float = 0.0) -> JudgeLine:
    move, rotate = Track(), Track()
    move.cut(0.0, 1000.0, complex(TAP_X, y), complex(TAP_X, y))
    rotate.cut(0.0, 1000.0, angle, angle)
    return JudgeLine(bpm=120.0, notes=notes, move=move, rotate=rotate)


def stray_downs(chart: Chart, result) -> list[str]:
    """每一发落在别人容差带里、又不是冲着它去的 `DOWN`。"""
    slots = [
        slot for slot in note_table(chart) if slot.kind in (NoteType.TAP, NoteType.HOLD)
    ]
    problems: list[str] = []
    for timestamp, events in result.frames:
        moment = timestamp / 1000.0
        for event in events:
            if event.action is not Touch.DOWN:
                continue
            position = complex(event.x, event.y)
            for slot in slots:
                delta = moment - slot.seconds
                if not -SCAN_BACK < delta < SCAN_AHEAD:
                    continue
                lateral = deviation(slot.geometry, slot.note, position, moment)
                if lateral >= TAP_TOLERANCE:
                    continue
                if HEAD_WINDOW[0] <= delta <= HEAD_WINDOW[1]:
                    continue
                problems.append(
                    f"{moment:.3f}s 指针 {event.pointer} 的按下落在 "
                    f"{slot.kind.name}@{slot.seconds:.3f}s 的容差带里（Δ={delta * 1000:+.0f}ms、"
                    f"横向 {lateral:.3f}）—— 这一下不是冲着它去的"
                )
    return problems


class _Quiet:
    def track(self, iterable, description):
        return iter(iterable)


def check_planner() -> list[str]:
    """一个 drag 挤在一个还没按的 Hold 前面：新按下的落点必须避开它的容差带。"""
    problems: list[str] = []
    try:
        from algorithms import geometric
        import planner  # noqa: F401 - 顺带确认规划器包能整体 import
    except ImportError as error:  # noqa: BLE001 - 依赖缺失不该让整份自检跑不起来
        return [f"导入规划器失败，跳过规划器自检：{error}"]

    notes = [Note(NoteType.DRAG, 10.000, 0.0, 0.0), Note(NoteType.HOLD, 10.150, 0.5, 0.0)]
    chart = Chart(3, 0.0, SCREEN, [_line(2.0, notes)])
    result = geometric.GeometricPlanner().plan(chart, _Quiet())

    problems += stray_downs(chart, result)

    # 正面判据：这一发真的"先按到别处、再 MOVE 过来"了（否则上面那条空过就没意义）
    moved = False
    for timestamp, events in result.frames:
        for event in events:
            if event.action is not Touch.DOWN or abs(event.x - TAP_X) < 1e-9:
                continue
            for later, later_events in result.frames:
                if later <= timestamp or later - timestamp > 100:
                    continue
                if any(
                    item.pointer == event.pointer
                    and item.action is Touch.MOVE
                    and abs(item.x - TAP_X) < 1e-9
                    for item in later_events
                ):
                    moved = True
    if not moved:
        problems.append(
            "drag 那一发按下落在 Hold 的容差带里，却没有「先按到补集、再 MOVE 到目标」——"
            "补集落脚点这一条被摘掉了？"
        )

    # 关掉这一条（down_lead_ticks=0）时上面两条都该红 —— 反向验证
    off = geometric.GeometricPlanner({"down_lead_ticks": 0}).plan(chart, _Quiet())
    if not stray_downs(chart, off):
        problems.append(
            "把 down_lead_ticks 关到 0 之后，按下依旧没有落在别人带子里 —— "
            "判据抓不住东西（多半是容差带算错了）"
        )

    # 判定区只"擦个边"时不许并：并了就等于把这个 drag 交给一个够不着它的按下
    from tests.coverage import check_coverage

    clipped = Chart(
        3,
        0.0,
        SCREEN,
        [
            _line(2.0, [Note(NoteType.TAP, 10.000, 0.0, 0.0)]),
            # 竖着的线：它的判定区是一条横带，正好从 tap 的判定区中间横穿过去 —— 只擦个边
            _line(5.0, [Note(NoteType.DRAG, 10.000, 0.0, 0.0)], angle=math.pi / 2),
        ],
    )
    _, misses, _ = check_coverage(clipped, geometric.GeometricPlanner().plan(clipped, _Quiet()))
    if misses:
        problems.append(f"判定区只擦个边却并了，drag 于是没人管：{misses[0]}")
    _, loose, _ = check_coverage(
        clipped, geometric.GeometricPlanner({"merge_min_area": 0.0}).plan(clipped, _Quiet())
    )
    if not loose:
        problems.append(
            "把 merge_min_area 关到 0 之后 drag 依旧有覆盖 —— 判据抓不住东西"
            "（多半是两块判定区没有真的只擦个边）"
        )

    # `geometric_pure` 的"吸附 + 顺手判掉"：一个 drag 的容差区够得着同刻那个 tap 的判定点，
    # 一发按下就该判两个 —— 按下次数必须比 `geometric` 少
    from algorithms import registry
    from algorithms.utils import Touch as Action

    def downs(result) -> int:
        return sum(
            1
            for _, events in result.frames
            for event in events
            if event.action is Action.DOWN
        )

    pair = Chart(
        3,
        0.0,
        SCREEN,
        [
            _line(
                2.0,
                [
                    Note(NoteType.TAP, 10.000, 0.0, 0.0),
                    Note(NoteType.DRAG, 10.000, 0.0, 1.0),
                ],
            )
        ],
    )
    plain = registry.create("geometric").plan(pair, _Quiet())
    pure = registry.create("geometric_pure").plan(pair, _Quiet())
    if downs(pure) >= downs(plain):
        problems.append(
            f"geometric_pure 没省下手指：它按了 {downs(pure)} 次、geometric 按了 {downs(plain)} 次"
            "（落点没吸附到那个 tap 的判定点上，或者吸附了却没敢省掉它那一次按下）"
        )
    _, pure_misses, _ = check_coverage(pair, pure)
    if pure_misses:
        problems.append(f"geometric_pure 省手指却把覆盖省掉了：{pure_misses[0]}")

    return problems
