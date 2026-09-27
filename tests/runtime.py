"""与算法无关的运行时检查：闸门放行、游戏时钟、坐标换算、播放器排期、规划缓存。"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
from pathlib import Path

from algorithms import create
from algorithms.geometry import Screen
from algorithms.utils import PlanResult, SilentProgress, Touch
from storage import ChartRef, plan_path_for
import planner
from .stubs import Recorder, _trunk_args


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

