#!/usr/bin/env python3
"""auto_phigros 主干：注入游戏 -> 开谱前闸住 -> 规划(带缓存) -> 放行 -> 跟着游戏时钟打。

frida 在这条链上是主干。规划（``planner.py``）与触控（``touch.py``）都是被它调用的
一块，各自都能单独跑。

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
全都自动对齐 —— 详见 ``touch.py`` 顶部。主机只多一个手工补偿：``--latency``。

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

包名、agent 路径、输出目录都是项目内固定常量，不做成参数。
"""

from __future__ import annotations

import argparse
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
from algorithms.chart import OFFICIAL_SCREEN
from algorithms.utils import PlanResult
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
    """frida 注入、消息接收与闸门放行。

    ``level-start`` 必须**在处理它的那条消息里**跑完再放行，所以这里不排队：agent
    把现场交给 :attr:`on_level_start`，回调抛异常也照样放行 —— 卡死游戏比规划失败
    严重得多。
    """

    def __init__(
        self,
        agent_path: Path,
        *,
        attach: bool = False,
        host: str | None = None,
        device_id: str | None = None,
    ) -> None:
        self.agent_path = agent_path
        self.attach = attach
        self.host = host
        self.device_id = device_id

        self.ready = threading.Event()
        self.on_level_start: Callable[[LevelStart], None] | None = None
        """放行之前要干的活，由 :func:`main` 装上。"""

        self.clock: touch.GameClock | None = None
        """`progress` 事件往里喂；由 :func:`main` 装上。"""

        self.last_chart: CapturedChart | None = None
        """最近一次 FromJson 抓到的谱面，供随后的 level-start 配对。"""

        self.level_label = "?"
        """当前这一局的"歌名 [难度]"，结算那行用它标头。"""

        self._session: frida.core.Session | None = None
        self._script: frida.core.Script | None = None
        self._pid: int | None = None

    # ------------------------------------------------------------ 生命周期

    def start(self) -> None:
        if frida.__version__ not in TESTED_FRIDA:
            print(
                f"[main] 警告：frida 客户端 {frida.__version__}，本机实测可用的是 {TESTED_FRIDA}。\n"
                f"       已知 17.19.0 在此设备上 attach 会报 "
                f"'agent connection closed unexpectedly'，详见 README 的环境要求。",
                file=sys.stderr,
            )

        if not self.agent_path.is_file():
            raise FileNotFoundError(f"找不到 agent：{self.agent_path}\n先构建：npm run build")

        if self.host:
            device = frida.get_device_manager().add_remote_device(self.host)
        elif self.device_id:
            device = frida.get_device(self.device_id)
        else:
            device = frida.get_usb_device(timeout=10)
        print(f"[main] 设备：{device.name} ({device.type})")

        if self.attach:
            self._session = device.attach(PACKAGE)
            print(f"[main] 已附加到 {PACKAGE}")
        else:
            self._pid = device.spawn([PACKAGE])
            self._session = device.attach(self._pid)
            print(f"[main] 已启动 {PACKAGE} (pid={self._pid})")

        self._script = self._session.create_script(
            self.agent_path.read_text(encoding="utf-8"), name="auto_phigros"
        )
        self._script.on("message", self._on_message)
        self._script.load()

        if self._pid is not None:
            device.resume(self._pid)
            print("[main] 已恢复运行")

    def wait_ready(self, timeout: float = READY_TIMEOUT) -> bool:
        """等 agent 报 ready，确认 hook 真的装上了。"""
        if self.ready.wait(timeout):
            return True
        print(f"[main] 警告：{timeout:.0f}s 内未收到 agent 的 ready 消息", file=sys.stderr)
        return False

    def stop(self) -> None:
        if self._script is not None:
            try:
                self._script.unload()
            except Exception:  # noqa: BLE001 - 进程可能已经没了
                pass
        if self._session is not None:
            try:
                self._session.detach()
            except Exception:  # noqa: BLE001
                pass

    def release(self, seq: int) -> None:
        """放行第 seq 道闸门，游戏的下一帧从 ``SortForNoteWithFloorPosition`` 里继续。"""
        if self._script is None:
            return
        self._script.post({"type": "release", "payload": {"seq": seq}})

    # ------------------------------------------------------------ 消息分发

    def _on_message(self, message: dict[str, Any], _data: bytes | None) -> None:
        if message.get("type") == "error":
            print(f"[agent error] {message.get('description')}", file=sys.stderr)
            print(message.get("stack") or "", file=sys.stderr)
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
            print(
                f"[level] {context.get('songsId')} / {context.get('songsName')} "
                f"[{context.get('songsLevel')}] -> {context.get('chartAddressableKey')}"
            )
        elif event == "level-start":
            self._on_level_start(payload)
        elif event == "progress":
            if self.clock is not None:
                self.clock.feed(float(payload.get("time") or 0.0))
        elif event == "result":
            self._on_result(payload)
        elif event == "level-start-released":
            print(f"[gate #{int(payload.get('seq') or 0):04d}] 已放行，游戏继续")
        elif event == "hooked":
            print(
                f"[hooked] {payload.get('signature')} @ {payload.get('rva')} "
                f"(Unity {payload.get('unityVersion')})"
            )
        elif event == "ready":
            print(f"[ready] Unity {payload.get('unityVersion')}, pid={payload.get('pid')}")
            self.ready.set()
        elif event in ("warn", "fatal", "chart-error"):
            print(f"[{event}] {payload}", file=sys.stderr)

    def _on_chart(self, payload: dict[str, Any]) -> None:
        text = payload.get("json")
        if not isinstance(text, str):
            print(f"[chart #{payload.get('seq')}] agent 没回传正文，丢弃", file=sys.stderr)
            return

        context = payload.get("context") or {}
        ref = ChartRef(
            seq=int(payload.get("seq") or 0),
            context=context,
            digest=str(payload.get("hash") or "unknown"),
        )
        self.last_chart = CapturedChart(ref=ref, text=text, received_at=time.time())

        song = context.get("songsName") or context.get("songsId") or "?"
        print(
            f"[chart #{ref.seq:04d}] {song} [{context.get('songsLevel')}] "
            f"{len(text)} 字符 -> {context.get('chartAddressableKey')}"
        )

    def _on_note_count(self, notes: int) -> None:
        if self.last_chart is not None:
            self.last_chart.notes_reported = notes

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
        print(
            f"[result #{seq:04d}] {self.level_label}  {score_text} 分"
            f"（{number('percent', '%')}）  最大连击 {count('maxCombo')}  {verdict}"
        )
        print(
            f"              Perfect {count('perfect')}  Good {count('good')}  "
            f"Bad {count('bad')}  Miss {count('miss')}"
            f"（早 {count('early')} / 晚 {count('late')}）"
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
        print(f"[gate #{start.seq:04d}] 谱面启动：{song}，镜像 {state}（游戏已停住）")
        print(f"            游戏延迟 {_offset_text(start.offset)}")

        try:
            if self.on_level_start is not None:
                self.on_level_start(start)
        except Exception as error:  # noqa: BLE001 - 放行优先于一切
            print(
                f"[gate #{start.seq:04d}] 处理出错：{type(error).__name__}: {error}",
                file=sys.stderr,
            )
        finally:
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


# --------------------------------------------------------------- 打歌现场


@dataclass(slots=True)
class Session:
    """这一把的全部家当：触控后端、游戏时钟、手工补偿，以及当前这一局的播放器。

    后端**整个会话只开一次**（推 server、起 JVM、连控制通道要一两秒），跨关复用；
    时钟每局重置；播放器每局换一个。
    """

    backend: backends.Backend | None
    clock: touch.GameClock
    latency: float
    player: touch.Player | None = None
    seq: int = 0

    def play(self, plan: PlanResult, *, mirror: bool, seq: int) -> None:
        self.stop()
        if self.backend is None:
            print(f"[touch #{seq:04d}] 触控后端没起来，这一局只采集不打", file=sys.stderr)
            return

        # 上一局的时钟样本对这一局没有意义（开播前 nowTime 是钉住的），清掉重新对表
        self.clock.reset()
        self.seq = seq
        self.player = touch.Player(
            plan, self.backend, self.clock, mirror=mirror, latency=self.latency
        )
        self.player.start()
        print(
            f"[touch #{seq:04d}] 已就绪：{plan.event_count} 个事件"
            f"{'，已按谱面镜像翻转' if mirror else ''}"
            f"{f'，手工补偿 {self.latency * 1000:+.0f}ms' if self.latency else ''}"
            "（等游戏时钟走到第一个音符）"
        )

    def poll(self) -> None:
        """主线程偶尔看一眼：时钟重锚了要立刻报，这一局打完了就把账报掉。"""
        shift = self.clock.take_shift()
        # 还没发出过任何事件时的重锚是**正常**的：那是音乐起播、时钟从"钉在 0"变成
        # "跟着音频走"，对表从头来过，重锚量等于起播前等了多久（一两秒）。这时候事件
        # 一个都还没发，报出来只会吓人。真正可疑的是**打到一半**估计值整体挪。
        if shift is not None and abs(shift[1]) > REANCHOR_WARN and self.player and self.player.sent:
            print(
                f"[touch #{self.seq:04d}] 时钟重锚：估计往后挪了 {shift[1] * 1000:+.0f}ms"
                f"（第 {self.clock.reanchors} 次）—— 接下来一两秒的排期会整体偏晚",
                file=sys.stderr,
            )

        if self.player is not None and self.player.finished():
            self._report()
            self.player = None

    def stop(self) -> None:
        if self.player is None:
            return
        self.player.stop()
        self.player.join(2.0)
        self._report()
        self.player = None

    def _report(self) -> None:
        assert self.player is not None
        player = self.player
        if player.error is not None:
            print(f"[touch #{self.seq:04d}] 发送出错：{player.error}", file=sys.stderr)

        line = (
            f"[touch #{self.seq:04d}] 打完：发了 {player.sent} 个事件，"
            f"最大迟到 {player.late * 1000:.1f}ms"
        )
        if player.skipped:
            line += f"，另有 {player.skipped} 个迟到太多没发"
        if player.late_count:
            worst = max(player.worst, key=lambda item: item[1]) if player.worst else None
            line += f"（{player.late_count} 帧超过 {touch.LATE_WARN * 1000:.0f}ms"
            if worst is not None:
                line += f"，最差在谱面 {worst[0]:.2f}s 迟到 {worst[1] * 1000:.0f}ms"
            line += "）"
        print(line)
        print(
            f"            单次发送最长 {player.max_send * 1000:.1f}ms；"
            f"时钟重锚 {self.clock.reanchors} 次，采样最大间隔 {self.clock.max_gap * 1000:.0f}ms"
        )


def handle_level_start(start: LevelStart, args: argparse.Namespace, session: Session) -> None:
    """处理一次开谱：规划（或读缓存）、按需落盘、把播放器架好。游戏停在闸门上等着。

    谱面只有一份 —— ``FromJson`` 抓到的原文，也就是策划写的那份；规划只对着它做一次，
    算出来的就是**规范解**（不镜像、不偏移）。镜像与延迟都是运行时的事，
    由播放器临时改（``touch.Player``），所以缓存对所有局面通用。
    """
    raw = start.chart
    if raw is None:
        print(
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
            planner=args.planner,
            ref=ref,
            cache=args.cache,
            directory=PLANS_DIR,
            progress=planner.TqdmProgress(),
        )
    except Exception as error:  # noqa: BLE001 - 规划失败也要把谱面留下来
        print(f"[plan #{seq:04d}] 规划失败：{type(error).__name__}: {error}", file=sys.stderr)

    if args.save_chart:
        path = save_chart(
            raw.text,
            ref,
            CHARTS_DIR,
            notes_in_json=result.stats.get("notes") if result else None,
            notes_reported=raw.notes_reported,
        )
        print(f"[chart #{seq:04d}] 已保存 {path.name}")

    if result is None:
        return

    cached = "（缓存）" if result.stats.get("cached") else ""
    print(f"[plan #{seq:04d}] {planner.summary(result)}{cached}")
    if raw.notes_reported is not None:
        inline = result.stats.get("notes")
        verdict = "一致" if inline == raw.notes_reported else "不一致！"
        print(f"           音符数核对：游戏 {raw.notes_reported}，JSON {inline} -> {verdict}")
    for warning in result.warnings:
        print(f"           ~ {warning}")

    if start.mirror is None:
        print(f"[plan #{seq:04d}] 警告：读不到谱面镜像开关，按不镜像处理", file=sys.stderr)
    session.play(result, mirror=bool(start.mirror), seq=seq)


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
        help="用哪个规划器（默认 %(default)s）",
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
        help="注入链路的手工补偿（秒），正数=提前发；跟着游戏时钟走剩下不用管（默认 0）",
    )
    return parser.parse_args()


def open_session(args: argparse.Namespace) -> Session:
    """把触控后端架起来。架不起来就降级成"只采集不打"，不影响谱面和缓存。"""
    clock = touch.GameClock()
    backend: backends.Backend | None = None
    try:
        backend = backends.create(args.backend, serial=args.device_id)
        backend.open(OFFICIAL_SCREEN)
        print(f"[main] 触控后端就绪（{args.backend}）")
    except Exception as error:  # noqa: BLE001 - 打不了歌也要能采谱面
        print(f"[main] 触控后端 {args.backend} 起不来，这一把只采集不打：{error}", file=sys.stderr)
        backend = None
    return Session(backend=backend, clock=clock, latency=args.latency)


def main() -> int:
    args = parse_args()
    agent = Agent(AGENT, attach=args.attach, host=args.host, device_id=args.device_id)
    try:
        agent.start()
    except FileNotFoundError as error:
        print(f"[main] {error}", file=sys.stderr)
        return 2
    except frida.TimedOutError:
        print("[main] 未发现 USB 设备。检查 adb devices / frida-server，或用 -H 指定地址", file=sys.stderr)
        return 2
    except frida.TransportError as error:
        print(f"[main] 与 frida-server 的通信中断：{error}", file=sys.stderr)
        if "agent connection closed" in str(error):
            print(
                f"[main] 这个报错几乎总是 frida 版本问题：frida-server 与客户端都换成 "
                f"{TESTED_FRIDA} 再试。\n"
                f"       判别方法：attach 一个无关进程（如 com.android.systemui）也失败，"
                f"就与游戏无关。",
                file=sys.stderr,
            )
        return 2

    print(f"[main] 规划器 {planner.describe(args.planner)}")
    agent.wait_ready()

    session = open_session(args)
    agent.clock = session.clock
    agent.on_level_start = lambda start: handle_level_start(start, args, session)
    print("[main] 就绪：每次开谱游戏都会停在闸门上，规划完自动放行，然后跟着游戏时钟打；Ctrl+C 结束")

    try:
        # 真正的活都在 frida 的消息线程和播放器线程上做（闸门等不起），
        # 主线程只负责偶尔看一眼有没有打完。用带超时的 sleep 而不是 Event().wait()：
        # 后者不响应 Ctrl+C。
        while True:
            time.sleep(0.2)
            session.poll()
    except KeyboardInterrupt:
        pass
    finally:
        session.stop()
        if session.backend is not None:
            session.backend.close()
        agent.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
