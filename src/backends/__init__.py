"""触控后端：把规划出来的触控事件送到设备上。

本包只提供"按名字造一个后端"的能力，不提供命令行 —— 上层入口是 ``touch.py``
（它既被 ``main.py`` 调用，也能单独跑，`--backend` 就是这里的名字）。
"""

from .registry import DEFAULT_BACKEND, BackendInfo, catalog, create, register
from .utils import Backend, to_pixels

__all__ = [
    "DEFAULT_BACKEND",
    "Backend",
    "BackendInfo",
    "catalog",
    "create",
    "register",
    "to_pixels",
]
