"""控制台检查：命令解析、回显三条规矩、"空读不退出"，以及"子进程不许继承 stdin"的审计。

控制台是**主干**（跑在主线程上读输入），所以空读与 EOF 都不能把它带走 —— 只有 `quit` /
`exit` / Ctrl+C 才让两者一起退出。回显三条规矩：每条命令都得有回话（包括 `status_lines()`
给空的时候）、管道模式下命令要回显、别的线程打印完提示符必须画回来。
"""

from __future__ import annotations

import contextlib
import io
import threading
import time

from . import SOURCE_ROOT
from .stubs import make_config


def check_console() -> list[str]:
    """控制台自检：`latency` 的三种写法与"打错命令不该把控制台带走"、落盘边界
    （`inject` / `verbose` 绝不落盘，别的该落的真落），以及**空读 / EOF 不退出**。
    """
    from runtime.console import Console
    from runtime.options import Options

    class FakeController:
        """只放控制台真正会碰的那几样东西 —— 控制台对主干的依赖就这么多。"""

        def __init__(self) -> None:
            self.options = Options()
            self.config = make_config()
            self.stopping = False
            self.agent = None
            self.calls: list[str] = []
            self.persists = 0

        def status_lines(self) -> list[str]:
            return ["（替身）状态"]

        def stop(self) -> None:
            self.calls.append("stop")
            self.stopping = True

        def persist(self) -> None:
            self.persists += 1

        def print_devices(self) -> None:
            from runtime.output import log

            self.calls.append("print_devices")
            log("（替身）设备列表")

        def select_device(self, identifier: str) -> bool:
            self.calls.append(f"select:{identifier}")
            return True

        def launch(self, *, spawn: bool, pid: int | None = None) -> bool:
            self.calls.append(f"launch:{spawn}:{pid}")
            return True

        def open_backend(self, name: str | None = None) -> None:
            self.calls.append(f"backend:{name}")

        def add_host(self, host: str) -> bool:
            self.calls.append(f"host:{host}")
            return True

    problems: list[str] = []
    fake = FakeController()
    console = Console(fake)

    def run(line: str) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            console.execute(line)
        return buffer.getvalue()

    def expect(condition: bool, complaint: str) -> None:
        if not condition:
            problems.append(complaint)

    # latency：绝对值、毫秒、增减三种写法
    run("latency 0.02")
    expect(abs(fake.options.latency - 0.02) < 1e-9, f"latency 0.02 没生效：{fake.options.latency}")
    run("latency 20ms")
    expect(
        abs(fake.options.latency - 0.02) < 1e-9, f"latency 20ms 没生效：{fake.options.latency}"
    )
    run("latency +5ms")
    expect(
        abs(fake.options.latency - 0.025) < 1e-9, f"latency +5ms 不成增量：{fake.options.latency}"
    )
    run("latency -30ms")
    expect(abs(fake.options.latency + 0.005) < 1e-9, f"latency -30ms 不成增量：{fake.options.latency}")
    run("latency 说不清")
    expect(abs(fake.options.latency + 0.005) < 1e-9, "看不懂的数不该改动设置")
    expect(fake.persists > 0, "latency 改了却没落盘")

    # 会话内的开关：改了当场生效，但**不许落盘**
    before = fake.persists
    run("inject off")
    expect(not fake.options.inject, "inject off 没生效")
    run("inject on")
    run("verbose on")
    expect(fake.options.verbose, "verbose on 没生效")
    expect(
        fake.persists == before,
        "inject / verbose 不该落盘：一个持久化的 inject off 会让人下次以为在打歌",
    )

    # 规划器：认识的要换（并落盘）、不认识的原样不动
    run("planner radical")
    expect(fake.options.planner == "radical", f"planner radical 没生效：{fake.options.planner}")
    expect(fake.persists > before, "planner 改了却没落盘")
    run("planner 没有这个")
    expect(fake.options.planner == "radical", "换到不存在的规划器时不该改动设置")

    # 规划器参数：改了就落盘、名字写错不许写进去、类型不对不许覆盖、reset 回到默认值
    run("option flick_repeats 3")
    expect(
        fake.config.planner_options.get("radical", {}).get("flick_repeats") == 3,
        f"option 没写进配置：{fake.config.planner_options}",
    )
    run("option 没这个参数 1")
    expect(
        fake.config.planner_options.get("radical", {}).get("没这个参数") is None,
        "不认识的参数不该写进配置",
    )
    run("option flick_repeats 不是整数")
    expect(
        fake.config.planner_options.get("radical", {}).get("flick_repeats") == 3,
        "类型不对的值不该覆盖掉原来的",
    )
    run("option flick_repeats reset")
    expect(
        "flick_repeats" not in fake.config.planner_options.get("radical", {}),
        "reset 之后不该还留着",
    )

    # 设备与注入
    run("devices")
    expect("print_devices" in fake.calls, "devices 没有去列设备")
    run("device ABC123")
    expect("select:ABC123" in fake.calls, "device <id> 没有去选设备")
    run("spawn")
    run("attach")
    run("attach 30501")
    run("respawn")
    run("reattach")
    expect(
        [call for call in fake.calls if call.startswith("launch:")]
        == ["launch:True:None", "launch:False:None", "launch:False:30501", "launch:True:None",
            "launch:False:None"],
        f"spawn / attach 没走对：{fake.calls}",
    )
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        console.execute("attach 不是数字")
    expect(bool(buffer.getvalue().strip()), "attach 给了非数字 pid 时不该一声不吭")

    # 会落盘的设置
    run("cache off")
    expect(fake.config.cache is False, "cache off 没写进配置")
    run("save-chart on")
    expect(fake.config.save_chart is True, "save-chart on 没写进配置")
    run("backend recording")
    expect(fake.config.backend == "recording", "backend 没写进配置")
    expect(
        "backend:None" not in fake.calls,
        "没有会话时换后端不该去重开（下次注入再起就行）",
    )
    fake.agent = object()
    run("backend scrcpy")
    expect("backend:scrcpy" in fake.calls, "有会话时换后端应当当场重开")
    run("host add 192.168.1.10:27042")
    expect("host:192.168.1.10:27042" in fake.calls, "host add 没去加远程 server")

    # latency auto：自校准开关，以及"手动给数就把自校准关掉"这条规矩
    controller = FakeController()
    controller.config.auto_latency = True
    console = Console(controller)

    def say(line: str) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            console.execute(line)
        return buffer.getvalue()

    text = say("latency")
    expect("自校准 开" in text, f"latency 该报告自校准状态：{text!r}")
    text = say("latency 20ms")
    expect(abs(controller.options.latency - 0.02) < 1e-9, f"latency 20ms 没生效：{controller.options.latency}")
    expect(
        controller.config.auto_latency is False,
        "手动给了数就该把自校准关掉（人一旦自己定了值，别再让机器改）",
    )
    expect("自校准" in text and "关掉" in text, f"关掉这件事要说出来：{text!r}")
    expect(controller.persists >= 1, "自校准开关变了要落盘")
    say("latency auto on")
    expect(controller.config.auto_latency is True, "latency auto on 没生效")
    text = say("latency auto off")
    expect(controller.config.auto_latency is False, "latency auto off 没生效")
    expect("自校准" in text, f"开关没回话：{text!r}")

    # 打错、打空、打注释都不该炸，也不该被当成命令
    for line in ("没这个命令", "", "   ", "# 只是注释", "status 多余的参数"):
        try:
            run(line)
        except Exception as error:  # noqa: BLE001 - 就是来看它炸不炸的
            problems.append(f"执行 {line!r} 时抛了 {type(error).__name__}: {error}")

    # 每一条命令都要说话 —— 包括"看不懂"的。空回显是最难查的一种"没反应"。
    for line in (
        "没这个命令", "latency 说不清", "inject 说不清", "verbose", "planner 没有这个", "status",
        "devices", "device", "backend", "backend 没有这个", "cache", "save-chart", "host",
    ):
        if not run(line).strip():
            problems.append(f"{line!r} 一个字都没回")

    # status 就算拿不到任何一行，也不能沉默
    class EmptyController(FakeController):
        def status_lines(self) -> list[str]:
            return []

    silent = Console(EmptyController())
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        silent.execute("status")
    expect(bool(buffer.getvalue().strip()), "status_lines() 是空的时候，status 不该一声不吭")

    # quit / exit 都让主干收工
    for word in ("quit", "exit"):
        controller = FakeController()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            Console(controller).execute(word)
        expect(controller.stopping, f"{word} 没有让主干收工")

    # 管道/重定向：命令要回显出来，日志里才看得出哪条命令配哪行输出
    class FakeStdin:
        """一根假 stdin：按行喂命令，喂完就是 EOF。"""

        def __init__(self, *lines: str, tty: bool = False, repeat_last: bool = False) -> None:
            self.lines = list(lines)
            self._tty = tty
            self._repeat = repeat_last

        def isatty(self) -> bool:
            return self._tty

        def readline(self) -> str:
            if self.lines:
                return self.lines.pop(0)
            return "" if not self._repeat else ""

    def drive(stdin, controller: FakeController) -> str:
        """把循环跑起来（它会停在"停靠"里），再从另一个线程收工，把输出拿回来。"""
        buffer = io.StringIO()
        console = Console(controller, stdin=stdin)
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            runner = threading.Thread(target=console.run, name="selftest-console")
            runner.start()
            runner.join(0.5)
            stayed = runner.is_alive()
            controller.stopping = True  # 收工：停靠里的循环必须因此结束
            runner.join(2.0)
            if runner.is_alive():
                problems.append("收到收工之后控制台循环没有结束")
        return ("停靠" if stayed else "退出") + "\n" + buffer.getvalue()

    # 1) 管道：命令回显，读到 EOF 之后**停靠而不是退出**
    controller = FakeController()
    text = drive(FakeStdin("status\n"), controller)
    expect(text.startswith("停靠"), "管道读到 EOF 时控制台不该退出（退出就等于人没有面板了）")
    expect("auto> status" in text, f"管道模式下命令没有回显：{text!r}")
    expect("待机" in text, f"停靠时该说清楚它还在跑、怎么收工：{text!r}")
    _ = time

    # 2) 终端：一次空读不算 EOF（那是读被中止了），后面的命令照样执行
    controller = FakeController()
    text = drive(FakeStdin("", "devices\n", tty=True), controller)
    expect("print_devices" in controller.calls, f"终端上的一次空读把控制台带走了：{text!r}")
    expect("待机" in text, "连续空读到头之后应当停靠，而不是悄悄消失")

    # 3) 终端上的空读：读完那一行就继续（不该退出，也不该当 EOF）
    controller = FakeController()
    text = drive(FakeStdin("devices\n", "status\n", tty=True), controller)
    expect(text.startswith("停靠"), f"终端上正常读完命令之后应当停靠：{text!r}")
    expect("（替身）状态" in text, f"第二条命令没有执行：{text!r}")

    # 提示符会被别的线程的输出顶掉，输出完必须画回来：擦掉 -> 打印 -> 再画
    from runtime.console import PROMPT
    from runtime.output import log, set_prompt_hooks

    buffer = io.StringIO()
    console = Console(FakeController(), stdin=FakeStdin(tty=True))
    console._waiting = True  # noqa: SLF001 - 模拟"正等着输入"
    set_prompt_hooks(console._clear_prompt, console._draw_prompt)  # noqa: SLF001
    try:
        with contextlib.redirect_stdout(buffer):
            log("[main] 别的线程说话了")
    finally:
        set_prompt_hooks()
    text = buffer.getvalue()
    expect(text.count("[main] 别的线程说话了") == 1, f"那行输出应当只出现一次：{text!r}")
    expect(text.startswith("\r"), f"打印前应当先擦掉提示符：{text!r}")
    expect(text.endswith(PROMPT), f"打印完应当把提示符画回来，而不是擦掉就完事：{text!r}")
    expect(PROMPT in text.split("[main]")[1], f"提示符应当在那行输出之后：{text!r}")

    # 没在等输入的时候（正在执行命令）不该乱插提示符
    buffer = io.StringIO()
    console._waiting = False  # noqa: SLF001
    set_prompt_hooks(console._clear_prompt, console._draw_prompt)  # noqa: SLF001
    try:
        with contextlib.redirect_stdout(buffer):
            log("命令自己的输出")
    finally:
        set_prompt_hooks()
    expect(
        buffer.getvalue() == "命令自己的输出\n",
        f"没在等输入时不该动屏幕：{buffer.getvalue()!r}",
    )

    # 输出必须当场出去，不能攒在缓冲里
    class FlushSpy(io.StringIO):
        def __init__(self) -> None:
            super().__init__()
            self.flushes = 0

        def flush(self) -> None:
            self.flushes += 1
            super().flush()

    spy = FlushSpy()
    with contextlib.redirect_stdout(spy):
        log("必须立刻出去")
    expect(spy.getvalue() == "必须立刻出去\n", f"log 的内容不对：{spy.getvalue()!r}")
    expect(
        spy.flushes >= 1,
        "log() 没有 flush —— 管道里会攒成「敲了一条没反应、再敲一条上一条才出来」",
    )

    # 谁都不许继承我们的 stdin：`adb shell` 会把本地 stdin 转发给设备端，把控制台里敲的那一行半路吃掉
    for path in sorted(SOURCE_ROOT.glob("*.py")) + sorted((SOURCE_ROOT / "backends").glob("*.py")):
        for number in _subprocess_calls_without_stdin(path.read_text(encoding="utf-8")):
            problems.append(f"{path.name}:{number} 的 subprocess 调用没有 stdin=DEVNULL")

    return problems


def _subprocess_calls_without_stdin(text: str) -> list[int]:
    """找出所有没给 ``stdin=`` 的 subprocess 调用，返回行号。

    判据是"这次调用的括号里有没有 ``stdin=``"，不是"这一行里有没有"（参数写成多行很正常）。
    """
    import re

    found: list[int] = []
    for match in re.finditer(r"subprocess\.(?:run|Popen|call|check_call|check_output)\(", text):
        depth, index = 1, match.end()
        while index < len(text) and depth:
            if text[index] == "(":
                depth += 1
            elif text[index] == ")":
                depth -= 1
            index += 1
        if "stdin=" not in text[match.end() : index]:
            found.append(text[: match.start()].count("\n") + 1)
    return found
