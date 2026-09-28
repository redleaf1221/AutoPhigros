#!/usr/bin/env python3
"""把一份 npz 读成 JSON —— 谱面原文、规划结果、meta 都能看。

    python src/npz.py charts/0002_Glaciaxion.SunsetRay.0_HD_93215ea2.npz
    python src/npz.py plans/xxx_radical.npz --member meta
    python src/npz.py charts/xxx.npz --member chart -o chart.json    # 取出谱面原文
    python src/npz.py plans/xxx.npz --compact

谱面与规划结果都是二进制 npz，所以总得有个不开 Python 也能把它们看成人话的东西。
本模块只是 ``formats.storage.read_npz`` 的一层壳：**哪一段是什么**由那边定义，
这里只负责读出来、打成 JSON。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from formats.storage import NpzFormatError, read_npz


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="npz.py", description="auto_phigros 工具：把 npz 读成 JSON"
    )
    parser.add_argument("path", type=Path, help="谱面或规划结果的 .npz")
    parser.add_argument("--member", default=None, help="只要这一段（比如 meta / chart / event_xy）")
    parser.add_argument("-o", "--out", type=Path, default=None, help="写到文件（默认打到屏幕）")
    parser.add_argument("--compact", action="store_true", help="压成一行（默认缩进 2 格）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        values = read_npz(args.path)
    except (OSError, NpzFormatError) as error:
        print(f"[npz] 读不了 {args.path}：{error}", file=sys.stderr)
        return 2

    if args.member is not None:
        if args.member not in values:
            print(
                f"[npz] {args.path.name} 里没有 {args.member}；它有：{', '.join(values)}",
                file=sys.stderr,
            )
            return 2
        values = values[args.member]

    text = json.dumps(values, ensure_ascii=False, indent=None if args.compact else 2)
    if args.out is None:
        print(text)
    else:
        args.out.write_text(text, encoding="utf-8")
        print(f"[npz] 已写出 {args.out}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        # 下游提前把管道关了（`| head`、`| jq`、编辑器的预览窗）：不是错误，安静收工
        # 缓冲里剩下的输出改道到 devnull，免得解释器退出时再冲一次管道、又抛一遍
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        sys.exit(0)
