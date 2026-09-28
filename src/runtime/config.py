"""项目内的固定常量，以及**落盘的那份配置**。

写死在这里的是"默认值"，用户改过的覆盖在 `config.json` 里 —— **删掉它就回到刚 clone 的
状态**，所以它不进版本库。``inject`` / ``verbose`` 只在会话里活着（一个持久化的
``inject off`` 会让人下次以为在打歌），它们是 ``options.py`` 的事。
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any

from algorithms import DEFAULT_PLANNER
from .output import log

ROOT = Path(__file__).resolve().parents[2]
"""项目根目录 —— ``src/`` 的上一层（本文件在 ``src/runtime/`` 里）。"""

CONFIG_PATH = ROOT / "config.json"
"""落盘配置的位置。"""

AGENT = ROOT / "target" / "_.js"
"""frida agent（`npm run build` 的产物）。"""

CHARTS_DIR = ROOT / "charts"
"""采集到的谱面。"""

PLANS_DIR = ROOT / "plans"
"""规划结果 —— 落盘的规划同时就是缓存。"""

LOGS_DIR = ROOT / "logs"
"""运行日志。一次运行一份，按文件名排序就是历史。"""


def log_file_path(moment: datetime | None = None) -> Path:
    """这一次运行的日志文件：``logs/2026-02-14_13-45-02.log``。"""
    stamp = (moment or datetime.now()).strftime("%Y-%m-%d_%H-%M-%S")
    return LOGS_DIR / f"{stamp}.log"

PACKAGE = "com.PigeonGames.Phigros"
"""游戏包名。"""


@dataclass(slots=True)
class Config:
    """`config.json` 的内容。字段即语义，改它就是改行为。"""

    device: str | None = None
    """选中的设备 id（`frida.get_device_manager()` 里那个 id：USB 下就是 adb 序列号）。"""

    hosts: list[str] = field(default_factory=list)
    """手动加过的远程 frida-server 地址（`host add 192.168.1.10:27042`）。"""

    planner: str = DEFAULT_PLANNER
    """用哪个规划器。下一关生效（规划本来就在开谱那一刻做）。"""

    planner_options: dict[str, dict[str, Any]] = field(default_factory=dict)
    """各规划器的参数覆盖项：``{"radical": {"flick_repeats": 1}}``。

    没写的参数用规划器自己的默认值。控制台 ``option`` 改的就是这里，改了之后那份规划结果
    的缓存自动失效（参数算进 cache_key）。
    """

    backend: str = "scrcpy"
    """用哪个触控后端。换的时候当场重开。"""

    latency: float = 0.0
    """注入链路的手工补偿（秒），正数 = 提前发。立刻生效。"""

    auto_latency: bool = True
    """每一局**完整打完**之后，用这一局 Perfect 的中位数自动校准 `latency` 并落盘。"""

    cache: bool = True
    """把 `plans/` 里的规划结果当缓存用：命中就不重算。关掉 = 既不吃也不写。"""

    save_chart: bool = False
    """把采集到的谱面原文存到 `charts/`。"""

    log: bool = True
    """把这一次运行的输出抄一份到 `logs/<时间>.log`。"""

    # ------------------------------------------------------------ 读写

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        """读配置；不存在就按默认值写一份，坏了就备份成 `.bak` 再来一份。"""
        path = CONFIG_PATH if path is None else path
        if not path.is_file():
            config = cls()
            config.save(path)
            log(f"[config] 没有 {path.name}，已按默认值新建")
            return config

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            backup = path.with_suffix(path.suffix + ".bak")
            path.replace(backup)
            log(f"[config] {path.name} 读不了（{error}），已备份成 {backup.name} 并重建", file=sys.stderr)
            config = cls()
            config.save(path)
            return config

        if not isinstance(raw, dict):
            log(f"[config] {path.name} 的根节点不是对象，按默认值处理", file=sys.stderr)
            raw = {}

        known = {item.name for item in fields(cls)}
        unknown = sorted(set(raw) - known)
        if unknown:
            log(f"[config] 忽略不认识的字段：{'、'.join(unknown)}", file=sys.stderr)

        values = {name: raw[name] for name in known if name in raw}
        try:
            config = cls(**values)
        except TypeError as error:
            log(f"[config] 字段类型不对（{error}），按默认值处理", file=sys.stderr)
            return cls()
        return config

    def save(self, path: Path | None = None) -> None:
        """写回。**失败不抛** —— 配置存不下来不该把正在打的歌打断。"""
        path = CONFIG_PATH if path is None else path
        try:
            path.write_text(
                json.dumps(asdict(self), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        except OSError as error:
            log(f"[config] 写 {path.name} 失败：{error}", file=sys.stderr)
