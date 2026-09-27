#!/usr/bin/env python3
"""运行时控制台：一边打歌一边改设置、看状态、把游戏重新拉起来。

在主干的**终端**上读一行执行一行，跑在自己的守护线程里。它不是另一个入口 ——
``touch.py`` / ``planner.py`` 那种"能单独跑"是给离线用的；这个控制台离了正在跑的
主机一点意义都没有，所以它只接受一个 :class:`~main.Controller` 干活。

命令表就在这个文件里，每条命令只干一件事，参数怎么解析也在命令自己身上
（``latency`` 认 ``0.02`` 也认 ``20ms`` 和 ``+5ms``）。

回显这件事比看着要紧
--------------------
三条规矩，都是为了"输入之后一定有反应"：

1. **主干与 agent 的输出全都走 :func:`log`** —— 它是进程里唯一的写者，一整行不会被别的
   线程插花；
2. **提示符被输出顶掉就补回来** —— agent 每 100ms 就可能在打印，而 ``auto> `` 一旦写在
   屏幕上就留在那儿了，直接 ``print`` 会接在它后面，看起来就是"提示符没了"。所以打印前
   先用 ``\\r`` + 空格把那行擦掉，打印完再画一遍；
3. **每条命令都要说话，包括"看不懂"和"没这个命令"** —— 控制台是运行时唯一的面板，
   它沉默的时候人分不清是"命令没生效"还是"程序卡住了"。

stdin 不是终端（管道 / 重定向）时**照样读**：读到文件尾就说明白然后退出。曾经是反过来
的 —— 不是终端就一声不吭把自己关掉，于是敲什么都没反应、连提示符都没有。
"""

from __future__ import annotations

import sys
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from algorithms import catalog
from output import log, set_prompt_hooks

if TYPE_CHECKING:
    from main import Controller

PROMPT = "auto> "


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    usage: str
    help: str
    run: Callable[["Controller", list[str]], None]


def _planner(controller: Controller, argv: list[str]) -> None:
    """看或换规划器。"""
    if not argv:
        log("可用规划器：")
        for info in catalog():
            mark = "→" if info.name == controller.options.planner else " "
            log(f"  {mark} {info.name:<14} {info.summary}")
        return
    name = argv[0]
    if name not in {info.name for info in catalog()}:
        log(f"没有这个规划器：{name}（打一个不带参数的 planner 看清单）")
        return
    controller.options.planner = name
    log(f"规划器 -> {name}（下一关生效）")


def _latency(controller: Controller, argv: list[str]) -> None:
    """看或改注入补偿。支持 ``0.02`` / ``20ms`` / ``+5ms``（增减）。"""
    options = controller.options
    if not argv:
        log(f"手工补偿 {options.latency * 1000:+.0f}ms（正数=提前发）")
        return

    text = argv[0].strip().lower()
    relative = text.startswith(("+", "-"))
    try:
        if text.endswith("ms"):
            value = float(text[:-2]) / 1000.0
        else:
            value = float(text)
    except ValueError:
        log(f"看不懂这个数：{argv[0]}（要 0.02 / 20ms / +5ms 这样的）")
        return

    options.latency = options.latency + value if relative else value
    log(f"手工补偿 -> {options.latency * 1000:+.0f}ms（下一个事件起生效）")


def _inject(controller: Controller, argv: list[str]) -> None:
    """开关"是否真的把触控发给设备"。"""
    value = _switch(argv)
    if value is None:
        log(f"触控注入现在是 {'开' if controller.options.inject else '关'}；用法：inject on|off")
        return
    controller.options.inject = value
    if value:
        log("触控注入 -> 开")
    else:
        log("触控注入 -> 关（照常排期与计时，只是不碰设备）")


def _verbose(controller: Controller, argv: list[str]) -> None:
    """开关"是否把每一个 Perfect 也打出来"。"""
    value = _switch(argv)
    if value is None:
        log(f"判定流水现在是 {'开' if controller.options.verbose else '关'}；用法：verbose on|off")
        return
    controller.options.verbose = value
    log(f"判定流水（含 Perfect）-> {'开' if value else '关'}")


def _status(controller: Controller, argv: list[str]) -> None:
    lines = controller.status_lines()
    for line in lines:
        log(line)
    if not lines:
        # 空白回显是最难查的一种"没反应"：宁可报"这本身就不对"，也不要什么都不说
        log("[状态] 一个字都没拿到 —— 这本身就不对，检查 Controller.status_lines")


def _respawn(controller: Controller, argv: list[str]) -> None:
    log("重新启动游戏并注入……（要几秒）")
    controller.restart(spawn=True)


def _reattach(controller: Controller, argv: list[str]) -> None:
    log("正在附加到已经在跑的游戏……")
    controller.restart(spawn=False)


def _quit(controller: Controller, argv: list[str]) -> None:
    """收工。拆除是**当场**做的（可能要好几百毫秒到几秒），不是给主干留个标志就走。

    和 Ctrl+C 落到同一件事上（``Controller.stop``）：请求主干停手、unload 脚本、detach
    会话、停播放器、关后端。之所以不等主干"发现"标志位 —— 它可能正卡在启动阶段的阻塞调用
    里，那时候屏幕上看不出任何变化，游戏上却还挂着我们的 hook。
    """
    log("收工：断开注入、停掉触控")
    controller.stop()


COMMANDS: tuple[Command, ...] = (
    Command("status", "status", "现在什么情况：agent、后端、时钟、这一局", _status),
    Command("planner", "planner [名字]", "看可用规划器 / 换一个（下一关生效）", _planner),
    Command("latency", "latency [值]", "看 / 改注入补偿：0.02、20ms、+5ms（立即生效）", _latency),
    Command("inject", "inject on|off", "是否真的把触控发给设备", _inject),
    Command("verbose", "verbose on|off", "是否把每一个 Perfect 也打出来", _verbose),
    Command("respawn", "respawn", "重新启动游戏并注入（游戏被关掉后用）", _respawn),
    Command("reattach", "reattach", "注入到已经在跑的游戏", _reattach),
    Command("help", "help", "列出这些命令", lambda controller, argv: _help()),
    Command("quit", "quit", "收工：断开注入并退出（等同 Ctrl+C）", _quit),
)

_ALIASES = {"?": "help", "exit": "quit"}


def _help() -> None:
    log("命令：")
    for command in COMMANDS:
        log(f"  {command.usage:<22} {command.help}")


def _switch(argv: list[str]) -> bool | None:
    """把 ``on`` / ``off`` 解析成布尔；看不懂就 None（调用方负责抱怨）。"""
    if not argv:
        return None
    return {"on": True, "off": False, "1": True, "0": False}.get(argv[0].strip().lower())


class Console:
    """读一行、执行一行。

    ``Controller.stop()`` 之后循环就结束了 —— ``quit`` 是当场把拆除做掉的，不必等下一次
    回车，也不必等主干"发现"标志位。
    """

    def __init__(self, controller: Controller, *, stdin=None) -> None:
        self.controller = controller
        self._stdin = stdin if stdin is not None else sys.stdin
        self._interactive = bool(getattr(self._stdin, "isatty", bool)())
        """是不是终端。只影响"用什么方式回显"，不影响读不读 —— 见模块开头。"""
        self._waiting = False
        """提示符已经画在屏幕上、正在等一行输入。别的线程要打印时据此把它擦掉重画。"""
        self._commands = {command.name: command for command in COMMANDS}
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        set_prompt_hooks(self._clear_prompt, self._draw_prompt)
        log(
            "[main] 控制台就绪："
            + ("stdin 是终端" if self._interactive else "stdin 不是终端（管道/重定向，读到 EOF 就退出）")
        )
        _help()
        self._thread = threading.Thread(target=self._loop, name="console", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------ 提示符

    def _clear_prompt(self) -> None:
        """把 ``auto> `` 那一行擦掉（只在终端上、而且提示符真的画着的时候）。"""
        if self._interactive and self._waiting:
            sys.stdout.write("\r" + " " * len(PROMPT) + "\r")
            sys.stdout.flush()

    def _draw_prompt(self) -> None:
        if self._interactive and self._waiting:
            sys.stdout.write(PROMPT)
            sys.stdout.flush()

    # ------------------------------------------------------------ 循环

    def _loop(self) -> None:
        try:
            while not self.controller.stopping:
                self._waiting = True
                self._draw_prompt()
                # 回车之后光标已经在新的一行（终端回显了那一行并换了行），所以这里不必再动
                # 屏幕 —— 要擦的是"别的线程把输出接在提示符后面"，那件事由 log() 管。
                line = self._stdin.readline()
                self._waiting = False

                if line == "":
                    log("[main] 输入结束（EOF），控制台退出；主干继续跑，要改设置只能重开一个终端")
                    return
                if not self._interactive:
                    # 管道/重定向：没有终端回显，就把命令自己打出来，
                    # 日志里才看得出"哪一条命令对应哪一行输出"
                    log(f"{PROMPT}{line.rstrip()}")
                self.execute(line)
        finally:
            # 线程一走就把提示符那两个钩子摘掉：登记与"控制台还在跑"必须是同一件事，
            # 否则收工之后别人打印还会去擦、去画一个早就没人等的提示符。
            self._waiting = False
            set_prompt_hooks()

    def execute(self, line: str) -> None:
        """执行一行命令。空行、``#`` 开头的注释行都直接跳过。"""
        words = line.split("#", 1)[0].split()
        if not words:
            return

        name = _ALIASES.get(words[0].lower(), words[0].lower())
        command = self._commands.get(name)
        if command is None:
            log(f"没有这个命令：{words[0]}（help 看清单）")
            return
        try:
            command.run(self.controller, words[1:])
        except Exception as error:  # noqa: BLE001 - 一条命令炸了不该把控制台带走
            log(f"{name} 出错：{type(error).__name__}: {error}")
