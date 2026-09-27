#!/usr/bin/env python3
"""auto_phigros 主干：注入游戏 -> 开谱前闸住 -> 规划(带缓存) -> 放行 -> 跟着游戏时钟打。

frida 在这条链上是主干。规划（``planner.py``）与触控（``touch.py``）都是被它调用的
一块，各自都能单独跑；运行时可调的设置收在 ``options.py``，在终端上改设置的是
``console.py``。

闸门
----
agent 把 hook 架在 ``LevelControl::SortForNoteWithFloorPosition``：谱面已经解析完、
**谱面镜像已经应用完**、整局参数已经填好，但判定线一根都还没生成的那个瞬间。hook
一进去就把 Unity 主线程按住 —— 渲染停帧、音乐不响、音符不落。等主机把谱面收下、
规划（或从缓存里读）好、该存的都存了，再 ``script.post({"type": "release"})`` 放行。

所以 **规划一定发生在开谱之前**：算出来的规划结果必然是给这一局的，不存在"前几小节
已经过去了规划还没算完"。代价是主机卡多久游戏就冻多久（命中缓存时几乎为零），
期间 Android 理论上可能弹 ANR；主机侧一律 try/finally 保证放行。

同步
----
放行之后触控模块就跟着**游戏的时钟**（``ProgressControl.nowTime``）走：agent 每
100ms 回传一次，主机据此排事件。游戏侧的延迟设置、加载、起播前那三秒、掉帧、暂停恢复
全都自动对齐 —— 详见 ``touch.py`` 顶部。主机只多一个手工补偿：``options.latency``。

活着还是死了
------------
agent 每半秒被真的问一声（``ping``）：进程被杀 frida 会主动报 detached，但被 Android
冻结的进程不会 —— 连接好着、却什么都不执行，只有发一条消息等一个回答才分得出来。
一旦认定死了就把正在跑的播放器停掉（剩下的排期一次性灌进一个已经死掉或者冻住的游戏，
只会更糟），并且**不会自作主张重连**：游戏关掉了就打 ``respawn``，还开着就打
``reattach``。重连是人的决定，不是程序的猜测。

收工
----
控制台的 ``quit`` 与 Ctrl+C 走的是**同一条路**（``Controller.stop``）：请求主干停手、当场
unload 脚本并 detach 会话、停播放器、关触控后端。之所以不能"只置一个标志、等主干自己发现"：
主干可能正卡在启动阶段那些阻塞调用里（frida 找设备自带 10 秒超时、等 agent 握手最长
``READY_TIMEOUT``、推 scrcpy 要一两秒），那时候置标志等于什么都不做 —— 屏幕上还是那个
``auto> ``，游戏上却已经挂着我们的 hook。Ctrl+C 看起来干脆，只是因为它能打断阻塞调用。

两条路唯一不同的地方也出在这里：Ctrl+C 是信号，**再按一下就落在拆除中途**，把 unload /
detach 打断（那就等于没取消注入，还甩一份堆栈出来），所以拆除期间先把它屏蔽掉。

拆除只做一次，谁先到谁做，后到的那个等它做完 —— 主干必须等到：拆除的最后一步只是起了一条
线程去 detach（进程冻住时它会挂住，所以不能同步等死），主干要是扭头就把进程结束了，
设备上就留下一个还挂着 hook 的游戏。另外，**闸门还开着就先放行再断会话**：Unity 主线程正卡在
``recv("release")`` 上，直接断会话的话那条 ``wait()`` 永远等不到消息，游戏会冻在那儿。

收工**只撤我们自己的东西**：脚本、会话、播放器、触控后端。游戏本身一动不动 —— 即使它是我们
spawn 出来、还没放行的那个，放不放它跑也不是收工该管的事。

判定流水
--------
agent 在 ``ScoreControl::Perfect/Good/Bad/Miss`` 上都挂了号，每次判决都把"哪个音符、
判成什么、早/晚多少"发回来，音符身份由 ``LevelControl::SetCodeForNote`` 那一刻建好的
音符表翻译。于是漏音不再靠猜：日志里直接写着是第几条线的哪个音符、在谱面第几秒。
默认只打 Miss / Good / Bad（Perfect 一局几百条，用 ``verbose on`` 打开）。

用法（conda 环境 auto_phigros）：

    python main.py                              # 启动游戏并注入（默认）
    python main.py --attach                     # 注入到已在运行的游戏
    python main.py --planner radical            # 换规划器
    python main.py --backend recording          # 换触控后端：不碰设备，只跑一遍调度器
    python main.py --save-chart                 # 顺便把谱面原文存下来
    python main.py --no-cache                   # 不吃也不写规划缓存
    python main.py --latency 0.02               # 手工补偿注入链路（正数=提前发）
    python main.py -H 192.168.1.10:27042        # 走远程 frida-server
    python main.py -D <device-id>               # 指定设备

跑起来之后终端就是控制台，``help`` 看命令（``planner`` / ``latency`` / ``inject`` /
``verbose`` / ``status`` / ``respawn`` / ``reattach`` / ``quit``）。

包名、agent 路径、输出目录都是项目内固定常量，不做成参数。
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import frida

import backends
import planner
import touch
from algorithms import DEFAULT_PLANNER, catalog
from algorithms.chart import OFFICIAL_SCREEN, NoteType
from algorithms.utils import PlanResult
from console import Console
from output import log
from options import Options
from storage import ChartRef, save_chart

ROOT = Path(__file__).resolve().parent
AGENT = ROOT / "target" / "_.js"
CHARTS_DIR = ROOT / "charts"
PLANS_DIR = ROOT / "plans"
PACKAGE = "com.PigeonGames.Phigros"

TESTED_FRIDA = ["17.10.1", "17.17.0"]
"""实测可用的 frida 版本。

17.19.0 在 Android 15 + KernelSU 的设备上 attach 任何进程都会抛
``TransportError: agent connection closed unexpectedly``（spawn 本身正常，
挂在注入环节，与 Phigros 无关 —— attach systemui 也一样失败）。17.10.1 正常。
"""

READY_TIMEOUT = 30.0

REANCHOR_WARN = 0.02
"""时钟重锚挪动超过这么多秒就当场报警。

重锚会让接下来一小段的事件整体偏晚，而"最大迟到"那个指标量的是"相对我自己的排期"，
排期本身错位它照样报 0 —— 所以必须单独盯这一个量。
"""

PROBE_INTERVAL = 0.5
"""隔多久问 agent 一声"还活着吗"（秒）。"""

PING_TIMEOUT = 2.0
"""一声问候等多久算没回应（秒）。比采样间隔（100ms）宽出一个量级，不会误判。"""

TEARDOWN_TIMEOUT = 3.0
"""断开旧会话最多等多久（秒）。进程被冻住时 unload/detach 会挂住，超了就直接丢下它。"""

SHUTDOWN_WAIT = 6.0
"""``shutdown()`` 里"等已经开始了的那次拆除做完"最多等多久（秒）。

比 ``TEARDOWN_TIMEOUT`` 宽：拆除里 unload/detach 自己最多等 3 秒，还要算上停播放器
（最多 2 秒）与关后端。真超了也确实该丢下它走人 —— 收工不能变成另一种卡住。
"""

NOTE_TYPE_NAMES = {int(kind): kind.name.title() for kind in NoteType}
"""``ChartNote.type`` 的数字 -> 名字。与谱面 JSON 的 ``type`` 同源（``algorithms.chart``）。"""

JUDGE_ARITHMETIC_TOLERANCE = 0.05
"""``delta`` 与 ``nowTime − realTime`` 允许差多少秒。

两个数其实是同一帧里算出来的同一个量（游戏算它自己的早晚量，我们读它当时的 ``nowTime``），
正常应当一模一样；0.05 只是给"我晚一步才读到"留的余量。
"""

MISS_GAP_LIMIT = 3.0
"""Miss 不带早晚量，只能靠"判定时刻离音符时刻多远"来怀疑表抄错了。

游戏的 Miss 阈值是 0.1~0.18 秒，正常就是刚越过窗口就判掉。但**卡顿之后会补判一批**：
一帧停了 1 秒，扫描游标就往前挪 1 秒，中间的音符全是"晚了很多"的合法 Miss。
所以这里只能粗着来 —— 留 3 秒，够容下卡顿补判，又比真出过的那次（差 89.9 秒）小两个数量级。
真正严的判据在另一条路径上：命中的那些判决带 `delta`，可以拿恒等式逐条对。
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
    ) -> None:
        """``device`` / ``options`` 可以不给 —— 只有在"只喂消息、不真的连设备"的场合
        （``selftest.py`` 的闸门与结算自检）才这么用；真跑的时候 :meth:`start` 一定要有设备。
        """
        self.agent_path = agent_path
        self.device = device
        self.options = options if options is not None else Options()
        self.attach = attach

        self.ready = threading.Event()
        self.on_level_start: Callable[[LevelStart], None] | None = None
        """放行之前要干的活，由 :class:`Controller` 装上。"""

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
            self._session = self.device.attach(PACKAGE)
            log(f"[main] 已附加到 {PACKAGE}")
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
            log(
                f"[level] {context.get('songsId')} / {context.get('songsName')} "
                f"[{context.get('songsLevel')}] -> {context.get('chartAddressableKey')}"
            )
        elif event == "level-start":
            self._on_level_start(payload)
        elif event == "note-index":
            log(f"[notes] 音符表就绪：{int(payload.get('notes') or 0)} 个音符")
        elif event == "progress":
            if self.clock is not None:
                self.clock.feed(float(payload.get("time") or 0.0))
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
        self._check_judge(payload)

    def _check_judge(self, payload: dict[str, Any]) -> None:
        """拿游戏自己的算术核对音符表：``delta`` 必须等于 ``nowTime − realTime``。

        这三个数来自三处 —— ``delta`` 是游戏判决时算的早晚量、``time`` 是它当时的
        ``nowTime``、``note.time`` 是我们从音符表里抄来的 ``realTime``，它们之间有个恒等式。
        抄错了当场露馅：真出过一次，表建早了（``SetInformation`` 才算 ``realTime``，
        而我挂在了它前面的 ``SetCodeForNote``），整张表全是 0，于是一个 89.969 秒才判掉的
        音符被记成 "@ 0.000s"，只能靠人肉看出来。

        Miss 那条路径不带早晚量，就退一步查"判定时刻离音符时刻多远"：正常刚刚越过窗口，
        离谱的一定是表错了。同类问题一局只报第一次，不刷屏；条数在结算时汇总。
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
            if -JUDGE_ARITHMETIC_TOLERANCE <= gap <= MISS_GAP_LIMIT:
                return
            detail = f"判定时刻 {moment:.3f}s 离音符的 {real_time:.3f}s 差了 {gap:+.3f}s"

        self.judge_mismatches += 1
        if self.judge_mismatches == 1:
            log(
                f"[judge] 警告：音符表与游戏对不上账 —— {detail}。"
                f"（同类问题本局只报这一次，结算时给总数）",
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


# --------------------------------------------------------------- 打歌现场


class Controller:
    """这一把的全部家当：设置、设备、触控后端、游戏时钟、agent、当前播放器。

    它也是 ``console.py`` 唯一的依赖 —— 控制台只认 :attr:`options`、
    :meth:`status_lines`、:meth:`restart`、:meth:`stop` 与 :attr:`stopping`。
    """

    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.options = Options(planner=args.planner, latency=args.latency)
        self.clock = touch.GameClock()

        self.backend: backends.Backend | None = None
        """触控后端**整个会话只开一次**（推 server、起 JVM、连控制通道要一两秒），
        跨关复用；``respawn`` 也不用重开 —— 它挂在设备上，不挂在游戏进程上。"""

        self.agent: Agent | None = None
        self.player: touch.Player | None = None
        self.player_seq = 0
        self.agent_state = "未启动"
        """最近一次探活的结果：``未启动`` / ``ok`` / ``hang`` / ``dead``。"""

        self._stopping = threading.Event()
        self._teardown_lock = threading.Lock()
        self._teardown_started = False
        self._teardown_done = threading.Event()
        self._device: frida.core.Device | None = None
        self._watchdog: threading.Thread | None = None
        self._player_lock = threading.Lock()
        """护着 :attr:`player` 的交接：三个线程（主线程 / 探活 / 开谱）都会碰它。"""

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

    def open(self) -> bool:
        """找设备、注入。失败就报清楚并且返回 False（这些是本会话的前提，退不的）。"""
        try:
            self._device = self._find_device()
        except frida.TimedOutError:
            log("[main] 未发现 USB 设备。检查 adb devices / frida-server，或用 -H 指定地址", file=sys.stderr)
            return False
        except frida.InvalidArgumentError as error:
            log(
                f"[main] 找不到这个设备：{error}（-D 要填 frida-ls-devices 里的那个 id；"
                f"USB 下就是 adb 的序列号）",
                file=sys.stderr,
            )
            return False
        except frida.TransportError as error:
            log(f"[main] 与 frida-server 的通信中断：{error}", file=sys.stderr)
            return False
        log(f"[main] 设备：{self._device.name} ({self._device.type})")

        if self.stopping:
            # 找设备这一步（frida 自己的 10 秒超时）打断不了，但"还没注入"是可以不做的
            log("[main] 刚找到设备就收到收工，注入就不做了")
            return False

        try:
            self.start_agent(attach=self.args.attach)
        except FileNotFoundError as error:
            log(f"[main] {error}", file=sys.stderr)
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
            return False
        return True

    def _find_device(self) -> frida.core.Device:
        if self.args.host:
            return frida.get_device_manager().add_remote_device(self.args.host)
        if self.args.device_id:
            return frida.get_device(self.args.device_id)
        return frida.get_usb_device(timeout=10)

    def start_agent(self, *, attach: bool) -> None:
        """（重新）注入一次。设备已经找好了，这里只管会话。"""
        assert self._device is not None
        agent = Agent(AGENT, self._device, self.options, attach=attach)
        self.agent = agent
        agent.start()
        self._await_ready(agent)
        agent.clock = self.clock
        agent.on_level_start = self.handle_level_start
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

    def restart(self, *, spawn: bool) -> None:
        """把 agent 换一个新的：``spawn=True`` 重新启动游戏，否则附加到正在跑的那个。

        旧会话先丢掉、**播放器先停掉**：手上的排期是给上一个进程的，往新进程里灌没有
        任何意义。触控后端不重开（它挂在设备上），游戏时钟清空重对（新的一局从零开始）。
        """
        if self._device is None:
            log("[main] 还没找到设备，重连无从谈起", file=sys.stderr)
            return
        if self.stopping:
            log("[main] 已经收工了，不再重连（要接着打就重新起一个 main.py）", file=sys.stderr)
            return

        self.stop_player()
        old, self.agent = self.agent, None
        self.agent_state = "已断开"
        if old is not None:
            old.stop()
        self.clock.reset()

        try:
            self.start_agent(attach=not spawn)
        except Exception as error:  # noqa: BLE001 - 重连失败不该把主机带走
            log(f"[main] 重连失败：{type(error).__name__}: {error}", file=sys.stderr)
            self.agent_state = "未启动"
            # 半途而废的注入要收干净：会话可能已经建起来了，而这条线路上没人会再来收它
            # （主干还在跑，不会走收工那一步）
            self._stop_agent()
            return
        log(f"[main] 重连完成 —— {self.options.summary()}")
        log("[main] 自己点到那首歌，开谱时自动接管")

    def open_backend(self) -> None:
        """把触控后端架起来。架不起来就降级成"只采集不打"，不影响谱面和缓存。"""
        name = self.args.backend
        try:
            backend = backends.create(name, serial=self.args.device_id)
            backend.open(OFFICIAL_SCREEN)
            log(f"[main] 触控后端就绪（{name}）")
        except Exception as error:  # noqa: BLE001 - 打不了歌也要能采谱面
            log(f"[main] 触控后端 {name} 起不来，这一把只采集不打：{error}", file=sys.stderr)
            backend = None
        self.backend = backend

    # ------------------------------------------------------------ 探活

    def watch(self) -> None:
        """起一条守护线程，每 ``PROBE_INTERVAL`` 秒问 agent 一声。"""
        self._watchdog = threading.Thread(target=self._watch, name="agent-watchdog", daemon=True)
        self._watchdog.start()

    def _watch(self) -> None:
        while not self._stopping.wait(PROBE_INTERVAL):
            agent = self.agent
            if agent is None:
                continue
            state = agent.probe(PING_TIMEOUT)
            if state == self.agent_state:
                continue
            self.agent_state = state
            if state == "ok":
                continue
            if state == "hang":
                log(
                    f"[main] agent 超过 {PING_TIMEOUT:.0f}s 没应答 —— 进程多半被系统冻结了"
                    f"（切后台/息屏）。触控已停。",
                    file=sys.stderr,
                )
            else:
                log(
                    f"[main] agent 没了：{agent.detached or '原因不明'}。触控已停；"
                    f"游戏关掉了就 respawn，还开着就 reattach。",
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

    def poll(self) -> None:
        """主线程偶尔看一眼：时钟重锚了要立刻报，这一局打完了就把账报掉。"""
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
                cache=self.args.cache,
                directory=PLANS_DIR,
                progress=planner.TqdmProgress(),
            )
        except Exception as error:  # noqa: BLE001 - 规划失败也要把谱面留下来
            log(f"[plan #{seq:04d}] 规划失败：{type(error).__name__}: {error}", file=sys.stderr)

        if self.args.save_chart:
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
            backend_text = f"触控后端 {self.args.backend} 已就绪"

        now = self.clock.now()
        clock_text = (
            "游戏时钟还没对上表"
            if now is None
            else (
                f"游戏时钟 {now:.2f}s（重锚 {self.clock.reanchors} 次，"
                f"采样最大间隔 {self.clock.max_gap * 1000:.0f}ms）"
            )
        )

        lines = [
            f"[状态] {self.options.summary()}",
            f"       {agent_text}；{backend_text}",
            f"       {clock_text}",
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
        return lines


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="auto_phigros：开谱前闸住游戏，规划并跟着游戏时钟打")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--spawn", action="store_true", help="启动游戏并注入（默认）")
    mode.add_argument("--attach", action="store_true", help="注入到已在运行的游戏")
    parser.add_argument("-H", "--host", default=None, help="远程 frida-server，如 192.168.1.10:27042")
    parser.add_argument(
        "-D",
        "--device-id",
        default=None,
        help="设备 id / adb 序列号（frida-ls-devices 可见；USB 设备两者是同一个字符串）",
    )
    parser.add_argument(
        "--planner",
        default=DEFAULT_PLANNER,
        choices=[info.name for info in catalog()],
        help="用哪个规划器（默认 %(default)s；跑起来之后可以在控制台里改）",
    )
    parser.add_argument(
        "--backend",
        default=backends.DEFAULT_BACKEND,
        choices=[info.name for info in backends.catalog()],
        help="用哪个触控后端（默认 %(default)s；recording = 不碰设备，只跑一遍调度器）",
    )
    parser.add_argument(
        "--save-chart",
        action="store_true",
        help=f"把谱面 JSON 存到 {CHARTS_DIR.name}/",
    )
    parser.add_argument(
        "--cache",
        action=argparse.BooleanOptionalAction,
        default=True,
        help=f"把 {PLANS_DIR.name}/ 里的规划结果当缓存用：命中就不重算（默认开；--no-cache 表示既不吃也不写）",
    )
    parser.add_argument(
        "--latency",
        type=float,
        default=0.0,
        help="注入链路的手工补偿（秒），正数=提前发；跟着游戏时钟走剩下不用管（默认 0，控制台可改）",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    controller = Controller(args)
    try:
        # 面板**第一个**支起来：找设备、注入、等 agent 握手、推 scrcpy 要好几秒
        # （等 ready 最长 READY_TIMEOUT），之前那段时间里敲什么都没人听。控制台只依赖
        # controller 的 options / status_lines / restart / stop，这几样在 open() 之前就是好的。
        Console(controller).start()

        if not controller.open():
            # 收工在启动阶段就到了（quit / Ctrl+C）不算失败 —— 那是人让它停的
            return 0 if controller.stopping else 2

        if controller.stopping:
            # 已经注入了，但后端与探活还没起：这几秒的活也省掉，收工要立刻见效
            log("[main] 还没开打就收到了收工，后端与探活都不起了")
            return 0

        log(f"[main] {controller.options.summary()}")
        log(f"[main] 规划器 {planner.describe(controller.options.planner)}")
        controller.open_backend()
        controller.watch()
        log("[main] 就绪：每次开谱游戏都会停在闸门上，规划完自动放行，然后跟着游戏时钟打")

        # 真正的活都在 frida 的消息线程、播放器线程与控制台线程上做（闸门等不起），
        # 主线程只负责偶尔看一眼有没有打完。用带超时的 sleep 而不是 Event().wait()：
        # 后者不响应 Ctrl+C。
        while not controller.stopping:
            time.sleep(0.2)
            controller.poll()
    except KeyboardInterrupt:
        # Ctrl+C 不另走一条拆除的路：它和控制台的 quit 都落到底下这一句上
        pass
    finally:
        controller.shutdown()

    return 0


if __name__ == "__main__":
    sys.exit(main())
