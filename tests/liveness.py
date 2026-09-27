"""存活探测与收工：`ok` / `hang` / `dead` 三种结局，以及收工那条路上的一串规矩。"""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

from storage import ChartRef
import planner
from .stubs import Recorder, _trunk_args


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

