"""配置自检：`config.json` 的读写、修复、以及"落盘的边界"。

这一组盯的是"**删掉 config.json 就该回到刚 clone 的状态**"这条规矩：读不出来的文件要
备份重来而不是炸掉、不认识的字段要吭声、代码里的默认值要真的当默认值。另外顺手钉住另一头：
`inject` / `verbose` **不许**出现在落盘字段里 —— 一个持久化的 `inject off` 会让人下一次
以为在打歌、其实一根手指都没发出去。
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
from dataclasses import fields
from pathlib import Path

from runtime import config as settings


def check_config() -> list[str]:
    problems: list[str] = []
    quiet = (contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()))

    def expect(condition: bool, complaint: str) -> None:
        if not condition:
            problems.append(complaint)

    with contextlib.ExitStack() as stack:
        for manager in quiet:
            stack.enter_context(manager)
        workspace = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        path = workspace / "config.json"
        original = settings.CONFIG_PATH
        settings.CONFIG_PATH = path
        stack.callback(setattr, settings, "CONFIG_PATH", original)

        # 1) 不存在就按默认值建一份
        fresh = settings.Config.load()
        expect(path.is_file(), "config.json 不存在时应当按默认值建一份出来")
        expect(fresh.device is None, "默认不该预选设备")
        expect(fresh.planner, "默认规划器不该是空的")

        # 2) 往返
        fresh.device = "ABC123"
        fresh.latency = 0.02
        fresh.cache = False
        fresh.hosts = ["192.168.1.10:27042"]
        fresh.save()
        again = settings.Config.load()
        for name in ("device", "latency", "cache", "hosts", "planner", "backend", "save_chart"):
            expect(
                getattr(again, name) == getattr(fresh, name),
                f"{name} 往返之后变了：{getattr(again, name)!r} != {getattr(fresh, name)!r}",
            )

        # 3) 坏文件：备份 + 重建，不许炸
        path.write_text("{ 这不是 json", encoding="utf-8")
        repaired = settings.Config.load()
        expect((workspace / "config.json.bak").is_file(), "读不了的文件应当先备份成 .bak")
        expect(repaired.device is None, "备份重建之后该回到默认值")
        expect(path.is_file(), "重建之后 config.json 该在")

        # 4) 不认识的字段：忽略，但别的字段照收
        path.write_text(
            json.dumps({"device": "X", "latency": 0.05, "没这个字段": 1}), encoding="utf-8"
        )
        loaded = settings.Config.load()
        expect(loaded.device == "X" and abs(loaded.latency - 0.05) < 1e-9, "认识的字段被连累丢掉了")

        # 5) 会话内的开关不许落盘
        names = {item.name for item in fields(settings.Config)}
        for session_only in ("inject", "verbose"):
            expect(
                session_only not in names,
                f"{session_only} 不该落盘：一个持久化的它会在下一次悄悄改变行为",
            )

    return problems
