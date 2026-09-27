"""谱面与规划的往返：`.psap` 编解码，以及按 `Chart::Mirror` 规则的镜像判据。"""

from __future__ import annotations

import json
import math
import tempfile
from pathlib import Path

from algorithms.chart import Chart
from algorithms.utils import PlanResult
from formats.storage import ChartRef, decode_plan, encode_plan, plan_path, save_chart
import planner
from .coverage import check_coverage


def check_storage(text: str, planner_name: str, result) -> list[str]:
    """走一遍 planner.py 与 storage.py 的公开接口：规划 -> 落盘 -> 读回来。"""
    problems: list[str] = []
    with tempfile.TemporaryDirectory() as workspace:
        plans = Path(workspace) / "plans"
        ref = ChartRef.of_text(text, seq=1, context={"songsId": "SelfTest", "songsLevel": "EZ"})

        produced = planner.plan(text, planner=planner_name, ref=ref, cache=True, directory=plans)
        if produced.frames != result.frames:
            problems.append("planner.plan 的结果和直接调用规划器不一致")

        path = plan_path(produced, ref, plans)
        if not path.is_file():
            problems.append("规划结果没写出来")
        else:
            restored = decode_plan(path.read_bytes())
            if restored.planner != result.planner:
                problems.append(f"解码出的规划器名不对：{restored.planner}")
            if restored.screen != result.screen:
                problems.append(f"解码出的屏幕不对：{restored.screen}")
            if restored.frames != result.frames:
                problems.append("解码出的事件流与原始结果不一致")
            if len(encode_plan(restored)) != path.stat().st_size:
                problems.append("编解码往返后字节数变了")
            if not path.with_suffix(".meta.json").is_file():
                problems.append("规划结果的 .meta.json 没写出来")

        charts = Path(workspace) / "charts"
        payload = save_chart(text, ref, charts, notes_in_json=len(result.frames))
        if not payload.is_file() or not (charts / f"{ref.stem}.meta.json").is_file():
            problems.append("谱面或它的 .meta.json 没写出来")
        if payload.read_text(encoding="utf-8") != text:
            problems.append("谱面原文写进去变了样")
    return problems


def _legacy_mirror(value: float) -> float:
    """v1 打包坐标 ``x*1000 + y`` 的水平镜像，照抄 ``JudgeLine::Mirror`` 那三行。

    反编译 0x1d28afc，``oldVersion`` 那一支算的是（0x4956D800 = ``880000.0f``）：

    .. code-block:: text

        新值 = 880000 − 旧值 + 2 × (旧值 mod 1000)

    把 ``旧值 = 1000X + Y`` 代进去就是 ``1000(880 − X) + Y`` —— 即 ``x → 880 − x``、``y`` 不动。
    照抄而不是化简，是为了让"模型"与"游戏"的差别一眼可见。
    """
    return 880000.0 - value + 2.0 * math.fmod(value, 1000.0)


def mirror_chart(text: str) -> str:
    """把一份官谱按 ``Chart::Mirror`` 的规则镜像过来 —— 测试用的"游戏行为"模型。

    这是**独立于实现**的判据：``Chart::Mirror`` @ ``0x1d286ac`` 只动三样东西 ——
    移动事件 ``x → 1 − x``、旋转事件 ``θ → −θ``、音符 ``positionX → −positionX``。
    有了它，就能直接验"把规划结果翻一下"是不是镜像后谱面的解，而不用真去开一次游戏。

    v1 的老谱要另算：``Chart::Mirror`` 把 ``formatVersion == 1`` 作为 ``oldVersion`` 传下去，
    ``JudgeLine::Mirror`` 于是走打包整数的分支（见 :func:`_legacy_mirror`）。这个分支是**真存在**的
    —— 采到的 ``Credits`` HD 就是 v1，所以这里不能只伺候 v3。
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

    ``Chart::Mirror`` 对场景做的是整个画面绕中线左右翻（判定线 ``x → 1 − x``、
    ``θ → −θ``、音符 ``positionX → −positionX``），三者合起来正好是判定线上每个点
    ``(x, y) → (16 − x, y)``。既然要按的每个点都只是翻了个身，规划结果跟着翻一下就该
    照样全中 —— 这就是"镜像不重算"的全部依据，也是它唯一的验收标准。
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

