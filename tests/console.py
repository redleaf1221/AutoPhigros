"""控制台检查：命令解析、回显三条规矩，以及"子进程不许继承 stdin"的源码级审计。"""

from __future__ import annotations

import contextlib
import io

import planner
from .__init__ import ROOT, ROOT


def check_console() -> list[str]:
    """控制台自检：命令解析与那几个旋钮。

    盯的是 ``latency`` 的三种写法（绝对值 / 毫秒 / 增减）与"打错命令不该把控制台带走"。
    控制台是运行时唯一的面板，解析错了的表现是"改了但没生效"或者"面板没了"，
    比直接报错难查得多，所以值得钉住。
    """
    from console import Console
    from options import Options

    class FakeController:
        """只放控制台真正会碰的那几样东西 —— 控制台对主干的依赖就这么多。"""

        def __init__(self) -> None:
            self.options = Options()
            self.stopping = False
            self.restarts: list[bool] = []

        def status_lines(self) -> list[str]:
            return ["（替身）状态"]

        def restart(self, *, spawn: bool) -> None:
            self.restarts.append(spawn)

        def stop(self) -> None:
            self.stopping = True

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
    expect(
        abs(fake.options.latency + 0.005) < 1e-9, f"latency -30ms 不成增量：{fake.options.latency}"
    )
    run("latency 说不清")
    expect(
        abs(fake.options.latency + 0.005) < 1e-9, "看不懂的数不该改动设置"
    )

    # 开关
    run("inject off")
    expect(not fake.options.inject, "inject off 没生效")
    run("inject on")
    expect(fake.options.inject, "inject on 没生效")
    run("verbose on")
    expect(fake.options.verbose, "verbose on 没生效")

    # 规划器：认识的要换、不认识的原样不动
    run("planner radical")
    expect(fake.options.planner == "radical", f"planner radical 没生效：{fake.options.planner}")
    run("planner 没有这个")
    expect(fake.options.planner == "radical", "换到不存在的规划器时不该改动设置")

    # 重连与收工
    run("respawn")
    run("reattach")
    expect(fake.restarts == [True, False], f"respawn/reattach 没走对：{fake.restarts}")
    run("quit")
    expect(fake.stopping, "quit 没有让主干收工")

    # 打错、打空、打注释都不该炸，也不该被当成命令
    for line in ("没这个命令", "", "   ", "# 只是注释", "status 多余的参数"):
        try:
            run(line)
        except Exception as error:  # noqa: BLE001 - 就是来看它炸不炸的
            problems.append(f"执行 {line!r} 时抛了 {type(error).__name__}: {error}")

    # 每一条命令都要说话 —— 包括"看不懂"的。空回显是最难查的一种"没反应"。
    for line in ("没这个命令", "latency 说不清", "inject 说不清", "verbose", "planner 没有这个", "status"):
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

    # 管道/重定向：命令要回显出来，日志里才看得出哪条命令配哪行输出
    class FakeStdin:
        """一根假 stdin：按行喂命令，喂完就是 EOF。"""

        def __init__(self, *lines: str, tty: bool = False) -> None:
            self.lines = list(lines)
            self._tty = tty

        def isatty(self) -> bool:
            return self._tty

        def readline(self) -> str:
            return self.lines.pop(0) if self.lines else ""

    buffer = io.StringIO()
    piped = Console(FakeController(), stdin=FakeStdin("status\n"))
    with contextlib.redirect_stdout(buffer):
        piped._loop()  # noqa: SLF001 - 直接跑循环，喂完就 EOF
    text = buffer.getvalue()
    for fragment in ("auto> status", "EOF"):
        if fragment not in text:
            problems.append(f"管道模式下少了 {fragment!r}：{text!r}")

    # 提示符会被别的线程的输出顶掉，输出完必须画回来：擦掉 -> 打印 -> 再画
    from console import PROMPT
    from output import log, set_prompt_hooks

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

    # 谁都不许继承我们的 stdin：`adb shell` 会把本地 stdin 转发给设备端，
    # 用户在控制台里敲的那一行就被它半路吃掉了 —— 这种错不报错，只表现为"偶尔有一行没反应"
    for path in sorted(ROOT.glob("*.py")) + sorted((ROOT / "backends").glob("*.py")):
        for number in _subprocess_calls_without_stdin(path.read_text(encoding="utf-8")):
            problems.append(f"{path.name}:{number} 的 subprocess 调用没有 stdin=DEVNULL")

    return problems


def _subprocess_calls_without_stdin(text: str) -> list[int]:
    """找出所有没给 ``stdin=`` 的 subprocess 调用，返回行号。

    判据是"这次调用的括号里有没有 ``stdin=``"，不是"这一行里有没有" —— 参数写成多行
    是很正常的写法（``scrcpy.py`` 里就是）。
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

