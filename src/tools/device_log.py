#!/usr/bin/env python3
"""把一次运行的日志读回来：设备实际判了什么，以及游戏自己记的账目。

``judge.py`` 的裁判准不准只能拿设备对，而"标准答案"就在 ``logs/`` 里：每条 ``[judge]``
带着音符身份（线 / 上下 / 第几个）、早晚量、判定时刻，末尾还有 ``[result]`` 的分数与计数。
认不出的行跳过并计数（``skipped``），不悄悄少算几个音符。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

JUDGE_RE = re.compile(
    r"^\[judge\]\s+(?P<kind>\w+)\s+(?P<type>\w+)\s+@\s*(?P<seconds>\d+\.\d+)s\s+"
    r"线\s*(?P<line>\d+)\s+(?P<side>[上下])\s+第\s*(?P<index>\d+)\s+个\s+"
    r"x=\s*(?P<x>[+-][\d.]+)\s+#(?P<code>\d+)"
    r"(?:\s+(?P<side_time>[早晚])\s*(?P<ms>\d+)ms|\s+判定于\s*(?P<at>\d+\.\d+)s)?"
)

# 后缀认两种：日志是当时的记录，谱面存成 npz 之前记的是 `.json` 名字。这里只认名字，
# 落到 charts/ 里找的是现在的文件（按 stem 对，见 judge.compare）。
CHART_RE = re.compile(r"^\[chart #\d+\]\s+已保存\s+(?P<name>\S+\.(?:json|npz))")
PLAN_RE = re.compile(r"^\[plan #\d+\]\s+(?P<planner>[\w-]+):")
LATENCY_RE = re.compile(r"手工补偿\s*(?P<value>[+-]?\d+(?:\.\d+)?)ms")
RESULT_RE = re.compile(
    r"^\[result #\d+\].*?\s+(?P<score>\d+)\s*分（(?P<percent>[\d.]+)%）\s*最大连击\s*(?P<combo>\d+)"
)
COUNTS_RE = re.compile(
    r"Perfect\s+(?P<perfect>\d+)\s+Good\s+(?P<good>\d+)\s+Bad\s+(?P<bad>\d+)\s+"
    r"Miss\s+(?P<miss>\d+)"
)


@dataclass(slots=True)
class DeviceJudge:
    """日志里的一条判定。``key`` 就是音符身份 —— 裁判那边用的是同一把钥匙。"""

    kind: str
    note_type: str
    seconds: float
    line: int
    above: bool
    index: int
    x: float
    code: int
    delta: float | None = None
    """与游戏一致：**晚为正**（"早 187ms" 记成 -0.187）。"""
    at: float | None = None
    """Miss 没有早晚量，只有判定时刻。"""

    @property
    def key(self) -> tuple[int, bool, int]:
        return (self.line, self.above, self.index)

    @property
    def where(self) -> str:
        side = "上" if self.above else "下"
        return f"@{self.seconds:8.3f}s 线 {self.line:<3}{side} 第 {self.index:<4}个 x={self.x:+7.3f}"


@dataclass(slots=True)
class DeviceRun:
    """一次运行里"设备那边"的全部事实。"""

    path: Path
    chart: str | None = None
    """采集时存下来的谱面文件名（``[chart #…] 已保存 …``）。"""
    planner: str | None = None
    """这一局用的规划器（``[plan #…] <名字>:``）。"""
    latency: float | None = None
    """那一局的手工补偿（秒）。对账时要用它重放：计划里的落点是"音符自己那一刻的判定点"，
    补偿没抵掉处理延迟时那个落点就是偏的。"""
    judges: dict[tuple[int, bool, int], DeviceJudge] = field(default_factory=dict)
    result: dict[str, float | int] = field(default_factory=dict)
    """``score`` / ``percent`` / ``maxCombo`` / 四个计数；读不到的键就不在。"""
    skipped: int = 0
    """看着像判定、但认不出音符身份的行数。"""

    @property
    def counts(self) -> dict[str, int]:
        table = {"Perfect": 0, "Good": 0, "Bad": 0, "Miss": 0}
        for judge in self.judges.values():
            table[judge.kind] = table.get(judge.kind, 0) + 1
        return table

    @property
    def label(self) -> str:
        table = self.counts
        return (
            f"{table['Perfect']}P  {table['Good']}G  {table['Bad']}B  {table['Miss']}M"
        )


def parse(text: str, *, path: Path | None = None) -> DeviceRun:
    """读日志正文。认不出的行跳过（判定行例外：计数到 ``skipped``）。"""
    run = DeviceRun(path=path or Path("<内存>"))
    for line in text.splitlines():
        if line.startswith("[judge]"):
            match = JUDGE_RE.match(line)
            if match is None:
                if "noteCode" in line:  # 音符表里没有它的那种
                    run.skipped += 1
                continue
            delta: float | None = None
            at: float | None = None
            if match.group("ms") is not None:
                delta = int(match.group("ms")) / 1000.0
                if match.group("side_time") == "早":
                    delta = -delta
            elif match.group("at") is not None:
                at = float(match.group("at"))
            judge = DeviceJudge(
                kind=match.group("kind"),
                note_type=match.group("type"),
                seconds=float(match.group("seconds")),
                line=int(match.group("line")),
                above=match.group("side") == "上",
                index=int(match.group("index")),
                x=float(match.group("x")),
                code=int(match.group("code")),
                delta=delta,
                at=at,
            )
            run.judges.setdefault(judge.key, judge)
            continue

        if run.chart is None and (found := CHART_RE.match(line)):
            run.chart = found.group("name")
        if run.planner is None and (found := PLAN_RE.match(line)):
            run.planner = found.group("planner")
        if run.latency is None and (found := LATENCY_RE.search(line)):
            run.latency = float(found.group("value")) / 1000.0
        if (found := RESULT_RE.match(line)) and not run.result:
            run.result = {
                "score": int(found.group("score")),
                "percent": float(found.group("percent")),
                "maxCombo": int(found.group("combo")),
            }
        elif run.result and (found := COUNTS_RE.search(line)):
            for name in ("perfect", "good", "bad", "miss"):
                run.result[name] = int(found.group(name))
    return run


def read(path: Path) -> DeviceRun:
    return parse(Path(path).read_text(encoding="utf-8", errors="replace"), path=Path(path))
