#!/usr/bin/env python3
"""auto_phigros：**控制台就是主干**。

    python src/main.py

没有命令行参数 —— 一切都在控制台里做，而且能记住：

    devices            列出设备（USB、本机、远程 server）
    device <id>        选中一台（记住）；只有一台时会自动选中
    spawn              在选中的设备上启动游戏并注入
    attach [pid]       附加到已经在跑的游戏（pid 可选，名字对不上时用它）
    planner / latency / backend / cache / save-chart / host …
    status / help / quit

分工是：

* ``console.py`` —— 主干：读一行、执行一行（跑在**主线程**上，所以 Ctrl+C 天然落在它身上）
* ``controller.py`` —— 这一把的全部家当（设备、后端、时钟、agent、播放器、收工）
* ``agent.py`` —— 一次注入的全都：会话、闸门、消息分发、判决对账、结算
* ``config.py`` —— 固定常量 + 落盘的 ``config.json``（不存在就按默认值建一份）

规划与触控仍然是独立模块（``planner.py`` / ``touch.py`` / ``render.py`` / ``judge.py``），
都能单独跑。
"""

from __future__ import annotations

import sys

from runtime.config import Config, log_file_path
from runtime.console import Console
from runtime.controller import Controller
from runtime.options import Options
from runtime.output import log, log_to_file, stop_file_log


def main(config: Config | None = None) -> int:
    """起主干。``config`` 只给自检用（避免自检去动真的 config.json）。"""
    config = Config.load() if config is None else config
    if config.log:
        start_logging()
    controller = Controller(config, Options(planner=config.planner, latency=config.latency))
    try:
        controller.start()
        Console(controller).run()
    except KeyboardInterrupt:
        # 主干就在读输入的那条线程上：Ctrl+C 落在这里，和 quit 走同一条收工路
        pass
    finally:
        controller.shutdown()
        stop_file_log()
    return 0


def start_logging() -> None:
    """把这一次运行的输出抄进 ``logs/<时间>.log``，并**把路径念出来**。

    念出来是要紧的：日志的价值在"出事之后"，那时候人得知道去哪找它，而不是回头翻代码
    猜文件名规则。
    """
    path = log_file_path()
    if log_to_file(path):
        log(f"[main] 日志：{path}")
    else:
        log(f"[main] 日志文件建不起来（{path}），这一趟只写屏幕", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        # **没有命令行参数**（一切都在控制台里，见模块开头）。但收到参数时不能默默起主干：
        # `python src/main.py --help` 会把终端交给一个正在读 stdin 的控制台循环，看着就像卡死了
        # —— 我自己就这么踩过一次（进程挂在 `main.py --help` 上，等输入）。
        print(
            f"main.py 没有命令行参数（收到的是 {' '.join(sys.argv[1:])}）。\n"
            f"直接 `python src/main.py`，跑起来之后在控制台里打 `help` 看有哪些命令。",
            file=sys.stderr,
        )
        sys.exit(2)
    sys.exit(main())
