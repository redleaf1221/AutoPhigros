#!/usr/bin/env python3
"""触控模块：把规划结果按**游戏的时钟**发到设备上。

`main.py`（frida 主干）每次开谱调它一次；它自己也能单独跑：

    python src/touch.py plans/0002_..._conservative.npz
    python src/touch.py plans/x.npz --mirror --latency 0.02
    python src/touch.py plans/x.npz --backend recording     # 不连设备，只跑调度器

本模块只管三件事：**对表**（`Clock`）、**排事件**（`Player`）、**命令行**；怎么把事件送
出去是 `backends/` 那包的事（scrcpy 真发、recording 干跑）。

对表跟着 agent 每 100ms 回传的 `nowTime` 走（`audioTime − mainOffset − chart.offset −
玩家offset`），游戏侧的延迟已经在里面，不能再加一次。样本满足 `h − v = τ + d`（`d ≥ 0`
是那一次的传输延迟），所以对最近 2 秒的样本取 `min(h − v)` 就是 τ 的**偏晚**估计 ——
错也只错在晚一点，不会早。`.npz` 里存的是规范解（不镜像、不偏移），镜像与 `--latency`
都只在执行时加。
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
from formats.storage import NpzFormatError, load_plan

POLL = 0.002
"""快到点时重新对表的间隔（秒）：跟得上映射的微调，也不至于空转烧 CPU。"""

COARSE_NAP = 0.05
"""离得还早就先按这个粒度睡。用 ``time.sleep`` 而不用 ``Event.wait(超时)``：Windows 上后者
只有系统时钟滴答（15.6ms）的精度，能把事件拖晚十几毫秒。"""

CLOCK_WINDOW = 2.0
"""估计时钟偏移时回看多久的样本（秒）。太短会被一次抖动带偏，太长跟不上晶振漂移。"""

CLOCK_STALL = 0.25
"""多久没看到新的时间值就认为游戏时钟停住了（暂停 / 音乐还没起）：采样间隔 100ms 的两个半。"""

CLOCK_RESUME_RATE = 0.4
"""兜底放行的速率判据：:data:`CLOCK_RESUME_WINDOW` 那么长的窗口里值走了 0.4 秒以上。
只看速率不看总量（暂停期间 ``nowTime`` 会缓慢爬升），仅用于 ``Play(true)`` 没送到的兜底。"""

CLOCK_RESUME_WINDOW = 1.5
"""上面那个速率判据的窗口长度（秒）。"""


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
        """换一局：样本、统计、按住状态全部从头开始。

        统计也要清 —— 跨局留着的 ``max_gap`` 会把上一局换关时的空档算到本局头上。
        `_value_host` 清成"从没见过样本"，免得拿上一局的时间戳去判断"停住/还在"。
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
        """游戏说"我停住了"（暂停）：一个事件都不发，也不采信样本。

        ``host_for`` 一律返回 None（游戏既不判定，手指还可能正落在暂停菜单的按钮上），
        ``now()`` 返回按住那一刻的值；期间来的样本要么是同一个数、要么在缓慢爬升，
        两种都会把 `min(h−v)` 拖偏，所以一律不采信。
        """
        with self._lock:
            if not self._held:
                self._held_value = self._samples[-1][1] if self._samples else None
            self._held = True
            self._probe_at = None  # 兜底判据从这一刻重新起算

    def release(self) -> None:
        """游戏说"我继续了"（恢复）：放开手，并且从零重新对表。

        暂停期间的对齐已经不可信（见 :meth:`hold`）；清掉样本后，恢复后的头几个样本会把
        偏移重新算出来（≤100ms）。
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
        """按住期间的一眼：游戏是真在跑，还是 `nowTime` 只是在缓慢爬升？

        真在跑（速率过了 :data:`CLOCK_RESUME_RATE`）就兜底放行：清掉按住状态与样本、
        重新对表，并由 :meth:`take_auto_release` 让调用方报一声。
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
                    # 值往前走了。作废旧对齐前先确认它**停过**：上一个值从 `_value_host`
                    # 起一直没变、超过 stall（暂停时 hook 照常每 100ms 送同一个数，看得见）。
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
                # 时钟停着（还没开音乐）：到点的立刻补上，没到点的一律等着 —— 硬推会发早。
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
        """时钟停了吗：判据是**值**多久没变，不是多久没收到样本。

        暂停时 hook 照常每 100ms 送一次，只是每次都送同一个数。
        """
        return moment - self._value_host > self._stall


def _origin(samples: Iterable[tuple[float, float]]) -> float:
    """`min(主机时刻 − 游戏时间)`：真实偏移的偏晚估计（见模块开头）。"""
    return min(host - game for host, game in samples)


class LocalClock:
    """单机跑的时钟：不接游戏，从主机时钟自己走，先留一段起跑线。"""

    def __init__(self, *, lead_in: float, now: Callable[[], float] = time.monotonic) -> None:
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

    ``options`` 是**共享的**运行时设置，不是构造时抄一份的副本：延迟改了下一个事件就按
    新值发、注入开关改了立刻生效。
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
        # 镜像只在执行时改：规划结果里存的是规范解
        self.plan = plan.mirrored() if mirror else plan
        self.backend = backend
        self.clock = clock
        self.options = options if options is not None else Options()
        self.sent = 0
        self.late = 0.0
        """最晚发出去的那一次迟到了多少秒（含发送本身的耗时；负数=早发）。"""
        self.late_count = 0
        """迟到超过 ``options.late_warn`` 的帧数。"""
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

        只记"发出去过的"（注入关着时不记）：抬起只能还给真的按下去过的那些指针。
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
        self._wake.set()  # 立刻打断睡眠，别让它睡满 COARSE_NAP 才停

    def join(self, timeout: float | None = None) -> bool:
        self._done.wait(timeout)
        if self._thread is not None:
            self._thread.join(timeout)
        return self._done.is_set()

    def finished(self) -> bool:
        return self._done.is_set()

    def release_all(self) -> int:
        """把**我们按着的**手指全抬起来，返回补发了几个事件。

        暂停、结算、退出、收工都要它：排期停了不等于手抬了，留在屏幕上的手指会被游戏继续
        读成"在位"（暂停菜单是按手指位置做射线找按钮的）。抬起来的位置记进 :attr:`_lifted`
        供 :meth:`resume` 按回原位；注入关着时 `_down` 是空的，一发都不发。
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
        """游戏说"我继续了"：把暂停时抬掉的指针**按回原位**，在播放器线程里立刻发。

        按回原位，而不是把计划里紧接着那个 `MOVE` 改写成 `DOWN`：flick 判的就是"按下之后
        有没有那一下位移"，按在目标位置上不动，`isNewFlick` 永远不会点亮。只设旗子并叫醒
        播放器线程 —— hold 主体只有约 67ms 的容忍（`_safeFrame = 2`），而发送只能在播放器
        线程里做（后端只允许一个线程写）。
        """
        with self._lock:
            self._resume = True
        self._wake.set()

    # -------------------------------------------------------- 调度

    def _repress(self) -> int:
        """把暂停时抬掉的指针按回原位。返回补了几个按下（0 = 没什么要做的）。

        在播放器线程里做（后端只能一个线程写）。例外：计划里紧接着的那一帧本来就有同一个
        指针的 `DOWN` 就不补 —— 同一个 pointer 连按两次是不合法的输入序列。
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

                if -remaining > self.options.late_skip:
                    # 已经晚到救不回来了：补发会把积压一次性灌进设备，丢掉并记账。
                    self.skipped += len(events)
                    index += 1
                    continue

                started = time.monotonic()
                if self.options.inject:
                    self.backend.send(events)
                    self._remember(events)
                else:
                    # 注入关着：照样按排期走、照样记迟到，只是不碰设备（仍是一次调度演练）。
                    self.muted += len(events)
                cost = time.monotonic() - started
                self.max_send = max(self.max_send, cost)

                self.sent += len(events)
                # 迟到的量要算上发送本身的耗时：真正"发出去"是在 send 返回之后
                lateness = cost - remaining
                self.late = max(self.late, lateness)
                if lateness > self.options.late_warn:
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
        prog="touch.py", description="auto_phigros 触控模块：把规划结果发到设备上"
    )
    parser.add_argument("plan", type=Path, help="规划结果 .npz")
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
    parser.add_argument("--lead-in", type=float, default=3.0, help="几秒后开始发（默认 %(default)s）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        plan = load_plan(args.plan)
    except (OSError, NpzFormatError) as error:
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

    options = Options(latency=args.latency, lead_in=args.lead_in)
    player = Player(plan, backend, LocalClock(lead_in=options.lead_in), mirror=args.mirror, options=options)
    print(f"[touch] {options.lead_in:.0f} 秒后开始{'（已镜像）' if args.mirror else ''}")
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
