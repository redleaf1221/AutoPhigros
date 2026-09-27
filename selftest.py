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


def mirror_chart(text: str) -> str:
    """把一份官谱按 ``Chart::Mirror`` 的规则镜像过来 —— 测试用的"游戏行为"模型。

    这是**独立于实现**的判据：``Chart::Mirror`` @ ``0x1d286ac`` 只动三样东西 ——
    移动事件 ``x → 1 − x``、旋转事件 ``θ → −θ``、音符 ``positionX → −positionX``。
    有了它，就能直接验"把规划结果翻一下"是不是镜像后谱面的解，而不用真去开一次游戏。

    只处理 ``formatVersion >= 2``（本项目采到的谱面都是 v3）；v1 的老整数打包格式
    要按 ``i → 880 − i`` 走，这里不伺候。
    """
    chart = json.loads(text)
    for line in chart["judgeLineList"]:
        for event in line.get("judgeLineMoveEvents") or ():
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


class Recorder:
    """顶掉 frida 的 script，只记下 post 出去的东西。"""

    def __init__(self) -> None:
        self.posted: list[dict] = []

    def post(self, message: dict) -> None:
        self.posted.append(message)

    @property
    def released(self) -> list[int]:
        return [int(message.get("payload", {}).get("seq", 0)) for message in self.posted]


class FakeSession:
    """顶掉真打歌现场，只记下"架了哪个规划结果、带什么运行时设置"。"""

    def __init__(self) -> None:
        self.plays: list[tuple[object, bool, int]] = []

    def play(self, plan, *, mirror: bool, seq: int) -> None:
        self.plays.append((plan, mirror, seq))


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
            session = FakeSession()
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
                args = argparse.Namespace(planner="stub", save_chart=False, cache=True)
                agent.on_level_start = lambda start: trunk.handle_level_start(start, args, session)
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

            if len(session.plays) != 1:
                problems.append(f"mirror={mirror} 时播放器应当被架一次，实际 {len(session.plays)} 次")
            elif session.plays[0][1] is not expected:
                problems.append(
                    f"mirror={mirror} 时播放器拿到的是 {session.plays[0][1]!r}"
                )

        # 4) 没收到谱面就直接放行，不能崩也不能卡
        agent, recorder = build()
        args = argparse.Namespace(planner="stub", save_chart=False, cache=True)
        agent.on_level_start = lambda start: trunk.handle_level_start(start, args, FakeSession())
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
        player = touch.Player(plan, backend, clock, mirror=mirror, latency=latency)
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
        ("结算自检", check_result()),
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
            m_total, m_misses, m_worst = check_mirror(text, result)
            if m_misses:
                problems += [f"镜像后漏掉 {note}" for note in m_misses[:4]]
            if m_total != total:
                problems.append(f"镜像自检覆盖的音符数不对：{m_total} vs {total}")

            ok = not problems and not misses
            failed = failed or not ok
            print(
                f"  [{info.name}] {len(result.frames)} 帧 / {result.event_count} 事件 / "
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
