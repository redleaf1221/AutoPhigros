#!/usr/bin/env python3
"""规划器自检：拿真实谱面跑一遍，确认规划结果既能打、又存得回去。

不连设备也能跑，用的就是 ``charts/`` 里已经采集到的谱面。改过任何规划器之后跑一下，
比盯着代码看靠谱。三件事：

1. **事件流自洽** —— 每个指针的 DOWN / UP 严格配对，事件按时间有序，没有未抬起的指针。
2. **覆盖完整** —— 每个音符被判定的那一刻，确实有手指按在它的判定点上。

   判据用的是 Phigros 的**垂直判定**（详见 ``algorithms/geometry.py`` 顶部）：
   ``JudgeControl::GetFingerPosition`` 为每根手指、每条判定线只算两个量（判定线局部
   坐标下的横向与法向分量），而 ``JudgeControl::CheckNote`` 里**只**把横向分量拿去比
   ``touchPos >= 1.9``，法向分量从来没用过。也就是判定线"无限细"，触点离线多远无所谓，
   只看它投到线上落在哪儿 —— 所以这里比的是沿判定线的横向偏差，不是欧氏距离。

   而且比的是**游戏真正能看见手指的那些时刻**。游戏不会连续读手指位置：`fingerPositionX`
   每帧更新一次，两次事件之间手指在屏幕上是不动的，判定线却一直在动 —— 于是"手指投影到
   判定线上的横向偏移"逐帧漂移。所以判据是"判定窗口里存在**一整帧**都落在容差内"，
   而不是"把窗口当成连续时间、只要有一瞬间对上就算数"。两者差得很远：

   * Drag 的窗口是 ±100ms，但规划器只在自己算好的那一毫秒把手指摆到位 —— 如果
     1ms 之后这根手指就被挪走/抬起，窗口里能对上手指的只剩那 1ms，60fps 下
     是否撞上一帧全看运气。**实测 Eradication Catastrophe IN 上就漏了 4 个 drag。**
   * Tap / Hold 的头判反而只看"按下那一帧"（`phase == Began`），所以按下之后马上抬起
     没关系 —— 但**必须真的有一次 DOWN**，用手感很好的 MOVE 顶替是不会被判的。

3. **存得回去** —— 走一遍 ``planner.plan(save=True)`` 与 ``storage``，``.psap`` 编解码
   往返一致，文件真的产出。

4. **闸门放行** —— 拿假消息喂 ``main.Agent``，确认"该放行时一定放行、一局只放行一次"。
   游戏是停在闸门上等主机的：少放一次游戏永远卡死，多放一次下一关的闸门会被提前放掉。

5. **控制台** —— ``console.py`` 的命令解析与那几个运行时旋钮，重点是 ``latency`` 的
   三种写法和"打错命令不该把面板带走"。

用法（conda 环境 auto_phigros）：``python selftest.py``
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import sys
import tempfile
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent

import planner  # noqa: E402
from algorithms import catalog, create  # noqa: E402
from algorithms.chart import Chart, NoteType  # noqa: E402
from algorithms.geometry import Screen, place_note  # noqa: E402
from algorithms.utils import PlanResult, SilentProgress, Touch  # noqa: E402
from storage import (  # noqa: E402
    ChartRef,
    decode_plan,
    encode_plan,
    plan_path,
    plan_path_for,
    save_chart,
)

TOLERANCE = 1.9 * 0.9
"""CheckNote 的阈值是 1.9，单位是谱面的 positionX；乘 0.9 换算到 16 宽的虚拟屏幕。

Tap 与 Hold 的头判走这条。注意它只在**手指的 phase 是 Began 的那一帧**才被调用
（`JudgeControl::Update` 里 `if (finger.phase == 0) CheckNote(...)`）—— 也就是说
判定发生在"按下事件被处理的那一帧"，按下之后手指停多久都无所谓。
"""

DRAG_TOLERANCE = 2.1 * 0.9
"""`DragControl::Judge` 的横向容差是 **2.1**（与 Flick 同），不是 Tap 的 1.9。"""

DRAG_WINDOW_MS = 100.0
"""Drag 的判定窗：`|note.realTime − nowTime| <= 0.1` 时才会去比对手指。"""

FLICK_WINDOW_MS = 140.0
"""Flick 的候选窗：`CheckFlick` 把它收窄到 `PerfectTimeRange × 1.75 = ±0.14s`。"""

FRAME_MS = 1000.0 / 60.0
"""一帧（60fps）。下面是这个自检里最要紧的一个数，理由见 `check_coverage`。"""

RUN_STEP_MS = 1.0
"""扫"手指在位"的步长。1ms 的粒度对 16.7ms 的判据来说够细了。"""

HOLD_SAMPLE_MS = 8
"""扫 hold 主体时的步长，与规划器的采样间隔一致。"""

HOLD_GRACE_MS = 67
"""hold 中途"没有手指在容差内"最多能连多久。

来自游戏，不是人情：`HoldControl::Judge` 里 `_safeFrame` 初值 2，手指落空时每帧减 1，
减到 < 0 才判 Miss —— **连续 3 帧落空都忍得下来，第 4 帧才判**。按 60fps 折算约 67ms。

这条宽容是实测出来的，不是理论值：Glaciaxion IN 有一条判定线每 53ms 在两个位置之间
跳一次（归一化 0.2 ↔ 0.8，世界坐标 3.2 ↔ 12.8）。手指按在其中一个位置，另一个相位
**必然**落空 —— `geometric` 就是这么打的，实机 All Perfect。所以判据只能是
"没有一段超过这个窗口完全落空"，而不是"每一刻都在容差内"：后者在闪烁目标上是掷硬币，
报不报错取决于那 8ms 落在哪个相位。

> 也幸亏有这条宽容，才没把 `geometric` 冤成漏音。真要吹毛求疵：它是在 60fps 下勉强过关
> （53ms 落空 ≈ 3 帧），120fps 下同一段就是 6 帧、必 Miss —— 想稳就跟 `conservative`
> 走，它每 8ms 重采一次线位置，手指跟着跳。
"""


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


class Tracks:
    """把事件流按指针拆开，方便回答"某时刻哪些手指在屏幕上、在哪"。"""

    def __init__(self, result) -> None:
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
        return [
            (timestamp, position)
            for timestamp, _, position in self.downs
            if lo_ms <= timestamp <= hi_ms
        ]


def _deviation(line, note, position: complex, seconds: float) -> float:
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

            # Drag / Flick 都是**逐帧**比对手指位置的：判定线在动，而手指在两次事件之间
            # 不动，所以"手指投影到判定线上的横向偏移"逐帧漂。要求窗口里存在一整帧都在
            # 容差内（见 `_longest_run`）。Flick 还额外要 `isNewFlick`（够快的滑动），
            # 那是输入速度的事，规划这一层看不到，这里只保证位置与时长。
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

    只看"那一刻有没有手指在点上"是不够的：`CheckNote` 只为 `phase == Began` 的手指
    调用（`JudgeControl::Update` 里那一句），也就是说判定发生在"按下事件被处理的那一帧"。
    一根早就按在屏幕上、只是被 MOVE 过来的手指是判不到 tap 的 —— 必须真的 DOWN 一次。
    Tap 的接受窗口是 `realTime − 0.01 ~ realTime + 0.22`，按下那一帧落在里面就算数。
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

    为什么是"最长的一段"而不是"整段都不许有空洞"：帧是等间隔的，一段不短于一帧的
    连续覆盖里**必然**含有一帧，所以只要最长的一段够一帧，任何帧相位下都至少有一帧
    能判到；反过来，覆盖短于一帧时能不能判到就纯看帧落在哪儿 —— 也就是掷硬币。
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

    这条判据是本项目里最容易被"简化"回去的东西 —— 它看着啰嗦（"窗口里最长的一段连续
    覆盖够不够一帧"），很容易被改回"那一瞬间在不在点上"。而后者在
    `Eradication Catastrophe` IN 上放过 4 个必漏的 drag（手指到位 1ms 后就被抬起），
    也在 `geometric` 上放过一批只停 1 tick（8ms）的。所以拿两根合成时间轴把**判定结论**
    钉住（不是钉住 `_longest_run` 这个函数，而是钉住 `check_coverage` 报不报）：

    * 手指到位后停一整帧以上 → 不该报；
    * 只停 5ms 就抬起 → 必须报。
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


def check_storage(text: str, planner_name: str, result) -> list[str]:
    """走一遍 planner.py 与 storage.py 的公开接口：规划 -> 落盘 -> 读回来。"""
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as workspace:
        plans = Path(workspace) / "plans"
        ref = ChartRef.of_text(text, seq=1, context={"songsId": "SelfTest", "songsLevel": "EZ"})

        produced = planner.plan(text, planner=planner_name, ref=ref, cache=True, directory=plans)
        if produced.frames != result.frames:
            problems.append("planner.plan 的结果和直接调用规划器不一致")

        path = plan_path(produced, ref, plans)
        if not path.is_file():
            problems.append("规划结果没写出来")
        else:
            restored = decode_plan(path.read_bytes())
            if restored.planner != result.planner:
                problems.append(f"解码出的规划器名不对：{restored.planner}")
            if restored.screen != result.screen:
                problems.append(f"解码出的屏幕不对：{restored.screen}")
            if restored.frames != result.frames:
                problems.append("解码出的事件流与原始结果不一致")
            if len(encode_plan(restored)) != path.stat().st_size:
                problems.append("编解码往返后字节数变了")
            if not path.with_suffix(".meta.json").is_file():
                problems.append("规划结果的 .meta.json 没写出来")

        charts = Path(workspace) / "charts"
        payload = save_chart(text, ref, charts, notes_in_json=len(result.frames))
        if not payload.is_file() or not (charts / f"{ref.stem}.meta.json").is_file():
            problems.append("谱面或它的 .meta.json 没写出来")
        if payload.read_text(encoding="utf-8") != text:
            problems.append("谱面原文写进去变了样")
    return problems


def _legacy_mirror(value: float) -> float:
    """v1 打包坐标 ``x*1000 + y`` 的水平镜像，照抄 ``JudgeLine::Mirror`` 那三行。

    反编译 0x1d28afc，``oldVersion`` 那一支算的是（0x4956D800 = ``880000.0f``）：

    .. code-block:: text

        新值 = 880000 − 旧值 + 2 × (旧值 mod 1000)

    把 ``旧值 = 1000X + Y`` 代进去就是 ``1000(880 − X) + Y`` —— 即 ``x → 880 − x``、``y`` 不动。
    照抄而不是化简，是为了让"模型"与"游戏"的差别一眼可见。
    """
    return 880000.0 - value + 2.0 * math.fmod(value, 1000.0)


def mirror_chart(text: str) -> str:
    """把一份官谱按 ``Chart::Mirror`` 的规则镜像过来 —— 测试用的"游戏行为"模型。

    这是**独立于实现**的判据：``Chart::Mirror`` @ ``0x1d286ac`` 只动三样东西 ——
    移动事件 ``x → 1 − x``、旋转事件 ``θ → −θ``、音符 ``positionX → −positionX``。
    有了它，就能直接验"把规划结果翻一下"是不是镜像后谱面的解，而不用真去开一次游戏。

    v1 的老谱要另算：``Chart::Mirror`` 把 ``formatVersion == 1`` 作为 ``oldVersion`` 传下去，
    ``JudgeLine::Mirror`` 于是走打包整数的分支（见 :func:`_legacy_mirror`）。这个分支是**真存在**的
    —— 采到的 ``Credits`` HD 就是 v1，所以这里不能只伺候 v3。
    """
    chart = json.loads(text)
    old_version = int(chart.get("formatVersion") or 0) == 1
    for line in chart["judgeLineList"]:
        for event in line.get("judgeLineMoveEvents") or ():
            if old_version:
                event["start"] = _legacy_mirror(event["start"])
                event["end"] = _legacy_mirror(event["end"])
            else:
                event["start"] = 1.0 - event["start"]
                event["end"] = 1.0 - event["end"]
        for event in line.get("judgeLineRotateEvents") or ():
            event["start"] = -event["start"]
            event["end"] = -event["end"]
        for key in ("notesAbove", "notesBelow"):
            for note in line.get(key) or ():
                note["positionX"] = -note["positionX"]
    return json.dumps(chart)


def check_mirror(text: str, result: PlanResult) -> tuple[int, list[str], float]:
    """镜像自检：把**原始谱面**的规划结果整体翻过来，拿去对**镜像后的谱面**。

    ``Chart::Mirror`` 对场景做的是整个画面绕中线左右翻（判定线 ``x → 1 − x``、
    ``θ → −θ``、音符 ``positionX → −positionX``），三者合起来正好是判定线上每个点
    ``(x, y) → (16 − x, y)``。既然要按的每个点都只是翻了个身，规划结果跟着翻一下就该
    照样全中 —— 这就是"镜像不重算"的全部依据，也是它唯一的验收标准。
    """
    problems: list[str] = []

    flipped = result.mirrored()
    if flipped.screen != result.screen:
        problems.append("镜像改变了屏幕尺寸")
    if flipped.event_count != result.event_count:
        problems.append(f"镜像改变了事件数：{result.event_count} -> {flipped.event_count}")
    if flipped.mirrored().frames != result.frames:
        problems.append("镜像两次回不到原样")

    mirrored_chart = Chart.parse(mirror_chart(text))
    if mirrored_chart.note_count != Chart.parse(text).note_count:
        problems.append("镜像改变了音符数")

    return check_coverage(mirrored_chart, flipped)


FLICK_WINDOW_MS = 140
"""``JudgeControl::CheckFlick`` 的候选窗口：``PerfectTimeRange × 1.75 = 0.08 × 1.75``。"""

FLICK_JUMP = 1.0
"""多大的位移算一次"手指跳变"（虚拟屏单位）。

这判据是**保守**的那一侧：只有跳变才一定够快、让 ``FingerManagement::Update`` 把
``Fingers.isNewFlick`` 置起来。真机上慢一点的划动有时也算数 —— 所以它说没问题就是真没问题
（那时机会只会更多），它报问题就值得看一眼。
"""


def check_flick(chart: Chart, result: PlanResult) -> list[str]:
    """滑键手势自检：每个 flick 在游戏那套规则下都得**点得亮**。

    ``FlickControl::Judge`` 自己不看手指 —— 它只等 ``ChartNote.isJudgedForFlick``。点灯的是
    ``JudgeControl::CheckFlick``（``0x1d21828``）：只有那一帧手指带 ``Fingers.isNewFlick``
    （一次"新起手"，由 ``FingerManagement::Update`` 按瞬时速度算）时才跑；跑的时候在
    ``nowTime ± 0.14s`` 里**按时间顺序**挑第一个还没判过、且
    ``|positionX − fingerPositionX| < 2.1`` 的 flick 点亮，然后把手指那个标志清掉。

    两条推论就是这个自检的全部内容：

    1. **一次"新起手"只点亮一个音符**，还会被窗口里更早的 flick 抢走；
    2. 一个 flick 只划一下 = 只有一次机会，被抢走就必然漏。

    实测就是这么漏的：``conservative`` 原先每个 flick 只划一下，按这套规则走一遍，
    Dlyrotz HD 上 70 个 flick 里有 **7 个永远点不亮** —— 而真机上一局只漏一两个，
    因为慢划动偶尔也算一次起手。把每个 flick 划两下之后就全归零了
    （``flick_repeats``，见 ``ConservativeConfig``）。
    """
    flicks = sorted(
        (
            (note.seconds, index, note, line)
            for index, line in enumerate(chart.lines)
            for note in line.notes
            if note.kind is NoteType.FLICK
        ),
        key=lambda item: item[0],
    )
    if not flicks:
        return []

    # 每一次"新起手"：手指位置的一次跳变（同一根手指、相邻两个事件之间）
    edges: list[tuple[int, complex]] = []
    for items in Tracks(result).items.values():
        previous = None
        for timestamp, action, position in items:
            if previous is not None and action is not Touch.UP and abs(position - previous) >= FLICK_JUMP:
                edges.append((timestamp, position))
            previous = position
    edges.sort(key=lambda edge: edge[0])

    # 照 CheckFlick 的规则点亮：窗口里时刻最早的那个
    marked: set[int] = set()
    for timestamp, position in edges:
        chosen = None
        for seconds, index, note, line in flicks:
            if id(note) in marked or abs(seconds * 1000 - timestamp) > FLICK_WINDOW_MS:
                continue
            if _deviation(line, note, position, timestamp / 1000.0) >= DRAG_TOLERANCE:
                continue
            if chosen is None or seconds < chosen[0]:
                chosen = (seconds, index, note)
        if chosen is not None:
            marked.add(id(chosen[2]))

    missing = [item for item in flicks if id(item[2]) not in marked]
    if not missing:
        return []
    chances = min(
        sum(
            1
            for timestamp, position in edges
            if abs(seconds * 1000 - timestamp) <= FLICK_WINDOW_MS
            and _deviation(line, note, position, timestamp / 1000.0) < DRAG_TOLERANCE
        )
        for seconds, _, note, line in missing
    )
    problems = [
        f"有 {len(missing)} 个 flick 点不亮（最少的那个只有 {chances} 次起手机会）："
        + "、".join(f"{seconds:.3f}s" for seconds, _, _, _ in missing[:5])
    ]
    return problems


class Recorder:
    """顶掉 frida 的 script，只记下 post 出去的东西。"""

    def __init__(self) -> None:
        self.posted: list[dict] = []

    def post(self, message: dict) -> None:
        self.posted.append(message)

    @property
    def released(self) -> list[int]:
        return [int(message.get("payload", {}).get("seq", 0)) for message in self.posted]


def _trunk_args(**overrides) -> argparse.Namespace:
    """``Controller`` 真正会读的那几个命令行参数 —— 就这几个，多一个都不给。"""
    fields = {
        "planner": "stub",
        "latency": 0.0,
        "save_chart": False,
        "cache": True,
    }
    return argparse.Namespace(**{**fields, **overrides})


def check_gate() -> tuple[list[str], str]:
    """闸门自检：不连设备，只往 agent 里喂消息。返回 (问题列表, 被吞掉的输出)。"""
    try:
        import main as trunk
    except ImportError as error:  # frida 没装也不该让整份自检跑不起来
        return [f"导入 main.py 失败，跳过闸门自检：{error}"], ""

    problems: list[str] = []
    chart_text = '{"authored": 1}'

    def build() -> tuple[object, Recorder]:
        agent = trunk.Agent(Path("unused.js"))
        recorder = Recorder()
        agent._script = recorder  # noqa: SLF001 - 自检就是要顶掉真 frida
        return agent, recorder

    def feed(agent, event: str, **payload) -> None:
        agent._on_message({"type": "send", "payload": {"event": event, **payload}}, None)

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        # 1) 正常一局：谱面 -> 音符数 -> 开谱，放行恰好一次，现场也配得上
        agent, recorder = build()
        seen: list[object] = []
        agent.on_level_start = seen.append
        feed(agent, "chart", seq=7, hash="aaaa", context={"songsId": "SelfTest"}, json=chart_text)
        feed(agent, "chart-parsed", notes=42)
        feed(agent, "level-start", seq=1, chartSeq=7, mirror=False)

        if recorder.released != [1]:
            problems.append(f"正常一局应当恰好放行一次 seq=1，实际 {recorder.released}")
        if len(seen) != 1:
            problems.append(f"on_level_start 应当被调用一次，实际 {len(seen)} 次")
        else:
            start = seen[0]
            if start.mirror is not False:
                problems.append(f"镜像开关读错：{start.mirror!r}")
            if start.chart is None or start.chart.notes_reported != 42:
                problems.append("游戏报的音符数没有配到这一局的谱面上")

        # 2) 作业抛异常也必须放行 —— 卡死游戏比规划失败严重得多
        agent, recorder = build()

        def boom(_start) -> None:
            raise RuntimeError("规划器炸了")

        agent.on_level_start = boom
        feed(agent, "level-start", seq=2, chartSeq=7, mirror=True)
        if recorder.released != [2]:
            problems.append(f"作业抛异常时也必须放行，实际 {recorder.released}")

        # 3) 开谱 -> 规划：喂进去的一定是 FromJson 的那份原文（**规范解**），
        #    镜像不进规划、而是交给播放器在执行时翻
        for mirror, expected in ((True, True), (False, False), (None, False)):
            agent, recorder = build()
            calls: list[dict] = []
            plays: list[tuple[object, bool, int]] = []
            # 真正跑的是 Controller.handle_level_start，只把最后一步"架播放器"换成记账 ——
            # 于是这一段验的是真代码，而不是它的一份复述
            controller = trunk.Controller(_trunk_args())
            controller.play = lambda plan, *, mirror, seq: plays.append((plan, mirror, seq))
            real_plan = trunk.planner.plan
            real_progress = trunk.planner.TqdmProgress

            def fake_plan(text, **kwargs):
                calls.append({"text": text, **kwargs})
                return PlanResult(
                    planner=kwargs.get("planner", "stub"), screen=Screen(16.0, 9.0), frames=[]
                )

            trunk.planner.plan = fake_plan
            trunk.planner.TqdmProgress = lambda: None
            try:
                feed(agent, "chart", seq=7, hash="aaaa", json=chart_text)
                agent.on_level_start = controller.handle_level_start
                feed(agent, "level-start", seq=3, chartSeq=7, mirror=mirror)
            finally:
                trunk.planner.plan = real_plan
                trunk.planner.TqdmProgress = real_progress

            if len(calls) != 1:
                problems.append(f"mirror={mirror} 时规划器应当被调用一次，实际 {len(calls)} 次")
                continue
            if calls[0]["text"] != chart_text:
                problems.append(f"mirror={mirror} 时喂给规划器的不是 FromJson 的原文")
            if calls[0]["cache"] is not True:
                problems.append(f"mirror={mirror} 时没有把缓存开关传下去")
            if "mirror" in calls[0]["ref"].context:
                problems.append(f"mirror={mirror} 时镜像被塞进了缓存的身份里")

            if len(plays) != 1:
                problems.append(f"mirror={mirror} 时播放器应当被架一次，实际 {len(plays)} 次")
            elif plays[0][1] is not expected:
                problems.append(f"mirror={mirror} 时播放器拿到的是 {plays[0][1]!r}")

        # 4) 没收到谱面就直接放行，不能崩也不能卡
        agent, recorder = build()
        controller = trunk.Controller(_trunk_args())
        controller.play = lambda plan, *, mirror, seq: None
        agent.on_level_start = controller.handle_level_start
        feed(agent, "level-start", seq=4, chartSeq=99, mirror=True)
        if recorder.released != [4]:
            problems.append(f"没有谱面时也必须放行，实际 {recorder.released}")

    captured = buffer.getvalue()
    return problems, captured if problems else ""


def check_clock() -> list[str]:
    """游戏时钟自检：喂合成的样本流，看估出来的是不是真的。

    ``GameClock`` 是"完美同步"的全部依据（触控模块按它排事件），而它要对付的正是
    "样本必然晚到、还会抖动"这件事。判据只有一条：**宁可偏晚，绝不能偏早** ——
    偏晚最多是晚按一下，偏早就是抢拍。

    1. 稳定推进 + 固定延迟：估计值应当恰好晚一个"最小延迟"；
    2. 延迟抖动：取最小值应当把抖动滤掉；
    3. 起播前的等待（`nowTime` 被钉在 0.00001）：不能提前放行，音乐起来后要重新对表；
    4. 中途暂停：暂停期间不能发事件，恢复后也要重新对表；
    5. 时钟倒着走：当成重开一局。
    """
    import touch

    problems: list[str] = []
    tolerance = 0.001

    class Harness:
        """一个被我们完全控制的"主机时钟"。"""

        def __init__(self) -> None:
            self.moment = 0.0
            self.clock = touch.GameClock(now=lambda: self.moment)

        def feed(self, moment: float, value: float) -> None:
            """在主机时刻 `moment` 收到"游戏时间是 value"的样本。"""
            self.moment = moment
            self.clock.feed(value)

        def run(self, game_at, *, start: float, stop: float, step: float, delay: float) -> None:
            moment = start
            while moment <= stop:
                self.feed(moment, game_at(moment - delay))
                moment += step

    def lag(harness: Harness, truth: float) -> float:
        """估计值落在真值后面多少秒。正 = 偏晚（安全），负 = 偏早（抢拍）。

        "偏晚"是刻意的：`min(h − v)` 把真实偏移**高估**了一个最小传输延迟，
        于是我们总在游戏时间真正到点之后一点点才动手。抢拍比晚按严重得多，
        所以判据是"绝不为负"。
        """
        estimate = harness.clock.now()
        assert estimate is not None
        return truth - estimate

    # 1) 稳定推进 + 固定延迟：恰好晚一个延迟，绝不早
    h = Harness()
    h.run(lambda t: t - 12.5, start=0.0, stop=20.0, step=0.1, delay=0.005)
    behind = lag(h, h.moment - 12.5)
    if not -tolerance <= behind <= 0.005 + tolerance:
        problems.append(f"稳定推进时应当恰好晚 5ms，实际 {behind * 1000:+.1f}ms")
    ahead = h.clock.host_for(30.0)
    if ahead is None:
        problems.append("时钟明明在走，host_for 却算不出来")
    elif not 0 <= ahead - (30.0 + 12.5) <= 0.005 + tolerance:
        problems.append(f"host_for 应当晚 5ms 以内，实际 {(ahead - 30.0 - 12.5) * 1000:+.1f}ms")

    # 2) 延迟抖动：最小延迟是 0，所以估计应当紧贴真值（且不早）
    h = Harness()
    h.run(lambda t: t - 12.5, start=0.0, stop=10.0, step=0.1, delay=0.0)
    for extra in (0.03, 0.02, 0.01):  # 塞几笔大延迟进去，最小值不该被带偏
        h.feed(h.moment + 0.1, h.moment + 0.1 - 12.5 - extra)
    drift = lag(h, h.moment - 12.5)
    if not -tolerance <= drift <= 0.001 + tolerance:
        problems.append(f"抖动时估计被带偏了 {drift * 1000:+.1f}ms")

    # 3) 起播前 nowTime 被钉住
    h = Harness()
    h.run(lambda t: 0.00001, start=0.0, stop=3.0, step=0.1, delay=0.005)
    if h.clock.host_for(1.0) is not None:
        problems.append("时钟停着的时候不该放行后面的事件")
    if abs((h.clock.now() or 0) - 0.00001) > tolerance:
        problems.append(f"停着的时候 now 应当是 0.00001，实际 {h.clock.now()}")
    h.run(lambda t: t - 3.0, start=3.1, stop=9.0, step=0.1, delay=0.005)
    behind = lag(h, h.moment - 3.0)
    if not -tolerance <= behind <= 0.005 + tolerance:
        problems.append(f"起播后没有重新对表，{behind * 1000:+.1f}ms")

    # 4) 中途暂停两秒
    h = Harness()
    h.run(lambda t: t, start=0.0, stop=5.0, step=0.1, delay=0.005)
    frozen = 5.0
    h.run(lambda t: frozen, start=5.1, stop=7.0, step=0.1, delay=0.005)
    if h.clock.host_for(frozen + 1.0) is not None:
        problems.append("暂停期间不该放行后面的事件")
    if abs((h.clock.now() or 0) - frozen) > 0.05:
        problems.append(f"暂停期间时钟应当钉在 {frozen}，实际 {h.clock.now()}")
    h.run(lambda t: frozen + (t - 7.0), start=7.1, stop=13.0, step=0.1, delay=0.005)
    behind = lag(h, frozen + (h.moment - 7.0))
    if not -tolerance <= behind <= 0.005 + tolerance:
        problems.append(f"恢复后没有重新对表，{behind * 1000:+.1f}ms")

    # 5) 时钟倒着走 = 重开一局
    h = Harness()
    h.run(lambda t: t, start=0.0, stop=5.0, step=0.1, delay=0.0)
    h.run(lambda t: t, start=5.1, stop=7.0, step=0.1, delay=0.0)
    drift = lag(h, h.moment)
    if not -tolerance <= drift <= 0.001 + tolerance:
        problems.append(f"重开一局后没有重新对表，{drift * 1000:+.1f}ms")

    # 6) 传输打嗝：400ms 收不到样本，之后一口气补上一串**过时**的样本。
    #    这是 late good 的头号嫌疑 —— 补上来的头几笔带的是几百毫秒前的值，
    #    要是把它们误当成"暂停过"、把窗口清掉重新对表，就会照着它们的延迟整体晚发。
    h = Harness()
    h.run(lambda t: t - 12.5, start=0.0, stop=5.0, step=0.1, delay=0.005)
    before = h.clock.reanchors
    for stale in (0.0, 0.1, 0.2, 0.3, 0.4):
        h.feed(5.4, 5.0 + stale - 12.5)  # 值从 5.0 排到 5.4，但全都在 5.4 这一刻到达
    if h.clock.reanchors != before:
        problems.append("传输打嗝补样本时不该重锚 —— 旧对齐并没有失效")
    drift = lag(h, h.moment - 12.5)
    if not -tolerance <= drift <= 0.005 + tolerance:
        problems.append(f"传输打嗝之后对齐偏了 {drift * 1000:+.1f}ms")

    return problems


def check_pixels() -> list[str]:
    """虚拟屏 → 设备像素的换算：三种宽高比都要对。"""
    from algorithms.geometry import Screen
    from backends import to_pixels

    problems: list[str] = []
    screen = Screen(16.0, 9.0)

    def expect(width, height, x, y, want) -> None:
        got = to_pixels(screen, (width, height), x, y)
        if any(abs(a - b) > 1 for a, b in zip(got, want)):
            problems.append(f"{width}x{height} 的 ({x}, {y}) 算成了 {got}，应当是 {want}")

    # 16:9：正好铺满
    expect(1920, 1080, 0, 0, (0, 1080))
    expect(1920, 1080, 8, 4.5, (960, 540))
    expect(1920, 1080, 16, 9, (1920 - 1, 0))
    # 20:9：左右各留 240px 黑边，画面仍然居中、比例不变
    expect(2400, 1080, 8, 4.5, (1200, 540))
    expect(2400, 1080, 0, 4.5, (240, 540))
    expect(2400, 1080, 16, 4.5, (2160, 540))
    # 4:3：铺满
    expect(1024, 768, 8, 4.5, (512, 384))
    expect(1024, 768, 0, 0, (0, 768))

    # y 轴一定要翻过来：虚拟屏朝上，Android 朝下
    top = to_pixels(screen, (1920, 1080), 8, 9)[1]
    bottom = to_pixels(screen, (1920, 1080), 8, 0)[1]
    if top >= bottom:
        problems.append(f"y 轴没有翻转：y=9 在 {top}，y=0 在 {bottom}")
    return problems


def check_cache(text: str) -> list[str]:
    """缓存自检：命中、失效、以及"不吃也不写"。"""
    from algorithms.utils import SilentProgress

    problems: list[str] = []
    ref = ChartRef.of_text(text, seq=1, context={"songsId": "CacheTest", "songsLevel": "EZ"})

    with tempfile.TemporaryDirectory() as workspace:
        out = Path(workspace)
        fresh = planner.plan(text, ref=ref, cache=True, directory=out, progress=SilentProgress())
        if fresh.stats.get("cached"):
            problems.append("第一次规划不该算命中缓存")

        again = planner.plan(text, ref=ref, cache=True, directory=out, progress=SilentProgress())
        if not again.stats.get("cached"):
            problems.append("第二次规划应当命中缓存")
        if again.frames != fresh.frames:
            problems.append("缓存读回来的事件流与原来不一致")

        # 算法指纹一变，缓存就该失效
        meta_path = plan_path_for(ref, fresh.planner, out).with_suffix(".meta.json")
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["cache_key"] = "stale"
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        stale = planner.plan(text, ref=ref, cache=True, directory=out, progress=SilentProgress())
        if stale.stats.get("cached"):
            problems.append("算法指纹变了还能命中缓存")

        # cache=False：既不吃也不写
        bare = Path(workspace) / "bare"
        only = planner.plan(text, ref=ref, cache=False, directory=bare, progress=SilentProgress())
        if only.stats.get("cached") or list(bare.glob("*.psap")):
            problems.append("cache=False 时不该读也不该写缓存")

    return problems


def check_player() -> list[str]:
    """播放器自检：不连设备，用记录后端跑一遍，看事件是不是按时发的。"""
    import touch
    from algorithms.geometry import Screen
    from algorithms.utils import PlanResult, TouchEvent
    from backends import create
    from options import Options

    problems: list[str] = []
    moments = (1000, 1500, 2000)
    xs = (2.0, 8.0, 8.0)
    frames = [
        (timestamp, (TouchEvent(0, action, x, 4.5),))
        for timestamp, action, x in zip(
            moments, (Touch.DOWN, Touch.MOVE, Touch.UP), xs, strict=True
        )
    ]
    plan = PlanResult(planner="stub", screen=Screen(16.0, 9.0), frames=frames)
    latency = 0.02

    for mirror in (False, True):
        backend = create("recording")
        backend.open(plan.screen)
        clock = touch.LocalClock(lead_in=0.05)
        player = touch.Player(
            plan, backend, clock, mirror=mirror, options=Options(latency=latency)
        )
        player.start()
        if not player.join(10.0):
            problems.append(f"mirror={mirror} 时播放器没跑完")
            continue
        if player.error is not None:
            problems.append(f"mirror={mirror} 时播放器出错：{player.error}")
            continue
        if len(backend.calls) != len(frames):
            problems.append(f"mirror={mirror} 时发了 {len(backend.calls)} 批，应当是 {len(frames)}")
            continue
        if player.sent != len(frames):
            problems.append(f"mirror={mirror} 时事件数不对：{player.sent}")

        for timestamp, (moment, _) in zip(moments, backend.calls, strict=True):
            # 应当比"游戏时钟走到这一刻"早 latency 秒发出
            drift = moment - (clock.host_for(timestamp / 1000.0) - latency)
            if abs(drift) > 0.03:
                problems.append(f"mirror={mirror} 的 {timestamp}ms 那批偏了 {drift * 1000:+.0f}ms")

        got = [event.x for _, batch in backend.calls for event in batch]
        want = [16.0 - x for x in xs] if mirror else list(xs)
        if any(abs(a - b) > 1e-9 for a, b in zip(got, want, strict=True)):
            problems.append(f"mirror={mirror} 时坐标是 {got}，应当是 {want}")

    return problems


def check_console() -> list[str]:
    """控制台自检：命令解析与那几个旋钮。

    盯的是 ``latency`` 的三种写法（绝对值 / 毫秒 / 增减）与"打错命令不该把控制台带走"。
    控制台是运行时唯一的面板，解析错了的表现是"改了但没生效"或者"面板没了"，
    比直接报错难查得多，所以值得钉住。
    """
    from console import Console
    from options import Options

    class FakeController:
        """只放控制台真正会碰的那几样东西 —— 控制台对主干的依赖就这么多。"""

        def __init__(self) -> None:
            self.options = Options()
            self.stopping = False
            self.restarts: list[bool] = []

        def status_lines(self) -> list[str]:
            return ["（替身）状态"]

        def restart(self, *, spawn: bool) -> None:
            self.restarts.append(spawn)

        def stop(self) -> None:
            self.stopping = True

    problems: list[str] = []
    fake = FakeController()
    console = Console(fake)

    def run(line: str) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            console.execute(line)
        return buffer.getvalue()

    def expect(condition: bool, complaint: str) -> None:
        if not condition:
            problems.append(complaint)

    # latency：绝对值、毫秒、增减三种写法
    run("latency 0.02")
    expect(abs(fake.options.latency - 0.02) < 1e-9, f"latency 0.02 没生效：{fake.options.latency}")
    run("latency 20ms")
    expect(
        abs(fake.options.latency - 0.02) < 1e-9, f"latency 20ms 没生效：{fake.options.latency}"
    )
    run("latency +5ms")
    expect(
        abs(fake.options.latency - 0.025) < 1e-9, f"latency +5ms 不成增量：{fake.options.latency}"
    )
    run("latency -30ms")
    expect(
        abs(fake.options.latency + 0.005) < 1e-9, f"latency -30ms 不成增量：{fake.options.latency}"
    )
    run("latency 说不清")
    expect(
        abs(fake.options.latency + 0.005) < 1e-9, "看不懂的数不该改动设置"
    )

    # 开关
    run("inject off")
    expect(not fake.options.inject, "inject off 没生效")
    run("inject on")
    expect(fake.options.inject, "inject on 没生效")
    run("verbose on")
    expect(fake.options.verbose, "verbose on 没生效")

    # 规划器：认识的要换、不认识的原样不动
    run("planner radical")
    expect(fake.options.planner == "radical", f"planner radical 没生效：{fake.options.planner}")
    run("planner 没有这个")
    expect(fake.options.planner == "radical", "换到不存在的规划器时不该改动设置")

    # 重连与收工
    run("respawn")
    run("reattach")
    expect(fake.restarts == [True, False], f"respawn/reattach 没走对：{fake.restarts}")
    run("quit")
    expect(fake.stopping, "quit 没有让主干收工")

    # 打错、打空、打注释都不该炸，也不该被当成命令
    for line in ("没这个命令", "", "   ", "# 只是注释", "status 多余的参数"):
        try:
            run(line)
        except Exception as error:  # noqa: BLE001 - 就是来看它炸不炸的
            problems.append(f"执行 {line!r} 时抛了 {type(error).__name__}: {error}")

    # 每一条命令都要说话 —— 包括"看不懂"的。空回显是最难查的一种"没反应"。
    for line in ("没这个命令", "latency 说不清", "inject 说不清", "verbose", "planner 没有这个", "status"):
        if not run(line).strip():
            problems.append(f"{line!r} 一个字都没回")

    # status 就算拿不到任何一行，也不能沉默
    class EmptyController(FakeController):
        def status_lines(self) -> list[str]:
            return []

    silent = Console(EmptyController())
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        silent.execute("status")
    expect(bool(buffer.getvalue().strip()), "status_lines() 是空的时候，status 不该一声不吭")

    # 管道/重定向：命令要回显出来，日志里才看得出哪条命令配哪行输出
    class FakeStdin:
        """一根假 stdin：按行喂命令，喂完就是 EOF。"""

        def __init__(self, *lines: str, tty: bool = False) -> None:
            self.lines = list(lines)
            self._tty = tty

        def isatty(self) -> bool:
            return self._tty

        def readline(self) -> str:
            return self.lines.pop(0) if self.lines else ""

    buffer = io.StringIO()
    piped = Console(FakeController(), stdin=FakeStdin("status\n"))
    with contextlib.redirect_stdout(buffer):
        piped._loop()  # noqa: SLF001 - 直接跑循环，喂完就 EOF
    text = buffer.getvalue()
    for fragment in ("auto> status", "EOF"):
        if fragment not in text:
            problems.append(f"管道模式下少了 {fragment!r}：{text!r}")

    # 提示符会被别的线程的输出顶掉，输出完必须画回来：擦掉 -> 打印 -> 再画
    from console import PROMPT
    from output import log, set_prompt_hooks

    buffer = io.StringIO()
    console = Console(FakeController(), stdin=FakeStdin(tty=True))
    console._waiting = True  # noqa: SLF001 - 模拟"正等着输入"
    set_prompt_hooks(console._clear_prompt, console._draw_prompt)  # noqa: SLF001
    try:
        with contextlib.redirect_stdout(buffer):
            log("[main] 别的线程说话了")
    finally:
        set_prompt_hooks()
    text = buffer.getvalue()
    expect(text.count("[main] 别的线程说话了") == 1, f"那行输出应当只出现一次：{text!r}")
    expect(text.startswith("\r"), f"打印前应当先擦掉提示符：{text!r}")
    expect(text.endswith(PROMPT), f"打印完应当把提示符画回来，而不是擦掉就完事：{text!r}")
    expect(PROMPT in text.split("[main]")[1], f"提示符应当在那行输出之后：{text!r}")

    # 没在等输入的时候（正在执行命令）不该乱插提示符
    buffer = io.StringIO()
    console._waiting = False  # noqa: SLF001
    set_prompt_hooks(console._clear_prompt, console._draw_prompt)  # noqa: SLF001
    try:
        with contextlib.redirect_stdout(buffer):
            log("命令自己的输出")
    finally:
        set_prompt_hooks()
    expect(
        buffer.getvalue() == "命令自己的输出\n",
        f"没在等输入时不该动屏幕：{buffer.getvalue()!r}",
    )

    # 输出必须当场出去，不能攒在缓冲里
    class FlushSpy(io.StringIO):
        def __init__(self) -> None:
            super().__init__()
            self.flushes = 0

        def flush(self) -> None:
            self.flushes += 1
            super().flush()

    spy = FlushSpy()
    with contextlib.redirect_stdout(spy):
        log("必须立刻出去")
    expect(spy.getvalue() == "必须立刻出去\n", f"log 的内容不对：{spy.getvalue()!r}")
    expect(
        spy.flushes >= 1,
        "log() 没有 flush —— 管道里会攒成「敲了一条没反应、再敲一条上一条才出来」",
    )

    # 谁都不许继承我们的 stdin：`adb shell` 会把本地 stdin 转发给设备端，
    # 用户在控制台里敲的那一行就被它半路吃掉了 —— 这种错不报错，只表现为"偶尔有一行没反应"
    for path in sorted(ROOT.glob("*.py")) + sorted((ROOT / "backends").glob("*.py")):
        for number in _subprocess_calls_without_stdin(path.read_text(encoding="utf-8")):
            problems.append(f"{path.name}:{number} 的 subprocess 调用没有 stdin=DEVNULL")

    return problems


def _subprocess_calls_without_stdin(text: str) -> list[int]:
    """找出所有没给 ``stdin=`` 的 subprocess 调用，返回行号。

    判据是"这次调用的括号里有没有 ``stdin=``"，不是"这一行里有没有" —— 参数写成多行
    是很正常的写法（``scrcpy.py`` 里就是）。
    """
    import re

    found: list[int] = []
    for match in re.finditer(r"subprocess\.(?:run|Popen|call|check_call|check_output)\(", text):
        depth, index = 1, match.end()
        while index < len(text) and depth:
            if text[index] == "(":
                depth += 1
            elif text[index] == ")":
                depth -= 1
            index += 1
        if "stdin=" not in text[match.end() : index]:
            found.append(text[: match.start()].count("\n") + 1)
    return found


def check_liveness() -> list[str]:
    """存活探测自检：``ok`` / ``hang`` / ``dead`` 三种结局都要认得出来。

    没有设备也验得了 —— 会话与脚本本来就是"能 post、能 ping 的对象"，顶掉它们就行。
    真设备上验这一段要拔线或者杀进程（还得赌上"拔的是哪一根"），而它恰恰是"游戏没了
    以后别把剩下的排期灌进去"的唯一依据，所以值得在这里钉住。

    另外钉住两件"很难查"的事：收工只收一次（账别报两遍、播放器别停两次），
    以及收工时别把刚换上的新播放器顺手摘掉（它的排期还在跑，账却再没人收）。
    """
    import threading
    import time as timing

    import main as trunk

    problems: list[str] = []

    class FakeExports:
        def __init__(self, behaviour) -> None:
            self.behaviour = behaviour

        def ping(self) -> object:
            return self.behaviour()

    class FakeScript:
        """只要有 ``exports_sync.ping`` 与 ``post`` —— Agent 用到的就这两样。"""

        def __init__(self, behaviour) -> None:
            self.exports_sync = FakeExports(behaviour)
            self.posted: list[dict] = []

        def post(self, message: dict) -> None:
            self.posted.append(message)

    def build(behaviour, *, session: object | None = object()):
        agent = trunk.Agent(Path("unused.js"))
        agent._script = FakeScript(behaviour)  # noqa: SLF001 - 自检就是要顶掉真会话
        agent._session = session  # noqa: SLF001
        return agent

    def expect(condition: bool, complaint: str) -> None:
        if not condition:
            problems.append(complaint)

    # 1) 答得上话 = 活着
    agent = build(lambda: "pong")
    expect(agent.probe(0.5) == "ok", "答了 pong 却不认成 ok")

    # 2) 没有会话 = 死了（进程被杀之后 frida 会把会话收掉）
    expect(build(lambda: "pong", session=None).probe(0.5) == "dead", "没有会话却不认成 dead")

    # 3) ping 抛异常 = 死了，而且要记下原因
    def boom():
        raise RuntimeError("进程没了")

    agent = build(boom)
    expect(agent.probe(0.5) == "dead", "ping 抛异常却不认成 dead")
    expect(bool(agent.detached), "ping 抛了却没记下断线原因")

    # 4) ping 卡住 = hang（进程被冻住），而且要按给定的超时就回来
    gate = threading.Event()
    agent = build(lambda: (gate.wait(5.0), "pong")[1])
    started = timing.monotonic()
    state = agent.probe(0.2)
    cost = timing.monotonic() - started
    expect(state == "hang", f"ping 卡住时报的是 {state}，应当是 hang")
    expect(cost < 1.5, f"hang 判定等了 {cost:.2f}s，超时没起作用")
    expect(agent.probe(0.2) == "hang", "上一次探活还没回来，第二次不该再发一轮")
    gate.set()

    # 5) 主动断开 = 立刻就是 dead，不必等 teardown 回来
    agent = build(lambda: "pong")
    agent.stop()
    expect(agent.probe(0.5) == "dead", "断开之后还不认成 dead")

    # 6) 收工只收一次：探活与主线程可能同时收同一根棒
    class FakePlayer:
        def __init__(self) -> None:
            self.stopped = 0

        def stop(self) -> None:
            self.stopped += 1

        def join(self, timeout: float | None = None) -> bool:
            return True

    controller = trunk.Controller(_trunk_args())
    player = FakePlayer()
    reports: list[object] = []
    controller.player = player
    controller._report = reports.append  # type: ignore[method-assign]
    controller.stop_player()
    controller.stop_player()
    expect(player.stopped == 1, f"播放器被停了 {player.stopped} 次，应当只有一次")
    expect(len(reports) == 1, f"账报了 {len(reports)} 次，应当只有一次")
    expect(controller.player is None, "收工之后 player 该是空的")

    # 7) "打完收工"不能顺手把刚换上的新播放器摘掉
    controller = trunk.Controller(_trunk_args())
    installed = FakePlayer()
    controller.player = installed
    if controller._take_if(FakePlayer()):  # noqa: SLF001
        problems.append("取走的不是当前那个播放器，却报告说取到了")
    if controller.player is not installed:
        problems.append("比身份失败时不该动当前的播放器")
    expect(controller._take_if(installed), "当前那个播放器应当取得到")  # noqa: SLF001
    expect(controller.player is None, "取到之后 player 该是空的")

    return problems


def check_shutdown() -> list[str]:
    """收工自检：``quit`` 与 Ctrl+C 必须是同一条路、同一件事。

    这条路上出错都**不报错**，只表现为"看起来退出来了、其实没有"，所以值得钉住：

    * ``quit`` 只置一个标志、等主干发现 —— 主干卡在启动阶段的长调用里时，屏幕上看不出
      任何变化，而游戏上还挂着我们的 hook；
    * Ctrl+C 要按两下：第二下落在拆除中途，把 unload / detach 打断，注入就留在游戏里了；
    * 闸门还开着就断会话 —— Unity 主线程永远等不到放行，游戏冻在那儿，只能去杀进程；
    * 收工之后还在规划、还在架播放器 —— 排期会灌进一个我们已经放手的进程。

    全部用替身跑，不需要设备。最后两条走的是**真的** ``main()``：只在外面顶掉
    ``Controller`` / ``Console``，看两个入口是不是都落到 ``shutdown()`` 上。
    """
    # 这一组要往真线程、真 main() 里跑，中间必然打出一些本来就该出现的日志 —— 全接进
    # 缓冲里，别把它们混进自检结果（有问题照样从返回值出去）。
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return _check_shutdown()


def _check_shutdown() -> list[str]:
    """收工自检的正文 —— 判据与理由见 :func:`check_shutdown`。"""
    import threading
    import time as timing

    import signal

    try:
        import main as trunk
    except ImportError as error:  # noqa: BLE001 - frida 没装也不该让整份自检跑不起来
        return [f"导入 main.py 失败，跳过收工自检：{error}"]

    from options import Options

    problems: list[str] = []
    events: list[str] = []
    handlers: list[object] = []

    def expect(condition: bool, complaint: str) -> None:
        if not condition:
            problems.append(complaint)

    class FakeScript(Recorder):
        """既记下 post 出去的东西，也在事件表里留一行 —— 这里顺序才是关键。"""

        def post(self, message: dict) -> None:
            events.append("release")
            super().post(message)

        def unload(self) -> None:
            events.append("unload")

    class FakeSession:
        def detach(self) -> None:
            events.append("detach")

    class IdlePlayer:
        def stop(self) -> None:
            events.append("player-stop")

        def join(self, timeout: float | None = None) -> bool:
            # 拆除期间 Ctrl+C 必须是屏蔽的：第二下要是能落进来，unload/detach 就会被
            # 打断 —— 那等于没取消注入。就在这里当场采一下，别只信源码看着对。
            handlers.append(signal.getsignal(signal.SIGINT))
            return True

    def build() -> tuple[object, object, FakeScript]:
        controller = trunk.Controller(_trunk_args())
        agent = trunk.Agent(Path("unused.js"))
        script = FakeScript()
        agent._script = script  # noqa: SLF001 - 自检就是要顶掉真会话
        agent._session = FakeSession()  # noqa: SLF001
        controller.agent = agent
        controller._report = lambda player: None  # type: ignore[method-assign]
        return controller, agent, script

    # 1) stop() 当场拆完，而且三件事一件不少、顺序不乱
    events.clear()
    controller, _, _ = build()
    controller.player = IdlePlayer()
    controller.stop()
    expect(controller.stopping, "stop() 回来之后 stopping 还是 False")
    expect(
        events == ["player-stop", "unload", "detach"],
        f"收工该做的事没做全、或者顺序不对：{events}",
    )
    expect(
        bool(handlers) and all(handler is signal.SIG_IGN for handler in handlers),
        f"拆除期间没有屏蔽 Ctrl+C（采到的处理器是 {handlers}）—— 再按一下就会打断 detach",
    )

    # 2) 闸门还开着：先放行，再断会话。闸门走**真的**那条消息路径（不是手写字段），
    #    回调故意卡住，模拟"正在规划、游戏停在闸门上"的那一刻。
    events.clear()
    controller, agent, script = build()
    opened = threading.Event()
    finish = threading.Event()

    def slow_work(_start: object) -> None:
        opened.set()
        finish.wait(2.0)

    agent.on_level_start = slow_work
    feed = threading.Thread(
        target=lambda: agent._on_level_start(  # noqa: SLF001 - 自检就是往里喂消息
            {"seq": 7, "chartSeq": 3, "mirror": False, "offset": {}}
        ),
        name="selftest-gate",
    )
    feed.start()
    opened.wait(2.0)
    controller.stop()  # 主线程收工，此刻闸门正开着
    expect(script.released == [7], f"闸门开着却没放行它：{script.released}")
    expect(events[:1] == ["release"], f"闸门开着却先断了会话（游戏会冻在那儿）：{events}")
    expect(agent.gate_open is None, "放行之后闸门该销号")
    finish.set()
    feed.join(2.0)
    expect(script.released == [7], f"放行了不止一次：{script.released}")

    # 3) 拆除只做一次，而且后到的那个必须等它做完
    events.clear()
    controller, _, _ = build()
    gate = threading.Event()

    class SlowPlayer:
        def stop(self) -> None:
            events.append("player-stop")

        def join(self, timeout: float | None = None) -> bool:
            gate.wait(2.0)  # 卡住拆除，模拟"unload/detach 挂住"那种拆到一半的状态
            return True

    controller.player = SlowPlayer()
    first = threading.Thread(target=controller.stop, name="selftest-first")
    first.start()
    timing.sleep(0.3)
    expect("detach" not in events, f"替身本该卡住拆除，它却已经拆完了：{events}")
    gate.set()
    controller.shutdown()  # 主干就是"后到的那个"：这里必须等第一次拆完才返回
    expect("detach" in events, f"后到的 shutdown() 在拆除还没做完时就返回了：{events}")
    for name in ("player-stop", "unload", "detach"):
        expect(events.count(name) == 1, f"{name} 做了 {events.count(name)} 次，应当只有一次")
    first.join(2.0)

    # 4) 收工之后：不规划、不架播放器
    events.clear()
    controller, _, _ = build()
    controller._stopping.set()  # noqa: SLF001 - 收工是在别处发起的（quit / Ctrl+C）

    planned: list[str] = []
    original_plan = trunk.planner.plan
    trunk.planner.plan = lambda *args, **kwargs: planned.append("plan")  # type: ignore[assignment]
    try:
        controller.handle_level_start(
            trunk.LevelStart(
                seq=1,
                chart_seq=1,
                mirror=False,
                offset={},
                chart=trunk.CapturedChart(
                    ref=ChartRef(seq=1, context={}, digest="x"), text="{}", received_at=0.0
                ),
            )
        )
    finally:
        trunk.planner.plan = original_plan  # type: ignore[assignment]
    expect(not planned, "收工之后还在规划")

    built: list[int] = []

    class QuietPlayer:
        def __init__(self, *args: object, **kwargs: object) -> None:
            built.append(1)

        def start(self) -> None:
            pass

    class QuietPlan:
        """只要 ``event_count`` —— ``touch.Player`` 已经被顶掉了，别的用不上。"""

        event_count = 0

    controller.backend = object()  # 只要不是 None，play() 就会往下走
    original_player = trunk.touch.Player
    trunk.touch.Player = QuietPlayer  # type: ignore[assignment]
    try:
        controller.play(QuietPlan(), mirror=False, seq=1)  # type: ignore[arg-type]
    finally:
        trunk.touch.Player = original_player  # type: ignore[assignment]
    expect(not built, "收工之后还架了播放器（排期会灌进一个我们已经放手的进程）")

    # 5) 收工只动我们自己的东西：游戏本身一根手指都不碰 —— 即使它是我们 spawn 出来、
    #    还没放行的那个。"放不放它跑"不是收工该管的事。
    class FakeDevice:
        def __init__(self) -> None:
            self.touched: list[str] = []

        def resume(self, pid: int) -> None:
            self.touched.append(f"resume({pid})")

        def kill(self, pid: int) -> None:
            self.touched.append(f"kill({pid})")

    device = FakeDevice()
    agent = trunk.Agent(Path("unused.js"), device)
    agent.pid = 4242  # 我们自己 spawn 出来的，还没 resume
    agent._script = FakeScript()  # noqa: SLF001
    agent._session = FakeSession()  # noqa: SLF001
    agent.stop()
    expect(not device.touched, f"收工不该动游戏本身，却动了：{device.touched}")

    # 6) 两个入口（quit / Ctrl+C）在真的 main() 里落到同一件事上
    class FakeConsole:
        def start(self) -> None:
            pass

    class SpyController:
        """只实现 main() 真正会用到的接口，把"谁被调了"记下来。"""

        def __init__(
            self,
            *,
            quit_during_open: bool = False,
            fail_open: bool = False,
            interrupt: bool = False,
        ) -> None:
            self.options = Options()
            self.quit_during_open = quit_during_open
            self.fail_open = fail_open
            self.interrupt = interrupt
            self.calls: list[str] = []
            self._stopping = False

        @property
        def stopping(self) -> bool:
            return self._stopping

        def open(self) -> bool:
            self.calls.append("open")
            if self.quit_during_open:
                self.stop()  # 控制台的 quit 就是这个时机：设备刚找到、还没注入完
            return not self.fail_open

        def open_backend(self) -> None:
            self.calls.append("open_backend")

        def watch(self) -> None:
            self.calls.append("watch")

        def poll(self) -> None:
            self.calls.append("poll")
            if self.interrupt:
                raise KeyboardInterrupt

        def stop(self) -> None:
            self.calls.append("stop")
            self._stopping = True

        def shutdown(self) -> None:
            self.calls.append("shutdown")
            self._stopping = True

    def run_main(spy: SpyController) -> tuple[int, list[str]]:
        original = (trunk.Controller, trunk.Console, sys.argv)
        trunk.Controller = lambda args: spy  # type: ignore[assignment]
        trunk.Console = lambda controller: FakeConsole()  # type: ignore[assignment]
        sys.argv = ["main.py"]  # main() 自己会 parse_args，别把自检的参数喂给它
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return trunk.main(), spy.calls
        finally:
            trunk.Controller, trunk.Console, sys.argv = original  # type: ignore[assignment]

    code, calls = run_main(SpyController(quit_during_open=True))
    expect(code == 0, f"启动阶段收到 quit，main() 返回了 {code}（那是人的决定，不是失败）")
    expect(calls[-1:] == ["shutdown"], f"quit 之后收工必须是最后一步：{calls}")
    expect(
        not {"open_backend", "watch", "poll"} & set(calls),
        f"启动阶段就收工了，不该再开后端 / 探活 / 进循环：{calls}",
    )

    code, calls = run_main(SpyController(interrupt=True))
    expect(code == 0, f"Ctrl+C 之后 main() 返回了 {code}")
    expect(calls[-1:] == ["shutdown"], f"Ctrl+C 之后收工必须是最后一步：{calls}")
    expect(
        "open_backend" in calls and "poll" in calls,
        f"Ctrl+C 之前本该正常走到打歌循环里：{calls}",
    )

    # 找设备那一步打断了（10 秒超时）、或者注入失败：是"人让它停的"还是"真失败"，
    # 退出码必须分得开 —— 脚本外面就是靠这个码判断该不该重试
    code, _ = run_main(SpyController(quit_during_open=True, fail_open=True))
    expect(code == 0, f"启动时收到 quit 而 open() 失败，main() 返回了 {code}，应当是 0")
    code, _ = run_main(SpyController(fail_open=True))
    expect(code == 2, f"没人让它停、open() 真失败，main() 返回了 {code}，应当是 2")

    return problems


def check_judge() -> list[str]:
    """判定对账自检：``delta`` 与 ``nowTime − realTime`` 必须对得上。

    这三个数来自三处 —— 游戏判决时算的早晚量、游戏当时的 ``nowTime``、我们从音符表抄来的
    ``realTime``，它们之间有个恒等式。表抄错了在这里就该露馅，而不必等到"Miss 出现在一个
    不可能的时刻"再靠人眼看出来（真出过一次：表建早了，``realTime`` 全是 0，
    89.969 秒判掉的音符被记成 "@ 0.000s"）。
    """
    problems: list[str] = []
    try:
        import main as trunk
    except ImportError as error:  # noqa: BLE001 - frida 没装也不该让整份自检跑不起来
        return [f"导入 main.py 失败，跳过判定对账自检：{error}"]

    agent = trunk.Agent(Path("unused.js"))
    # 一个真实形状的音符：Dlyrotz 里第 7 条线上方第 0 个，89.969 秒
    note = {
        "code": 7000000,
        "type": 4,
        "time": 89.969,
        "x": 6.0,
        "hold": 0.0,
        "line": 7,
        "above": True,
        "index": 0,
    }

    def judge(**fields) -> str:
        payload = {"kind": "Miss", "noteCode": 7000000, "delta": None, "note": dict(note), "at": 0}
        payload.update(fields)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            agent._on_judge(payload)  # noqa: SLF001
        return buffer.getvalue()

    def expect(condition: bool, complaint: str) -> None:
        if not condition:
            problems.append(complaint)

    # 1) 对得上：Good，晚 32ms
    text = judge(kind="Good", delta=0.032, time=90.001)
    expect("对不上账" not in text, f"明明对得上却报了账不平：{text.strip()!r}")
    expect("[judge] Good" in text, f"Good 那行没打出来：{text.strip()!r}")

    # 2) Miss 的正常情形：刚越过窗口（0.1 秒多一点），不该被冤枉
    expect("对不上账" not in judge(time=89.969 + 0.12), "正常的 Miss 被冤枉了")

    # 3) 表抄早了：realTime 是 0，而游戏说这个音符在 89.969 秒
    stale = judge(time=89.969, note=dict(note, time=0.0))
    expect("对不上账" in stale, "realTime 抄成 0 却没被发现")
    expect("差 +89.969s" in stale or "89.969" in stale, f"抱怨里没说清差了多少：{stale.strip()!r}")
    expect(agent.judge_mismatches == 1, f"账不平的条数不对：{agent.judge_mismatches}")

    # 4) 同类问题只报一次，别刷屏
    again = judge(time=89.969, note=dict(note, time=0.0))
    expect("对不上账" not in again, "同类问题只该报第一次")
    expect(agent.judge_mismatches == 2, f"第二次没计上数：{agent.judge_mismatches}")

    # 5) 早晚量与 nowTime 不搭（另一条路径也得被抓住）。先清零，否则会被"同类只报一次"挡住
    agent.judge_mismatches = 0  # noqa: SLF001
    skewed = judge(kind="Good", delta=0.032, time=120.0)
    expect("对不上账" in skewed, "早晚量与 nowTime 差得离谱却没被发现")
    expect(agent.judge_mismatches == 1, f"换了一条路径却没计上数：{agent.judge_mismatches}")

    # 6) 查不到音符（表里没有它）时不该乱报账不平
    expect("对不上账" not in judge(note=None), "没有音符可查时不该报账不平")

    # 7) Hold 的收尾判决：早晚量是从"按住结束 − 0.22s"量的，不是从头量的 ——
    #    拿头判的恒等式去对，每条 hold 都会被冤枉（实测 Dlyrotz HD 上正好冤枉了那 8 个 hold）
    hold_note = dict(note, type=3, time=10.0, hold=2.416)
    settle = 10.0 + 2.416 - trunk.HOLD_SETTLE_LEAD
    agent.judge_mismatches = 0  # noqa: SLF001
    text = judge(kind="Perfect", delta=0.012, time=settle + 0.012, note=hold_note)
    expect("对不上账" not in text, f"Hold 的收尾判决被冤枉了：{text.strip()!r}")
    expect(agent.judge_mismatches == 0, "Hold 的合法收尾不该计成账不平")

    # 8) 但 Hold 的**头判**照样要认（同一条恒等式，参考点是音符时刻）
    text = judge(kind="Perfect", delta=0.010, time=10.010, note=hold_note)
    expect("对不上账" not in text, f"Hold 的头判被冤枉了：{text.strip()!r}")

    # 9) Hold 也不许蒙混：两头的参考点都对不上，照样得报
    text = judge(kind="Perfect", delta=0.012, time=settle + 0.9, note=hold_note)
    expect("对不上账" in text, "Hold 两头都对不上却没报账不平")

    return problems


def check_result() -> list[str]:
    """结算那两行：数字要一个不差，读不到的字段要显式写成 `?`。

    字段名对不上时 frida 是**静默**返回 null 的（`<mirror>k__BackingField` 那次就是），
    所以"读不到"必须和"真的是 0"在输出里区分得出来 —— 这一条就是盯着这个。
    """
    problems: list[str] = []
    try:
        import main as trunk
    except ImportError as error:  # frida 没装也不该让整份自检跑不起来
        return [f"导入 main.py 失败，跳过结算自检：{error}"]

    agent = trunk.Agent(Path("unused.js"), attach=True)
    agent.level_label = "SelfTest [EZ]"

    def capture(payload: dict) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            agent._on_result(payload)
        return buffer.getvalue()

    perfect = capture(
        {
            "event": "result",
            "seq": 1,
            "score": 1000000.0,
            "percent": 100.0,
            "perfect": 93,
            "good": 0,
            "bad": 0,
            "miss": 0,
            "early": 0,
            "late": 0,
            "maxCombo": 93,
            "allPerfect": True,
            "fullCombo": True,
        }
    )
    for fragment in ("1000000 分", "100.00%", "最大连击 93", "All Perfect", "Perfect 93", "Miss 0"):
        if fragment not in perfect:
            problems.append(f"满分局的结算行里少了 {fragment!r}：{perfect.strip()!r}")
    if "?" in perfect:
        problems.append(f"满分局不该出现读不到的 ?：{perfect.strip()!r}")

    messy = capture(
        {
            "event": "result",
            "seq": 2,
            "score": 987654.0,
            "percent": 98.77,
            "perfect": 80,
            "good": 9,
            "bad": 3,
            "miss": 1,
            "early": 5,
            "late": 8,
            "maxCombo": 40,
            "allPerfect": False,
            "fullCombo": False,
        }
    )
    for fragment in ("987654 分", "Good 9", "Bad 3", "Miss 1", "早 5 / 晚 8"):
        if fragment not in messy:
            problems.append(f"有失误那局少了 {fragment!r}：{messy.strip()!r}")

    # 全连但不是全 Perfect：判定标签必须是 Full Combo
    full_combo = capture(
        {"event": "result", "seq": 3, "score": 999000.0, "maxCombo": 93,
         "allPerfect": False, "fullCombo": True}
    )
    if "Full Combo" not in full_combo:
        problems.append(f"全连那局没标 Full Combo：{full_combo.strip()!r}")

    # 字段全读不到：一个都不许伪装成 0
    missing = capture({"event": "result", "seq": 4})
    if missing.count("?") < 8:
        problems.append(f"字段读不到时应当处处是 ?：{missing.strip()!r}")

    return problems


def main() -> int:
    charts = sorted(
        path for path in (ROOT / "charts").glob("*.json") if not path.name.endswith(".meta.json")
    )

    print(f"垂直判定横向容差 {TOLERANCE:.3f}（CheckNote 的 1.9 × 屏幕缩放 0.9）")
    print("可用规划器：")
    for info in catalog():
        print(f"  {info.name:<14} {info.summary}")

    progress = SilentProgress()
    failed = False

    # 不依赖谱面样本的几项先跑 —— charts/ 空着的时候它们照样能验证东西
    gate_problems, gate_output = check_gate()
    print("\n闸门自检：" + ("通过" if not gate_problems else "有问题"))
    for problem in gate_problems:
        print(f"  ! {problem}")
    if gate_problems and gate_output:
        print("  —— agent 当时的输出 ——")
        print("\n".join(f"  | {line}" for line in gate_output.splitlines()))
    failed = failed or bool(gate_problems)

    for label, problems in (
        ("时钟自检", check_clock()),
        ("坐标换算自检", check_pixels()),
        ("播放器自检", check_player()),
        ("控制台自检", check_console()),
        ("存活探测自检", check_liveness()),
        ("收工自检", check_shutdown()),
        ("结算自检", check_result()),
        ("判定对账自检", check_judge()),
        ("在位时长自检", check_dwell()),
    ):
        print(f"{label}：" + ("通过" if not problems else "有问题"))
        for problem in problems:
            print(f"  ! {problem}")
        failed = failed or bool(problems)

    if not charts:
        print("\ncharts/ 里没有谱面，跳过规划与覆盖率自检。先用 main.py --save-chart 采一张。")
        return 1 if failed else 2

    for label, problems in (("缓存自检", check_cache(charts[0].read_text(encoding="utf-8"))),):
        print(f"{label}：" + ("通过" if not problems else "有问题"))
        for problem in problems:
            print(f"  ! {problem}")
        failed = failed or bool(problems)

    for path in charts:
        text = path.read_text(encoding="utf-8")
        chart = Chart.parse(text)
        print(
            f"\n{path.name}\n"
            f"  v{chart.format_version}  {len(chart.lines)} 判定线 / {chart.note_count} 音符"
        )
        for info in catalog():
            try:
                result = create(info.name).plan(chart, progress)
            except Exception as error:  # noqa: BLE001
                print(f"  [{info.name}] 规划失败：{type(error).__name__}: {error}")
                failed = True
                continue

            problems = check_stream(result)
            total, misses, worst = check_coverage(chart, result)
            problems += check_storage(text, info.name, result)
            problems += check_flick(chart, result)
            m_total, m_misses, m_worst = check_mirror(text, result)
            if m_misses:
                problems += [f"镜像后漏掉 {note}" for note in m_misses[:4]]
            if m_total != total:
                problems.append(f"镜像自检覆盖的音符数不对：{m_total} vs {total}")

            ok = not problems and not misses
            failed = failed or not ok
            print(
                f"  [{info.name}] {len(result.frames)} 帧 / {result.event_count} 个事件 / "
                f"{result.pointer_count} 指针 / {result.duration_ms}ms -> "
                f"{'全部通过' if ok else '有问题'}"
            )
            print(
                f"        覆盖 {total - len(misses)}/{total} 个音符，最大横向偏差 {worst:.4f}"
                f"（容差 {TOLERANCE:.2f}）"
            )
            print(
                f"        镜像后覆盖 {m_total - len(m_misses)}/{m_total} 个音符，"
                f"最大横向偏差 {m_worst:.4f}"
            )
            for problem in problems[:4]:
                print(f"        ! {problem}")
            for miss in misses[:6]:
                print(f"        x 漏掉 {miss}")
            if len(misses) > 6:
                print(f"        x 还有 {len(misses) - 6} 个")

    print("\n" + ("全部通过" if not failed else "有项目未通过"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
