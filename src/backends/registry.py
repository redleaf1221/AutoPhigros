"""后端注册表 —— "支持多种后端"的全部机关。

和 ``algorithms/registry.py`` 一个套路：注册表只登记"后端叫什么、在哪个模块、干什么用的"，
**不** import 它们；真正的 import 发生在 :func:`create`，所以没被选中的后端连同它的依赖
（比如 scrcpy 那个只在这里用到的 ``socket``）都不会被加载。

要加一个后端，写一个模块、在里面放一个类并赋值给 ``BACKEND``，再到 ``_BUILTIN`` 里
登记一行即可；外部插件可以调 :func:`register`。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .utils import Backend


@dataclass(frozen=True, slots=True)
class BackendInfo:
    name: str
    module: str
    summary: str


_BUILTIN: tuple[BackendInfo, ...] = (
    BackendInfo(
        name="scrcpy",
        module="backends.scrcpy",
        summary="scrcpy 控制协议：走它的控制通道注入多点触控（目前唯一的真后端）",
    ),
    BackendInfo(
        name="recording",
        module="backends.recording",
        summary="干跑：不连设备，只把发出去的记下来（自检与试调度器用）",
    ),
)

DEFAULT_BACKEND = "scrcpy"

_registry: dict[str, BackendInfo] = {info.name: info for info in _BUILTIN}


def register(info: BackendInfo) -> None:
    """登记一个额外的后端。同名会覆盖。"""
    _registry[info.name] = info


def catalog() -> list[BackendInfo]:
    return [_registry[name] for name in sorted(_registry)]


def create(name: str = DEFAULT_BACKEND, **kwargs: Any) -> Backend:
    try:
        info = _registry[name]
    except KeyError:
        available = ", ".join(sorted(_registry))
        raise KeyError(f"未知的后端 {name!r}；可用：{available}") from None
    module = importlib.import_module(info.module)
    return module.BACKEND(**kwargs)  # type: ignore[no-any-return]
