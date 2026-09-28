"""谱面与规划的往返：两种 npz 的读写，以及按 `Chart::Mirror` 规则的镜像判据。"""

from __future__ import annotations

import json
import math
import tempfile
from pathlib import Path
from typing import Any, Mapping

from algorithms.chart import Chart
from algorithms.utils import PlanResult
from formats.storage import (
    ChartRef,
    load_chart,
    load_plan,
    plan_meta,
    plan_path_for,
    read_npz,
    save_chart,
)
import planner
from .coverage import check_coverage


def check_storage(
    text: str, planner_name: str, result, options: Mapping[str, Any] | None = None
) -> list[str]:
    """走一遍 planner.py 与 storage.py 的公开接口：规划 -> 落盘 -> 读回来。

    ``options`` 要跟体检那边用的一致，否则这条会拿"默认参数的规划"去比"带参数的规划"。
    """
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as workspace:
        plans = Path(workspace) / "plans"
        ref = ChartRef.of_text(text, seq=1, context={"songsId": "SelfTest", "songsLevel": "EZ"})

        produced = planner.plan(
            text, planner=planner_name, options=options, ref=ref, cache=True, directory=plans
        )
        if produced.frames != result.frames:
            problems.append("planner.plan 的结果和直接调用规划器不一致")

        path = plan_path_for(ref, produced.planner, plans)
        if not path.is_file():
            problems.append("规划结果没写出来")
        else:
            restored = load_plan(path)
            if restored != produced:
                problems.append("规划结果读回来与原结果不一致（事件流 / 统计 / 警告）")
            if plan_meta(path).get("cache_key") != planner.cache_key(produced.planner, options):
                problems.append("规划结果的 meta 里没记对算法指纹")
            # npz.py（读成 JSON）走的也是这条路：一段都不许少
            values = read_npz(path)
            if values["meta"].get("planner") != produced.planner:
                problems.append("npz 读出来的 meta 里规划器名不对")
            if len(values["frame_time"]) != len(produced.frames):
                problems.append("npz 读出来的帧数不对")

        charts = Path(workspace) / "charts"
        chart_path = save_chart(text, ref, charts, notes_in_json=7, notes_reported=7)
        if not chart_path.is_file():
            problems.append("谱面没写出来")
        else:
            restored_text, restored_ref = load_chart(chart_path)
            if restored_text != text:
                problems.append("谱面原文写进去变了样")
            if restored_ref != ref:
                problems.append(f"读回来的谱面身份不对：{restored_ref}")
    return problems


def _legacy_mirror(value: float) -> float:
    """v1 打包坐标 ``x*1000 + y`` 的水平镜像，照抄 ``JudgeLine::Mirror``（0x1d28afc）。

    ``oldVersion`` 那一支算的是 ``新值 = 880000 − 旧值 + 2 × (旧值 mod 1000)``
    （0x4956D800 = ``880000.0f``）；把 ``旧值 = 1000X + Y`` 代进去就是 ``x → 880 − x``、``y`` 不动。
    """
    return 880000.0 - value + 2.0 * math.fmod(value, 1000.0)


def mirror_chart(text: str) -> str:
    """把一份官谱按 ``Chart::Mirror`` 的规则镜像过来 —— 测试用的"游戏行为"模型。

    ``Chart::Mirror`` @ ``0x1d286ac`` 只动三样：移动事件 ``x → 1 − x``、旋转事件 ``θ → −θ``、
    音符 ``positionX → −positionX``；v1 老谱另走 ``oldVersion`` 那一支（见 :func:`_legacy_mirror`）。
    """
    chart = json.loads(text)
    old_version = int(chart.get("formatVersion") or 0) == 1
    for line in chart["judgeLineList"]:
        for event in line.get("judgeLineMoveEvents") or ():
            if old_version:
                event["start"] = _legacy_mirror(event["start"])
                event["end"] = _legacy_mirror(event["end"])
            else:
                event["start"] = 1.0 - event["start"]
                event["end"] = 1.0 - event["end"]
        for event in line.get("judgeLineRotateEvents") or ():
            event["start"] = -event["start"]
            event["end"] = -event["end"]
        for key in ("notesAbove", "notesBelow"):
            for note in line.get(key) or ():
                note["positionX"] = -note["positionX"]
    return json.dumps(chart)


def check_mirror(text: str, result: PlanResult) -> tuple[int, list[str], float]:
    """镜像自检：把**原始谱面**的规划结果整体翻过来，拿去对**镜像后的谱面**。

    ``Chart::Mirror`` 是画面绕中线左右翻，判定线上每个点 ``(x, y) → (16 − x, y)``，
    所以规划结果跟着翻一下就该照样全中 —— 这就是"镜像不重算"的全部验收标准。
    """
    problems: list[str] = []

    flipped = result.mirrored()
    if flipped.screen != result.screen:
        problems.append("镜像改变了屏幕尺寸")
    if flipped.event_count != result.event_count:
        problems.append(f"镜像改变了事件数：{result.event_count} -> {flipped.event_count}")
    if flipped.mirrored().frames != result.frames:
        problems.append("镜像两次回不到原样")

    mirrored_chart = Chart.parse(mirror_chart(text))
    if mirrored_chart.note_count != Chart.parse(text).note_count:
        problems.append("镜像改变了音符数")

    return check_coverage(mirrored_chart, flipped)
