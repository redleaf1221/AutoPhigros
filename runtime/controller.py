"""这一把的全部家当：设置、设备、触控后端、游戏时钟、agent、当前播放器。

它是 ``console.py`` 唯一的依赖（控制台只认它），也是唯一知道"现在到底连着谁、在打什么"
的地方。``main.py`` 只负责组装，逻辑都在这里。
"""

from __future__ import annotations

import signal
import sys
import threading
import time
from typing import Any

import frida

import backends
import planner
import touch
from .agent import Agent, LevelStart, MIN_LATENCY_SAMPLES, PING_TIMEOUT, READY_TIMEOUT, TESTED_FRIDA
from algorithms.chart import OFFICIAL_SCREEN
from algorithms.utils import PlanResult
from .config import AGENT, CHARTS_DIR, PACKAGE, PLANS_DIR, Config
from .options import Options
from .output import log, log_path
from formats.storage import save_chart


PROBE_INTERVAL = 0.5
"""隔多久问 agent 一声"还活着吗"（秒）。"""

POLL_INTERVAL = 0.2
"""主干（poll 线程）隔多久看一眼"时钟重锚了没、这一局打完了没"（秒）。"""

MAX_AUTO_STEP = 0.1
"""延迟自校准单次最多挪这么多（秒）。越过就说明不是"送达延迟"那么简单。"""

CLOCK_WAIT_WARN = 3.0
"""播放器等游戏时钟等这么久就报一声。

正常情况下第一个样本 100ms 内就来。等不到只有两种可能：时钟停着（还在起播前），或者
排期映射错了 —— 后者会表现为"一个事件都不发、音符一个个被 Miss 掉"，所以必须让它出声。
"""


SHUTDOWN_WAIT = 6.0
"""``shutdown()`` 里"等已经开始了的那次拆除做完"最多等多久（秒）。

比 ``TEARDOWN_TIMEOUT`` 宽：拆除里 unload/detach 自己最多等 3 秒，还要算上停播放器
（最多 2 秒）与关后端。真超了也确实该丢下它走人 —— 收工不能变成另一种卡住。
"""


REANCHOR_WARN = 0.02
"""时钟重锚挪动超过这么多秒就当场报警。

重锚会让接下来一小段的事件整体偏晚，而"最大迟到"那个指标量的是"相对我自己的排期"，
排期本身错位它照样报 0 —— 所以必须单独盯这一个量。
"""


class Controller:
    """这一把的全部家当：设置、设备、触控后端、游戏时钟、agent、当前播放器。

    它也是 ``console.py`` 唯一的依赖 —— 控制台只认这里的方法与 :attr:`config` /
    :attr:`options` / :attr:`stopping`。**设备与注入都由命令驱动**：``select_device()``
    选设备，``launch()`` 注入，命令行参数那一套已经没有了。
    """

    def __init__(self, config: Config, options: Options | None = None) -> None:
        self.config = config
        """落盘的那份配置（device / planner / latency / cache / save_chart / backend）。"""
        self.options = options if options is not None else Options(
            planner=config.planner, latency=config.latency
        )
        """运行期旋钮。与 config 的分工：``options`` 立刻生效，``config`` 负责记住。"""
        self.clock = touch.GameClock()

        self.backend: backends.Backend | None = None
        """触控后端**整个会话只开一次**（推 server、起 JVM、连控制通道要一两秒），
        跨关复用；重新 ``launch`` 也不用重开 —— 它挂在设备上，不挂在游戏进程上。"""

        self.agent: Agent | None = None
        self.player: touch.Player | None = None
        self.player_seq = 0
        self.agent_state = "未启动"
        """最近一次探活的结果：``未启动`` / ``ok`` / ``hang`` / ``dead``。"""
        self._player_started = 0.0
        """当前播放器是何时架起来的（主机单调时钟）：用来判断"静默多久才算这一局没了"。"""
        self._clock_warned = False
        """这一局已经报过"等不到时钟"了没有（只报一次，别刷屏）。"""

        self._stopping = threading.Event()
        self._teardown_lock = threading.Lock()
        self._teardown_started = False
        self._teardown_done = threading.Event()
        self._device: frida.core.Device | None = None
        self._watchdog: threading.Thread | None = None
        self._poller: threading.Thread | None = None
        self._player_lock = threading.Lock()
        """护着 :attr:`player` 的交接：三个线程（主干 / 探活 / 开谱）都会碰它。"""

    # ------------------------------------------------------------ 生命周期

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    def stop(self) -> None:
        """收工：请求主干停手，**并且当场开始拆除**。控制台的 ``quit`` 与 Ctrl+C 都走它。

        两件事一起做，是因为"只置一个标志、等主干自己发现"在这里不成立：主干可能正卡在
        启动阶段的阻塞调用里（frida 找设备自带 10 秒超时、等 agent 握手、推 scrcpy 要一两秒），
        那时候置标志等于什么都不做 —— 屏幕上还是那个 ``auto> ``，游戏上却已经挂着我们的 hook，
        人就以为"退出来了"。Ctrl+C 看起来干脆，只是因为它能打断阻塞调用。

        可重复调、可从任何线程调：拆除只做一次，后来的人只是等它做完。
        """
        self._stopping.set()
        self.shutdown()

    def shutdown(self) -> None:
        """拆除一次，且只拆一次；第二个到的人在这里等第一个拆完。

        为什么**必须等**：拆除的最后一步（``Agent.stop``）只是起了一条线程去 unload / detach
        —— 进程冻住时它会挂住，所以不能同步等死。主干要是扭头就把进程结束了，设备上就留下
        一个还挂着 hook 的游戏，而"取消注入"正是收工要办的事。

        为什么拆之前先屏蔽 SIGINT：**Ctrl+C 常常要按两下**（第一下没见动静，人就再按一下），
        第二下会落在拆除中途，把 unload / detach 打断 —— 那就等于没取消注入，还甩一份
        ``KeyboardInterrupt`` 的堆栈出来。第一下已经进来了，够了。信号只能在主线程装，
        装完还原（自检会在同一个进程里反复走这条路）。
        """
        previous = None
        if threading.current_thread() is threading.main_thread():
            previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            with self._teardown_lock:
                starter = not self._teardown_started
                self._teardown_started = True
            if starter:
                try:
                    self._teardown()
                finally:
                    self._teardown_done.set()
            self._teardown_done.wait(SHUTDOWN_WAIT)
        finally:
            if previous is not None:
                signal.signal(signal.SIGINT, previous)

    def _teardown(self) -> None:
        """真正的拆除：停播放器、关触控后端、断会话。每一步都自己兜住异常。

        收工是一串"本来就不一定成功"的动作（进程可能已经没了、后端可能早就退了），中间哪一步
        失败都不该拦住后面几步 —— 尤其是最后那一步"取消注入"。
        """
        for what, step in (
            ("停播放器", self.stop_player),
            ("关触控后端", self._close_backend),
            ("断开 frida 会话", self._stop_agent),
        ):
            try:
                step()
            except Exception as error:  # noqa: BLE001 - 收工路上不该再抛
                log(f"[main] 收工时{what}没做好：{type(error).__name__}: {error}", file=sys.stderr)

    def _close_backend(self) -> None:
        backend, self.backend = self.backend, None
        if backend is not None:
            backend.close()

    def _stop_agent(self) -> None:
        agent, self.agent = self.agent, None
        if agent is not None:
            agent.stop()

    def list_devices(self) -> list[frida.core.Device]:
        """列出**能跑这个项目的**设备：USB 的，以及手动加过的远程 server。

        为什么把 frida 自带的那几台滤掉：``local`` / ``socket`` / ``barebone`` 是给桌面进程
        用的，跟 Phigros 没关系，却永远存在（在 Windows 上它们还都报 ``remote`` 类型）——
        留着的话"只有一台就自动选中"这条规则永远不会触发，人每次都得手打 ``device <id>``。
        远程 server 用"加进去时返回的那台设备"认，不去猜类型。
        """
        manager = frida.get_device_manager()
        found: dict[str, frida.core.Device] = {}
        for host in self.config.hosts:
            try:
                device = manager.add_remote_device(host)
            except Exception as error:  # noqa: BLE001 - 加不上就跳过，别的设备照样列
                log(f"[main] 远程 server {host} 加不上：{error}", file=sys.stderr)
                continue
            found[device.id] = device
        for device in manager.enumerate_devices():
            if device.type == "usb":
                found[device.id] = device
        return list(found.values())

    def add_host(self, host: str) -> bool:
        """手动加一个远程 frida-server（原先的 ``-H``）。"""
        if host in self.config.hosts:
            log(f"[main] {host} 已经在列表里了")
        else:
            self.config.hosts.append(host)
            self.persist()
            log(f"[main] 已记住远程 server {host}")
        try:
            device = frida.get_device_manager().add_remote_device(host)
        except Exception as error:  # noqa: BLE001 - 记下来了，连不上就先报出来
            log(f"[main] 连不上 {host}：{error}", file=sys.stderr)
            return False
        log(f"[main] 连上了 {device.name}（id={device.id}）—— 用 device {device.id} 选中它")
        return True

    def select_device(self, identifier: str) -> bool:
        """选中一台设备并连上（原来靠 ``-D`` 指定 id 干的事）。"""
        try:
            device = frida.get_device(identifier, timeout=5)
        except frida.InvalidArgumentError:
            log(f"[main] 没有 id 为 {identifier} 的设备：先打 devices 看列表", file=sys.stderr)
            return False
        except frida.TransportError as error:
            log(f"[main] 连 {identifier} 失败：{error}", file=sys.stderr)
            return False

        self._device = device
        self.config.device = device.id
        self.persist()
        log(f"[main] 选中设备 {device.name}（{device.id}，{device.type}）")
        self.print_ready_hint()
        return True

    def auto_select_device(self) -> bool:
        """首轮自动选设备：**只有一个就选中它**，多了就列出来让人挑。"""
        try:
            devices = self.list_devices()
        except frida.TimedOutError:
            log("[main] 未发现 USB 设备：检查 adb devices 与 frida-server", file=sys.stderr)
            return False
        except frida.TransportError as error:
            log(f"[main] 与 frida-server 的通信中断：{error}", file=sys.stderr)
            return False

        if not devices:
            log("[main] 一台设备都没有：插上 USB（或 host add <地址>）之后再打 devices", file=sys.stderr)
            return False
        if len(devices) == 1:
            return self.select_device(devices[0].id)
        log(f"[main] 有 {len(devices)} 台设备，用 device <id> 选一台：")
        self.print_devices(devices)
        return False

    def print_devices(self, devices: list[frida.core.Device] | None = None) -> None:
        """把设备列表打出来，选中的那个打勾。"""
        devices = self.list_devices() if devices is None else devices
        if not devices:
            log("[main] 一台设备都没有（插 USB，或 host add <地址>）")
            return
        for device in devices:
            mark = "→" if self.config.device == device.id else " "
            log(f"  {mark} {device.id:<24} {device.name}（{device.type}）")

    def print_ready_hint(self) -> None:
        log("[main] 敲 spawn 启动游戏并注入，或 attach [pid] 附加到已经在跑的那个")

    def persist(self) -> None:
        """把运行期设置同步进 config 并落盘（控制台改完设置就调它）。"""
        self.config.planner = self.options.planner
        self.config.latency = self.options.latency
        self.config.save()

    def launch(self, *, spawn: bool, pid: int | None = None) -> bool:
        """在选中的设备上启动或附加，并注入。可以反复打（等于旧的 respawn / reattach）。

        失败一律**报清楚并留在原地**：不猜、不重试、不自己换个设备 —— 选哪台、要不要重来
        都是人的决定。
        """
        if self.stopping:
            log("[main] 已经收工了，不再注入", file=sys.stderr)
            return False
        if self._device is None:
            log("[main] 还没选设备：devices 看列表，device <id> 选一台", file=sys.stderr)
            return False

        self.stop_player()
        old, self.agent = self.agent, None
        self.agent_state = "已断开"
        if old is not None:
            old.stop()
        self.clock.reset()

        try:
            self.start_agent(attach=not spawn, pid=pid)
        except FileNotFoundError as error:
            log(f"[main] {error}", file=sys.stderr)
            return False
        except frida.ProcessNotFoundError as error:
            log(f"[main] 附加不上：{error}", file=sys.stderr)
            self._report_candidates()
            return False
        except frida.TransportError as error:
            log(f"[main] 与 frida-server 的通信中断：{error}", file=sys.stderr)
            if "agent connection closed" in str(error):
                log(
                    f"[main] 这个报错几乎总是 frida 版本问题：frida-server 与客户端都换成 "
                    f"{TESTED_FRIDA} 再试。\n"
                    f"       判别方法：attach 一个无关进程（如 com.android.systemui）也失败，"
                    f"就与游戏无关。",
                    file=sys.stderr,
                )
            self.agent_state = "未启动"
            self._stop_agent()
            return False
        except Exception as error:  # noqa: BLE001 - 注入失败不该把主干带走
            log(f"[main] 注入失败：{type(error).__name__}: {error}", file=sys.stderr)
            self.agent_state = "未启动"
            self._stop_agent()
            return False

        self.open_backend()
        self.watch()
        log(f"[main] 就绪 —— {self.options.summary()}")
        log("[main] 自己点到那首歌，开谱时自动接管")
        return True

    def _resolve_target(self, pid: int | None = None) -> int | str:
        """附加到谁：``attach <pid>`` 给了 pid 就用它，否则**先问应用列表，再问进程列表，
        最后才让 frida 自己按名字找**。

        这个顺序是被实机教出来的。那台设备上 Phigros 的进程名是 ``Phigros``（应用标签），
        不是包名 —— ``enumerate_processes()`` 里根本没有 ``com.PigeonGames.Phigros`` 那一条，
        于是 ``device.attach(PACKAGE)`` 报 ``ProcessNotFoundError: unable to find process with
        name 'com.PigeonGames.Phigros'``，而游戏明明开着。``enumerate_applications()`` 是从
        包管理器拿的（identifier → pid），root 看不到别的进程时它照样有 —— 所以它排第一。

        进程名那一层也留着，一是有些设备上它确实是包名，二是也可能想附加一个不是"应用"的
        进程（Unity 还可能跑在 ``包名:xxx`` 的子进程里，所以有前缀匹配）。
        """
        if pid is not None:
            return int(pid)
        device = self._device
        assert device is not None

        try:
            for app in device.enumerate_applications():
                if app.identifier == PACKAGE and app.pid:
                    log(f"[main] 应用列表里找到 {app.name or PACKAGE}（pid={app.pid}）")
                    return int(app.pid)
        except Exception as error:  # noqa: BLE001 - 拿不到应用列表不该挡住后面两条路
            log(f"[main] 读应用列表失败：{type(error).__name__}: {error}", file=sys.stderr)

        processes: list[Any] = []
        try:
            processes = list(device.enumerate_processes())
        except Exception as error:  # noqa: BLE001
            log(f"[main] 读进程列表失败：{type(error).__name__}: {error}", file=sys.stderr)

        for matcher in (lambda p: p.name == PACKAGE, lambda p: p.name.startswith(PACKAGE)):
            for process in processes:
                if matcher(process):
                    log(f"[main] 进程列表里找到 {process.name}（pid={process.pid}）")
                    return int(process.pid)

        log(f"[main] 应用与进程列表里都没有 {PACKAGE}，交给 frida 按名字找", file=sys.stderr)
        return PACKAGE

    def _report_candidates(self) -> None:
        """附加失败时把 frida 现在**看得见**的东西列出来。

        报错只写 "unable to find process with name X" 是不够的：看不清是"枚举被挡"、
        "名字对不上"还是"进程真没了"。列一次之后，下一次它就自己说明白了。
        """
        device = self._device
        if device is None:
            return
        try:
            running = [(app.identifier, app.pid, app.name) for app in device.enumerate_applications() if app.pid]
        except Exception as error:  # noqa: BLE001
            log(f"[main] 读应用列表也失败了：{error}", file=sys.stderr)
            running = []
        if running:
            log(f"[main] frida 现在看得见的应用（{len(running)} 个）：", file=sys.stderr)
            for identifier, pid, name in running[:20]:
                log(f"       {pid:>6}  {identifier}  ({name})", file=sys.stderr)
            if len(running) > 20:
                log(f"       …还有 {len(running) - 20} 个", file=sys.stderr)
        try:
            processes = list(device.enumerate_processes())
        except Exception as error:  # noqa: BLE001
            log(f"[main] 读进程列表也失败了：{error}", file=sys.stderr)
            return
        likely = [p for p in processes if "higi" in p.name or "igeon" in p.name]
        log(
            f"[main] 进程列表里名字像它的：{[f'{p.name} ({p.pid})' for p in likely] or '一个都没有'}"
            f"（共 {len(processes)} 个进程）",
            file=sys.stderr,
        )
        log(
            "[main] 名字对不上时用 --pid 直接指定，例如：main.py --attach --pid "
            f"{running[0][1] if running else 0}",
            file=sys.stderr,
        )

    def start_agent(self, *, attach: bool, pid: int | None = None) -> None:
        """（重新）注入一次。设备已经选好了，这里只管会话。"""
        assert self._device is not None
        target = self._resolve_target(pid) if attach else None
        agent = Agent(AGENT, self._device, self.options, attach=attach, target=target)
        self.agent = agent
        agent.start()
        self._await_ready(agent)
        agent.clock = self.clock
        agent.on_level_start = self.handle_level_start
        agent.on_play_state = self.handle_play_state
        agent.on_level_gone = self.handle_level_gone
        agent.on_result = self.handle_result
        self.agent_state = "ok"

    def _await_ready(self, agent: Agent) -> None:
        """等 agent 报 ready —— **按片等**，收工了就不再等。

        一次 ``Event.wait(READY_TIMEOUT)`` 的话，``quit`` 落在这个窗口里就是"按了没反应"
        （标志位拦不住 ``wait``）。而不明原因等满 30 秒本来就是该报警的事，不是该把人锁在
        里面按什么都没反应的事。
        """
        deadline = time.monotonic() + READY_TIMEOUT
        while time.monotonic() < deadline:
            if agent.ready.wait(0.1):
                return
            if self.stopping:
                log("[main] 还在等 agent 握手就收到了收工，不等了", file=sys.stderr)
                return
        log(f"[main] 警告：{READY_TIMEOUT:.0f}s 内未收到 agent 的 ready 消息", file=sys.stderr)

    def open_backend(self, name: str | None = None) -> None:
        """把触控后端架起来。架不起来就降级成"只采集不打"，不影响谱面和缓存。

        ``name`` 不给就用配置里的那个。换后端时**先把旧的关掉**（它可能占着 scrcpy server
        与一条 adb forward），再起新的。
        """
        name = self.config.backend if name is None else name
        if self.backend is not None:
            self._close_backend()
        try:
            backend = backends.create(name, serial=self.config.device)
            backend.open(OFFICIAL_SCREEN)
            log(f"[main] 触控后端就绪（{name}）")
        except Exception as error:  # noqa: BLE001 - 打不了歌也要能采谱面
            log(f"[main] 触控后端 {name} 起不来，这一把只采集不打：{error}", file=sys.stderr)
            backend = None
        self.backend = backend

    # ------------------------------------------------------------ 主干线程

    def start(self) -> None:
        """把非主干的活支起来：首轮选设备 + 一条 poll 守护线程。

        **主干是控制台**（跑在主线程上读输入），所以这里只起后台件；poll 循环退到守护线程后
        Ctrl+C 天然落在"正在读输入的那个线程"上，不再有"信号撞在别人身上"那种事。
        """
        if self.config.device:
            log(f"[config] 上次选的是 {self.config.device}，试着连回来")
            if not self.select_device(self.config.device):
                log("[config] 连不上，改由 devices / device <id> 手动选")
                self.config.device = None
                self.persist()
                self.auto_select_device()
        else:
            self.auto_select_device()

        self._poller = threading.Thread(target=self._poll_loop, name="poll", daemon=True)
        self._poller.start()

    def _poll_loop(self) -> None:
        """主干循环的替身：偶尔看一眼时钟重锚与"这一局打完了"。"""
        while not self._stopping.wait(POLL_INTERVAL):
            try:
                self.poll()
            except Exception as error:  # noqa: BLE001 - 看一眼而已，不该把线程带走
                log(f"[main] poll 出错：{type(error).__name__}: {error}", file=sys.stderr)

    # ------------------------------------------------------------ 探活

    def watch(self) -> None:
        """起一条守护线程，每 ``PROBE_INTERVAL`` 秒问 agent 一声（只起一次）。"""
        if self._watchdog is not None and self._watchdog.is_alive():
            return
        self._watchdog = threading.Thread(target=self._watch, name="agent-watchdog", daemon=True)
        self._watchdog.start()

    def _watch(self) -> None:
        while not self._stopping.wait(PROBE_INTERVAL):
            self._watch_once()

    def _watch_once(self) -> None:
        """探活一轮：ok / hang / dead 三种结局，以及"我们自己正忙"时的豁免。

        抽成一个方法是为了能被自检直接调 —— 这个判断错了的后果是"把正在干活的游戏
        当成冻住了，白停一次触控"，而它在真机上一年也难复现第二次。
        """
        agent = self.agent
        if agent is None:
            return
        # 我们自己正忙的时候（卡在闸门 / 谱面在通道上传输）ping 必然排不上队 ——
        # 那不是"游戏冻住了"。这是确知的事实，不是猜测（见 Agent.busy）。
        busy = agent.busy()
        state = agent.probe(PING_TIMEOUT)
        if state == "hang" and busy is not None:
            self.agent_state = "忙"
            return
        if state == self.agent_state:
            return
        self.agent_state = state
        if state == "ok":
            return
        if state == "hang":
            log(
                f"[main] agent 超过 {PING_TIMEOUT:.0f}s 没应答 —— 进程多半被系统冻结了"
                f"（切后台/息屏）。触控已停。",
                file=sys.stderr,
            )
        else:
            log(
                f"[main] agent 没了：{agent.detached or '原因不明'}。触控已停；"
                f"游戏关掉了就打 spawn，还开着就打 attach。",
                file=sys.stderr,
            )
        # 剩下的排期是给上一个进程的：往一个死掉或冻住的游戏里灌输入只会更糟
        self.stop_player()

    # ------------------------------------------------------------ 打歌

    def play(self, plan: PlanResult, *, mirror: bool, seq: int) -> None:
        if self.stopping:
            # 收工是在别处发起的（quit / Ctrl+C）而这条开谱消息刚到：触控后端已经关了、
            # 会话也快断了，这一局的排期没有任何去处，架播放器只会把事件灌进一个我们
            # 已经放手的进程。
            log(f"[touch #{seq:04d}] 正在收工，这一局不架播放器了", file=sys.stderr)
            return

        self.stop_player()
        if self.backend is None:
            log(f"[touch #{seq:04d}] 触控后端没起来，这一局只采集不打", file=sys.stderr)
            return

        # 上一局的时钟样本对这一局没有意义（开播前 nowTime 是钉住的），清掉重新对表
        self.clock.reset()
        self.player_seq = seq
        self._player_started = time.monotonic()
        self._clock_warned = False
        self.player = touch.Player(
            plan, self.backend, self.clock, mirror=mirror, options=self.options
        )
        self.player.start()
        log(
            f"[touch #{seq:04d}] 已就绪：{plan.event_count} 个事件"
            f"{'，已按谱面镜像翻转' if mirror else ''}"
            f"{'，注入关着（只排期不碰设备）' if not self.options.inject else ''}"
            "（等游戏时钟走到第一个音符）"
        )

    def _check_clock_wait(self) -> None:
        """播放器等游戏时钟等太久了就报一声（**只是报，不改状态**）。

        正常第一个样本 100ms 内就到。等不到要么是时钟停着（还在起播前），要么是排期映射
        错了 —— 后者会表现为"一个事件都不发、音符一个个被判 Miss"，所以必须让它出声。

        "这一局还在不在"这件事**不在这里判断**：靠计时去猜（"多久没样本了"）会把"暂停"
        和"退出"混成一锅（两者在样本上的差别只有一次抖动那么宽），而且暂停时样本照样每
        100ms 来一次、只是值不变。这种事要由 agent 的 hook 直说 —— 见 `agent.py` 的暂停
        / 退场 hook。
        """
        player = self.player
        if player is None or self._clock_warned:
            return
        alive = time.monotonic() - self._player_started
        if alive > CLOCK_WAIT_WARN and player.sent == 0:
            self._clock_warned = True
            log(
                f"[touch #{self.player_seq:04d}] 已经等了 {alive:.1f}s 还没发出第一个事件，"
                f"首个事件在谱面 {player.first_seconds:.3f}s。"
                f"（这一局很可能一个音符都按不到，盯着 `[judge]` 看）",
                file=sys.stderr,
            )

    def handle_result(self, seq: int) -> None:
        """这一局**完整打完了**：按需用这一局的判定做延迟自校准。

        量的是什么：游戏报的 `delta` 是"它判定时 `nowTime` 与音符 `realTime` 之差"，
        我们按时送、游戏晚一帧处理，于是它稳定偏正（实测 +29ms）。校准就是把这一批
        Perfect 的**中位数**加到 `latency` 上（正数 = 提前发，正好抵掉它）。

        三条保守的地方：

        * **只认完整打完**（结算消息到达），中途退出不校准 —— 那半局的样本是被打断的；
        * 只收 Perfect、取**中位数**，条数不够（`MIN_LATENCY_SAMPLES`）就不改；
        * 单次调整有上限（`MAX_AUTO_STEP`），越过就拒绝并说清楚 —— 那说明出的不是"送达延迟"
          而是别的问题（时钟映射坏了、被蹭掉一片），拿它去调 latency 只会把病养大。
        """
        agent = self.agent
        if agent is None or not self.config.auto_latency:
            return
        sample = agent.latency_sample()
        if sample is None:
            log(
                f"           （这一局 Perfect 样本不足 {MIN_LATENCY_SAMPLES} 条，延迟自校准跳过）"
            )
            return
        middle, count = sample
        if abs(middle) > MAX_AUTO_STEP:
            log(
                f"           延迟自校准跳过：这一局 Perfect 的中位数是 {middle * 1000:+.0f}ms，"
                f"超过单次上限 {MAX_AUTO_STEP * 1000:.0f}ms —— 那多半不是送达延迟，"
                f"先看 `[judge]` 里的早/晚分布。",
                file=sys.stderr,
            )
            return
        before = self.options.latency
        self.options.latency = before + middle
        self.config.latency = self.options.latency
        self.persist()
        log(
            f"           延迟自校准：{count} 条 Perfect 的中位数 {middle * 1000:+.0f}ms → "
            f"手工补偿 {before * 1000:+.0f}ms 变为 {self.options.latency * 1000:+.0f}ms（已记住）"
        )

    def handle_play_state(self, playing: bool, moment: float | None) -> None:
        """游戏自己报的播放状态（``ProgressControl::Play``）。

        **暂停**：先把时钟按住（`GameClock.hold()`）—— 暂停期间游戏的时间不是时间源，
        一个事件都不该发出去；再把按着的手指抬起来（暂停菜单是按手指位置射线找按钮的，
        手指停在那儿可能替人把"重开"按了）。抬掉的位置记在播放器里。

        **恢复**：把抬掉的指针**按回原位**（不是按到计划的下一个位置 —— 那会把 flick 的
        位移吃掉），然后 `release()` 放开时钟并**从零重新对表**。这一条是"恢复之后播放像
        死了一样"的解药：暂停期间 `nowTime` 要么停着、要么缓慢爬升，而 `min(h−v)` 会因此
        漂到暂停之后，恢复后每个事件都被判"迟到太多"而丢掉。与其猜，不如重新对一次。
        """
        where = "" if moment is None else f"（停在谱面 {moment:.3f}s）"
        if playing:
            if self.player is not None:
                self.player.resume()
            self.clock.release()
            log(f"[level] 游戏起播 / 恢复{where}，时钟重新对表")
            return
        self.clock.hold()
        lifted = self.player.release_all() if self.player is not None else 0
        log(
            f"[level] 游戏暂停{where} —— 触控停手"
            + (f"，把按着的 {lifted} 根手指抬起来" if lifted else "")
        )

    def handle_level_gone(self, moment: float | None) -> None:
        """这一局没了：``LevelControl::OnDestroy()``（退出到选歌 / 重开 / 结算清场）。

        这是**信号**，不是推断 —— 原先主机靠"多久没收到进度样本"猜，而暂停与退出在样本上
        只差一次抖动那么宽（暂停时样本照样每 100ms 来一次、只是值不变）。猜出来的东西
        不能当判据，所以那条路撤了，改由 hook 直说。
        """
        where = "" if moment is None else f"（最后一刻在谱面 {moment:.3f}s）"
        log(
            f"[level] 这一局不在了{where}：退出到选歌 / 重开 / 结算清场。"
            f"触控停掉，时钟清空，等下一次开谱。"
        )
        self.stop_player()
        self.clock.reset()

    def poll(self) -> None:
        """主干偶尔看一眼：时钟重锚了要立刻报、这一局打完了就把账报掉。"""
        self._check_clock_wait()
        if self.clock.take_auto_release():
            log(
                "[level] 没收到「游戏恢复」的信号，但游戏时钟又在走了 —— 自己放行并重新对表。"
                "（`Play(true)` 那个 hook 该看一下）",
                file=sys.stderr,
            )
        shift = self.clock.take_shift()
        # 还没发出过任何事件时的重锚是**正常**的：那是音乐起播、时钟从"钉在 0"变成
        # "跟着音频走"，对表从头来过，重锚量等于起播前等了多久（一两秒）。这时候事件
        # 一个都还没发，报出来只会吓人。真正可疑的是**打到一半**估计值整体挪。
        if shift is not None and abs(shift[1]) > REANCHOR_WARN and self.player and self.player.sent:
            log(
                f"[touch #{self.player_seq:04d}] 时钟重锚：估计往后挪了 {shift[1] * 1000:+.0f}ms"
                f"（第 {self.clock.reanchors} 次）—— 接下来一两秒的排期会整体偏晚",
                file=sys.stderr,
            )

        player = self.player
        if player is not None and player.finished() and self._take_if(player):
            self._report(player)

    def _take_player(self) -> touch.Player | None:
        """把当前播放器取走（取走之后 :attr:`player` 就是 None）。

        取与置空在同一次加锁里完成，所以"这一局打完了"与"agent 死了、停手"同时收工，
        也只有一个能拿到它 —— 交出去的棒只有一根，账不会被报两遍。
        """
        with self._player_lock:
            player, self.player = self.player, None
            return player

    def _take_if(self, player: touch.Player) -> bool:
        """当前播放器**就是这一个**才取走它，返回是否真取到了。

        为什么不能直接"取走再看是不是同一个"：``play()`` 可能刚换成新的，那样就会把
        新播放器顺手摘掉 —— 它的排期还在跑，账却再也没有人收。比一比身份就没这个问题：
        不是同一个就什么都不动。
        """
        with self._player_lock:
            if self.player is not player:
                return False
            self.player = None
            return True

    def stop_player(self) -> None:
        player = self._take_player()
        if player is None:
            return
        player.stop()
        player.join(2.0)
        self._report(player)

    def _report(self, player: touch.Player) -> None:
        if player.error is not None:
            log(f"[touch #{self.player_seq:04d}] 发送出错：{player.error}", file=sys.stderr)

        line = (
            f"[touch #{self.player_seq:04d}] 打完：发了 {player.sent} 个事件，"
            f"最大迟到 {player.late * 1000:.1f}ms"
        )
        if player.muted:
            line += f"，另有 {player.muted} 个因注入关着没发"
        if player.skipped:
            line += f"，另有 {player.skipped} 个迟到太多没发"
        if player.late_count:
            worst = max(player.worst, key=lambda item: item[1]) if player.worst else None
            line += f"（{player.late_count} 帧超过 {touch.LATE_WARN * 1000:.0f}ms"
            if worst is not None:
                line += f"，最差在谱面 {worst[0]:.2f}s 迟到 {worst[1] * 1000:.0f}ms"
            line += "）"
        log(line)
        log(
            f"            单次发送最长 {player.max_send * 1000:.1f}ms；"
            f"时钟重锚 {self.clock.reanchors} 次，采样最大间隔 {self.clock.max_gap * 1000:.0f}ms"
        )

    def handle_level_start(self, start: LevelStart) -> None:
        """处理一次开谱：规划（或读缓存）、按需落盘、把播放器架好。游戏停在闸门上等着。

        谱面只有一份 —— ``FromJson`` 抓到的原文，也就是策划写的那份；规划只对着它做一次，
        算出来的就是**规范解**（不镜像、不偏移）。镜像与延迟都是运行时的事，
        由播放器临时改（``touch.Player``），所以缓存对所有局面通用。
        """
        if self.stopping:
            # 收工是在别处发起的：这一局不必再算一遍（规划一张谱要几秒），闸门照样由 agent 放行
            log(f"[gate #{start.seq:04d}] 正在收工，这一局不规划了")
            return

        raw = start.chart
        if raw is None:
            log(
                f"[gate #{start.seq:04d}] 没收到本局的谱面（chartSeq={start.chart_seq}），只能放行",
                file=sys.stderr,
            )
            return

        ref = raw.ref
        seq = ref.seq
        result = None
        try:
            result = planner.plan(
                raw.text,
                planner=self.options.planner,
                ref=ref,
                cache=self.config.cache,
                directory=PLANS_DIR,
                progress=planner.TqdmProgress(),
            )
        except Exception as error:  # noqa: BLE001 - 规划失败也要把谱面留下来
            log(f"[plan #{seq:04d}] 规划失败：{type(error).__name__}: {error}", file=sys.stderr)

        if self.config.save_chart:
            path = save_chart(
                raw.text,
                ref,
                CHARTS_DIR,
                notes_in_json=result.stats.get("notes") if result else None,
                notes_reported=raw.notes_reported,
            )
            log(f"[chart #{seq:04d}] 已保存 {path.name}")

        if result is None:
            return

        cached = "（缓存）" if result.stats.get("cached") else ""
        log(f"[plan #{seq:04d}] {planner.summary(result)}{cached}")
        if raw.notes_reported is not None:
            inline = result.stats.get("notes")
            if inline is None:
                # 缓存那份的 stats 在旁边的 .meta.json 里；真读不到就说读不到，
                # 别拿 None 去比然后喊"不一致"（那是假警报，真踩过）
                log(f"           音符数核对：游戏 {raw.notes_reported}，这份规划里没记（跳过）")
            else:
                verdict = "一致" if inline == raw.notes_reported else "不一致！"
                log(f"           音符数核对：游戏 {raw.notes_reported}，JSON {inline} -> {verdict}")
        for warning in result.warnings:
            log(f"           ~ {warning}")

        if start.mirror is None:
            log(f"[plan #{seq:04d}] 警告：读不到谱面镜像开关，按不镜像处理", file=sys.stderr)
        self.play(result, mirror=bool(start.mirror), seq=seq)

    # ------------------------------------------------------------ 状态

    def status_lines(self) -> list[str]:
        """给控制台的 ``status``：每一行都是一件"现在到底怎么样"的事实。"""
        agent = self.agent
        if agent is None:
            agent_text = "没有 agent"
        elif agent.pid is None:
            agent_text = f"agent {self.agent_state}（附加模式）"
        else:
            agent_text = f"agent {self.agent_state}（pid {agent.pid}）"

        if self.backend is None:
            backend_text = "触控后端没起来（这一把只采集不打）"
        else:
            backend_text = f"触控后端 {self.config.backend} 已就绪"

        device_text = (
            f"设备 {self._device.name}（{self._device.id}）"
            if self._device is not None
            else "还没选设备（devices / device <id>）"
        )

        now = self.clock.now()
        if self.clock.held:
            clock_text = f"游戏时钟 按住中（暂停）—— 停在 {now or 0:.2f}s"
        elif now is None:
            clock_text = "游戏时钟还没对上表"
        else:
            clock_text = (
                f"游戏时钟 {now:.2f}s（重锚 {self.clock.reanchors} 次，"
                f"采样最大间隔 {self.clock.max_gap * 1000:.0f}ms）"
            )

        lines = [
            f"[状态] {self.options.summary()}",
            f"       {device_text}；{backend_text}",
            f"       {agent_text}；{clock_text}",
            f"       落盘 {self.config.cache and '吃' or '不吃'}缓存"
            f"{'，采集时存谱面' if self.config.save_chart else ''}"
            f"{'，延迟自校准 开' if self.config.auto_latency else ''}"
            f"{'；日志 ' + str(log_path()) if log_path() else '；没在记日志'}",
        ]

        level = agent.level_label if agent is not None else "?"
        if self.player is None:
            lines.append(f"       本局 {level}；现在没有在播放")
        else:
            player = self.player
            lines.append(
                f"       本局 {level}；已发 {player.sent} / {player.plan.event_count} 个事件，"
                f"最大迟到 {player.late * 1000:.1f}ms"
            )
        if agent is not None and not agent.playing:
            lines.append("       游戏那边：音乐没在走（暂停 / 还没起播）")
        return lines

