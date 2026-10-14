"""规划器注册表 —— "支持多种规划器"的全部机关。

注册表只登记"叫什么、在哪个模块、干什么用的"，**不** import 它们：真正的 import 发生在
:func:`create` / :func:`parameters`，所以没被选中的规划器连同依赖都不会被加载。

加一个新规划器：写一个模块（里面放 ``PLANNER`` 与 ``CONFIG``），到 ``_BUILTIN`` 里登记一行。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Mapping

if TYPE_CHECKING:
    from .utils import Parameter, Planner


@dataclass(frozen=True, slots=True)
class PlannerInfo:
    name: str
    module: str
    summary: str


_BUILTIN: tuple[PlannerInfo, ...] = (
    PlannerInfo(
        name="conservative",
        module="algorithms.conservative",
        summary="保守算法：整 note 处理，flick / hold 拆成连续手势，最稳但吃手指",
    ),
    PlannerInfo(
        name="radical",
        module="algorithms.radical",
        summary="激进算法：1ms 时间栅格上贪心复用指针，hold 退化为 tap + drag",
    ),
    PlannerInfo(
        name="geometric",
        module="algorithms.geometric",
        summary="几何算法：125Hz 帧，按判定区求交合并，最省手指",
    ),
)

DEFAULT_PLANNER = "conservative"

_registry: dict[str, PlannerInfo] = {info.name: info for info in _BUILTIN}


def register(info: PlannerInfo) -> None:
    """登记一个额外的规划器。同名会覆盖。"""
    _registry[info.name] = info


def catalog() -> list[PlannerInfo]:
    return [_registry[name] for name in sorted(_registry)]


def names() -> list[str]:
    return sorted(_registry)


def create(name: str, options: Mapping[str, Any] | None = None) -> Planner:
    """按名字造一个规划器，并把参数覆盖项交给它。"""
    return _module(name).PLANNER(options)


def parameters(name: str) -> tuple[Parameter, ...]:
    """某个规划器能调什么（名字、默认值、说明）。"""
    from .utils import parameters as describe

    return describe(_module(name).CONFIG)


def validate(name: str, options: Mapping[str, Any]) -> None:
    """按规划器自己的参数表校验一遍覆盖项：名字写错、类型不对都当场报错。"""
    from .utils import build_options

    build_options(_module(name).CONFIG, options)


def _module(name: str) -> Any:
    try:
        info = _registry[name]
    except KeyError:
        raise KeyError(f"未知的规划器 {name!r}；可用：{'、'.join(names())}") from None
    return importlib.import_module(info.module)
