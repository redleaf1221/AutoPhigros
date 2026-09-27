"""与 `main.py` 打交道时的替身。

`main.py` 的 `Agent` 只要一个"能 post、能 ping 的对象"，`Controller` 只要几个命令行字段
—— 顶掉它们就能在没有设备、没有 frida 会话的情况下把闸门、探活、收工这几条路走一遍。"""

from __future__ import annotations

import argparse


class Recorder:
    """顶掉 frida 的 script，只记下 post 出去的东西。"""

    def __init__(self) -> None:
        self.posted: list[dict] = []

    def post(self, message: dict) -> None:
        self.posted.append(message)

    @property
    def released(self) -> list[int]:
        return [int(message.get("payload", {}).get("seq", 0)) for message in self.posted]


def _trunk_args(**overrides) -> argparse.Namespace:
    """``Controller`` 真正会读的那几个命令行参数 —— 就这几个，多一个都不给。"""
    fields = {
        "planner": "stub",
        "latency": 0.0,
        "save_chart": False,
        "cache": True,
    }
    return argparse.Namespace(**{**fields, **overrides})

