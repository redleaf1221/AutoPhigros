#!/usr/bin/env python3
"""进程里**唯一**的那个写者。

主干、agent 的消息处理和运行时控制台都往这里写 —— :func:`log` 的用法与 ``print``
一模一样（``file=`` 之类照常透传）。之所以要统一，是因为控制台的提示符 ``auto> `` 是
**不带换行**地画在屏幕上的：别的线程一旦直接 ``print``，那行输出就接在提示符后面，
看起来就像"提示符没了"。写者只有一个，才谈得上"打印前先擦掉、打印完再画回来"。

控制台通过 :func:`set_prompt_hooks` 把这两件事登记进来；没有控制台的时候（``touch.py``
单独跑、自检里）它就只是个 ``print``。

:func:`log_to_file` 把**到达屏幕的一切**再抄一份到文件（不是只抄 :func:`log` 走过的）：
开发时要看的是"屏幕上看见过什么"，`scrcpy.py` 那种直接 ``print`` 的也得在里面。抄写按
**行的语义**来 —— 只写完整行，而且一行里只保留最后一个 ``\\r`` 之后的内容（终端的显示
规则），于是提示符的擦除/重画不会把日志文件弄得满是回车和空白。
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Callable

_LOCK = threading.Lock()
_clear_prompt: Callable[[], None] | None = None
_draw_prompt: Callable[[], None] | None = None

_file: "LogFile | None" = None
"""当前这份日志文件；None = 没在记。"""

PENDING_LIMIT = 8192
"""没等到换行的残片最多攒这么多字符（一直不换行的输出不该把内存吃光）。"""


def set_prompt_hooks(
    clear: Callable[[], None] | None = None, draw: Callable[[], None] | None = None
) -> None:
    """登记（或传 None 注销）提示符的擦除与重画。控制台启动时登记、线程退出时注销。"""
    global _clear_prompt, _draw_prompt
    with _LOCK:
        _clear_prompt, _draw_prompt = clear, draw


def log(*args: object, **kwargs: object) -> None:
    """一整行原子地写出去，并且保证写完提示符还在。

    **默认 ``flush=True``。** 不 flush 的话，输出攒在哪里、什么时候露面完全由 stdout
    是什么决定（终端是行缓冲、管道是块缓冲）—— 表现就是"敲了一条命令没反应，再敲一条，
    上一条的输出才出来"。输出必须当场出去，这件事不能赌缓冲策略。
    """
    kwargs.setdefault("flush", True)
    with _LOCK:
        clear, draw = _clear_prompt, _draw_prompt
        if clear is not None:
            clear()
        print(*args, **kwargs)
        if draw is not None:
            draw()


class LogFile:
    """行缓冲的抄写目标。屏幕上的东西按终端的规则还原成一行行文本。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        # `newline="\n"`：日志文件一律 LF 行尾。Windows 的文本模式会把 \n 翻成 \r\n，
        # 于是"文件里除了行尾不该有别的回车"这条判据就没法查了（自检按字节读才发现）。
        # 而且 LF 的日志跨工具都省事：grep / diff / 直接喂给别的脚本。
        self.stream = path.open("a", encoding="utf-8", newline="\n")
        self._pending = ""

    def write(self, text: str) -> None:
        self._pending += text
        if len(self._pending) > PENDING_LIMIT:
            # 一直不换行的输出（比如某个库在画进度条）：只留尾巴，别把内存吃光
            self._pending = self._pending[-PENDING_LIMIT:]
        while "\n" in self._pending:
            line, self._pending = self._pending.split("\n", 1)
            # 终端的规则：一行里只有最后一个 \r 之后的部分是"看得见"的。
            # 提示符的擦除（\r + 空格 + \r）因此不会进日志。
            self.stream.write(line.rsplit("\r", 1)[-1] + "\n")
        self.stream.flush()  # 每行都落盘：崩溃现场才是日志最有用的时候

    def flush(self) -> None:
        self.stream.flush()

    def close(self) -> None:
        try:
            self.stream.close()
        except OSError:
            pass


class _Tee:
    """把 ``sys.stdout`` / ``sys.stderr`` 包起来：写屏幕的同时抄给日志文件。"""

    def __init__(self, stream: object, sink: LogFile) -> None:
        self._stream = stream
        self._sink = sink

    def write(self, text: str) -> int:
        written = self._stream.write(text)  # type: ignore[attr-defined]
        try:
            self._sink.write(text)
        except (OSError, ValueError):
            pass  # 日志写不进去不该把程序带崩：屏幕上那份照旧
        return written

    def flush(self) -> None:
        self._stream.flush()  # type: ignore[attr-defined]
        self._sink.flush()

    def __getattr__(self, name: str) -> object:
        # isatty / encoding / fileno 这些照旧问真身 —— 控制台靠 isatty 判断交互模式
        return getattr(self._stream, name)


def log_to_file(path: Path) -> bool:
    """开始把输出抄到 ``path``（追加）。已经开着就换一份。返回是否真的开起来了。

    开不起来（目录没权限、磁盘满）只回 False，不抛：日志是辅助，不该拦住干活。
    """
    global _file
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        sink = LogFile(path)
    except OSError:
        return False
    with _LOCK:
        _file = sink
        sys.stdout = _Tee(sys.stdout, sink)  # type: ignore[assignment]
        sys.stderr = _Tee(sys.stderr, sink)  # type: ignore[assignment]
    return True


def stop_file_log() -> None:
    """停止抄写，并把 ``sys.stdout`` / ``sys.stderr`` 还原成真身。"""
    global _file
    with _LOCK:
        if _file is None:
            return
        if isinstance(sys.stdout, _Tee):
            sys.stdout = sys.stdout._stream  # type: ignore[assignment]  # noqa: SLF001
        if isinstance(sys.stderr, _Tee):
            sys.stderr = sys.stderr._stream  # type: ignore[assignment]  # noqa: SLF001
        _file.close()
        _file = None


def log_path() -> Path | None:
    """现在记在哪份文件里（没在记就是 None）。"""
    return None if _file is None else _file.path
