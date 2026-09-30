#!/usr/bin/env python3
"""运行时控制台 —— **它就是主干**。

``main.py`` 起完后台件就把主线程交给这里：读一行、执行一行，直到 ``quit`` / ``exit`` /
Ctrl+C。所以 Ctrl+C 天然落在读输入那条线程上，与 ``quit`` 走同一条收工路。

* **空读不等于退出**：终端上读到空行多半是一次被中止的读，接着读就是；只有真正到了输入
  末尾（管道 EOF）才停靠 —— 待机、不再读，但不退出，因为退出意味着你没有面板可用了；
* **每条命令都要说话**，包括"看不懂"和"没这个命令"：控制台沉默的时候，人分不清是命令
  没生效还是程序卡住了；
* **会记住的命令改完当场落盘**进 ``config.json``（planner / option / latency / backend /
  cache / save-chart / log / device / host）。
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

import backends
from algorithms import catalog, parameters, parse_option, validate
from .config import log_file_path
from .output import log, log_path, log_to_file, set_prompt_hooks, stop_file_log

if TYPE_CHECKING:
    from .controller import Controller

PROMPT = "auto> "

PARK_POLL = 0.2
"""停靠（输入到头了）之后隔多久看一眼是不是该收工了（秒）。"""

EMPTY_READ_LIMIT = 3
"""终端上连续读到几次空就认为真的到头了（防止一次中止的读被当成 EOF、或者反过来空转）。"""


@dataclass(frozen=True, slots=True)
class Command:
    name: str
    usage: str
    help: str
    run: Callable[["Controller", list[str]], None]


# --------------------------------------------------------------- 设备与注入


def _devices(controller: Controller, argv: list[str]) -> None:
    """列出 frida 现在能看到的设备。"""
    controller.print_devices()


def _device(controller: Controller, argv: list[str]) -> None:
    """看 / 换选中的设备。"""
    if not argv:
        current = controller.config.device
        if current is None:
            log("还没选设备。打 devices 看列表，再 device <id> 选一台")
        else:
            log(f"当前设备 {current}（换一台：device <id>）")
        return
    controller.select_device(argv[0])


def _spawn(controller: Controller, argv: list[str]) -> None:
    """在选中的设备上启动游戏并注入。"""
    log("启动游戏并注入……（要几秒）")
    controller.launch(spawn=True)


def _attach(controller: Controller, argv: list[str]) -> None:
    """附加到已经在跑的游戏；可给 pid（名字对不上时用它）。"""
    pid = None
    if argv:
        try:
            pid = int(argv[0])
        except ValueError:
            log(f"pid 得是数字，给的是 {argv[0]!r}（不留参数就按包名找）")
            return
    log("附加到已经在跑的游戏……")
    controller.launch(spawn=False, pid=pid)


# --------------------------------------------------------------- 设置


def _planner(controller: Controller, argv: list[str]) -> None:
    """看或换规划器（下一关生效）。"""
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
    controller.persist()
    log(f"规划器 -> {name}（下一关生效）")


def _option(controller: Controller, argv: list[str]) -> None:
    """看 / 改当前规划器的参数。改了写进 ``config.json``，那份缓存随之失效。"""
    name = controller.options.planner
    stored = controller.config.planner_options.get(name, {})
    if not argv:
        log(f"{name} 的参数（= 号后面是当前值）：")
        for item in parameters(name):
            current = stored.get(item.name, item.default)
            mark = " *" if item.name in stored else ""
            log(f"  {item.name:<22} = {current!r:<8}{mark} {item.help}")
        log(f"{len(stored)} 个改过（*）；改：option <名字> <值>，还原：option <名字> reset")
        return
    if len(argv) < 2:
        log("用法：option <名字> <值>（或 option <名字> reset）；不带参数看清单")
        return
    key, value = argv[0], argv[1]
    defaults: dict[str, Any] = {item.name: item.default for item in parameters(name)}
    if key not in defaults:
        log(f"{name} 没有 {key} 这个参数（打一个不带参数的 option 看清单）")
        return
    if value.strip().lower() == "reset":
        stored.pop(key, None)
        if not stored:
            controller.config.planner_options.pop(name, None)
        controller.persist()
        log(f"{name}.{key} -> {defaults[key]!r}")
        return
    overrides = controller.config.planner_options.setdefault(name, {})
    had, previous = key in overrides, overrides.get(key)
    try:
        overrides[key] = parse_option(value)
        validate(name, overrides)  # 借规划器自己的参数表校验：名字、类型都在这里挡下来
    except ValueError as error:
        if had:
            overrides[key] = previous
        else:
            overrides.pop(key, None)
        log(f"改不了：{error}")
        return
    controller.persist()
    log(f"{name}.{key} -> {overrides[key]!r}（下一关生效）")


def _latency(controller: Controller, argv: list[str]) -> None:
    """看或改注入补偿。支持 ``0.02`` / ``20ms`` / ``+5ms``（增减）/ ``auto on|off``。

    手动给数就把自校准关掉：人一旦自己定了这个值，说明他知道得比统计多。
    """
    options = controller.options
    auto = "开" if controller.config.auto_latency else "关"
    if not argv:
        log(f"手工补偿 {options.latency * 1000:+.0f}ms（正数=提前发）；延迟自校准 {auto}")
        log("用法：latency 0.02 / latency 20ms / latency +5ms / latency auto on|off")
        return

    if argv[0].strip().lower() == "auto":
        value = _switch(argv[1:])
        if value is None:
            log(f"延迟自校准现在是 {auto}；用法：latency auto on|off")
            return
        controller.config.auto_latency = value
        controller.persist()
        log(f"延迟自校准 -> {'开' if value else '关'}")
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
    was_auto = controller.config.auto_latency
    controller.config.auto_latency = False
    controller.persist()
    log(
        f"手工补偿 -> {options.latency * 1000:+.0f}ms（下一个事件起生效）"
        + ("；自校准关掉了" if was_auto else "")
    )


def _backend(controller: Controller, argv: list[str]) -> None:
    """看或换触控后端；换了当场重开。"""
    if not argv:
        log("可用触控后端：")
        for info in backends.catalog():
            mark = "→" if info.name == controller.config.backend else " "
            log(f"  {mark} {info.name:<14} {info.summary}")
        return
    name = argv[0]
    if name not in {info.name for info in backends.catalog()}:
        log(f"没有这个后端：{name}（打一个不带参数的 backend 看清单）")
        return
    controller.config.backend = name
    controller.persist()
    if controller.agent is None:
        log(f"触控后端 -> {name}（下次注入时起）")
        return
    controller.open_backend(name)
    log(f"触控后端 -> {name}（已重开）")


def _cache(controller: Controller, argv: list[str]) -> None:
    """开关"把 plans/ 里的规划结果当缓存用"。"""
    value = _switch(argv)
    if value is None:
        log(f"缓存现在是 {'开' if controller.config.cache else '关'}；用法：cache on|off")
        return
    controller.config.cache = value
    controller.persist()
    log(f"缓存 -> {'开' if value else '关'}")



def _save_chart(controller: Controller, argv: list[str]) -> None:
    """开关"顺便把谱面原文存到 charts/"。"""
    value = _switch(argv)
    if value is None:
        log(f"存谱面现在是 {'开' if controller.config.save_chart else '关'}；用法：save-chart on|off")
        return
    controller.config.save_chart = value
    controller.persist()
    log(f"存谱面 -> {'开' if value else '关'}")


def _log(controller: Controller, argv: list[str]) -> None:
    """开关"把输出抄一份到 logs/"（开发时最有用的那个开关）。"""
    value = _switch(argv)
    if value is None:
        path = log_path()
        log(f"日志现在是 {'开：' + str(path) if path else '关'}；用法：log on|off")
        return
    controller.config.log = value
    controller.persist()
    if not value:
        # 先说再关：让日志文件的最后一行是"日志到此为止"
        log("日志 -> 关（屏幕上照旧）")
        stop_file_log()
        return
    path = log_file_path()
    if log_to_file(path):
        log(f"日志 -> 开：{path}")
    else:
        log(f"日志建不起来（{path}），仍只写屏幕", file=sys.stderr)


def _host(controller: Controller, argv: list[str]) -> None:
    """加 / 看远程 frida-server（地址形如 192.168.1.10:27042）。"""
    if not argv:
        hosts = controller.config.hosts
        log(f"记住的远程 server：{'、'.join(hosts) if hosts else '（没有）'}")
        log("加一个：host add 192.168.1.10:27042")
        return
    if argv[0] != "add" or len(argv) < 2:
        log("用法：host add 192.168.1.10:27042（不带参数看已保存的）")
        return
    controller.add_host(argv[1])


def _inject(controller: Controller, argv: list[str]) -> None:
    """开关"是否真的把触控发给设备"（只在这次会话里有效）。"""
    value = _switch(argv)
    if value is None:
        log(f"触控注入现在是 {'开' if controller.options.inject else '关'}；用法：inject on|off")
        return
    controller.options.inject = value
    log(f"触控注入 -> {'开' if value else '关（照常排期与计时，不碰设备）'}")


def _gate(controller: Controller, argv: list[str]) -> None:
    """开关"开谱时把游戏拦住等我们规划完"（只在这次会话里有效，立即生效）。"""
    value = _switch(argv)
    if value is None:
        log(f"闸门现在是 {'开' if controller.options.gate else '关'}；用法：gate on|off")
        return
    controller.set_gate(value)
    log(
        f"闸门 -> {'开（开谱时拦住游戏，等我们规划完）' if value else '关（不拦，游戏一秒都不停）'}"
    )


def _verbose(controller: Controller, argv: list[str]) -> None:
    """开关"是否把每一个 Perfect 也打出来"（只在这次会话里有效）。"""
    value = _switch(argv)
    if value is None:
        log(f"判定流水现在是 {'开' if controller.options.verbose else '关'}；用法：verbose on|off")
        return
    controller.options.verbose = value
    log(f"判定流水（含 Perfect）-> {'开' if value else '关'}")


def _detach(controller: Controller, argv: list[str]) -> None:
    """断开注入：设备与触控后端留着，之后还能 attach / spawn 接回来。"""
    controller.detach()


def _status(controller: Controller, argv: list[str]) -> None:
    lines = controller.status_lines()
    for line in lines:
        log(line)
    if not lines:
        log("[状态] 一个字都没拿到 —— 检查 Controller.status_lines")


def _quit(controller: Controller, argv: list[str]) -> None:
    """收工。拆除是当场做的（可能要好几百毫秒到几秒），和 Ctrl+C 落到同一件事上。"""
    log("收工：断开注入、停掉触控")
    controller.stop()


COMMANDS: tuple[Command, ...] = (
    Command("devices", "devices", "列出设备（USB、本机、远程 server）", _devices),
    Command("device", "device <id>", "选中一台设备", _device),
    Command("spawn", "spawn", "在选中的设备上启动游戏并注入", _spawn),
    Command("attach", "attach [pid]", "附加到已经在跑的游戏（pid 可选）", _attach),
    Command("detach", "detach", "断开注入（设备与后端留着，可再 attach）", _detach),
    Command("status", "status", "现在什么情况：设备、agent、后端、时钟、这一局", _status),
    Command("planner", "planner [名字]", "看可用规划器 / 换一个（下一关生效）", _planner),
    Command("option", "option [名字 值]", "看 / 改当前规划器的参数（下一关生效）", _option),
    Command("latency", "latency [值]", "看 / 改注入补偿：0.02、20ms、+5ms", _latency),
    Command("backend", "backend [名字]", "看 / 换触控后端（换了当场重开）", _backend),
    Command("cache", "cache on|off", "把 plans/ 里的规划当缓存用", _cache),
    Command("save-chart", "save-chart on|off", "顺便把谱面原文存到 charts/", _save_chart),
    Command("log", "log on|off", "把输出抄一份到 logs/", _log),
    Command("host", "host add <地址>", "加 / 看远程 frida-server", _host),
    Command("inject", "inject on|off", "是否真的把触控发给设备（仅本次会话）", _inject),
    Command("gate", "gate on|off", "开谱时是否拦住游戏等规划完（仅本次会话）", _gate),
    Command("verbose", "verbose on|off", "是否把每一个 Perfect 也打出来（仅本次会话）", _verbose),
    Command("help", "help", "列出这些命令", lambda controller, argv: _help()),
    Command("quit", "quit", "收工：断开注入并退出（等同 Ctrl+C）", _quit),
)

_ALIASES = {
    "?": "help",
    "exit": "quit",
    "respawn": "spawn",
    "reattach": "attach",
}


def _help() -> None:
    log("命令：")
    for command in COMMANDS:
        log(f"  {command.usage:<24} {command.help}")


def _switch(argv: list[str]) -> bool | None:
    """把 ``on`` / ``off`` 解析成布尔；看不懂就 None（调用方负责抱怨）。"""
    if not argv:
        return None
    return {"on": True, "off": False, "1": True, "0": False}.get(argv[0].strip().lower())


class Console:
    """读一行、执行一行。``Controller.stop()`` 之后循环就结束。

    ``stdin`` 可以换掉（自检用假 stdin 喂命令）；只有真终端才走 :func:`input` —— 它在
    Ctrl+C 上的行为最规矩（抛 ``KeyboardInterrupt`` 给主线程），而假 stdin 只能 ``readline``。
    """

    def __init__(self, controller: Controller, *, stdin=None) -> None:
        self.controller = controller
        self._stdin = stdin if stdin is not None else sys.stdin
        self._interactive = bool(getattr(self._stdin, "isatty", bool)())
        """是不是终端。决定"用 input 还是 readline"，以及回显方式 —— 见模块开头。"""
        self._waiting = False
        """提示符已经画在屏幕上、正在等一行输入。别的线程要打印时据此把它擦掉重画。"""
        self._commands = {command.name: command for command in COMMANDS}

    # ------------------------------------------------------------ 主干

    def run(self) -> None:
        """在主线程上跑：读一行、执行一行。"""
        set_prompt_hooks(self._clear_prompt, self._draw_prompt)
        log(
            "[main] 控制台就绪："
            + ("stdin 是终端" if self._interactive else "stdin 不是终端（管道/重定向）")
        )
        _help()
        try:
            self._loop()
        finally:
            # 提示符钩子与"控制台还在跑"必须是同一件事：摘晚了，收工之后别人打印
            # 还会去擦、去画一个没人等的提示符。
            self._waiting = False
            set_prompt_hooks()

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

    def _read(self) -> str | None:
        """读一行；``None`` 表示输入到头了（EOF）。

        **空串也是"没读到东西"**，不是 EOF —— 终端上挂起的一次读被中止时就会这样
        （控制台事件、窗口变化都算），所以上面把它和 EOF 分开处理。
        """
        self._waiting = True
        self._draw_prompt()
        try:
            if self._interactive and self._stdin is sys.stdin:
                return input()
            return self._stdin.readline()
        except EOFError:
            return None
        finally:
            self._waiting = False

    def _loop(self) -> None:
        empty = 0
        while not self.controller.stopping:
            line = self._read()

            if line is None or line == "":
                empty += 1
                if not self._interactive or empty >= EMPTY_READ_LIMIT:
                    self._park(reason="输入到头了" if line is None else "读不到输入")
                    return
                # 终端上的一次空读：多半是读被中止了，接着读就是
                continue
            empty = 0

            if not self._interactive:
                # 管道/重定向：没有终端回显，就把命令自己打出来，
                # 日志里才看得出"哪一条命令对应哪一行输出"
                log(f"{PROMPT}{line.rstrip()}")
            self.execute(line)

    def _park(self, *, reason: str) -> None:
        """输入到头了：**待机，不退出**（退出意味着人没有面板可用了）。

        不再读输入，主干与 agent 照常跑；``quit`` 得从别处给（另一条命令、或者 Ctrl+C）。
        """
        log(f"[main] {reason}（EOF），控制台进入待机：不再读输入，按 Ctrl+C 收工")
        while not self.controller.stopping:
            time.sleep(PARK_POLL)

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
