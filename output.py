#!/usr/bin/env python3
"""进程里**唯一**的那个写者。

主干、agent 的消息处理和运行时控制台都往这里写 —— :func:`log` 的用法与 ``print``
一模一样（``file=`` 之类照常透传）。之所以要统一，是因为控制台的提示符 ``auto> `` 是
**不带换行**地画在屏幕上的：别的线程一旦直接 ``print``，那行输出就接在提示符后面，
看起来就像"提示符没了"。写者只有一个，才谈得上"打印前先擦掉、打印完再画回来"。

控制台通过 :func:`set_prompt_hooks` 把这两件事登记进来；没有控制台的时候（``touch.py``
单独跑、自检里）它就只是个 ``print``。
"""

from __future__ import annotations

import threading
from typing import Callable

_LOCK = threading.Lock()
_clear_prompt: Callable[[], None] | None = None
_draw_prompt: Callable[[], None] | None = None


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
