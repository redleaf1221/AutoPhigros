"""设备枚举：**先确保 adb server 在跑**。

盯的是冷启动那一下：adb server 还没起来时 frida 走 USB 一台设备都看不到，列表空着，
人只会以为线没插好或者 frida-server 没起。`list_devices()` 必须先替 `adb devices` 做一遍。
"""

from __future__ import annotations

import contextlib
import io
from types import SimpleNamespace

from backends import scrcpy
from runtime import controller
from .stubs import make_config


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


def check_device() -> list[str]:
    """设备自检的入口。"""
    problems: list[str] = []

    def expect(condition: bool, complaint: str) -> None:
        if not condition:
            problems.append(complaint)

    # 1) `start_adb_server()` 真的去调 `adb start-server`，而且成了之后只调一次
    fake = FakeRun()
    with _patched_run(fake), _fresh_server():
        expect(scrcpy.start_adb_server(), "adb start-server 成功了却报失败")
        expect(scrcpy.start_adb_server(), "adb start-server 第二次报了失败")
    expect(
        fake.calls == [[str(scrcpy.ADB), "start-server"]],
        f"adb 被调成了 {fake.calls}（期望一次 `adb start-server`）",
    )

    # 2) 找不到 adb：不许抛，也不许把失败记成"起过了"（下次还得再试）
    fake = FakeRun(missing=True)
    with _patched_run(fake), _fresh_server():
        expect(not scrcpy.start_adb_server(), "adb 不在却报成功")
        expect(not scrcpy.start_adb_server(), "adb 不在时第二次不该报成功")
    expect(len(fake.calls) == 2, f"adb 不在时只该每次都试：{fake.calls}")

    # 3) 枚举设备前会先确保 adb 在跑；一台都列不到、adb 又起不来时，要把这两件事一起报出来
    calls: list[bool] = []
    manager = FakeManager()
    with (
        contextlib.redirect_stderr(io.StringIO()) as buffer,
        contextlib.redirect_stdout(io.StringIO()),
    ):
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
        "adb start-server" in text and str(scrcpy.ADB) in text,
        f"列表空 + adb 起不来时没有说清楚：{text!r}",
    )

    # 4) 列表非空时不啰嗦：起不来也照常返回设备
    class OneManager(FakeManager):
        def enumerate_devices(self):
            return [SimpleNamespace(id="D1", name="Phone", type="usb")]

    with (
        contextlib.redirect_stderr(io.StringIO()) as buffer,
        contextlib.redirect_stdout(io.StringIO()),
    ):
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

    return problems


@contextlib.contextmanager
def _patched_run(fake: FakeRun):
    original = scrcpy.subprocess.run
    scrcpy.subprocess.run = fake
    try:
        yield
    finally:
        scrcpy.subprocess.run = original


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
