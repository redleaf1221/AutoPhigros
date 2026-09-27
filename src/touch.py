#!/usr/bin/env python3
"""触控模块：把规划结果按**游戏的时钟**发到设备上。

`main.py`（frida 主干）每次开谱调它一次；它自己也能单独跑：

    python src/touch.py plans/0002_..._conservative.psap
    python src/touch.py plans/x.psap --mirror --latency 0.02
    python src/touch.py plans/x.psap --backend recording     # 不连设备，只跑调度器

本模块只管三件事：**对表**（`Clock`）、**排事件**（`Player`）、**命令行**。
"事件怎么送出去"是 `backends/` 那包的事 —— 目前有 scrcpy（真发）与 recording（干跑）。

同步是怎么做到的
----------------
不自己数拍子，而是**跟着游戏的时钟走**。agent 每 100ms 把
`ProgressControl::Update` 里刚算出来的 `nowTime` 回传一次，本模块用这些样本估计
"游戏时间 → 主机单调时钟"的映射，然后按它排事件。于是游戏自己的东西全都自动算进去了：

* **延迟设置** —— `nowTime = audioTime − (mainOffset + chart.offset + 用户offset)`，
  游戏侧的 offset 已经在 `nowTime` 里了，跟着 `nowTime` 走就是跟着它走，
  **不需要也不能再加一次**（加了就是双份）；
* 加载、起播前那 3 秒、掉帧、暂停、恢复 —— 时钟停了事件就停，时钟走事件就走。

样本带的是"读到时的值"，而它到主机手上必然晚了一小段。设真实关系是
`host = game + τ`，样本满足 `h_i − v_i = τ + d_i`（`d_i ≥ 0` 是那一次的传输延迟），
所以对最近的样本取 `min(h_i − v_i)` 就是 `τ` 的一个**偏晚**的估计 ——
错也只错在"晚了一点"，不会早。窗口 2 秒，跟着设备与主机的晶振漂移慢慢挪。

剩下的固定偏差（主机→adb→设备→InputManager 这条注入链路的耗时）由 `--latency`
手工补：正数 = 提前发。这个数只能上设备调，默认 0。

镜像同理只在**执行时**临时改：`.psap` 存的是**规范解**（不镜像、不偏移），
游戏里开了镜像就 `PlanResult.mirrored()` 翻一下坐标再发，缓存不用重算。
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable, Iterable, Protocol, Sequence

import backends
from algorithms.utils import PlanResult, Touch, TouchEvent
from backends import Backend, catalog, create
from runtime.options import Options
from formats.storage import decode_plan

POLL = 0.002
"""快到点时重新对表的间隔（秒）。2ms 一小步，既跟得上映射的微调又不至于空转烧 CPU。"""

COARSE_NAP = 0.05
"""离得还早就先按这个粒度睡。

**为什么不用 `Event.wait(超时)` 来睡**：Windows 上带超时的锁等待只有系统时钟滴答
（15.6ms）的精度，实测能把事件拖晚 14ms；`time.sleep` 从 Python 3.11 起走的是高精度
可等待定时器，同样是睡 2ms 就真能 2ms 醒。代价是 stop 标志只能在每一小步之间看到 ——
最长 50ms 的延迟，收尾时无所谓。
"""

CLOCK_WINDOW = 2.0
"""估计时钟偏移时回看多久的样本。太短会被一次抖动带偏，太长跟不上晶振漂移。"""

CLOCK_STALL = 0.25
"""多久没有新样本就认为游戏时钟停住了（暂停 / 音乐还没起）。

采样间隔是 100ms，取 250ms = 两个半样本的余量。不能取太大：暂停一旦短于这个阈值就
认不出来，恢复之后会把事件**发早**（等于把暂停那段时间丢掉了）；正常播放时 `nowTime`
是音频时钟，每个样本都在变，所以 250ms 足够把"停住"和"在走"分开。
"""

CLOCK_RESUME_RATE = 0.4
"""兜底放行的判据：在 :data:`CLOCK_RESUME_WINDOW` 那么长的窗口里值走了 0.4 秒以上。

只用于"`Play(true)` 那个信号没送到"的兜底（用到会报警）。为什么看**速率**而不是"总共走了
多少"：暂停期间 `nowTime` 可能缓慢爬升（每样本几毫秒），攒一分钟也能攒出 1 秒 —— 按总量判
会在暂停中途误放行，把触控送进还开着暂停菜单的游戏（那可能替人按到按钮）。速率能把"爬升"
和"真的在跑"分开：真在跑时 1.5 秒里会走满 1.5 秒，远超 0.4。
"""

CLOCK_RESUME_WINDOW = 1.5
"""上面那个速率判据的窗口长度（秒）。"""

LEAD_IN = 3.0
"""单机跑时留的起跑线：`python src/touch.py` 之后你有 3 秒切回游戏窗口。"""

LATE_WARN = 0.02
"""迟到超过这么多秒就记一笔。

20ms 是"还判得成 Perfect"（窗口 80ms）与"值得追查"之间的一个折中；它是排查 late good
时唯一能对着游戏看的数字，所以要留名字、要报出来。
"""

LATE_SKIP = 0.15
"""已经迟到超过这么多秒的事件就**别发了**。

Good 的窗口是 180ms，越过 150ms 基本等于已经判没了；这时候再补发不但救不回来，
还会在游戏刚从卡顿里缓过来的时候再灌它一管子输入 —— 而输入管线被灌满正是卡顿的原因。
所以宁可丢掉，也不要"补发一堆积压"。丢了多少会如实报出来。
"""


class Clock(Protocol):
    """游戏时间（秒）→ 主机单调时钟（秒）。"""

    def feed(self, game_seconds: float, host_time: float | None = None) -> None:
        ...

    def host_for(self, game_seconds: float) -> float | None:
        """游戏时钟走到 `game_seconds` 时，主机的 monotonic 时刻是多少。

        返回 None 表示现在还算不出来（还没有样本，或者时钟停着且还没到那一点）。
        """
        ...

    def now(self) -> float | None:
        """游戏时钟现在读到多少。"""
        ...


class GameClock:
    """跟着 agent 回传的 `nowTime` 走的时钟。"""

    def __init__(
        self, *, window: float = CLOCK_WINDOW, stall: float = CLOCK_STALL, now: Callable[[], float] = time.monotonic
    ) -> None:
        self._window = window
        self._stall = stall
        self._now = now
        self._lock = threading.Lock()
        self._samples: deque[tuple[float, float]] = deque()
        self._value_host = 0.0
        """当前这个时间值是**从什么时候**开始没变过的 —— 用来认出"时钟停了多久"。"""

        self.reanchors = 0
        """重锚过几次。每次重锚都会让接下来一小段的排期整体挪一下。"""
        self.max_gap = 0.0
        """相邻两个样本之间最大的间隔。它是"采样断过"的直接证据。"""
        self._shift: tuple[float, float] | None = None
        """最近一次重锚 `(发生在哪个主机时刻, 估计值往后挪了多少秒)`；正数 = 会晚发。"""
        self._held = False
        """游戏说了"我停住了"（``ProgressControl::Play(false)``）—— 在此期间它的时间不是时间源。"""
        self._held_value: float | None = None
        """按住的那一刻游戏读到多少（暂停时显示这个值，比"没有样本"有信息量）。"""
        self.auto_released = False
        """没收到"恢复"信号、但时钟明显又在走了 —— 兜底放行过。要做成告警，不能悄悄发生。"""
        self._probe_at: float | None = None
        """兜底判据的窗口起点（主机时刻）；None = 还没开始看。"""
        self._probe_value = 0.0
        """兜底判据的窗口起点对应的游戏时间。"""

    def reset(self) -> None:
        """换一局：样本和统计都从头开始。

        统计也要清 —— 否则一局的账会把上一局的补齐（日志里"采样最大间隔 10300ms"
        其实是上一局换关时的空档，看着像是本局出了问题）。`_value_host` 一起清成
        "从没见过样本"，免得新一局拿着上一局的时间戳去判断"停住/还在"。按住的状态也清。
        """
        with self._lock:
            self._samples.clear()
            self.reanchors = 0
            self.max_gap = 0.0
            self._value_host = 0.0
            self._shift = None
            self._held = False
            self._held_value = None
            self._probe_at = None

    def hold(self) -> None:
        """游戏说"我停住了"（暂停）。

        为什么不能只靠"值多久没变"：那种判据在暂停**期间**就要开始猜了，而这里有一个
        明确的信号（``Play(false)``）。按住之后：

        * ``host_for`` 一律返回 None —— 暂停期间一个事件都不该发出去（游戏既不判定，
          我们的排期也没有意义；更要紧的是此时的手指可能正落在暂停菜单的按钮上）；
        * ``now()`` 返回按住那一刻的值，供人看"停在哪一秒"；
        * 期间来的样本**全部不采信** —— 它们要么是同一个数，要么（更坏）在缓慢爬升，
          而"缓慢爬升"正是让 `min(h−v)` 漂到暂停之后、恢复后所有事件都被判迟到的元凶。
        """
        with self._lock:
            if not self._held:
                self._held_value = self._samples[-1][1] if self._samples else None
            self._held = True
            self._probe_at = None  # 兜底判据从这一刻重新起算

    def release(self) -> None:
        """游戏说"我继续了"（恢复）：把手放开，并且**从零重新对表**。

        为什么要清样本而不是接着用：暂停期间的对齐已经不可信（见 :meth:`hold`），而
        "恢复后所有事件都迟到太多、播放像死了一样"就是这么来的。清掉之后，恢复后的
        头几个样本会把偏移重新算出来（≤100ms），代价是那一小段里可能有一两个事件偏晚。
        """
        with self._lock:
            self._held = False
            self._held_value = None
            self._probe_at = None
            self._samples.clear()
            self._value_host = 0.0

    @property
    def held(self) -> bool:
        return self._held

    def _looks_running(self, moment: float, game_seconds: float) -> bool:
        """按住期间的一眼：游戏是真的又在跑了，还是 `nowTime` 只是在缓慢爬升？

        真的在跑 → 兜底放行（返回 True，并把按住状态清掉、样本清掉重新对表），并由
        :meth:`take_auto_release` 让调用方报一声。判据是**速率**，见 :data:`CLOCK_RESUME_RATE`。
        """
        if self._probe_at is None:
            self._probe_at, self._probe_value = moment, game_seconds
            return False
        if moment - self._probe_at > CLOCK_RESUME_WINDOW:
            self._probe_at, self._probe_value = moment, game_seconds
            return False
        if game_seconds - self._probe_value < CLOCK_RESUME_RATE:
            return False
        self._held = False
        self._held_value = None
        self._probe_at = None
        self._samples.clear()
        self._value_host = 0.0
        self.auto_released = True
        return True

    def take_auto_release(self) -> bool:
        """取走"兜底放行过"这件事（读一次就清），供调用方报警。"""
        with self._lock:
            flag, self.auto_released = self.auto_released, False
            return flag

    def feed(self, game_seconds: float, host_time: float | None = None) -> None:
        moment = self._now() if host_time is None else host_time
        with self._lock:
            if self._held and not self._looks_running(moment, game_seconds):
                # 按住期间不采信样本：它们要么是同一个数，要么在缓慢爬升，两种都不能用来对表
                return
            if self._samples:
                host_prev, game_prev = self._samples[-1]
                self.max_gap = max(self.max_gap, moment - host_prev)

                if game_seconds < game_prev - 1e-3:
                    # 时钟倒着走了：重开了一局
                    self._reanchor(moment, game_seconds)
                elif game_seconds > game_prev + 1e-4:
                    # 值往前走了。要作废旧对齐，得先确认它**停过**：上一个值从
                    # `_value_host` 起一直没变，超过 stall 就算停过。
                    #
                    # 这一条能成立，靠的是"暂停时 hook 照常每 100ms 送一次，只是每次
                    # 都送同一个数"—— 也就是说暂停一定**看得见**，不需要再从"跨了一大段
                    # 时间却没怎么走"去推断。那条推断试过，是错的：传输打嗝之后补上来的
                    # 头一笔样本本来就带旧值（400ms 只走了 5ms），和暂停长得一模一样，
                    # 于是窗口被误清、`origin` 退化成那个样本自己的 h−v —— 它带多少延迟
                    # 我们就晚发多少，要等窗口重新填满才自愈。表现就是偶发的 late good。
                    if host_prev - self._value_host > self._stall:
                        self._reanchor(moment, game_seconds)
                    self._value_host = moment
            else:
                self._value_host = moment

            self._samples.append((moment, game_seconds))
            cutoff = moment - self._window
            while len(self._samples) > 1 and self._samples[0][0] < cutoff:
                self._samples.popleft()

    def host_for(self, game_seconds: float) -> float | None:
        with self._lock:
            if self._held or not self._samples:
                # 游戏说了它停着：这期间一个事件都不发（见 hold()）。
                return None
            moment = self._now()
            if self._frozen(moment):
                # 时钟停着（还没开音乐）：已经到点却来不及发的立刻补上，
                # 还没到点的一律等着 —— 这时候按"主机时钟"硬推会发早。
                return moment if game_seconds <= self._samples[-1][1] else None
            return _origin(self._samples) + game_seconds

    def now(self) -> float | None:
        with self._lock:
            if self._held:
                return self._held_value
            if not self._samples:
                return None
            moment = self._now()
            if self._frozen(moment):
                return self._samples[-1][1]
            return moment - _origin(self._samples)

    def take_shift(self) -> tuple[float, float] | None:
        """取走最近一次重锚（读一次就清），供调用方决定要不要报警。"""
        with self._lock:
            shift, self._shift = self._shift, None
            return shift

    def _reanchor(self, moment: float, game_seconds: float) -> None:
        if self._samples:
            # 新的 origin 会取"这一个样本"的 h−v，看看它比原来挪了多少
            self._shift = (moment, (moment - game_seconds) - _origin(self._samples))
            self.reanchors += 1
        self._samples.clear()
        self._value_host = moment

    def _frozen(self, moment: float) -> bool:
        """时钟停了吗：同一个值到 `stall` 秒以前就没再变过。

        注意判据是"**值**多久没变"，不是"多久没收到样本" —— 暂停时 hook 照常每 100ms
        送一次，只是每次都送同一个数。
        """
        return moment - self._value_host > self._stall


def _origin(samples: Iterable[tuple[float, float]]) -> float:
    """`min(主机时刻 − 游戏时间)`：真实偏移的偏晚估计（见模块开头）。"""
    return min(host - game for host, game in samples)


class LocalClock:
    """单机跑的时钟：不接游戏，从主机时钟自己走，先留一段起跑线。"""

    def __init__(self, *, lead_in: float = LEAD_IN, now: Callable[[], float] = time.monotonic) -> None:
        self._now = now
        self._origin = now() + lead_in

    def feed(self, game_seconds: float, host_time: float | None = None) -> None:
        """单机模式没有游戏时钟可跟，收下样本但不用。"""

    def host_for(self, game_seconds: float) -> float | None:
        return self._origin + game_seconds

    def now(self) -> float | None:
        return self._now() - self._origin


# ------------------------------------------------------------------ 播放


class Player:
    """按时钟把规划结果推出去。跑在自己的线程上，不占 frida 的消息线程。

    ``options`` 是**共享的**运行时设置（``options.py``），不是构造时抄一份的副本：
    延迟改了下一个事件就按新值发、注入开关改了立刻生效。抄一份的话就得在每个改设置
    的地方都记得同步正在跑的那个播放器 —— 迟早会漏。
    """

    def __init__(
        self,
        plan: PlanResult,
        backend: Backend,
        clock: Clock,
        *,
        mirror: bool = False,
        options: Options | None = None,
    ) -> None:
        # 镜像只在执行时改：.psap 里存的是规范解
        self.plan = plan.mirrored() if mirror else plan
        self.backend = backend
        self.clock = clock
        self.options = options if options is not None else Options()
        self.sent = 0
        self.late = 0.0
        """最晚发出去的那一次迟到了多少秒（含发送本身的耗时；负数=早发）。"""
        self.late_count = 0
        """迟到超过 :data:`LATE_WARN` 的帧数。"""
        self.worst: list[tuple[float, float]] = []
        """头几个迟到超标的帧 ``(谱面时刻, 迟到秒数)`` —— 直接对着游戏里的 late 看。"""
        self.max_send = 0.0
        """单次 `backend.send()` 最长花了多久。发送被阻塞的话它会露出来。"""
        self.skipped = 0
        """因为迟到太多而干脆没发的事件数。"""
        self.muted = 0
        """因为 :attr:`~options.Options.inject` 关着而没发出去的事件数。"""
        self.error: BaseException | None = None

        self._frames: list[tuple[float, tuple[TouchEvent, ...]]] = [
            (timestamp / 1000.0, events) for timestamp, events in self.plan.frames
        ]
        self.first_seconds = self._frames[0][0] if self._frames else 0.0
        """第一个事件在谱面第几秒 —— 等不到时钟的时候，报出它才知道在等什么。"""
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._down: dict[int, tuple[float, float]] = {}
        """**我们真的按下去、还没抬起来**的指针 → 最后一次发出去的位置。

        只记"发出去过的"（注入关着时不记）：抬起事件只能还给真的按下去过的那些指针，
        否则就是凭空给设备塞不存在的手指。
        """
        self.released = 0
        """收尾时补发了几个抬起事件（手指不该留在屏幕上）。"""
        self.repressed = 0
        """恢复时按回去了几个指针（暂停时被我们抬掉的）。"""
        self._lifted: dict[int, tuple[float, float]] = {}
        """暂停时被我们抬掉的那些指针 → 它们当时的位置（恢复时按回原位）。"""
        self._resume = False
        """游戏说恢复了、等播放器线程把抬掉的指针按回去。"""
        self._wake = threading.Event()
        """叫醒播放器线程：恢复要立刻按下去，收工要立刻停，都不能等下一次睡醒。"""
        self._index = 0
        """已经发到计划里的第几帧（恢复时要看一眼下一帧是不是本来就有 DOWN）。"""

    # -------------------------------------------------------- 生命周期

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("这个播放器已经跑过了")
        self._thread = threading.Thread(target=self._run, name="touch-player", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()  # 立刻打断睡眠，别让它带着一排期睡满 COARSE_NAP 才停

    def join(self, timeout: float | None = None) -> bool:
        self._done.wait(timeout)
        if self._thread is not None:
            self._thread.join(timeout)
        return self._done.is_set()

    def finished(self) -> bool:
        return self._done.is_set()

    def release_all(self) -> int:
        """把**我们按着的**手指全抬起来，返回补发了几个事件。

        什么时候要它：游戏暂停了、这一局不打了（结算/退出/重开）、收工了。手指是物理按在
        屏幕上的 —— 排期停了不等于手抬了，而一根留在屏幕上的手指会继续被游戏读成"在位"
        （暂停时更糟：暂停菜单是按**手指位置**做射线找按钮的，手指停在那儿可能替人把
        "重开"按了）。

        抬起来的**位置**会记在 :attr:`_lifted` 里，供 :meth:`resume` 按回原位。

        注入关着的时候：那些手指本来就没按下去（`_down` 是空的），所以一发都不发 ——
        用户的规矩是"收尾这一发要发"，但"发"的前提是"我们真的按过"。
        """
        with self._lock:
            pending = list(self._down.items())
            self._down.clear()
            self._lifted = dict(pending)
        if not pending:
            return 0
        events = tuple(
            TouchEvent(pointer, Touch.UP, x, y) for pointer, (x, y) in pending
        )
        try:
            self.backend.send(events)
        except BaseException as error:  # noqa: BLE001 - 收尾失败也要如实记下来
            self.error = self.error or error
            return 0
        with self._lock:
            self.released += len(events)
        return len(events)

    def resume(self) -> None:
        """游戏说"我继续了"：把暂停时抬掉的指针**按回原位**（在播放器线程里立刻发）。

        为什么按回**原位**，而不是把计划里紧接着那个 `MOVE` 改写成 `DOWN`：flick 判的就是
        "按下之后有没有那一下位移"。直接按在目标位置上，游戏看到的是一个不动的按下，
        `isNewFlick` 永远不会点亮 —— 那一下 flick 就废了（实测踩过：恢复之后有些 flick
        划不出来）。按回原位，计划里那一跳仍然是"从 A 到 B 的移动"。

        这件事必须**立刻**做（不能等下一个事件到点）：hold 的主体只有约 67ms 的缺席容忍
        （`_safeFrame = 2`），等一个 500ms 之后的事件再顺手按下去，hold 已经断了。所以这里
        只是设个旗子并把播放器叫醒（它每次循环都会看一眼），发送本身在播放器线程里做 ——
        后端（一条 scrcpy 连接）只能有一个线程写。
        """
        with self._lock:
            self._resume = True
        self._wake.set()

    # -------------------------------------------------------- 调度

    def _repress(self) -> int:
        """把暂停时抬掉的指针按回原位。返回补了几个按下（0 = 没什么要做的）。

        在播放器线程里做（后端只能一个线程写）。有一个例外不补：计划里紧接着的那一帧本来
        就有同一个指针的 `DOWN` —— 那就让计划自己按，同一个 pointer 连按两次是不合法的
        输入序列。
        """
        with self._lock:
            if not self._resume:
                return 0
            self._resume = False
            pending = dict(self._lifted)
            self._lifted.clear()
            if not pending:
                return 0
            upcoming = (
                {event.pointer for event in self._frames[self._index][1] if event.action is Touch.DOWN}
                if self._index < len(self._frames)
                else set()
            )
        events = tuple(
            TouchEvent(pointer, Touch.DOWN, x, y)
            for pointer, (x, y) in pending.items()
            if pointer not in upcoming
        )
        if not events:
            return 0
        if self.options.inject:
            self.backend.send(events)
            self._remember(events)
        else:
            self.muted += len(events)
        self.repressed += len(events)
        return len(events)

    def _run(self) -> None:
        try:
            index = 0
            while index < len(self._frames):
                self._index = index
                if self._stop.is_set():
                    return
                # 恢复要**立刻**把手按回去，不能等下一个事件到点（hold 只有约 67ms 容忍）
                self._repress()
                if self._stop.is_set():
                    return

                seconds, events = self._frames[index]
                host = self.clock.host_for(seconds)
                if host is None:
                    self._wake.wait(POLL)
                    self._wake.clear()
                    continue

                remaining = host - self.options.latency - time.monotonic()
                if remaining > 0:
                    nap = COARSE_NAP if remaining > COARSE_NAP else POLL
                    # 用 wait 而不是 sleep：恢复 / 收工要能立刻打断这一觉
                    self._wake.wait(min(remaining, nap))
                    self._wake.clear()
                    continue

                if -remaining > LATE_SKIP:
                    # 已经晚到救不回来了。补发只会把积压一次性灌进设备 —— 而输入管线
                    # 被灌爆正是当初卡顿的原因，等于火上浇油。丢掉，并且如实记账。
                    self.skipped += len(events)
                    index += 1
                    continue

                started = time.monotonic()
                if self.options.inject:
                    self.backend.send(events)
                    self._remember(events)
                else:
                    # 注入关着：照样按排期走到这里、照样记迟到，只是不碰设备。
                    # 于是"关掉注入"仍然是一次完整的调度演练，而不是另一种后端。
                    self.muted += len(events)
                cost = time.monotonic() - started
                self.max_send = max(self.max_send, cost)

                self.sent += len(events)
                # 迟到的量要算上发送本身的耗时：真正"发出去"是在 send 返回之后
                lateness = cost - remaining
                self.late = max(self.late, lateness)
                if lateness > LATE_WARN:
                    self.late_count += 1
                    if len(self.worst) < 5:
                        self.worst.append((seconds, lateness))
                index += 1
        except BaseException as error:  # noqa: BLE001 - 记下来交给主线程报
            self.error = error
        finally:
            # 无论怎么结束（打完了 / 被打断 / 出错了），**都不留手指在屏幕上**
            self.release_all()
            self._done.set()

    def _remember(self, events: Sequence[TouchEvent]) -> None:
        """记下"现在哪些手指是按着的"，收尾时要照着它抬。"""
        with self._lock:
            for event in events:
                if event.action is Touch.DOWN or event.action is Touch.MOVE:
                    self._down[event.pointer] = (event.x, event.y)
                elif event.action is Touch.UP or event.action is Touch.CANCEL:
                    self._down.pop(event.pointer, None)


# ------------------------------------------------------------------ 命令行


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="touch.py", description="auto_phigros 触控模块：把 .psap 发到设备上"
    )
    parser.add_argument("plan", type=Path, help="规划结果 .psap")
    parser.add_argument(
        "--backend",
        default=backends.DEFAULT_BACKEND,
        choices=[info.name for info in catalog()],
        help="用哪个后端（默认 %(default)s）",
    )
    parser.add_argument("--serial", default=None, help="设备序列号（连着多台时才要）")
    parser.add_argument(
        "--mirror", action="store_true", help="按谱面镜像水平翻转（游戏里开了镜像才加）"
    )
    parser.add_argument(
        "--latency", type=float, default=0.0, help="注入链路的手工补偿（秒），正数=提前发"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        plan = decode_plan(args.plan.read_bytes())
    except (OSError, ValueError) as error:
        print(f"[touch] 读不了 {args.plan}：{error}", file=sys.stderr)
        return 2

    try:
        backend: Backend = create(args.backend, serial=args.serial)
    except KeyError as error:  # --backend 有 choices 兜着，这里只是别让它炸成栈
        print(f"[touch] {error}", file=sys.stderr)
        return 2

    print(
        f"[touch] {args.plan.name}：{len(plan.frames)} 帧 / {plan.event_count} 个事件 / "
        f"{plan.duration_ms / 1000:.1f}s -> {args.backend}"
    )
    try:
        backend.open(plan.screen)
    except Exception as error:  # noqa: BLE001 - 后端起不来就报清楚
        print(f"[touch] 后端起不来：{error}", file=sys.stderr)
        return 1

    player = Player(
        plan, backend, LocalClock(), mirror=args.mirror, options=Options(latency=args.latency)
    )
    print(f"[touch] {LEAD_IN:.0f} 秒后开始{'（已镜像）' if args.mirror else ''}")
    try:
        player.start()
        player.join()
    except KeyboardInterrupt:
        player.stop()
    finally:
        backend.close()

    if player.error is not None:
        print(f"[touch] 发送出错：{player.error}", file=sys.stderr)
        return 1

    print(f"[touch] 发了 {player.sent} 个事件，最大迟到 {player.late * 1000:.1f}ms")
    if player.muted:
        print(f"[touch] 另有 {player.muted} 个事件因为注入关着没发")
    return 0


if __name__ == "__main__":
    sys.exit(main())
