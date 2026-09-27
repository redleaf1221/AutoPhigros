"""与 `agent.py` / `controller.py` 打交道时的替身。

`agent.Agent` 只要一个"能 post、能 ping 的对象"，`controller.Controller` 只要一份配置
—— 顶掉它们就能在没有设备、没有 frida 会话的情况下把闸门、探活、收工、附加目标这几条路
走一遍。`make_config()` 出来的配置**不会落盘**（自检不该动真的 config.json）。
"""

from __future__ import annotations

from runtime.config import Config
from runtime.controller import Controller
from runtime.options import Options


class Recorder:
    """顶掉 frida 的 script，只记下 post 出去的东西。"""

    def __init__(self) -> None:
        self.posted: list[dict] = []

    def post(self, message: dict) -> None:
        self.posted.append(message)

    @property
    def released(self) -> list[int]:
        return [int(message.get("payload", {}).get("seq", 0)) for message in self.posted]


def make_config(**overrides: object) -> Config:
    """自检用的配置。落盘的去向由各处的 `config.CONFIG_PATH` 替换成临时目录。"""
    return Config(**overrides)  # type: ignore[arg-type]


def make_controller(config: Config | None = None, options: Options | None = None) -> Controller:
    """一份谁都不连的 Controller：没有设备、没有 agent、没有后端。"""
    return Controller(config if config is not None else make_config(), options)
