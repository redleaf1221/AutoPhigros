"""触控后端的契约层。

后端只认一件事：把一批**虚拟屏幕坐标**下的触控事件送出去。至于送进 scrcpy 的
控制通道、送进 frida、还是只是记在本子里，是各自的实现细节。

本模块只依赖 ``algorithms`` 里的数据类型（`Screen` / `TouchEvent`），不 import 任何
具体后端 —— 注册表按名字现 import，见 `registry.py`。
"""

from __future__ import annotations

from typing import Protocol, Sequence

from algorithms.geometry import Screen
from algorithms.utils import TouchEvent


class Backend(Protocol):
    """一个能收触控事件的东西。

    寿命由调用方管：主干整个会话开一次、跨关复用（起 scrcpy server 要一两秒，
    不能一关开一次）。
    """

    def open(self, screen: Screen) -> None:
        """开一条通道。`screen` 是规划结果所在的那块虚拟屏（官谱 16x9）。"""
        ...

    def close(self) -> None:
        ...

    def send(self, events: Sequence[TouchEvent]) -> None:
        """把**同一时刻**的一批事件发出去。

        一批一次调用（而不是一个事件一次）是有意的：一帧里几个手指要同时按下，
        攒成一个缓冲区一次写出去，能少几次系统调用、也少几次被调度打断的机会。
        """
        ...


def to_pixels(screen: Screen, device: tuple[int, int], x: float, y: float) -> tuple[int, int]:
    """虚拟屏幕坐标 → 设备像素坐标。真后端都要用它。

    游戏相机是"半高 5 个世界单位"，世界横向范围 `[-5A, 5A]`、`A = min(宽高比, 16/9)`。
    设备比 16:9 更宽时画面左右留黑边（`A` 被 16/9 卡住），所以换算要带上 `A`：

    .. code-block:: text

        px = 屏宽/2 + (虚拟x/16 − 0.5) * A * 屏高
        py = 屏高 * (1 − 虚拟y/9)          # Android 的 y 轴朝下，虚拟屏朝上

    更窄的设备上 `A = 屏宽/屏高`，化简之后就是干干净净的 `px = 虚拟x/16 * 屏宽`。
    """
    width, height = device
    aspect = min(width / height, 16 / 9)
    px = width / 2 + (x / screen.width - 0.5) * aspect * height
    py = height * (1 - y / screen.height)
    return (
        min(max(int(round(px)), 0), width - 1),
        min(max(int(round(py)), 0), height - 1),
    )
