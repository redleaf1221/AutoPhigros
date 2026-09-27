"""附加目标的解析：**先问应用列表，再问进程列表，最后才按名字找**。

这条顺序是被实机教出来的（2026-09-27 那台 OPPO/MTK 设备）：

    enumerate_processes()  里有 (30501, 'Phigros')          ← 进程名是**应用标签**
    enumerate_applications() 里有 ('com.PigeonGames.Phigros', 30501)
    device.attach('com.PigeonGames.Phigros') → ProcessNotFoundError

即"游戏明明开着，按包名却找不到"。所以这里的用例全都盯着一件事：**包名不在进程列表里
时也必须附加得上**，而且失败时要自己把 frida 看得见的东西列出来（不然永远只能靠猜）。
"""

from __future__ import annotations

import contextlib
import io
import tempfile
from pathlib import Path
from types import SimpleNamespace

from runtime import controller, config, agent
from .stubs import make_config


def _config(**overrides) -> config.Config:
    """自检用的配置：默认带上"附加模式"这个语境（`attach <pid>` 走的就是它）。"""
    fields = {"pid": None, "attach": True}
    fields.update(overrides)
    fields.pop("pid", None)
    fields.pop("attach", None)
    return make_config(**fields)


class FakeDevice:
    """只实现解析用得到的三样：两份清单 + ``attach``。"""

    def __init__(self, applications=(), processes=(), *, broke=()) -> None:
        self._applications = list(applications)
        self._processes = list(processes)
        self._broke = set(broke)
        self.attached: list[object] = []

    def enumerate_applications(self):
        if "applications" in self._broke:
            raise RuntimeError("读不到应用列表")
        return self._applications

    def enumerate_processes(self):
        if "processes" in self._broke:
            raise RuntimeError("读不到进程列表")
        return self._processes

    def attach(self, target):
        self.attached.append(target)
        return FakeSession()


class FakeSession:
    def __init__(self) -> None:
        self.handlers: list[str] = []

    def on(self, name, callback) -> None:
        self.handlers.append(name)

    def create_script(self, source, name=None):
        return FakeScript()

    def detach(self) -> None:
        pass


class FakeScript:
    def on(self, name, callback) -> None:
        pass

    def load(self) -> None:
        pass


def app(identifier: str, pid: int, name: str = "") -> SimpleNamespace:
    return SimpleNamespace(identifier=identifier, pid=pid, name=name)


def process(pid: int, name: str) -> SimpleNamespace:
    return SimpleNamespace(pid=pid, name=name)


def check_attach() -> list[str]:
    """附加目标自检的入口 —— 正文见 :func:`_check_attach`。"""
    # 这一组要真的走一遍解析与注入，中间会打出正常的日志；全接进缓冲里，别混进自检结果。
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        return _check_attach()


def _check_attach() -> list[str]:
    problems: list[str] = []

    def build(device: FakeDevice) -> controller.Controller:
        controller_ = controller.Controller(_config())
        controller_._device = device  # noqa: SLF001 - 自检就是要顶掉真设备
        return controller_

    def expect(condition: bool, complaint: str) -> None:
        if not condition:
            problems.append(complaint)

    # 1) 实机那种情形：进程列表里只有应用标签，包名在应用表里
    device = FakeDevice(
        applications=[app(config.PACKAGE, 30501, "Phigros")],
        processes=[process(30501, "Phigros"), process(1, "init")],
    )
    expect(
        build(device)._resolve_target() == 30501,  # noqa: SLF001
        "包名不在进程列表里时，没能从应用列表拿到 pid（这正是实机上 attach 失败的原因）",
    )

    # 2) 应用列表读不到，但进程名就是包名
    device = FakeDevice(processes=[process(4242, config.PACKAGE)], broke={"applications"})
    expect(build(device)._resolve_target() == 4242, "进程名等于包名时没能用上它")  # noqa: SLF001

    # 3) Unity 的子进程：包名 + ":xxx"
    device = FakeDevice(processes=[process(777, config.PACKAGE + ":unity")])
    expect(build(device)._resolve_target() == 777, "带 :子进程后缀的进程名没认出来")  # noqa: SLF001

    # 4) 两边都找不到：把包名原样交给 frida（它自己的报错更权威），而不是瞎猜一个 pid
    device = FakeDevice(processes=[process(1, "init")], broke={"applications"})
    expect(build(device)._resolve_target() == config.PACKAGE, "谁也不认识时不该编一个目标")  # noqa: SLF001

    # 5) attach <pid>：跳过一切查找
    device = FakeDevice(
        applications=[app(config.PACKAGE, 30501)], processes=[process(30501, "Phigros")]
    )
    expect(build(device)._resolve_target(1234) == 1234, "给了 pid 时没被优先采用")  # noqa: SLF001

    # 6) 真的注入时用的是 pid，不是名字
    with tempfile.TemporaryDirectory() as workspace:
        agent_file = Path(workspace) / "_.js"
        agent_file.write_text("// stub", encoding="utf-8")
        device = FakeDevice(processes=[process(30501, "Phigros")])
        instance = agent.Agent(agent_file, device, attach=True, target=30501)
        instance.start()
        expect(device.attached == [30501], f"附加时用的目标不对：{device.attached}")

    # 7) 附加失败时要把看得见的东西列出来（否则只能靠猜）
    device = FakeDevice(
        applications=[app(config.PACKAGE, 30501, "Phigros")],
        processes=[process(30501, "Phigros"), process(1, "init")],
    )
    buffer = io.StringIO()
    with contextlib.redirect_stderr(buffer), contextlib.redirect_stdout(buffer):
        build(device)._report_candidates()  # noqa: SLF001
    text = buffer.getvalue()
    for fragment in ("30501", config.PACKAGE, "Phigros"):
        expect(fragment in text, f"候选清单里少了 {fragment!r}：{text!r}")
    expect("--pid" in text, "没告诉人可以用 --pid 绕开名字查找")

    return problems

    return problems
