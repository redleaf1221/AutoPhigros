"""一次 frida 注入的**全部**：会话、闸门、消息分发、判决对账、结算。

从 ``main.py`` 拆出来的理由：这些是"agent 那边的事"，与"主机怎么调度、控制台怎么发命令"
没有关系。``Controller`` 只跟它打交道三件事 —— 起它、收它、把回调装上。
"""

from __future__ import annotations

import statistics
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import frida

import touch
from algorithms.chart import NoteType
from .config import PACKAGE
from .options import Options
from .output import log
from formats.storage import ChartRef


TESTED_FRIDA = ["17.10.1", "17.17.0"]
"""实测可用的 frida 版本。

17.19.0 在 Android 15 + KernelSU 的设备上 attach 任何进程都会抛
``TransportError: agent connection closed unexpectedly``（spawn 本身正常，
挂在注入环节，与 Phigros 无关 —— attach systemui 也一样失败）。17.10.1 正常。
"""


READY_TIMEOUT = 30.0


PING_TIMEOUT = 2.0
"""一声问候等多久算没回应（秒）。比采样间隔（100ms）宽出一个量级，不会误判。"""


TEARDOWN_TIMEOUT = 3.0
"""断开旧会话最多等多久（秒）。进程被冻住时 unload/detach 会挂住，超了就直接丢下它。"""


NOTE_TYPE_NAMES = {int(kind): kind.name.title() for kind in NoteType}
"""``ChartNote.type`` 的数字 -> 名字。与谱面 JSON 的 ``type`` 同源（``algorithms.chart``）。"""


JUDGE_ARITHMETIC_TOLERANCE = 0.05
"""``delta`` 与 ``nowTime − realTime`` 允许差多少秒。

两个数其实是同一帧里算出来的同一个量（游戏算它自己的早晚量，我们读它当时的 ``nowTime``），
正常应当一模一样；0.05 只是给"我晚一步才读到"留的余量。
"""


MISS_GAP_LIMIT = 3.0
"""Miss 不带早晚量，只能靠"判定时刻离音符时刻多远"来怀疑表抄错了（**上界**）。

游戏的 Miss 阈值是 0.1~0.18 秒，正常就是刚越过窗口就判掉。但**卡顿之后会补判一批**：
一帧停了 1 秒，扫描游标就往前挪 1 秒，中间的音符全是"晚了很多"的合法 Miss。
所以这里只能粗着来 —— 留 3 秒，够容下卡顿补判，又比真出过的那次（差 89.9 秒）小两个数量级。
真正严的判据在另一条路径上：命中的那些判决带 `delta`，可以拿恒等式逐条对。
"""

MISS_EARLY = 0.22
"""Miss 也可能**早于** ``realTime`` 触发，这是下界（秒）。

什么时候会早：hold 的**身体**宽限耗尽（`_safeFrame`，见 `HoldControl::Judge` 第 241-295 行）
—— 头判被一次"不是瞄它"的按下标记过、而那根手指随即离开，连续 4 帧没手指就判 Miss，
这个时刻可以早于音符自己的 realTime；手指一直没碰的则不会早（头判的 Miss 在 +0.22 之后）。
实测见过早 0.093s 的那一条（AT 谱 99.310s 的 hold，判定于 99.217s）。留 0.22 的余量。
原先下界是"早一点点都不行"（`-0.05`），于是这类完全正常的 Miss 全被当成"音符表抄错了"报出来
—— 假警报比漏报更浪费时间。
"""

MIN_LATENCY_SAMPLES = 30
"""延迟自校准至少要这么多条 Perfect 才给结论。少了那不是校准，是猜。"""

CHART_WAIT = 20.0
"""从"知道要读谱面了"到"谱面到手"最多算我们忙这么久（秒）。

9MB 的谱面在通道上走一趟要好几秒，超时了就不再拿它当借口 —— 免得真挂了也不报。
"""


HOLD_SETTLE_LEAD = 0.22
"""Hold 的收尾判决离"按住结束"还有这么远（秒）。

``HoldControl::Judge`` 在 ``nowTime > realTime + holdTime − 0.22`` 时结算，**传给 Perfect/Good
的早晚量是从这个结算点量的**，不是从按住那一刻量。所以一个 2.4 秒的 hold 收尾时，
``nowTime − realTime`` 是 +2.2 出头、而游戏报的 ``delta`` 只有十几毫秒 —— 拿头判的恒等式去对，
会把每一条 hold 都冤枉成"表抄错了"（实测 Dlyrotz HD 正好冤枉了 8 条，就是那 8 个 hold）。
"""


@dataclass(slots=True)
class CapturedChart:
    """agent 从 ``JsonUtility::FromJson`` 回传的一张谱面（**镜像之前**）。"""

    ref: ChartRef
    text: str
    received_at: float
    notes_reported: int | None = None
    """游戏自己的 ``Chart::GetNoteCount`` 报出来的音符数，比 FromJson 晚一步到达。"""


@dataclass(slots=True)
class LevelStart:
    """谱面真正启动那一刻的现场。

    谱面正文不在这里 —— 它由 :class:`CapturedChart`（FromJson 那个咽喉点）在**镜像之前**
    就抓走了，也就是 :attr:`chart`。闸门只多告诉两件事：这一局有没有开谱面镜像，
    以及游戏自己的延迟设置是多少。
    """

    seq: int
    """闸门编号，放行时原样回给 agent。"""
    chart_seq: int
    """对应的 FromJson 谱面编号。"""
    mirror: bool | None
    """``LevelStartInfo.mirror``：谱面镜像开关。读不到就是 None。"""
    offset: dict[str, float | None]
    """游戏生效的延迟（秒）：``total`` 是真正用的那个，``chart`` / ``user`` 是它的组成部分。"""
    chart: CapturedChart | None


class Agent:
    """一次 frida 注入：建会话、收消息、放行闸门、探活。

    一个 ``Agent`` 只对应**一个进程**。进程没了就把它丢掉、另起一个（``Controller.restart``），
    所以这里不打算"自己重连"—— 重连要选 spawn 还是 attach，那是人的决定。

    ``level-start`` 必须**在处理它的那条消息里**跑完再放行，所以这里不排队：agent
    把现场交给 :attr:`on_level_start`，回调抛异常也照样放行 —— 卡死游戏比规划失败
    严重得多。
    """

    def __init__(
        self,
        agent_path: Path,
        device: frida.core.Device | None = None,
        options: Options | None = None,
        *,
        attach: bool = False,
        target: int | str | None = None,
    ) -> None:
        """``device`` / ``options`` 可以不给 —— 只有在"只喂消息、不真的连设备"的场合
        （``tests/`` 里的闸门与结算自检）才这么用；真跑的时候 :meth:`start` 一定要有设备。

        ``target`` 是附加的目标：**pid 优先**（由 :meth:`Controller._resolve_target` 枚举出来），
        给不出来才是名字。这不是洁癖：实测这台设备上 Phigros 的**进程名是 ``Phigros``**，
        不是包名，按包名找永远找不到。
        """
        self.agent_path = agent_path
        self.device = device
        self.options = options if options is not None else Options()
        self.attach = attach
        self.target: int | str = PACKAGE if target is None else target
        """附加到谁：pid 或名字。"""

        self.ready = threading.Event()
        self.on_level_start: Callable[[LevelStart], None] | None = None
        """放行之前要干的活，由 :class:`Controller` 装上。"""

        self.on_play_state: Callable[[bool, float | None], None] | None = None
        """游戏报播放状态（暂停 / 恢复）时调它，由 :class:`Controller` 装上。"""

        self.on_level_gone: Callable[[float | None], None] | None = None
        """这一局没了（关卡对象销毁）时调它，由 :class:`Controller` 装上。"""

        self.playing: bool | None = None
        """游戏自己在说的"音乐在走吗"。``None`` = 还没观测到。

        两个来源，都是**观测**：

        * ``progress`` 采样里带的 ``ProgressControl.isPlaying``（每 100ms 一次，主力）——
          它在 ``ProgressControl`` 的构造函数里就是 true、``Play(false)`` 才清 0，
          所以从起播第一刻起就是准的；
        * ``play-state`` 事件（``Play`` 的调用）—— 只在**暂停 / 恢复 / 退场**三条路上响，
          **开谱起播不经过它**。

        踩过：这两个字段以前是个 bool、初值 False，而事件永远等不到第一声 —— 于是一局
        All Perfect 打完，`status` 从头到尾都在报"音乐没在走"。那是把"没听到"当成了
        "没在走"。现在没观测到就是 None，`status` 照实说不知道。
        """

        self.on_result: Callable[[int], None] | None = None
        """一局结算时报一声（主机拿它做延迟自校准），由 :class:`Controller` 装上。"""

        self.perfect_deltas = []
        """本局每一条 Perfect 的早晚量（秒，正数 = 晚）。

        只收 Perfect：它们是"按时送到"的那一批，`Good` 里混着被蹭掉、卡顿补判这些异常事件，
        拿它们去校准会把噪声学进去（见 :meth:`latency_sample`）。
        """

        self.busy_until = 0.0
        """**我们**（或这条 frida 通道）正忙到什么时候 —— 这期间的 ping 超时不算"游戏冻住了"。

        真踩过：9MB 的谱面在通道上传输时，脚本那边在序列化、我们这边在收，ping 排在后面
        必然超时 —— 于是加载谱面时被判成"进程被系统冻结"，白停一次触控。
        这不是"猜测"，是我们**自己知道**正在干的事。
        """

        self.clock: touch.GameClock | None = None
        """`progress` 事件往里喂；由 :class:`Controller` 装上。"""

        self.last_chart: CapturedChart | None = None
        """最近一次 FromJson 抓到的谱面，供随后的 level-start 配对。"""

        self.level_label = "?"
        """当前这一局的"歌名 [难度]"，结算那行用它标头。"""

        self.judge_mismatches = 0
        """本局有多少条判决与音符表对不上账（见 :meth:`_check_judge`）。"""

        self.gate_open: int | None = None
        """还开着的闸门编号：Unity 主线程正卡在 ``recv("release")`` 上等我们放行。

        收工时它必须**先放行再断会话** —— 见模块开头「收工」。
        """

        self.pid: int | None = None
        """spawn 出来的 pid；attach 模式下是 None。"""

        self._session: frida.core.Session | None = None
        self._script: frida.core.Script | None = None
        self._detached: str | None = None
        """会话断掉的原因；None 表示还没断。"""
        self._ping: threading.Thread | None = None
        """正在等回答的那次探活。它没回来之前不再发第二次。"""

    def target_text(self) -> str:
        """日志里的目标写法：pid 就写 pid，名字就写名字。"""
        return (
            f"{PACKAGE} (pid={self.target})" if isinstance(self.target, int) else str(self.target)
        )

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        if frida.__version__ not in TESTED_FRIDA:
            log(
                f"[main] 警告：frida 客户端 {frida.__version__}，本机实测可用的是 {TESTED_FRIDA}。\n"
                f"       已知 17.19.0 在此设备上 attach 会报 "
                f"'agent connection closed unexpectedly'，详见 README 的环境要求。",
                file=sys.stderr,
            )

        if not self.agent_path.is_file():
            raise FileNotFoundError(f"找不到 agent：{self.agent_path}\n先构建：npm run build")

        assert self.device is not None, "没有设备就没法注入"

        if self.attach:
            self._session = self.device.attach(self.target)
            log(f"[main] 已附加到 {self.target_text()}")
        else:
            self.pid = self.device.spawn([PACKAGE])
            # 收工可能就落在这几百毫秒到几秒里（spawn 要等进程真起来）：那就不注入了
            if self._stopped_early():
                return
            self._session = self.device.attach(self.pid)
            log(f"[main] 已启动 {PACKAGE} (pid={self.pid})")

        if self._stopped_early():
            self._close_session()
            return

        # 进程没了要第一时间知道：frida 会主动报 detached，比探活快、也不花一次往返
        self._session.on("detached", self._on_detached)

        self._script = self._session.create_script(
            self.agent_path.read_text(encoding="utf-8"), name="auto_phigros"
        )
        self._script.on("message", self._on_message)
        self._script.load()

        if self.pid is not None:
            self.device.resume(self.pid)
            log("[main] 已恢复运行")

    def stop(self, timeout: float = TEARDOWN_TIMEOUT) -> None:
        """断开。**尽力而为**：进程冻住时 unload/detach 会挂住，超时就直接丢下它。

        丢下是安全的 —— 这条会话只属于这一个 ``Agent`` 对象，进程真的死了 frida 会自己
        收拾；而卡住控制台线程是不可接受的（``respawn`` 就在那条线程上）。
        """
        # 闸门还开着就先放它走：Unity 主线程正卡在 recv("release") 上，直接断会话的话
        # 那条 wait() 永远等不到消息 —— 游戏会连着主线程一起冻在那儿，人只能去杀进程。
        # 放行是"取消注入"的一部分，不是可选项。
        if self.gate_open is not None:
            log(f"[gate #{self.gate_open:04d}] 收工，先把这道闸门放行再断会话（游戏不该被我们冻住）")
            self.release(self.gate_open)
            self.gate_open = None

        # 先把状态标成"已断"，探活与播放器立刻就能据此停手，不必等 teardown 回来
        self._detached = self._detached or "已断开"
        script, session = self._script, self._session
        self._script, self._session = None, None

        def teardown() -> None:
            for closer in (
                lambda: script.unload() if script is not None else None,
                lambda: session.detach() if session is not None else None,
            ):
                try:
                    closer()
                except Exception:  # noqa: BLE001 - 进程可能已经没了
                    pass

        thread = threading.Thread(target=teardown, name="agent-teardown", daemon=True)
        thread.start()
        thread.join(timeout)
        if thread.is_alive():
            log(f"[main] 旧会话 {timeout:.0f}s 内没断干净（进程可能冻住了），丢下它继续")

    def _stopped_early(self) -> bool:
        """会话是不是已经在**起它的路上**被收掉了（``quit`` / Ctrl+C 落在这几秒里）。

        依据是 :meth:`stop` 打的那个"已断"标记：它先于一切落下。真的收早了就别再往下
        注入了 —— 会话刚建好就没人管了。
        """
        if self._detached is None:
            return False
        log("[main] 注入还没做完就收到了收工，不再往下注入了")
        return True

    def _close_session(self) -> None:
        """丢掉一个刚建出来、还没被谁管起来的会话。"""
        session, self._session = self._session, None
        if session is None:
            return
        try:
            session.detach()
        except Exception:  # noqa: BLE001 - 进程可能已经没了
            pass

    def release(self, seq: int) -> None:
        """放行第 seq 道闸门，游戏的下一帧从 ``SortForNoteWithFloorPosition`` 里继续。"""
        if self._script is None:
            return
        self._script.post({"type": "release", "payload": {"seq": seq}})

    # ------------------------------------------------------------ 探活

    def probe(self, timeout: float = PING_TIMEOUT) -> str:
        """探一下还活着吗：``"ok"`` / ``"hang"`` / ``"dead"``。

        * **dead** —— 会话已经断了（进程被杀 / 退出 / 我们主动断的）；
        * **hang** —— 连接还在，但 ``ping`` 在 timeout 内没回来：进程多半被 Android
          冻结（切后台缓存）了，或者脚本线程卡住了；
        * **ok** —— 真的答了一声 ``pong``。

        为什么非要有 ``hang`` 这一档：进程被杀 frida 会主动报 detached，**冻住**的进程
        不会 —— socket 好着、心跳也在，只是不再执行任何东西。不真发一条消息等回答，
        就分不出"还活着"和"冻着"。

        frida 的 RPC 没有超时参数，所以丢到一条一次性线程里 join。超时之后那条线程仍然
        堵在 frida 内部 —— 记着它，在它回来之前不再发第二次，否则每探一次就漏一条线程。
        """
        if self._session is None or self._detached is not None:
            return "dead"
        if self._ping is not None and self._ping.is_alive():
            return "hang"

        outcome: list[bool] = []

        def ask() -> None:
            try:
                assert self._script is not None
                self._script.exports_sync.ping()
                outcome.append(True)
            except BaseException as error:  # noqa: BLE001 - 探活失败就是死了，不值得区分
                outcome.append(False)
                self._detached = self._detached or f"ping 失败：{error}"

        self._ping = threading.Thread(target=ask, name="agent-ping", daemon=True)
        self._ping.start()
        self._ping.join(timeout)
        if self._ping.is_alive():
            return "hang"
        return "ok" if outcome and outcome[0] else "dead"

    def _on_detached(self, *args: Any) -> None:
        reason = args[0] if args else "未知"
        self._detached = str(reason)

    @property
    def detached(self) -> str | None:
        """会话断掉的原因；None 表示还连着。"""
        return self._detached

    # ------------------------------------------------------------ 消息分发

    def _on_message(self, message: dict[str, Any], _data: bytes | None) -> None:
        if message.get("type") == "error":
            log(f"[agent error] {message.get('description')}", file=sys.stderr)
            log(message.get("stack") or "", file=sys.stderr)
            return

        payload = message.get("payload")
        if not isinstance(payload, dict):
            return

        event = payload.get("event")
        if event == "chart":
            self._on_chart(payload)
        elif event == "chart-parsed":
            self._on_note_count(int(payload.get("notes") or 0))
        elif event == "level-context":
            context = payload.get("context") or {}
            # 接下来就是 FromJson 读整张谱面、把它整条 send 过来 —— 大消息在通道上走的时候，
            # 我们这边的 ping 必然排在它后面。这不是"游戏冻住了"，是我们自己在忙（见 busy_until）。
            self.busy_until = time.monotonic() + CHART_WAIT
            log(
                f"[level] {context.get('songsId')} / {context.get('songsName')} "
                f"[{context.get('songsLevel')}] -> {context.get('chartAddressableKey')}"
            )
        elif event == "chart-parsed":
            self.busy_until = 0.0
            self._on_note_count(int(payload.get("notes") or 0))
        elif event == "level-start":
            self._on_level_start(payload)
        elif event == "note-index":
            log(f"[notes] 音符表就绪：{int(payload.get('notes') or 0)} 个音符")
        elif event == "progress":
            # 播放状态跟时钟一起采样：它是"音乐在走吗"唯一的观测，读不到就是 None
            observed = payload.get("playing")
            if observed is not None:
                self.playing = bool(observed)
            if self.clock is not None:
                self.clock.feed(float(payload.get("time") or 0.0))
        elif event == "play-state":
            self._on_play_state(payload)
        elif event == "level-gone":
            self._on_level_gone(payload)
        elif event == "judge":
            self._on_judge(payload)
        elif event == "result":
            self._on_result(payload)
        elif event == "level-start-released":
            log(f"[gate #{int(payload.get('seq') or 0):04d}] 已放行，游戏继续")
        elif event == "hooked":
            log(
                f"[hooked] {payload.get('signature')} @ {payload.get('rva')} "
                f"(Unity {payload.get('unityVersion')})"
            )
        elif event == "ready":
            log(f"[ready] Unity {payload.get('unityVersion')}, pid={payload.get('pid')}")
            self.ready.set()
        elif event in ("warn", "fatal", "chart-error"):
            log(f"[{event}] {payload}", file=sys.stderr)

    def _on_chart(self, payload: dict[str, Any]) -> None:
        text = payload.get("json")
        if not isinstance(text, str):
            log(f"[chart #{payload.get('seq')}] agent 没回传正文，丢弃", file=sys.stderr)
            return

        context = payload.get("context") or {}
        ref = ChartRef(
            seq=int(payload.get("seq") or 0),
            context=context,
            digest=str(payload.get("hash") or "unknown"),
        )
        self.last_chart = CapturedChart(ref=ref, text=text, received_at=time.time())

        song = context.get("songsName") or context.get("songsId") or "?"
        log(
            f"[chart #{ref.seq:04d}] {song} [{context.get('songsLevel')}] "
            f"{len(text)} 字符 -> {context.get('chartAddressableKey')}"
        )

    def _on_note_count(self, notes: int) -> None:
        if self.last_chart is not None:
            self.last_chart.notes_reported = notes

    def _on_play_state(self, payload: dict[str, Any]) -> None:
        """游戏自己在说"音乐停了 / 走了"（``ProgressControl::Play(bool)``）。

        为什么值得单独挂一个 hook：主机原先靠"值多久没变"去判断暂停 —— 那是**推断**，而
        暂停与退出在样本上只差一次抖动那么宽。现在这是**观测**：游戏亲口说的。

        这里只翻译与转发，怎么处置由 :class:`~controller.Controller` 决定（它才知道
        播放器与时钟的状态）。
        """
        self.playing = bool(payload.get("playing"))
        moment = payload.get("time")
        if self.on_play_state is not None:
            self.on_play_state(
                self.playing, float(moment) if isinstance(moment, (int, float)) else None
            )

    def _on_level_gone(self, payload: dict[str, Any]) -> None:
        """这一局没了：``LevelControl::OnDestroy()``（退出到选歌 / 重开 / 结算清场）。

        这是"关卡跑掉了"唯一的**信号**（不是计时推断）。剩下的排期没有去处，所以回调
        那边会停触控、清时钟并明说一句。
        """
        self.playing = None
        """这一局没了：播放状态无从谈起，别再替游戏说。"""
        self.perfect_deltas = []
        moment = payload.get("time")
        if self.on_level_gone is not None:
            self.on_level_gone(float(moment) if isinstance(moment, (int, float)) else None)

    def _on_judge(self, payload: dict[str, Any]) -> None:
        """一个音符的判决。

        Miss / Good / Bad 一律打出来 —— 它们本来就是"值得看一眼"的东西，而且一局也就
        那么几条。Perfect 一局几百条，只在 ``verbose`` 打开时才打。

        真正有用的是**音符身份**：agent 拿 ``noteCode`` 从音符表里翻译出"第几条线的
        上面/下面第几个、谱面第几秒、横向偏移多少"，于是漏音不再需要对着录像找。

        无论打不打印都要过一次账（:meth:`_check_judge`）：Perfect 占了绝大多数，
        拿它来当"表抄对了没有"的样本最合适。
        """
        kind = str(payload.get("kind") or "?")
        if kind != "Perfect" or self.options.verbose:
            log(f"[judge] {kind:<7} {_judge_text(payload)}")
        delta = payload.get("delta")
        if kind == "Perfect" and isinstance(delta, (int, float)):
            # 攒着给延迟自校准用：Perfect 是"按时送到"的那一批（见 latency_sample）
            self.perfect_deltas.append(float(delta))
            # Hold 的收尾报的是**头判**的 Δ（`_judgeTime`），一条 hold 会进两次，
            # 但两次都是同一个数 —— 中位数不受重复计数影响，不必特殊处理
        self._check_judge(payload)

    def busy(self) -> str | None:
        """我们现在**自己**在忙什么 —— 忙的时候 ping 超时不算"游戏冻住了"。

        两种忙都是我们**确知**的，不是猜的：

        * **卡在闸门上**：闸门的实现体就停在那个 hook 里等我们放行，而 frida 的回调跑在
          Unity 主线程上 —— 这期间 ping 排在它后面本来就轮不到（规划慢的时候必然超时）；
        * **谱面在通道上传输**：9MB 的 JSON 一边序列化一边收，ping 同样排不上队。

        真踩过：加载谱面那两秒被判成"进程被系统冻结"，白停一次触控、还打了一行吓人的日志。
        """
        if self.gate_open is not None:
            return "正卡在闸门上给这一局规划"
        if time.monotonic() < self.busy_until:
            return "谱面正在通道上传输"
        return None

    def latency_sample(self) -> tuple[float, int] | None:
        """本局的延迟样本：**Perfect 的早晚量中位数**（秒，正数 = 我们发晚了）。

        为什么用中位数而不是平均：`Bad`/`Miss` 与"被蹭掉"的那些偶尔会带进来一个几十上百
        毫秒的离群值，平均值会被它拽着跑；中位数只看"大多数按时的那些落在哪儿"，
        这正是我们要校准的量。

        为什么只收 Perfect：`Good` 里混着"被蹭掉""卡顿补判"这类异常事件，它们不代表
        我们的送达时刻。条数太少（< 30）时不给结论 —— 那不是校准，那是猜。
        """
        if len(self.perfect_deltas) < MIN_LATENCY_SAMPLES:
            return None
        return statistics.median(self.perfect_deltas), len(self.perfect_deltas)

    def _check_judge(self, payload: dict[str, Any]) -> None:
        """拿游戏自己的算术核对音符表：``delta`` 必须等于 ``nowTime − realTime``。

        这三个数来自三处 —— ``delta`` 是游戏判决时算的早晚量、``time`` 是它当时的
        ``nowTime``、``note.time`` 是我们从音符表里抄来的 ``realTime``，它们之间有个恒等式。
        抄错了当场露馅：真出过一次，表建早了（``SetInformation`` 才算 ``realTime``，
        而我挂在了它前面的 ``SetCodeForNote``），整张表全是 0，于是一个 89.969 秒才判掉的
        音符被记成 "@ 0.000s"，只能靠人肉看出来。

        Miss 那条路径不带早晚量，就退一步查"判定时刻离音符时刻多远"：正常刚刚越过窗口，
        离谱的一定是表错了。

        **一条都不省略**：对不上账的判决**每一条**都当场打出来（原先"同类只报第一次"，
        结果是知道"有 8 条对不上"却不知道是哪 8 条、各自差多少 —— 那诊断不了任何东西）。
        行首带序号，末尾给本局累计条数。
        """
        note = payload.get("note")
        moment = payload.get("time")
        if not isinstance(note, dict) or not isinstance(moment, (int, float)):
            return
        real_time = note.get("time")
        if not isinstance(real_time, (int, float)):
            return

        delta = payload.get("delta")
        if isinstance(delta, (int, float)):
            # Hold 有**两个**判决点：头判从头量、收尾判从"按住结束 − 0.22s"量。
            # 两端都认，否则每条 hold 都会被冤枉（见 HOLD_SETTLE_LEAD）。
            references = [real_time]
            hold = note.get("hold")
            if isinstance(hold, (int, float)) and hold > 0:
                references.append(real_time + hold - HOLD_SETTLE_LEAD)
            if any(
                abs((moment - reference) - delta) <= JUDGE_ARITHMETIC_TOLERANCE
                for reference in references
            ):
                return
            detail = (
                f"nowTime−realTime = {moment - real_time:+.3f}s，"
                f"游戏说 {delta:+.3f}s，差 {(moment - real_time) - delta:+.3f}s"
            )
        else:
            gap = moment - real_time
            # Miss 也可能**早于** realTime 触发：hold 中途松手、或者音符还没到点就被
            # 判定逻辑结算掉（实测最多早 0.15s 上下），所以下界留 MISS_EARLY 的余量，
            # 而不是"早一点都不行"—— 那样这类正常的 Miss 全会被冤枉成"表抄错了"。
            if -MISS_EARLY <= gap <= MISS_GAP_LIMIT:
                return
            detail = f"判定时刻 {moment:.3f}s 离音符的 {real_time:.3f}s 差了 {gap:+.3f}s"

        self.judge_mismatches += 1
        log(
            f"[judge] 警告 #{self.judge_mismatches}：音符表与游戏对不上账 —— {detail}\n"
            f"            {_judge_text(payload)}",
            file=sys.stderr,
        )

    def _on_result(self, payload: dict[str, Any]) -> None:
        """一局的终局账目：分数、四个判定、最大连击。

        字段直接来自 `ScoreControl`，没经过任何换算；某一项读不到就是 `None`
        （字段名对不上时 frida 会静默返回 null，所以在日志里区分得出来）。
        """
        seq = int(payload.get("seq") or 0)

        def count(name: str) -> str:
            value = payload.get(name)
            return "?" if value is None else f"{int(value)}"

        def number(name: str, unit: str = "") -> str:
            value = payload.get(name)
            return "?" if value is None else f"{value:.2f}{unit}"

        verdict = "?"
        if payload.get("allPerfect") is True:
            verdict = "All Perfect"
        elif payload.get("fullCombo") is True:
            verdict = "Full Combo"
        elif payload.get("allPerfect") is False:
            verdict = "—"

        score = payload.get("score")
        score_text = "?" if score is None else f"{int(round(float(score)))}"
        log(
            f"[result #{seq:04d}] {self.level_label}  {score_text} 分"
            f"（{number('percent', '%')}）  最大连击 {count('maxCombo')}  {verdict}"
        )
        log(
            f"              Perfect {count('perfect')}  Good {count('good')}  "
            f"Bad {count('bad')}  Miss {count('miss')}"
            f"（早 {count('early')} / 晚 {count('late')}）"
        )
        if self.judge_mismatches:
            log(
                f"              警告：本局有 {self.judge_mismatches} 条判决与音符表对不上账"
                f"（判决日志里的音符身份不可信）",
                file=sys.stderr,
            )
        # 结算 = "这一局完整打完了" —— 主机拿这个时点做延迟自校准
        if self.on_result is not None:
            try:
                self.on_result(seq)
            except Exception as error:  # noqa: BLE001 - 校准失败不该带走结算
                log(f"[result] 结算后处理出错：{type(error).__name__}: {error}", file=sys.stderr)

    def _on_level_start(self, payload: dict[str, Any]) -> None:
        """游戏已经停在闸门上了：先办正事，再放行。"""
        mirror = payload.get("mirror")
        raw_offset = payload.get("offset")
        start = LevelStart(
            seq=int(payload.get("seq") or 0),
            chart_seq=int(payload.get("chartSeq") or 0),
            mirror=mirror if isinstance(mirror, bool) else None,
            offset=dict(raw_offset) if isinstance(raw_offset, dict) else {},
            chart=self.last_chart,
        )

        state = {True: "开", False: "关", None: "读不到"}[start.mirror]
        song = "?"
        level = "?"
        if start.chart is not None:
            context = start.chart.ref.context
            song = context.get("songsName") or context.get("songsId") or "?"
            level = context.get("songsLevel") or "?"
        self.level_label = f"{song} [{level}]"
        self.judge_mismatches = 0
        self.perfect_deltas = []
        self.playing = None
        """新的一局：上一局的播放状态对它没有意义，等这一次采样。"""
        self.gate_open = start.seq
        log(f"[gate #{start.seq:04d}] 谱面启动：{song}，镜像 {state}（游戏已停住）")
        log(f"            游戏延迟 {_offset_text(start.offset)}")

        try:
            if self.on_level_start is not None:
                self.on_level_start(start)
        except Exception as error:  # noqa: BLE001 - 放行优先于一切
            log(
                f"[gate #{start.seq:04d}] 处理出错：{type(error).__name__}: {error}",
                file=sys.stderr,
            )
        finally:
            # 先销号再放行：收工那条路上会照着 gate_open 补一次放行，销了号就不会重复
            self.gate_open = None
            self.release(start.seq)


def _offset_text(offset: dict[str, float | None]) -> str:
    """把延迟拆开写清楚：哪一部分来自哪里。对不上账时要看的就是它。

    四项都直接读，谁也不靠减出来：``total`` 是 `levelInformation.offset`（游戏真正用的那个），
    另外三项是 `GameInformation.mainOffset`（静态字段，设备音频缓冲推出的补偿）、
    `chart.offset`（**谱面文件自带**，不是玩家设置）、`gameInformation.offset`（玩家设置里的
    延迟校准）。顺带核对三项之和等不等于 ``total``。
    """
    if not offset:
        return "读不到"

    def show(value: float | None) -> str:
        return "?" if value is None else f"{value * 1000:+.0f}ms"

    total = offset.get("total")
    chart = offset.get("chart")
    user = offset.get("user")
    main = offset.get("main")
    text = (
        f"合计 {show(total)} = 设备音频补偿 {show(main)} + 谱面 {show(chart)} + 玩家设置 {show(user)}"
    )
    if None not in (total, chart, main, user):
        residual = total - (main + chart + user)
        if abs(residual) > 0.0005:
            text += f"  ← 对不上账！差 {residual * 1000:+.1f}ms"
    return text


def _fixed(value: float | None, spec: str, dash: str = "?") -> str:
    return dash if value is None else format(value, spec)


def _judge_text(payload: dict[str, Any]) -> str:
    """把一条判定消息写成人话。查不到音符时老实说查不到（附上 noteCode）。"""
    note = payload.get("note")
    if not isinstance(note, dict):
        return f"noteCode={payload.get('noteCode')}（音符表里没有它）"

    def number(key: str) -> float | None:
        value = note.get(key)
        return float(value) if isinstance(value, (int, float)) else None

    kind = NOTE_TYPE_NAMES.get(int(number("type") or 0), f"type{note.get('type')}")
    side = "上" if note.get("above") else "下"
    text = (
        f"{kind:<4} @{_fixed(number('time'), '8.3f')}s  "
        f"线 {int(number('line') or 0):<3} {side} 第 {int(number('index') or 0):<4} 个"
        f"  x={_fixed(number('x'), '+7.3f')}"
        f"  #{int(number('code') or 0)}"
    )

    delta = payload.get("delta")
    if isinstance(delta, (int, float)):
        text += f"  {'晚' if delta >= 0 else '早'} {abs(delta) * 1000:.0f}ms"
    else:
        # Miss 那条路径不带早晚量，游戏自己的时钟才是"什么时候判的"
        moment = payload.get("time")
        if isinstance(moment, (int, float)):
            text += f"  判定于 {moment:.3f}s"
    return text

