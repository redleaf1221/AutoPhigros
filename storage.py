"""规划结果的编解码，以及调试用的落盘。

三个调用方各取所需：``main.py``（frida 主干）用 :func:`save_chart` 存 agent 回传的
谱面原文，``planner.py`` 用 :func:`save_plan` / :func:`plan_path_for` 存与查缓存，
``touch.py`` 用 :func:`decode_plan` 读回规划结果发给设备。三者共用 :class:`ChartRef`
拼文件名 —— 同一张谱面采出来的谱面文件、规划结果与缓存，文件名前缀必然对得上。

``.psap`` 是规划结果的二进制格式。光有写入没有读回的格式不算格式，所以
:func:`decode_plan` 也在同一个文件里。
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from algorithms.geometry import Screen
from algorithms.utils import PlanResult, Touch, TouchEvent

PLAN_MAGIC = b"APSP"
PLAN_VERSION = 1

_HEADER = struct.Struct("!4sBBddI")  # magic, 版本, 名字长度, 屏宽, 屏高, 帧数
_FRAME = struct.Struct("!qH")  # 时间戳(ms), 事件数
_EVENT = struct.Struct("!BIdd")  # 动作, 指针号, x, y


@dataclass(frozen=True, slots=True)
class ChartRef:
    """一张谱面的身份：来源上下文 + 内容哈希（``seq`` 只是元数据）。

    **身份里不带 ``seq``。** 它是 agent 的会话内计数器（这一局进程里解析的第几张谱面），
    换个会话同一张谱面就会拿到另一个号 —— 把它拼进文件名，缓存就永远对不上：
    实测同一张 Glaciaxion HD 攒出过 0001/0002/0003 三份，而第二次开谱照样得在闸门里
    现算几十秒。``seq`` 仍然写进 meta 供查账，但不参与命名。
    """

    seq: int = 0
    context: Mapping[str, Any] = field(default_factory=dict)
    digest: str = "unknown"

    @property
    def stem(self) -> str:
        return (
            f"{sanitize(self.context.get('songsId'))}_"
            f"{sanitize(self.context.get('songsLevel'), 'NA')}_{self.digest}"
        )

    @classmethod
    def of_text(
        cls, text: str, *, seq: int = 0, context: Mapping[str, Any] | None = None
    ) -> ChartRef:
        """没有 agent 给的哈希时（比如直接拿一个谱面文件跑）现算一个。"""
        return cls(seq, dict(context or {}), hashlib.sha1(text.encode("utf-8")).hexdigest()[:8])


def sanitize(value: str | None, fallback: str = "unknown") -> str:
    """把歌曲 id / 难度名变成安全的文件名片段。"""
    if not value:
        return fallback
    cleaned = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", value).strip("_")
    return cleaned[:64] or fallback


# --------------------------------------------------------------- 落盘


def save_chart(
    text: str,
    ref: ChartRef,
    directory: Path,
    *,
    notes_in_json: int | None = None,
    notes_reported: int | None = None,
) -> Path:
    """写下谱面原文与来源信息。``notes_reported`` 是游戏自己数出来的音符数。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    payload_path = directory / f"{ref.stem}.json"
    payload_path.write_text(text, encoding="utf-8")

    meta: dict[str, Any] = {
        "seq": ref.seq,
        "hash": ref.digest,
        "chars": len(text),
        "context": dict(ref.context),
        "payload_file": payload_path.name,
        "notes_in_json": notes_in_json,
        "notes_reported": notes_reported,
    }
    if notes_in_json is not None and notes_reported is not None:
        meta["notes_match"] = notes_in_json == notes_reported
    (directory / f"{ref.stem}.meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return payload_path


def plan_path(result: PlanResult, ref: ChartRef, directory: Path) -> Path:
    return plan_path_for(ref, result.planner, directory)


def plan_path_for(ref: ChartRef, planner_name: str, directory: Path) -> Path:
    """规划结果的文件名：**一张谱面 + 一个规划器对应一个文件**，所以它天然就是缓存的位置。"""
    return Path(directory) / f"{ref.stem}_{planner_name}.psap"


def save_plan(
    result: PlanResult, ref: ChartRef, directory: Path, *, cache_key: str | None = None
) -> Path:
    """写下规划结果与它的统计。

    ``cache_key`` 是"这份结果由哪一版算法算出来的"的指纹，写进 meta 供缓存失效判断；
    不认识缓存这回事的调用方不用传。
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    path = plan_path(result, ref, directory)
    path.write_bytes(encode_plan(result))

    meta: dict[str, Any] = {
        "planner": result.planner,
        "screen": {"width": result.screen.width, "height": result.screen.height},
        "frames": len(result.frames),
        "events": result.event_count,
        "pointers": result.pointer_count,
        "duration_ms": result.duration_ms,
        "stats": result.stats,
        "warnings": result.warnings,
        "chart": {"seq": ref.seq, "hash": ref.digest, "context": dict(ref.context)},
    }
    if cache_key is not None:
        meta["cache_key"] = cache_key
    path.with_suffix(".meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return path


def load_plan_meta(ref: ChartRef, planner_name: str, directory: Path) -> dict[str, Any] | None:
    """读规划结果的 meta（顺带确认 .psap 也在）；缺了或者坏了就当缓存未命中。"""
    path = plan_path_for(ref, planner_name, directory)
    meta_path = path.with_suffix(".meta.json")
    if not path.is_file() or not meta_path.is_file():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return meta if isinstance(meta, dict) else None


# --------------------------------------------------------------- 二进制格式
#
#   'APSP' | u8 版本 | u8 名字长度 | utf8 规划器名
#   f64 屏宽 | f64 屏高 | u32 帧数
#   每帧: i64 时间戳(ms) | u16 事件数 | 每事件: u8 动作 | u32 指针号 | f64 x | f64 y
#
# 坐标是虚拟屏幕坐标（官谱 16x9，y 轴向上）；映射到真实分辨率是触控模块的事。


def encode_plan(result: PlanResult) -> bytes:
    name = result.planner.encode("utf-8")[:255]
    chunks = [
        _HEADER.pack(
            PLAN_MAGIC,
            PLAN_VERSION,
            len(name),
            result.screen.width,
            result.screen.height,
            len(result.frames),
        ),
        name,
    ]
    for timestamp, events in result.frames:
        chunks.append(_FRAME.pack(timestamp, len(events)))
        for event in events:
            chunks.append(_EVENT.pack(event.action.value, event.pointer, event.x, event.y))
    return b"".join(chunks)


def decode_plan(data: bytes) -> PlanResult:
    magic, version, name_length, width, height, frame_count = _HEADER.unpack_from(data)
    if magic != PLAN_MAGIC:
        raise ValueError(f"不是规划结果文件：magic={magic!r}")
    if version != PLAN_VERSION:
        raise ValueError(f"规划结果版本不支持：{version}")

    offset = _HEADER.size
    planner = data[offset : offset + name_length].decode("utf-8")
    offset += name_length

    frames: list[tuple[int, tuple[TouchEvent, ...]]] = []
    for _ in range(frame_count):
        timestamp, event_count = _FRAME.unpack_from(data, offset)
        offset += _FRAME.size
        events = []
        for _ in range(event_count):
            action, pointer, x, y = _EVENT.unpack_from(data, offset)
            offset += _EVENT.size
            events.append(TouchEvent(pointer, Touch(action), x, y))
        frames.append((timestamp, tuple(events)))

    return PlanResult(planner=planner, screen=Screen(width, height), frames=frames)
