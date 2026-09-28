"""这一把的全部家当：设置、设备、触控后端、游戏时钟、agent、当前播放器。

它是 ``console.py`` 唯一的依赖，也是唯一知道"现在到底连着谁、在打什么"的地方。
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
"""一个时钟样本都没有时，等这么久就报一声（秒）；时钟 hook 活着的话第一个样本 100ms 内就来。"""


CLOCK_LATE_MARGIN = 0.2
"""游戏时钟越过首个事件这么多还没发出来才算"排期错了"（秒）；`poll()` 每 0.2s 才看一眼。"""


SHUTDOWN_WAIT = 6.0
"""``shutdown()`` 等已经开始了的那次拆除做完，最多等这么久（秒）；比 ``TEARDOWN_TIMEOUT`` 宽。"""


REANCHOR_WARN = 0.02
"""时钟重锚挪动超过这么多秒就报警：重锚会让接下来一小段的事件整体偏晚，"最大迟到"看不见它。"""


class Controller:
    """这一把的全部家当：设置、设备、触控后端、游戏时钟、agent、当前播放器。

    控制台只认这里的方法与 :attr:`config` / :attr:`options` / :attr:`stopping`；设备与注入
    都由命令驱动（``select_device()`` 选设备，``launch()`` 注入）。
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
        """触控后端**整个会话只开一次**（起一次要一两秒）、跨关复用；它挂在设备上，不挂在进程上。"""

        self.agent: Agent | None = None
        self.player: touch.Player | None = None
        self.player_seq = 0
        self.agent_state = "未启动"
        """最近一次探活的结果：``未启动`` / ``ok`` / ``hang`` / ``dead``。"""
        self._clock_wait_from = 0.0
        """播放器架好、闸门随即放行的那一刻（主机单调时钟）；只用于"时钟 hook 是不是断了"。"""
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
        """收工：请求主干停手，**并且当场开始拆除**（控制台的 ``quit`` 与 Ctrl+C 都走它）。

        主干可能正卡在启动阶段的阻塞调用里，只置标志等于什么都不做 —— 屏幕上还是 ``auto> ``，
        设备上却挂着我们的 hook。可重复调、可从任何线程调：拆除只做一次，后来的人等它做完。
        """
        self._stopping.set()
        self.shutdown()

    def shutdown(self) -> None:
        """拆除一次，且只拆一次；第二个到的人在这里等第一个拆完。

        必须等：``Agent.stop`` 只是起线程去 unload / detach，主干扭头就走会在设备上留下一个
        还挂着 hook 的游戏；拆之前还要先屏蔽 SIGINT（Ctrl+C 常常按两下，第二下会打断拆除）。
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
        """真正的拆除：停播放器、关触控后端、断会话；每一步都自己兜住异常。

        这是一串本来就不一定成功的动作（进程可能已经没了），哪一步失败都不该拦住"取消注入"。
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

    def detach(self) -> None:
        """断开注入：把 agent 拆掉（放行闸门、unload、detach），**设备与触控后端都留着**。

        与 `quit` 的区别就是后端留不留 —— 留着的话 `attach` / `spawn` 接回来时不用重开连接。
        """
        if self.agent is None:
            log("[main] 现在就没在注入（没有 agent）")
            return
        self.stop_player()
        self._stop_agent()
        self.agent_state = "未启动"
        self.clock.reset()
        log("[main] 已断开注入（设备与触控后端留着）")

    def list_devices(self) -> list[frida.core.Device]:
        """列出**能跑这个项目的**设备：USB 的，以及手动加过的远程 server。

        滤掉 ``local`` / ``socket`` / ``barebone``：跟 Phigros 没关系却永远存在（Windows 上都报
        ``remote``），留着的话"只有一台就自动选中"永远不会触发；远程 server 用加进去时返回的认。
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
        """手动加一个远程 frida-server。"""
        if host in self.config.hosts:
            log(f"[main] {host} 已经在列表里了")
        else:
            self.config.hosts.append(host)
            self.persist()
            log(f"[main] 已把远程 server {host} 写进配置")
        try:
            device = frida.get_device_manager().add_remote_device(host)
        except Exception as error:  # noqa: BLE001 - 记下来了，连不上就先报出来
            log(f"[main] 连不上 {host}：{error}", file=sys.stderr)
            return False
        log(f"[main] 连上了 {device.name}（id={device.id}）")
        return True

    def select_device(self, identifier: str) -> bool:
        """选中一台设备并连上。"""
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
            log("[main] 一台设备都没有：插上 USB 或 host add <地址>", file=sys.stderr)
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
            log("[main] 一台设备都没有：插上 USB 或 host add <地址>")
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
        """在选中的设备上启动或附加，并注入；可以反复打。

        失败一律**报清楚并留在原地**：不猜、不重试、不自己换设备 —— 选哪台、要不要重来是人决定的。
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
                    f"{TESTED_FRIDA}（判别：attach com.android.systemui 也失败就与游戏无关，"
                    f"见 README 的环境要求）",
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
        """附加到谁：``attach <pid>`` 给了 pid 就用它，否则先问应用列表、再问进程列表、最后交给 frida。

        ``enumerate_applications()`` 从包管理器拿 identifier → pid，root 看不到别的进程时也照样有
        （这台设备的进程名是应用标签不是包名）；进程列表那一层连 ``包名:子进程`` 的前缀匹配也算。
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
        """附加失败时把 frida 现在**看得见**的应用与进程列出来。

        只写 "unable to find process with name X" 分不清是"枚举被挡"、"名字对不上"还是"进程真没了"。
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

        一次 ``Event.wait(READY_TIMEOUT)`` 的话，``quit`` 落在这个窗口里就是"按了没反应"。
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
        """把触控后端架起来；架不起来就降级成"只采集不打"，不影响谱面和缓存。

        ``name`` 不给就用配置里的那个；换后端时**先把旧的关掉**（它可能占着 scrcpy server
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

        **主干是控制台**（跑在主线程上读输入），所以这里只起后台件；poll 退到守护线程后
        Ctrl+C 天然落在"正在读输入的那个线程"上。
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

        抽成方法是为了能被自检直接调：判错了就会把正在干活的游戏当成冻住了。
        """
        agent = self.agent
        if agent is None:
            return
        # 我们自己正忙时（卡在闸门 / 谱面在传输）ping 必然排不上队，那不是"游戏冻住了"
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
                f"（切后台/息屏）；触控已停。",
                file=sys.stderr,
            )
        else:
            log(
                f"[main] agent 没了：{agent.detached or '原因不明'}；触控已停。",
                file=sys.stderr,
            )
        # 剩下的排期是给上一个进程的：往一个死掉或冻住的游戏里灌输入只会更糟
        self.stop_player()

    # ------------------------------------------------------------ 打歌

    def play(self, plan: PlanResult, *, mirror: bool, seq: int) -> None:
        if self.stopping:
            # 收工在别处发起、这条开谱消息刚到：后端已关、会话快断，排期没有去处
            log(f"[touch #{seq:04d}] 正在收工，这一局不架播放器了", file=sys.stderr)
            return

        self.stop_player()
        if self.backend is None:
            log(f"[touch #{seq:04d}] 触控后端没起来，这一局只采集不打", file=sys.stderr)
            return

        # 上一局的时钟样本对这一局没有意义（开播前 nowTime 是钉住的），清掉重新对表
        self.clock.reset()
        self.player_seq = seq
        self._clock_wait_from = time.monotonic()
        self._clock_warned = False
        self.player = touch.Player(
            plan, self.backend, self.clock, mirror=mirror, options=self.options
        )
        self.player.start()
        log(
            f"[touch #{seq:04d}] 已就绪：{plan.event_count} 个事件"
            f"{'，已按谱面镜像翻转' if mirror else ''}"
            f"{'，注入关着' if not self.options.inject else ''}"
        )

    def _check_clock_wait(self) -> None:
        """播放器等游戏时钟等太久了就报一声：要么排期映射错了，要么时钟 hook 断了（只是报，不改状态）。

        "还在等"（时钟没走到第一个事件）与"时钟被自己按住"不算；这一局还在不在由 agent 的
        暂停 / 退场 hook 直说，不在这里靠计时猜。
        """
        player = self.player
        if player is None or self._clock_warned or player.sent:
            return

        agent = self.agent
        if agent is not None and agent.playing is False:
            # 游戏自己在说"音乐没在走"（暂停 / 还没起播）：时钟不走是应该的，不是我们的错
            return

        moment = self.clock.now()
        if moment is None:
            alive = time.monotonic() - self._clock_wait_from
            if alive <= CLOCK_WAIT_WARN:
                return
            reason = f"已经 {alive:.1f}s 一个时钟样本都没来"
        elif self.clock.held:
            return
        elif moment < player.first_seconds + CLOCK_LATE_MARGIN:
            return
        else:
            reason = (
                f"游戏时钟已经走到 {moment:.3f}s，越过了首个事件"
                f"（谱面 {player.first_seconds:.3f}s），却一个事件都没发出去"
            )

        self._clock_warned = True
        log(
            f"[touch #{self.player_seq:04d}] {reason}；这一局很可能一个音符都按不到",
            file=sys.stderr,
        )

    def handle_result(self, seq: int) -> None:
        """这一局**完整打完了**：按需用这一局的判定做延迟自校准。

        游戏报的 ``delta`` 稳定偏正（我们按时送、游戏晚一帧处理），校准就是把这一批
        Perfect 的**中位数**加到 ``latency`` 上；不完整打完、样本不足、单次超过上限都不改。
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
                f"超过单次上限 {MAX_AUTO_STEP * 1000:.0f}ms（多半不是送达延迟）",
                file=sys.stderr,
            )
            return
        before = self.options.latency
        self.options.latency = before + middle
        self.config.latency = self.options.latency
        self.persist()
        log(
            f"           延迟自校准：{count} 条 Perfect 的中位数 {middle * 1000:+.0f}ms → "
            f"手工补偿 {before * 1000:+.0f}ms 变为 {self.options.latency * 1000:+.0f}ms"
        )

    def handle_play_state(self, playing: bool, moment: float | None) -> None:
        """游戏自己报的播放状态（``ProgressControl::Play``）。

        **暂停**：`GameClock.hold()` 按住时钟，再抬起按着的手指 —— 暂停菜单是按手指位置射线
        找按钮的，手指停在那儿可能替人把"重开"按了。**恢复**：按回原位并从零重新对表。
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

        这是**信号**不是推断：暂停与退出在进度样本上只差一次抖动那么宽（暂停时样本照样每
        100ms 来一次、只是值不变），所以不靠计时猜。
        """
        where = "" if moment is None else f"（最后一刻在谱面 {moment:.3f}s）"
        log(
            f"[level] 这一局不在了{where}：退出到选歌 / 重开 / 结算清场；触控停掉，时钟清空"
        )
        self.stop_player()
        self.clock.reset()

    def poll(self) -> None:
        """主干偶尔看一眼：时钟重锚了要立刻报、这一局打完了就把账报掉。"""
        self._check_clock_wait()
        if self.clock.take_auto_release():
            log(
                "[level] 没收到「游戏恢复」的信号，但游戏时钟又在走了 —— 自己放行并重新对表",
                file=sys.stderr,
            )
        shift = self.clock.take_shift()
        # 还没发过事件时的重锚是正常的（起播时时钟从"钉在 0"变成"跟着音频走"）；可疑的是打到一半整体挪
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

        取与置空在同一次加锁里完成，所以同时收工也只有一个能拿到它，账不会被报两遍。
        """
        with self._player_lock:
            player, self.player = self.player, None
            return player

    def _take_if(self, player: touch.Player) -> bool:
        """当前播放器**就是这一个**才取走它，返回是否真取到了。

        直接"取走再看是不是同一个"的话，``play()`` 刚换上的新播放器会被顺手摘掉、账没人收。
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
            line += f"（{player.late_count} 帧超过 {self.options.late_warn * 1000:.0f}ms"
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

        谱面只有一份（``FromJson`` 抓到的原文），规划算出来的是**规范解**（不镜像、不偏移）；
        镜像与延迟由播放器临时改，所以缓存对所有局面通用。
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
        options = self.config.planner_options.get(self.options.planner, {})
        try:
            result = planner.plan(
                raw.text,
                planner=self.options.planner,
                options=options,
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
        tuned = f" 参数 {options}" if options else ""
        log(f"[plan #{seq:04d}] {planner.summary(result)}{cached}{tuned}")
        if raw.notes_reported is not None:
            inline = result.stats.get("notes")
            if inline is None:
                log(f"           音符数：游戏 {raw.notes_reported}，这份规划里没记")
            else:
                verdict = "一致" if inline == raw.notes_reported else "不一致！"
                log(f"           音符数：游戏 {raw.notes_reported}，JSON {inline} -> {verdict}")
        for warning in result.warnings:
            log(f"           ~ {warning}")

        if start.mirror is None:
            log(f"[plan #{seq:04d}] 读不到谱面镜像开关，按不镜像处理", file=sys.stderr)
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

        options_text = self.config.planner_options.get(self.options.planner, {})
        lines = [
            f"[状态] {self.options.summary()}"
            + (f"，参数 {options_text}" if options_text else ""),
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
        if agent is not None:
            if agent.playing is True:
                lines.append("       游戏那边：音乐在走")
            elif agent.playing is False:
                lines.append("       游戏那边：音乐没在走（暂停 / 还没起播）")
            else:
                # 没读到 isPlaying，也没听到过 Play —— 不知道就说不知道
                lines.append("       游戏那边：播放状态还没观测到（isPlaying 没读到）")
        return lines

