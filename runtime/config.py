"""项目内的固定常量，以及**落盘的那份配置**。

两种东西放在一起，是因为它们是同一件事的两面：哪些是"默认值"（写死在这里），哪些是
"用户改过的"（覆盖在 `config.json` 里）。判断标准很简单 —— **删掉 `config.json` 应该
回到刚 clone 下来的状态**，所以它不进版本库（见 `.gitignore`）。

哪些不进 config.json，以及为什么
--------------------------------
``inject`` / ``verbose`` 只活在会话里：

* 一个持久化的 ``inject off`` 会让你下一次以为正在打歌，其实一根手指都没发出去 ——
  这种"配置留下的坑"比多敲一次命令贵得多；
* ``verbose`` 是排障开关，开着一局几百条 Perfect，没有理由跨会话记住。

``spawn`` / ``attach`` 也不落盘：它们是**一次动作**，不是设置。下次启动要不要注入，
由你再打一次决定。
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path

from algorithms import DEFAULT_PLANNER
from .output import log

ROOT = Path(__file__).resolve().parent.parent
"""项目根目录。"""

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
    """这一次运行的日志文件：``logs/2026-02-14_13-45-02.log``。

    为什么一次一份而不是覆盖同一个文件：出问题的时候你要比的是"这次和上次有什么不一样"，
    而覆盖恰好把上一次抹掉了 —— 那是最需要它的时候。
    """
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

    backend: str = "scrcpy"
    """用哪个触控后端。换的时候当场重开。"""

    latency: float = 0.0
    """注入链路的手工补偿（秒），正数 = 提前发。立刻生效。"""

    auto_latency: bool = True
    """每一局**完整打完**之后，用这一局 Perfect 的中位数自动校准 `latency` 并落盘。

    为什么要它：我们发出触摸、游戏在**下一帧**才处理（实测中位 +29ms），所以设备那边量到的
    早晚量会稳定偏正。这不是"计划错了"，是送达晚了一帧 —— 该由延迟补偿吃掉，而且该自动吃。

    手动 `latency <值>` 会把它关掉（见 `console.py`）：人一旦自己定了数，就别再让机器改。
    """

    cache: bool = True
    """把 `plans/` 里的规划结果当缓存用：命中就不重算。关掉 = 既不吃也不写。"""

    save_chart: bool = False
    """把采集到的谱面原文存到 `charts/`。"""

    log: bool = True
    """把这一次运行的输出抄一份到 `logs/<时间>.log`。

    默认开着：这东西的价值全在"出事之后" —— 真出问题时再让人记得打开就晚了。代价只是
    一次运行一个文本文件（我们的输出量很小），所以不必省。
    """

    # ------------------------------------------------------------ 读写

    @classmethod
    def load(cls, path: Path | None = None) -> Config:
        """读配置；**不存在就按默认值写一份**，坏了就备份成 `.bak` 再来一份。

        为什么缺文件时要写出来而不是静默用默认值：让人看得见"能改什么"。一个空目录里
        跑一次 `python main.py` 就该多出一个可以照着改的 `config.json`。
        """
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
