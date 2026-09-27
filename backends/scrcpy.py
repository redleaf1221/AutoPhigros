#!/usr/bin/env python3
"""scrcpy 控制协议后端：只借它的控制通道往设备里注入多点触控。

为什么不自己造
--------------
把触摸事件送进 Android 有两条路：`adb shell input`（慢、单点、带抬起延迟）和
`InputManager.injectInputEvent`（快、多点、可精确到帧）。后者需要 INJECT_EVENTS 权限，
而 scrcpy 的 server 已经在设备上把这条路走通了 —— 我们只按它的协议发字节就行。

协议（对着 scrcpy **v4.1** 的 `app/src/control_msg.h` / `control_msg.c` 与
`server/.../control/ControlMessageReader.java` 逐字段核过；控制协议是**大端**）

.. code-block:: text

    INJECT_TOUCH_EVENT = 2
    u8  type
    u8  action          0 = ACTION_DOWN，1 = ACTION_UP，2 = ACTION_MOVE，3 = CANCEL
    u64 pointer_id      每根手指一个；别用 -1/-2/-3（scrcpy 自己保留）
    u32 x
    u32 y
    u16 screen_width
    u16 screen_height
    u16 pressure        u16 定点（2^16 = 1.0）：按下 0xffff，抬起 0
    u32 action_button   手指触控恒 0
    u32 buttons         手指触控恒 0
    = 32 字节

多指的事 server 自己管：`Controller.injectTouch` 发现同时有两个以上指针时，会把
DOWN/UP 改写成 `ACTION_POINTER_DOWN/UP | (index << 8)`。所以客户端只管发
DOWN/UP/MOVE + 各自的 pointer_id。

连接方式
--------
用 `tunnel_forward`（server 侧 listen、主机侧 connect），比默认的 `adb reverse`
少一个"主机监听 + 等 accept"的环节，而且能靠一个哨兵字节确认 server 真的起来了：

.. code-block:: sh

    adb push <scrcpy-server> /data/local/tmp/scrcpy-server.jar
    adb forward tcp:<port> localabstract:scrcpy_<scid>
    adb shell CLASSPATH=/data/local/tmp/scrcpy-server.jar app_process / \\
        com.genymobile.scrcpy.Server 4.1 scid=<scid> log_level=info \\
        video=false audio=false tunnel_forward=true

连上以后**先读掉一个字节** —— `DesktopConnection.open()` 在第一个 socket 上写了
一个 0，就是给客户端用来判断"对面真的在监听"的（`server.c` 的
`connect_and_read_byte`）。

只开控制通道（`video=false audio=false`）时 server 的 `displayData` 是 null，
`Controller.getEventPointAndDisplayId` 会走 else 分支**按原始坐标注入**：我们发的
就是设备像素，不用关心视频尺寸（但 `screen_width/height` 两个字段仍得填）。
"""

from __future__ import annotations

import re
import secrets
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Sequence

from algorithms.geometry import Screen
from algorithms.utils import Touch, TouchEvent

from .utils import to_pixels

ADB = Path(r"C:\UserData\platform-tools\adb.exe")
"""adb 可执行文件。项目里其他脚本用的也是这个（scrcpy 自带的那份不掺和）。"""

SERVER = Path(__file__).resolve().parent / "scrcpy-server-v4.1"
"""scrcpy 4.1 的 server（就是 release 里那个 `scrcpy-server`，没扩展名）。

放在项目**上一级**目录里 —— 那是用户下载 scrcpy 的位置，不去动它。
"""

VERSION = "4.1"
"""server 会把第一个参数当版本号比对，必须和 server 本体一致。"""

DEVICE_PATH = "/data/local/tmp/scrcpy-server.jar"
SOCKET_NAME = "scrcpy_{scid:08x}"
"""设备侧的抽象 socket 名。server 那边拼的是 `"scrcpy" + "_%08x"`。"""

TOUCH_MESSAGE = struct.Struct(">BBQIIHHHII")
"""一条 INJECT_TOUCH_EVENT，32 字节。"""

_ACTIONS = {Touch.DOWN: 0, Touch.UP: 1, Touch.MOVE: 2, Touch.CANCEL: 3}
"""本项目内部动作 → Android MotionEvent 动作码。

`Touch` 的编号是规划器内部用的（DOWN/MOVE/UP/CANCEL = 0/1/2/3），Android 那边是
DOWN/UP/MOVE/CANCEL = 0/1/2/3 —— 只有 MOVE 和 UP 对调，所以必须显式映射。
"""

_PRESSED = 0xFFFF
_RELEASED = 0

CONNECT_TIMEOUT = 20.0
"""server 是 `app_process` 起 JVM，冷启动要一两秒，宽着点。"""


class ScrcpyError(RuntimeError):
    """起不来、连不上、推不进去。"""


def _adb(*args: str, serial: str | None = None, timeout: float = 30.0) -> str:
    command = [str(ADB)]
    if serial:
        command += ["-s", serial]
    command += list(args)
    try:
        finished = subprocess.run(
            command, capture_output=True, timeout=timeout, check=False
        )
    except FileNotFoundError as error:
        raise ScrcpyError(f"找不到 adb：{ADB}") from error
    except subprocess.TimeoutExpired as error:
        raise ScrcpyError(f"adb {' '.join(args)} 超时") from error

    if finished.returncode != 0:
        message = finished.stderr.decode("utf-8", "replace").strip()
        raise ScrcpyError(f"adb {' '.join(args)} 失败：{message or finished.returncode}")
    return finished.stdout.decode("utf-8", "replace")


def screen_size(serial: str | None = None) -> tuple[int, int]:
    """问设备要屏幕像素尺寸，**横屏**口径。

    `wm size` 给的是设备自然方向下的物理尺寸（手机就是竖着那个），而 Phigros 只在横屏跑，
    所以按长边当宽归一 —— 换算坐标要的是游戏实际拿到的那块画面。
    """
    output = _adb("shell", "wm", "size", serial=serial)
    override = re.search(r"Override size:\s*(\d+)x(\d+)", output)
    physical = re.search(r"Physical size:\s*(\d+)x(\d+)", output)
    found = override or physical
    if not found:
        raise ScrcpyError(f"读不出屏幕尺寸：{output.strip()!r}")
    first, second = int(found.group(1)), int(found.group(2))
    return (max(first, second), min(first, second))


class ScrcpyBackend:
    """控制通道后端。`open()` 起一次，之后每帧 `send()` 一批事件。"""

    def __init__(self, *, serial: str | None = None) -> None:
        self.serial = serial
        self.screen: Screen | None = None
        self.device = (0, 0)
        # server 那边是 Integer.parseInt(scid, 16)，**有符号** 32 位；给到 0x80000000
        # 以上它会 NumberFormatException 直接退出。所以要压在 31 位以内。
        self.scid = secrets.randbelow(0x7FFFFFFF)
        self.port = 0

        self._socket: socket.socket | None = None
        self._shell: subprocess.Popen[bytes] | None = None
        self._logs: list[str] = []
        self._drain: threading.Thread | None = None

    # ------------------------------------------------------------ 生命周期

    def open(self, screen: Screen) -> None:
        if not ADB.is_file():
            raise ScrcpyError(f"找不到 adb：{ADB}")
        if not SERVER.is_file():
            raise ScrcpyError(f"找不到 scrcpy server：{SERVER}（去 scrcpy release 里拿那个 scrcpy-server）")

        self.screen = screen
        self.device = screen_size(self.serial)

        _adb("push", str(SERVER), DEVICE_PATH, serial=self.serial, timeout=120.0)
        self.port = self._free_port()
        _adb(
            "forward",
            f"tcp:{self.port}",
            f"localabstract:{SOCKET_NAME.format(scid=self.scid)}",
            serial=self.serial,
        )
        self._shell = self._spawn_server()
        self._socket = self._connect()

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            except OSError:
                pass
            self._socket = None

        if self.port:
            try:
                _adb("forward", "--remove", f"tcp:{self.port}", serial=self.serial, timeout=10.0)
            except ScrcpyError:
                pass
            self.port = 0

        if self._shell is not None:
            self._shell.terminate()
            self._shell = None
        # app_process 未必跟着 adb shell 一起走，按类名收尾一次；模式够具体，不会误伤
        try:
            _adb("shell", "pkill", "-f", "com.genymobile.scrcpy.Server", serial=self.serial, timeout=10.0)
        except ScrcpyError:
            pass

    # ------------------------------------------------------------ 发送

    def send(self, events: Sequence[TouchEvent]) -> None:
        if self._socket is None or self.screen is None:
            raise ScrcpyError("后端还没 open()")
        width, height = self.device
        buffer = bytearray()
        for event in events:
            x, y = to_pixels(self.screen, self.device, event.x, event.y)
            pressed = event.action is not Touch.UP
            buffer += TOUCH_MESSAGE.pack(
                2,
                _ACTIONS[event.action],
                event.pointer,
                x,
                y,
                width,
                height,
                _PRESSED if pressed else _RELEASED,
                0,
                0,
            )
        if buffer:
            self._socket.sendall(bytes(buffer))

    @property
    def logs(self) -> list[str]:
        """server 的输出，出问题时用来对账。"""
        return list(self._logs)

    # ------------------------------------------------------------ 内部

    @staticmethod
    def _free_port() -> int:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            return int(probe.getsockname()[1])

    def _spawn_server(self) -> subprocess.Popen[bytes]:
        command = [str(ADB)]
        if self.serial:
            command += ["-s", self.serial]
        command += [
            "shell",
            f"CLASSPATH={DEVICE_PATH}",
            "app_process",
            "/",
            "com.genymobile.scrcpy.Server",
            VERSION,
            f"scid={self.scid:08x}",
            "log_level=info",
            "video=false",
            "audio=false",
            "tunnel_forward=true",
        ]
        process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0
        )
        # server 会一直往 stdout 写日志，不抽走的话管道满了会把它自己堵死
        self._drain = threading.Thread(target=self._drain_logs, args=(process,), daemon=True)
        self._drain.start()
        return process

    def _drain_logs(self, process: subprocess.Popen[bytes]) -> None:
        assert process.stdout is not None
        for line in process.stdout:
            self._logs.append(line.decode("utf-8", "replace").rstrip())
            del self._logs[:-200]

    def _connect(self) -> socket.socket:
        deadline = time.monotonic() + CONNECT_TIMEOUT
        last: Exception | None = None
        while time.monotonic() < deadline:
            if self._shell is not None and self._shell.poll() is not None:
                raise ScrcpyError(
                    "scrcpy server 起来就退了：\n  " + "\n  ".join(self.logs[-8:])
                )
            try:
                connection = socket.create_connection(("127.0.0.1", self.port), timeout=1.0)
            except OSError as error:
                last = error
                time.sleep(0.1)
                continue

            # 哨兵字节：server 接受连接后先写一个 0，读到它才算真的握手成功
            connection.settimeout(5.0)
            try:
                handshake = connection.recv(1)
            except OSError as error:
                connection.close()
                last = error
                time.sleep(0.1)
                continue
            if handshake != b"\x00":
                connection.close()
                last = ScrcpyError(f"握手字节不对：{handshake!r}")
                time.sleep(0.1)
                continue

            connection.settimeout(None)
            # 关掉 Nagle。控制通道是"一小撮字节、一小撮字节"地发，让它攒包会把事件攒晚
            # 几十毫秒（Windows 上尤其），触控的同步就全废了。
            connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return connection

        raise ScrcpyError(f"连不上 scrcpy server（127.0.0.1:{self.port}）：{last}")


BACKEND = ScrcpyBackend
"""注册表按这个名字取类（见 ``backends/registry.py``）。"""


def available() -> str | None:
    """探测能不能用；能用返回 None，否则返回原因（不抛异常，方便上层降级）。"""
    if not ADB.is_file():
        return f"找不到 adb：{ADB}"
    if not SERVER.is_file():
        return f"找不到 scrcpy server：{SERVER}"
    return None


if __name__ == "__main__":
    # 直接跑这个文件是不行的（包内相对 import），要这样：
    #     python -m backends.scrcpy
    reason = available()
    print(reason or f"scrcpy 后端就绪：adb={ADB} server={SERVER} ({VERSION})")
    if reason is None:
        try:
            print(f"    当前设备屏幕：{screen_size()}")
        except ScrcpyError as error:
            print(f"    读屏幕尺寸失败（设备没连？）：{error}")
