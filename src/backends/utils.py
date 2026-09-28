"""触控后端的契约层。

后端只认一件事：把一批**虚拟屏幕坐标**下的触控事件送出去 —— 送进 scrcpy 的控制通道、
送进 frida、还是只记在本子里，是各自的实现细节。本模块只依赖 ``algorithms`` 的数据类型
（`Screen` / `TouchEvent`）；具体后端由 ``registry.py`` 按名字现 import。
"""

from __future__ import annotations

from typing import Protocol, Sequence

from algorithms.geometry import Screen
from algorithms.utils import TouchEvent


class Backend(Protocol):
    """一个能收触控事件的东西。主干整个会话开一次、跨关复用（起后端要一两秒）。"""

    def open(self, screen: Screen) -> None:
        """开一条通道。`screen` 是规划结果所在的那块虚拟屏（官谱 16x9）。"""
        ...

    def close(self) -> None:
        ...

    def send(self, events: Sequence[TouchEvent]) -> None:
        """把**同一时刻**的一批事件发出去。

        一批一次调用：一帧里几个手指要同时按下，攒成一个缓冲区一次写出去才不会被调度分开。
        """
        ...


def to_pixels(screen: Screen, device: tuple[int, int], x: float, y: float) -> tuple[int, int]:
    """虚拟屏幕坐标 → 设备像素坐标。真后端都要用它。

    ``px = 屏宽/2 + (x/虚拟宽 − 0.5) * A * 屏高``，``py = 屏高 * (1 − y/虚拟高)``，
    ``A = min(屏宽/屏高, 16/9)``：设备比 16:9 更宽时画面左右留黑边。
    """
    width, height = device
    aspect = min(width / height, 16 / 9)  # 游戏相机半高 5 个世界单位，横向 [-5A, 5A]
    px = width / 2 + (x / screen.width - 0.5) * aspect * height
    py = height * (1 - y / screen.height)  # Android 的 y 轴朝下，虚拟屏朝上
    return (
        min(max(int(round(px)), 0), width - 1),
        min(max(int(round(py)), 0), height - 1),
    )
