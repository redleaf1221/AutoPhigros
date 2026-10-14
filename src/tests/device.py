"""设备：**adb 只从 PATH 里找**，找不到就报错退出。

盯两件事：

1. 冷启动那一下：adb server 还没起来时 frida 走 USB 一台设备都看不到，列表空着，人只会以为
   线没插好或者 frida-server 没起，所以 `list_devices()` 必须先替 `adb devices` 做一遍；
2. adb 的路径只有 PATH 这一个来源：它搬过家，写死的那一份一旦过期，"找不到 adb"就会伪装成
   "一台设备都没有"，人在线和驱动上白找半天 —— 所以那条路必须是**报错退出**。
"""

from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path
from types import SimpleNamespace

import frida

from backends import scrcpy
from runtime import controller
from .stubs import make_config

FAKE_ADB = Path("fake-tools") / "adb.exe"
"""假的 adb 路径。故意**不带盘符**：这一份文件自己也要过"源码里不许写死盘符"那一关。"""

SOURCE = Path(__file__).resolve().parents[1]
"""``src/``：查"有没有人又写死了一条路径"时扫这里。"""

DRIVE_PATH = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")
"""盘符开头的路径（字母 + 冒号 + 斜杠那种；前面那个 lookbehind 是为了放过 `http://`）。"""


class FakeRun:
    """顶掉 `subprocess.run`：只记下 adb 被怎么调的，不真起进程。"""

    def __init__(self, *, code: int = 0, missing: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.code = code
        self.missing = missing

    def __call__(self, command, **kwargs):
        self.calls.append(list(command))
        if self.missing:
            raise FileNotFoundError(command[0])
        return SimpleNamespace(returncode=self.code, stdout=b"", stderr=b"")


class FakeManager:
    """顶掉 frida 的设备管理器：一台 USB 设备都枚举不到。"""

    def __init__(self) -> None:
        self.remote: list[str] = []

    def add_remote_device(self, host: str):
        self.remote.append(host)
        raise RuntimeError("连不上")

    def enumerate_devices(self):
        return []


class ExplodingFrida:
    """碰一下就炸（并把次数记下来）：adb 都找不到的时候，不该走到 frida 那边去。"""

    TimedOutError = frida.TimedOutError
    """`start()` 里 `auto_select_device()` 会 catch 它，所以这个假的也得有。"""
    TransportError = frida.TransportError

    def __init__(self) -> None:
        self.asked = 0

    def get_device_manager(self):
        self.asked += 1
        raise AssertionError("PATH 里都没有 adb，还去问 frida 要设备")


def check_device() -> list[str]:
    """设备自检的入口。"""
    problems: list[str] = []

    def expect(condition: bool, complaint: str) -> None:
        if not condition:
            problems.append(complaint)

    # 1) PATH 里有 adb：`adb start-server` 用的就是 PATH 里那一个，而且成了之后只调一次
    fake = FakeRun()
    with _patched_run(fake), _patched_which(FAKE_ADB), _fresh_server():
        expect(scrcpy.start_adb_server(), "adb start-server 成功了却报失败")
        expect(scrcpy.start_adb_server(), "adb start-server 第二次报了失败")
    expect(
        fake.calls == [[str(FAKE_ADB), "start-server"]],
        f"adb 被调成了 {fake.calls}（期望一次 `{FAKE_ADB} start-server`）",
    )

    # 2) PATH 里没有 adb：`adb_path()` 抛 AdbNotFound（不猜、不退回任何写死的位置），
    #    也不该白起一次进程 —— 连可执行文件都没有，`subprocess` 只会再报一次 FileNotFoundError
    fake = FakeRun(missing=True)
    with _patched_run(fake), _patched_which(None), _fresh_server():
        try:
            scrcpy.adb_path()
            problems.append("PATH 里没有 adb，adb_path() 却没报错")
        except scrcpy.AdbNotFound:
            pass
        expect(not scrcpy.start_adb_server(), "adb 不在却报成功")
        expect(not scrcpy.start_adb_server(), "adb 不在时第二次不该报成功")
        reason = scrcpy.available()
    expect(fake.calls == [], f"adb 不在时不该去起进程：{fake.calls}")
    expect(
        reason is not None and "PATH" in reason,
        f"adb 不在时 available() 该说清是 PATH 里没有：{reason!r}",
    )

    # 3) 起主干：头一件事就是把 adb 落实了 —— 没有就报错退出，而且**根本没到 frida 那一步**
    with _patched_which(None), _fresh_server(), _quiet() as buffer:
        instance = controller.Controller(make_config())
        double = ExplodingFrida()
        original = controller.frida
        controller.frida = double
        try:
            code = _exit_code(instance.start)
        finally:
            controller.frida = original
        text = buffer.getvalue()
    expect(code == 2, f"adb 不在时起主干没有报错退出（{code!r}）")
    expect("PATH" in text and "adb" in text, f"起主干时没说清是 PATH 里没有 adb：{text!r}")
    expect(double.asked == 0, "adb 都不在，起主干却已经去问 frida 要设备了")

    # 4) adb 整个不在时，列设备也要指名道姓说是 PATH 里没有（含糊成"server 没起来"等于没说）
    manager = FakeManager()
    holder: list[object] = []
    with _patched_which(None), _fresh_server(), _quiet() as buffer:
        instance = controller.Controller(make_config())
        original_frida = controller.frida
        controller.frida = SimpleNamespace(get_device_manager=lambda: manager)
        try:
            code = _exit_code(lambda: holder.append(instance.list_devices()))
        finally:
            controller.frida = original_frida
        text = buffer.getvalue()
    expect(code is None, f"adb 不在时列设备不该炸：{code}")
    expect(holder == [[]], f"假的 frida 一台都不该列出：{holder}")
    expect(
        "PATH" in text and "adb" in text,
        f"adb 不在时的设备列表没说是 PATH 里没有 adb：{text!r}",
    )

    # 5) 枚举设备前会先确保 adb 在跑；一台都列不到、adb server 又起不来时，两件事一起报出来
    calls: list[bool] = []
    with _patched_which(FAKE_ADB), _fresh_server(), _quiet() as buffer:
        instance = controller.Controller(make_config())
        original_frida, original_start = controller.frida, controller.start_adb_server
        controller.frida = SimpleNamespace(get_device_manager=lambda: manager)
        controller.start_adb_server = lambda: calls.append(True) or False
        try:
            devices = instance.list_devices()
        finally:
            controller.frida, controller.start_adb_server = original_frida, original_start
        text = buffer.getvalue()
    expect(devices == [], f"假的 frida 一台都不该列出：{devices}")
    expect(calls == [True], "列设备之前没有先确保 adb server 在跑")
    expect(
        "adb start-server" in text and str(FAKE_ADB) in text,
        f"列表空 + adb server 起不来时没有说清楚：{text!r}",
    )

    # 6) 列表非空时不啰嗦：server 起不来也照常返回设备
    class OneManager(FakeManager):
        def enumerate_devices(self):
            return [SimpleNamespace(id="D1", name="Phone", type="usb")]

    with _patched_which(FAKE_ADB), _fresh_server(), _quiet() as buffer:
        instance = controller.Controller(make_config())
        original_frida, original_start = controller.frida, controller.start_adb_server
        controller.frida = SimpleNamespace(get_device_manager=lambda: OneManager())
        controller.start_adb_server = lambda: False
        try:
            devices = instance.list_devices()
        finally:
            controller.frida, controller.start_adb_server = original_frida, original_start
        text = buffer.getvalue()
    expect([device.id for device in devices] == ["D1"], f"USB 设备没被列出来：{devices}")
    expect(text == "", f"设备列出来了还在抱怨 adb：{text!r}")

    # 7) 头不许写死：源码里出现盘符路径就是有人又把 adb 的位置钉在了地上
    for path in sorted(SOURCE.rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if DRIVE_PATH.search(line):
                problems.append(
                    f"{path.relative_to(SOURCE)}:{number} 里写死了盘符路径：{line.strip()}"
                )

    return problems


def _exit_code(action) -> int | str | None:
    """跑一下 ``action``：报了 `SystemExit` 就交出退出码。

    别的异常交出类名 —— "炸了"也是一种"没报错退出"，自检要看得见它，而不是被它带走。
    """
    try:
        action()
    except SystemExit as error:
        return error.code if isinstance(error.code, int) else 1
    except Exception as error:  # noqa: BLE001 - 见 docstring
        return type(error).__name__
    return None


@contextlib.contextmanager
def _quiet():
    """把 stdout / stderr 一起收进一个缓冲区。"""
    buffer = io.StringIO()
    with contextlib.redirect_stderr(buffer), contextlib.redirect_stdout(buffer):
        yield buffer


@contextlib.contextmanager
def _patched_run(fake: FakeRun):
    original = scrcpy.subprocess.run
    scrcpy.subprocess.run = fake
    try:
        yield
    finally:
        scrcpy.subprocess.run = original


@contextlib.contextmanager
def _patched_which(found: Path | None):
    """顶掉 `shutil.which`：自检不该取决于这台机器上装没装 adb、装在哪。"""
    original = scrcpy.shutil.which
    scrcpy.shutil.which = lambda name: None if found is None else str(found)
    try:
        yield
    finally:
        scrcpy.shutil.which = original


@contextlib.contextmanager
def _fresh_server():
    """把"已经拉过 server"这个进程级记号清掉，好在同一个进程里反复量。"""
    original = scrcpy._ADB_SERVER_READY.is_set()  # noqa: SLF001 - 自检就是要重放这个记号
    scrcpy._ADB_SERVER_READY.clear()  # noqa: SLF001
    try:
        yield
    finally:
        scrcpy._ADB_SERVER_READY.clear()  # noqa: SLF001
        if original:
            scrcpy._ADB_SERVER_READY.set()  # noqa: SLF001
