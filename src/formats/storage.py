"""谱面与规划结果的落盘：都是 npz，布局都在本模块里定义。

* **谱面** —— ``<来源>_<难度>_<哈希>.npz``：``chart`` 是 ``FromJson`` 抓到的原文（UTF-8
  字节），``meta`` 是这段原文的来源（哪首、哪档、哈希、音符数核对）；
* **规划结果** —— ``<来源>_<难度>_<哈希>_<规划器>.npz``：几列并行的数值（时间戳、指针、
  动作、坐标）+ 同一段 ``meta``（规划器名、统计、警告、缓存指纹）；读取一律 ``allow_pickle=False``。
"""

from __future__ import annotations

import hashlib
import json
import re
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Mapping

import numpy as np

from algorithms.geometry import Screen
from algorithms.utils import PlanResult, Touch, TouchEvent

CHART_SUFFIX = PLAN_SUFFIX = ".npz"
"""谱面与规划结果都是 npz（在不同的目录里，名字也差一个规划器后缀）。"""

CHART_ARRAYS = ("chart", "meta")
"""一份谱面该有的成员：原文 + 来源。"""

PLAN_ARRAYS = ("screen", "frame_time", "frame_events", "event_pointer", "event_action", "event_xy", "meta")
"""一份规划结果该有的成员：事件流 + 屏幕 + 来源统计。"""


class NpzFormatError(ValueError):
    """文件在，但不是本项目写出来的那份 npz（或者已经坏了）。"""


@dataclass(frozen=True, slots=True)
class ChartRef:
    """一张谱面的身份：来源上下文 + 内容哈希（``seq`` 只是元数据，不进文件名）。

    ``seq`` 是 agent 的会话内计数器（这一局进程里解析的第几张谱面），换个会话同一张谱面就会
    拿到另一个号；拼进文件名的话缓存永远对不上（见 ``docs/_stories_B.md``）。
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
        """没有 agent 给的哈希时（比如直接拿一段谱面原文跑）现算一个。"""
        return cls(seq, dict(context or {}), hashlib.sha1(text.encode("utf-8")).hexdigest()[:8])


def sanitize(value: str | None, fallback: str = "unknown") -> str:
    """把歌曲 id / 难度名变成安全的文件名片段。"""
    if not value:
        return fallback
    cleaned = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", value).strip("_")
    return cleaned[:64] or fallback


# --------------------------------------------------------------- 谱面


def chart_path_for(ref: ChartRef, directory: Path) -> Path:
    """谱面文件名：一张谱面一个文件。"""
    return Path(directory) / f"{ref.stem}{CHART_SUFFIX}"


def save_chart(
    text: str,
    ref: ChartRef,
    directory: Path,
    *,
    notes_in_json: int | None = None,
    notes_reported: int | None = None,
) -> Path:
    """写下谱面原文与它的来源。``notes_reported`` 是游戏自己数出来的音符数。"""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = chart_path_for(ref, directory)

    meta: dict[str, Any] = {
        "seq": ref.seq,
        "hash": ref.digest,
        "chars": len(text),
        "context": dict(ref.context),
        "notes_in_json": notes_in_json,
        "notes_reported": notes_reported,
    }
    if notes_in_json is not None and notes_reported is not None:
        meta["notes_match"] = notes_in_json == notes_reported
    np.savez_compressed(path, chart=_bytes(text), meta=_json_array(meta))
    return path


def load_chart(path: Path) -> tuple[str, ChartRef]:
    """读回谱面原文与它的来源 —— 谱面的身份本来就存在一起。"""
    with _open(path) as data:
        _require(data, path, CHART_ARRAYS)
        meta = _dict(data, path, "meta")
        text = _text(data["chart"])
    return text, ChartRef(
        seq=int(meta.get("seq") or 0),
        context=dict(meta.get("context") or {}),
        digest=str(meta.get("hash") or "") or "unknown",
    )


# --------------------------------------------------------------- 规划结果


def plan_path_for(ref: ChartRef, planner_name: str, directory: Path) -> Path:
    """规划结果的文件名：**一张谱面 + 一个规划器对应一个文件**，所以它天然就是缓存的位置。"""
    return Path(directory) / f"{ref.stem}_{planner_name}{PLAN_SUFFIX}"


def plan_arrays(result: PlanResult, ref: ChartRef, cache_key: str | None = None) -> dict[str, np.ndarray]:
    """把规划结果摊成 npz 的成员表：``screen`` / ``frame_time`` / ``frame_events`` /
    ``event_pointer`` / ``event_action`` / ``event_xy`` / ``meta``。逐字段的表在 impl.md
    「npz 里存了什么」；这里只说事件流怎么摆：
    **贴平**，帧只记长度，第 i 帧的事件是 ``[前 i 帧之和, +第 i 帧长度)`` 那一段 ——
    一帧一个数组会变成几百上千个小成员，而 npz 每个成员都有 zip 头，反而更笨重。
    """
    flat = [event for _, events in result.frames for event in events]
    meta: dict[str, Any] = {
        "planner": result.planner,
        "chart": {"seq": ref.seq, "hash": ref.digest, "context": dict(ref.context)},
        "stats": result.stats,
        "warnings": result.warnings,
    }
    if cache_key is not None:
        meta["cache_key"] = cache_key
    return {
        "screen": np.array([result.screen.width, result.screen.height], np.float64),
        "frame_time": np.fromiter((time for time, _ in result.frames), np.int64, len(result.frames)),
        "frame_events": np.fromiter((len(events) for _, events in result.frames), np.int64, len(result.frames)),
        "event_pointer": np.fromiter((event.pointer for event in flat), np.int32, len(flat)),
        "event_action": np.fromiter((int(event.action) for event in flat), np.uint8, len(flat)),
        "event_xy": np.array([(event.x, event.y) for event in flat], np.float64).reshape(len(flat), 2),
        "meta": _json_array(meta),
    }


def save_plan(
    result: PlanResult, ref: ChartRef, directory: Path, *, cache_key: str | None = None
) -> Path:
    """写下规划结果：事件流与它的来源、统计、警告都在同一个文件里。

    ``cache_key`` 是"这份结果由哪一版算法算出来的"的指纹，供缓存失效判断。
    """
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = plan_path_for(ref, result.planner, directory)
    np.savez_compressed(path, **plan_arrays(result, ref, cache_key))
    return path


def load_plan(path: Path) -> PlanResult:
    """读回一份规划结果（事件流 + 统计 + 警告）。

    文件不在会抛 ``OSError``，在但不是规划结果会抛 :class:`NpzFormatError` ——
    这两种情况调用方的处置不一样（一个是"还没算过"，一个是"盘上的东西坏了"）。
    """
    with _open(path) as data:
        _require(data, path, PLAN_ARRAYS)
        meta = _plan_meta(data, path)
        width, height = (float(value) for value in data["screen"])
        time = data["frame_time"]
        events_per_frame = data["frame_events"]
        pointer = data["event_pointer"]
        action = data["event_action"]
        xy = data["event_xy"]
        _check_shape(time, events_per_frame, pointer, action, xy)
        frames = _frames(time, events_per_frame, pointer, action, xy)
    return PlanResult(
        planner=str(meta["planner"]),
        screen=Screen(width, height),
        frames=frames,
        stats=dict(meta.get("stats") or {}),
        warnings=[str(item) for item in meta.get("warnings") or []],
    )


def plan_meta(path: Path) -> dict[str, Any]:
    """只读规划结果的来源与统计那一段，不动事件流（查缓存命中时用）。"""
    with _open(path) as data:
        _require(data, path, PLAN_ARRAYS)
        return _plan_meta(data, path)


def read_npz(path: Path) -> dict[str, Any]:
    """把一份 npz 读成 Python 数据（谱面或规划结果都行），给 ``npz.py`` 这样的查看工具用。

    ``chart`` 解回原文（能当 JSON 读就顺手读成 JSON），``meta`` 读成 JSON，其余成员是数值数组。
    """
    with _open(path) as data:
        values: dict[str, Any] = {}
        for name in data.files:
            if name == "chart":
                values[name] = _maybe_json(_text(data[name]))
            elif name == "meta":
                values[name] = _dict(data, path, name)
            else:
                values[name] = data[name].tolist()
        return values


# --------------------------------------------------------------- npz 的读写细节


@contextmanager
def _open(path: Path) -> Iterator[Any]:
    """打开一份 npz。文件级的毛病统一翻译成 :class:`NpzFormatError`。

    ``np.load`` 是按需读成员的：只取 ``meta`` 时不会把事件流解压出来。
    """
    try:
        with np.load(path, allow_pickle=False) as data:
            yield data
    except NpzFormatError:
        raise
    except (ValueError, KeyError, EOFError, zipfile.BadZipFile) as error:
        raise NpzFormatError(f"{Path(path).name} 不是一份 npz：{error}") from error


def _require(data: Any, path: Path, names: tuple[str, ...]) -> None:
    missing = [name for name in names if name not in data.files]
    if missing:
        raise NpzFormatError(f"{Path(path).name} 里缺 {', '.join(missing)}")


def _dict(data: Any, path: Path, name: str) -> dict[str, Any]:
    try:
        value = json.loads(str(data[name]))
    except KeyError as error:
        raise NpzFormatError(f"{Path(path).name} 里缺 {name}") from error
    except json.JSONDecodeError as error:
        raise NpzFormatError(f"{Path(path).name} 的 {name} 不是 JSON：{error}") from error
    if not isinstance(value, dict):
        raise NpzFormatError(f"{Path(path).name} 的 {name} 不是一份字典")
    return value


def _plan_meta(data: Any, path: Path) -> dict[str, Any]:
    meta = _dict(data, path, "meta")
    if "planner" not in meta:
        raise NpzFormatError(f"{Path(path).name} 的 meta 里没有规划器名")
    return meta


def _json_array(value: Any) -> np.ndarray:
    """一小段 JSON 直接当字符串成员存 —— 在 numpy 里也一眼看得见。"""
    return np.array(json.dumps(value, ensure_ascii=False))


def _bytes(text: str) -> np.ndarray:
    """原文按 UTF-8 字节存：numpy 的字符串是定长 Unicode（4 字节/字符），更占内存也更慢。"""
    return np.frombuffer(text.encode("utf-8"), np.uint8)


def _text(array: np.ndarray) -> str:
    if array.dtype != np.uint8 or array.ndim != 1:
        raise NpzFormatError("chart 不是 UTF-8 字节流")
    try:
        return array.tobytes().decode("utf-8")
    except UnicodeDecodeError as error:
        raise NpzFormatError(f"chart 不是 UTF-8：{error}") from error


def _maybe_json(text: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _check_shape(
    time: np.ndarray,
    events_per_frame: np.ndarray,
    pointer: np.ndarray,
    action: np.ndarray,
    xy: np.ndarray,
) -> None:
    """数组之间必须自洽 —— 自造的文件坏起来是没有下限的。"""
    if len(time) != len(events_per_frame):
        raise NpzFormatError(f"帧数对不上：{len(time)} 个时间戳 / {len(events_per_frame)} 个长度")
    if len(action) != len(pointer) or xy.shape != (len(pointer), 2):
        raise NpzFormatError("事件数组长度对不上")
    counted = int(events_per_frame.sum())
    if counted != len(pointer):
        raise NpzFormatError(f"事件总数对不上：帧里 {counted} 个 / 数组里 {len(pointer)} 个")


def _frames(
    time: np.ndarray,
    events_per_frame: np.ndarray,
    pointer: np.ndarray,
    action: np.ndarray,
    xy: np.ndarray,
) -> list[tuple[int, tuple[TouchEvent, ...]]]:
    frames: list[tuple[int, tuple[TouchEvent, ...]]] = []
    offset = 0
    for timestamp, count in zip(time.tolist(), events_per_frame.tolist()):
        events = tuple(
            TouchEvent(int(pointer[i]), Touch(int(action[i])), float(xy[i, 0]), float(xy[i, 1]))
            for i in range(offset, offset + int(count))
        )
        frames.append((int(timestamp), events))
        offset += int(count)
    return frames
