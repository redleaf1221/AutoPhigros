"""存活探测与收工：`ok` / `hang` / `dead` 三种结局，以及收工那条路上的一串规矩。"""

from __future__ import annotations

import contextlib
import io
import time
from pathlib import Path
from types import SimpleNamespace

from formats.storage import ChartRef
from .stubs import Recorder, make_config, make_controller


def check_liveness() -> list[str]:
    """存活探测自检：``ok`` / ``hang`` / ``dead`` 三种结局都要认得出来。

    没有设备也验得了（顶掉会话与脚本就行）—— 而它是"游戏没了以后别把剩下的排期灌进去"的
    唯一依据。另外钉住两件难查的事：收工只收一次，收工也不把刚换上的新播放器顺手摘掉。
    """
    import threading
    import time as timing

    from runtime import agent as agent_module
    from runtime import controller as controller_module

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
        agent = agent_module.Agent(Path("unused.js"))
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

    # 6) 我们自己正忙的时候，ping 超时**不算**"游戏冻住了"
    class CountingPlayer:
        def __init__(self) -> None:
            self.stopped = 0

        def stop(self) -> None:
            self.stopped += 1

        def join(self, timeout: float | None = None) -> bool:
            return True

    def watch(agent) -> tuple[str, list[str], int]:
        """跑一轮看门狗，拿回（agent_state、打出来的话、播放器被停了几次）。

        把 ping 超时临时压到 50ms：这里要的是"超时"这个结局，而不是真等 2 秒。
        """
        controller_ = make_controller()
        controller_.agent = agent
        controller_._report = lambda player: None  # type: ignore[method-assign]
        player = CountingPlayer()
        controller_.player = player
        original = controller_module.PING_TIMEOUT
        controller_module.PING_TIMEOUT = 0.05
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                controller_._watch_once()  # noqa: SLF001
        finally:
            controller_module.PING_TIMEOUT = original
            gate.set()  # 放掉那个还在等的假 ping，别留后台线程
        return controller_.agent_state, buffer.getvalue().strip().splitlines(), player.stopped

    # 真忙：卡在闸门上（`gate_open` 有值）
    gate.clear()  # 前面第 4 条把它放行过了
    agent = build(lambda: (gate.wait(5.0), "pong")[1])
    agent.gate_open = 7
    state, lines, stopped = watch(agent)
    expect(state == "忙", f"卡在闸门上的 ping 超时应当算「忙」，实际 {state}")
    expect(not lines, f"忙的时候不该打警告：{lines}")
    expect(stopped == 0, "忙的时候不该把触控停掉（游戏好着呢）")

    # 真忙：谱面正在通道上传输
    gate.clear()
    agent = build(lambda: (gate.wait(5.0), "pong")[1])
    agent.busy_until = timing.monotonic() + 5.0
    state, lines, stopped = watch(agent)
    expect(state == "忙", f"传谱面时的 ping 超时应当算「忙」，实际 {state}")
    expect(stopped == 0, "传谱面时不该把触控停掉")

    # 忙的**理由**没了（闸门关了、传输窗口过了）就该照常报警 —— 别拿"忙"当万能挡箭牌
    gate.clear()
    agent = build(lambda: (gate.wait(5.0), "pong")[1])
    state, lines, stopped = watch(agent)
    expect(state == "hang", f"没在忙的时候超时应当报 hang，实际 {state}")
    expect(any("冻结" in line for line in lines), f"该说清楚是冻住了：{lines}")
    expect(stopped == 1, "真 hang 要停触控")

    # 7) 收工只收一次：探活与主线程可能同时收同一根棒
    class FakePlayer:
        def __init__(self) -> None:
            self.stopped = 0

        def stop(self) -> None:
            self.stopped += 1

        def join(self, timeout: float | None = None) -> bool:
            return True

    controller = make_controller()
    player = FakePlayer()
    reports: list[object] = []
    controller.player = player
    controller._report = reports.append  # type: ignore[method-assign]
    controller.stop_player()
    controller.stop_player()
    expect(player.stopped == 1, f"播放器被停了 {player.stopped} 次，应当只有一次")
    expect(len(reports) == 1, f"账报了 {len(reports)} 次，应当只有一次")
    expect(controller.player is None, "收工之后 player 该是空的")

    # 8) "打完收工"不能顺手把刚换上的新播放器摘掉
    controller = make_controller()
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
    """收工自检：``quit`` 与 Ctrl+C 必须是同一条路、同一件事，而且只收一次。

    盯的都是**不报错**的错法：只置标志不当场拆、拆除期间 Ctrl+C 能落进来、闸门还开着就断
    会话、收工之后还在规划或架播放器 —— 它们只表现为"看起来退出来了、其实没有"。
    """
    # 这一组要往真线程、真 main() 里跑，中间必然打出正常的日志：全接进缓冲里，别混进自检结果。
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return _check_shutdown()


def _check_shutdown() -> list[str]:
    """收工自检的正文：前面用替身，最后跑**真的** ``main()``（只顶掉 Controller / Console）。"""
    import threading
    import time as timing

    import signal

    try:
        from runtime import agent as agent_module
        from runtime import config
        from runtime import controller as controller_module
        import main as main_module
        import planner
        import touch
    except ImportError as error:  # noqa: BLE001 - frida 没装也不该让整份自检跑不起来
        return [f"导入 main.py 失败，跳过收工自检：{error}"]

    from runtime.options import Options

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
            # 拆除期间 Ctrl+C 必须是屏蔽的（第二下会打断 unload/detach）：就在这里当场采一下。
            handlers.append(signal.getsignal(signal.SIGINT))
            return True

    def build() -> tuple[object, object, FakeScript]:
        controller = make_controller()
        agent = agent_module.Agent(Path("unused.js"))
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

    # 2) 闸门还开着：先放行、再断会话（闸门走真的消息路径，回调故意卡住，模拟"正在规划"）。
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

    # 2b) 闸门开着的时候把它关掉：必须**先放行**这一道，再把开关打给 agent
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
            {"seq": 9, "chartSeq": 3, "mirror": False, "offset": {}}
        ),
        name="selftest-gate-off",
    )
    feed.start()
    opened.wait(2.0)
    controller.set_gate(False)  # 游戏此刻正卡在闸门上
    expect(
        script.released == [9],
        f"关掉闸门却没放行正卡着的那一道（游戏会一直停在开谱那儿）：{script.posted}",
    )
    expect(script.gates == [False], f"闸门开关没打给 agent：{script.posted}")
    expect(not controller.options.gate, "关掉闸门之后 options.gate 该是 False")
    finish.set()
    feed.join(2.0)
    controller.set_gate(True)
    expect(script.gates == [False, True], f"再开回来没打给 agent：{script.posted}")

    # 2c) 刚起来的 agent 会收到"这一把的设置"：闸门关着的会话，接上去就不该拦游戏
    events.clear()
    controller, agent, script = build()
    controller.options.gate = False
    controller._bind_agent(agent)  # noqa: SLF001 - 自检就是要单独走这一步
    expect(script.gates == [False], f"接上 agent 时没把闸门开关打过去：{script.posted}")

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
    original_plan = planner.plan
    planner.plan = lambda *args, **kwargs: planned.append("plan")  # type: ignore[assignment]
    try:
        controller.handle_level_start(
            agent_module.LevelStart(
                seq=1,
                chart_seq=1,
                mirror=False,
                offset={},
                chart=agent_module.CapturedChart(
                    ref=ChartRef(seq=1, context={}, digest="x"), text="{}", received_at=0.0
                ),
            )
        )
    finally:
        planner.plan = original_plan  # type: ignore[assignment]
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
    original_player = touch.Player
    touch.Player = QuietPlayer  # type: ignore[assignment]
    try:
        controller.play(QuietPlan(), mirror=False, seq=1)  # type: ignore[arg-type]
    finally:
        touch.Player = original_player  # type: ignore[assignment]
    expect(not built, "收工之后还架了播放器（排期会灌进一个我们已经放手的进程）")

    # 5) 收工只动我们自己的东西：游戏本身一根手指都不碰 —— 即使它是我们 spawn 出来、还没 resume 的那个
    class FakeDevice:
        def __init__(self) -> None:
            self.touched: list[str] = []

        def resume(self, pid: int) -> None:
            self.touched.append(f"resume({pid})")

        def kill(self, pid: int) -> None:
            self.touched.append(f"kill({pid})")

    device = FakeDevice()
    agent = agent_module.Agent(Path("unused.js"), device)
    agent.pid = 4242  # 我们自己 spawn 出来的，还没 resume
    agent._script = FakeScript()  # noqa: SLF001
    agent._session = FakeSession()  # noqa: SLF001
    agent.stop()
    expect(not device.touched, f"收工不该动游戏本身，却动了：{device.touched}")

    # 6) 主干是控制台：`quit` 与 Ctrl+C 都要落到同一次收工上，而且只收一次
    class SpyController:
        """只实现 main() 真正会用到的接口，把"谁被调了"记下来。"""

        def __init__(self) -> None:
            self.config = make_config()
            self.options = Options()
            self.calls: list[str] = []
            self._stopping = False

        @property
        def stopping(self) -> bool:
            return self._stopping

        def start(self) -> None:
            self.calls.append("start")

        def stop(self) -> None:
            self.calls.append("stop")
            self._stopping = True

        def shutdown(self) -> None:
            self.calls.append("shutdown")
            self._stopping = True

    def run_main(spy: SpyController, behaviour: str) -> tuple[int, list[str]]:
        """跑真的 `main()`：只顶掉 Controller 与 Console 这两个协作者。"""
        class FakeConsole:
            def __init__(self, controller) -> None:
                self.controller = controller

            def run(self) -> None:
                if behaviour == "quit":
                    self.controller.stop()  # 控制台的 quit 干的就是这个
                elif behaviour == "ctrl-c":
                    raise KeyboardInterrupt  # 主干在读输入，信号就落在这里
                elif behaviour == "park":
                    self.controller.stop() if False else None

        original = (main_module.Controller, main_module.Console)
        main_module.Controller = lambda config, options=None: spy  # type: ignore[assignment]
        main_module.Console = FakeConsole  # type: ignore[assignment]
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                return main_module.main(spy.config), spy.calls
        finally:
            main_module.Controller, main_module.Console = original  # type: ignore[assignment]

    for behaviour, label in (("quit", "quit"), ("ctrl-c", "Ctrl+C")):
        spy = SpyController()
        code, calls = run_main(spy, behaviour)
        expect(code == 0, f"{label} 之后 main() 返回了 {code}")
        expect(calls[:1] == ["start"], f"{label} 之前应当先把后台件支起来：{calls}")
        expect(calls[-1:] == ["shutdown"], f"{label} 之后收工必须是最后一步：{calls}")
        expect(
            calls.count("shutdown") == 1,
            f"{label} 的收工应当只做一次：{calls}",
        )

    # 控制台正常结束（比如输入到头、随后从别处收工）也要收工，而不是把进程挂在半路
    spy = SpyController()
    code, calls = run_main(spy, "park")
    expect(code == 0 and calls[-1:] == ["shutdown"], f"控制台自然结束之后没有收工：{calls}")

    # 7) 等不到时钟：判据是**游戏时钟自己**而不是墙上的时间；这里只报不改状态（"还在不在"由 hook 说了算）。
    class IdlePlayer:
        def __init__(self) -> None:
            self.stopped = 0
            self.sent = 0
            self.first_seconds = 1.519

        def stop(self) -> None:
            self.stopped += 1

        def join(self, timeout: float | None = None) -> bool:
            return True

    def waiting(
        now: float | None,
        *,
        elapsed: float = 5.0,
        held: bool = False,
        playing: bool | None = None,
    ) -> tuple[str, IdlePlayer]:
        """架一个"一个事件都没发"的现场，返回（报出来的话、播放器）。"""
        controller = make_controller()
        controller._report = lambda player: None  # type: ignore[method-assign]
        if playing is not None:
            controller.agent = SimpleNamespace(pid=None, level_label="?", playing=playing)  # type: ignore[assignment]
        player = IdlePlayer()
        controller.player = player
        moment = time.monotonic()
        if now is not None:
            controller.clock.feed(now, host_time=moment)
        if held:
            controller.clock.hold()
        controller._clock_wait_from = moment - elapsed  # noqa: SLF001
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            controller._check_clock_wait()  # noqa: SLF001
        return buffer.getvalue(), player

    # 7a) 时钟在走、只是还没走到第一个事件 —— 等着就是对的
    text, player = waiting(0.00001)
    expect(not text, f"时钟还没走到第一个事件就报警了：{text!r}")
    expect(player.stopped == 0, "等时钟不是收工，不该把播放器停掉")

    # 7b) 真故障：游戏时钟已经越过第一个事件，却一个事件都没发 —— 必须出声
    text, player = waiting(2.400)
    expect("越过了首个事件" in text, f"时钟过了第一个事件还没发事件，却一声不吭：{text!r}")
    expect(player.stopped == 0, "报一声就够了，不许顺手把状态改了")

    # 7b') 但"刚过一点点"不算：poll 每 0.2s 一眼、播放器 2ms 醒一次，判据得留余量（CLOCK_LATE_MARGIN）。
    text, _ = waiting(1.600)
    expect(not text, f"才刚过第一个事件就报警（会与播放器赛跑）：{text!r}")

    # 7c) 时钟 hook 断了（一个样本都没来）也要出声，但不能太早
    text, _ = waiting(None)
    expect("一个时钟样本都没来" in text, f"一个样本都没有却一声不吭：{text!r}")
    text, _ = waiting(None, elapsed=1.0)
    expect(not text, f"才等了 1s 就报「一个时钟样本都没来」：{text!r}")

    # 8) 暂停不算"等不到"：时钟被我们自己按住、或者游戏说没在走 —— 谁都不该发事件
    text, _ = waiting(2.400, held=True)
    expect(not text, f"暂停（时钟按住）期间不该报等不到时钟：{text!r}")
    text, _ = waiting(2.400, playing=False)
    expect(not text, f"游戏自己说停着的时候不该报等不到时钟：{text!r}")

    # 9) 游戏自己报的暂停 / 退场：**信号驱动** —— 暂停按住时钟 + 抬手但不停播放器；退场停播放器 + 清时钟。
    class CountingPlayer:
        def __init__(self) -> None:
            self.stopped = 0
            self.lifted = 0
            self.resumed = 0

        def stop(self) -> None:
            self.stopped += 1

        def join(self, timeout: float | None = None) -> bool:
            return True

        def release_all(self) -> int:
            self.lifted += 1
            return 2

        def resume(self) -> None:
            self.resumed += 1

    controller = make_controller()
    controller._report = lambda player: None  # type: ignore[method-assign]
    player = CountingPlayer()
    controller.player = player
    controller.clock.feed(7.8, host_time=time.monotonic())
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        controller.handle_play_state(False, 7.8)  # 暂停
    expect(player.lifted == 1, "暂停时该把按着的手指抬起来")
    expect(player.stopped == 0, "暂停不是退场：不该把播放器停掉")
    expect(controller.clock.held, "暂停时该把时钟按住（暂停期间它的时间不是时间源）")
    expect(controller.clock.host_for(9.0) is None, "按住期间一个事件都不该放行")
    expect("暂停" in buffer.getvalue(), f"暂停没有明说：{buffer.getvalue()!r}")

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        controller.handle_play_state(True, 7.8)  # 恢复
    expect(player.resumed == 1, "恢复时要把抬掉的手指按回原位")
    expect(not controller.clock.held, "恢复时该放开时钟")
    expect("重新对表" in buffer.getvalue(), f"恢复没有说明重新对表：{buffer.getvalue()!r}")

    controller.clock.feed(7.8, host_time=time.monotonic())
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        controller.handle_level_gone(7.9)  # 退出 / 重开 / 结算清场
    expect(player.stopped == 1, "这一局没了就该停掉播放器")
    expect(controller.player is None, "停掉之后 player 该是空的")
    expect(controller.clock.now() is None, "这一局没了就该把时钟清空（下一局重新对表）")
    expect("不在了" in buffer.getvalue(), f"这一局没了却没有明说：{buffer.getvalue()!r}")

    # 10) detach：只断会话，**设备与触控后端都留着**（之后还能 attach / spawn 接回来）
    class DetachAgent:
        def __init__(self) -> None:
            self.stopped = 0

        def stop(self) -> None:
            self.stopped += 1

    controller = make_controller()
    controller._report = lambda player: None  # type: ignore[method-assign]
    controller.agent = DetachAgent()
    controller.agent_state = "ok"
    controller.backend = object()  # 假装后端起着
    player = CountingPlayer()
    controller.player = player
    controller.clock.feed(5.0, host_time=time.monotonic())
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        controller.detach()
    expect(controller.agent is None, "detach 之后该没有 agent 了")
    expect(controller.agent_state != "ok", f"detach 之后状态该变：{controller.agent_state}")
    expect(player.stopped == 1, "detach 要先把播放器停掉")
    expect(controller.clock.now() is None, "detach 要清游戏时钟（下一局重新对表）")
    expect(controller.backend is not None, "detach **不该**关触控后端（那是 quit 的事）")
    expect(not controller.stopping, "detach 不是收工：主干还要继续跑")

    # 没在注入时也不该炸，而且要说清楚
    controller = make_controller()
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        controller.detach()
    expect("没在注入" in buffer.getvalue(), f"没 agent 时要说明白：{buffer.getvalue()!r}")

    # 11) `status` 里那句"游戏在不在走"：**没观测到就说不知道**（playing 的三态：None 不许当成 False）。
    controller = make_controller()
    controller._report = lambda player: None  # type: ignore[method-assign]
    controller.agent_state = "ok"
    agent = SimpleNamespace(pid=None, level_label="?", playing=None)
    controller.agent = agent  # type: ignore[assignment]

    def playing_line() -> str:
        return next((line for line in controller.status_lines() if "游戏那边" in line), "")

    expect("还没观测到" in playing_line(), f"没观测到时不该下结论：{playing_line()!r}")
    agent.playing = True
    expect(
        "在走" in playing_line() and "没在走" not in playing_line(),
        f"游戏在走时没说对：{playing_line()!r}",
    )
    agent.playing = False
    expect("没在走" in playing_line(), f"游戏停着时没说对：{playing_line()!r}")

    return problems
