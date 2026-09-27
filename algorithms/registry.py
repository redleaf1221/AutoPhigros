"""规划器注册表 —— "支持多种规划器"的全部机关。

注册表只登记"规划器叫什么、在哪个模块、干什么用的"，**不** import 它们。
真正的 import 发生在 :func:`create`，所以没被选中的规划器连同它自己的依赖都不会被加载。

要加一个新规划器，写一个模块、在里面放一个可调用的 ``PLANNER``，再到
``_BUILTIN`` 里登记一行即可；外部插件可以调 :func:`register`。
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .utils import Planner


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


def create(name: str) -> Planner:
    try:
        info = _registry[name]
    except KeyError:
        available = ", ".join(sorted(_registry))
        raise KeyError(f"未知的规划器 {name!r}；可用：{available}") from None
    module = importlib.import_module(info.module)
    return module.PLANNER()
