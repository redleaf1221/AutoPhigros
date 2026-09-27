"""规划器的契约层。

规划器只依赖本模块、``screen`` 与 ``chart``：它们不 import frida、不打印、
不关心谱面从哪来、结果往哪去。主机用 ``registry`` 造出规划器、注入 ``Progress``、
把结果交给 ``storage`` —— 这就是本项目依赖注入的全部约定。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING, Any, Iterable, Iterator, NamedTuple, Protocol, TypeVar

if TYPE_CHECKING:
    from .chart import Chart
    from .geometry import Screen

Position = complex
"""虚拟屏幕坐标：实部为 x，虚部为 y，y 轴向上。"""

Vector = complex
"""方向向量，用单位复数表示。"""

T = TypeVar("T")

MIN_DWELL_MS = 20
"""一根手指摆到一个位置之后，至少要保持这么久才允许被挪走或抬起。

因为**游戏是逐帧读手指位置的**：``JudgeControl::GetFingerPosition`` 每帧重算一次
``fingerPositionX = (手指 − 判定线原点)·判定线朝向``，而手指在两次触控事件之间是不动的
—— 判定线一直在动，手指的横向偏移就跟着漂。所以一个音符能不能被判到，取决于"它窗口里
有没有**整整一帧**手指都落在容差内"，而不是"有没有那么一瞬间对上"：

* Tap / Hold 头判只看"按下那一帧"（``CheckNote`` 只为 ``phase == Began`` 的手指调用），
  按下去之后马上抬起没关系；
* Drag / Flick 是逐帧比位置的（``DragControl::Judge`` 的窗口是 ±0.1s），
  手指在窗口里只停 1 毫秒，能不能撞上帧全看运气 —— 实测就是这么漏音的。

取 20ms = 一帧多一点。一帧有多长由游戏的帧率策略定
（``GameInformation::CheckFrameRate``：刷新率 ≤ 89Hz 时 ``targetFrameRate = 60``，
更高则取 ``2 × 刷新率``，上限 300；``vSyncCount`` 恒为 0），
60fps 是它支持的最低档，就按这一档兜底。
"""


class PlanningError(RuntimeError):
    """规划器给不出可行解，例如同时按下的音符超过十指上限。"""


class Touch(IntEnum):
    """触控动作。

    编号是**本项目内部**的（DOWN/MOVE/UP/CANCEL = 0/1/2/3），随 ``.psap`` 一起落盘。
    Android 的 MotionEvent 动作码是 DOWN/UP/MOVE/CANCEL = 0/1/2/3 —— MOVE 与 UP 对调，
    所以下发到设备前由触控后端显式映射（``scrcpy.py`` 的 ``_ACTIONS``）；
    规划结果本身不跟 Android 的 ABI 绑在一起。
    """

    DOWN = 0
    MOVE = 1
    UP = 2
    CANCEL = 3


class TouchEvent(NamedTuple):
    """一次触控采样，坐标是虚拟屏幕坐标。"""

    pointer: int
    action: Touch
    x: float
    y: float

    def __str__(self) -> str:
        return f"#{self.pointer} {self.action.name:<4} ({self.x:7.3f}, {self.y:7.3f})"


class Progress(Protocol):
    """规划器汇报进度的唯一渠道，由主机注入（命令行下是 tqdm）。"""

    def track(self, iterable: Iterable[T], description: str) -> Iterator[T]:
        ...


class SilentProgress:
    """什么都不做的进度条，供测试与无终端场景使用。"""

    def track(self, iterable: Iterable[T], description: str) -> Iterator[T]:
        return iter(iterable)


@dataclass(slots=True)
class PlanResult:
    """一张谱面的完整规划结果：按时间戳（毫秒）分组的触控事件序列。"""

    planner: str
    screen: Screen
    frames: list[tuple[int, tuple[TouchEvent, ...]]]
    stats: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    @property
    def event_count(self) -> int:
        return sum(len(events) for _, events in self.frames)

    @property
    def pointer_count(self) -> int:
        return len({event.pointer for _, events in self.frames for event in events})

    @property
    def duration_ms(self) -> int:
        return self.frames[-1][0] if self.frames else 0

    def mirrored(self) -> PlanResult:
        """水平镜像：每个触点 ``x → 屏宽 − x``。

        游戏打开"谱面镜像"时，``Chart::Mirror`` 对场景做的是**整个画面绕中线左右翻**：
        判定线移动事件 ``x → 1 − x``、旋转事件 ``θ → −θ``、音符 ``positionX → −positionX``。
        三者合起来，判定线上的每个点都只是变成了 ``(16 − x, y)``：

        .. code-block:: text

            note' = (16 − lx, ly) + R(−θ)·(−offset) = (16 − (lx + offset·cosθ), ly + offset·sinθ)

        最后一项正好是原音符判定点的镜像。既然要按的每个点都只是翻了个身，那规划结果
        跟着翻一下，就是镜像后谱面的解 —— 镜像**不需要重算**，重算反而要再去读一遍谱面。
        """
        width = self.screen.width
        return PlanResult(
            planner=self.planner,
            screen=self.screen,
            frames=[
                (timestamp, tuple(event._replace(x=width - event.x) for event in events))
                for timestamp, events in self.frames
            ],
            stats=dict(self.stats),
            warnings=list(self.warnings),
        )


class Planner(Protocol):
    """规划器接口。``registry`` 按名字造出实例，主机只认这个形状。

    叫什么、干什么用的写在 ``registry`` 的清单里，不在这里重复一份 ——
    清单要能在不 import 任何算法的前提下列出来。
    """

    name: str

    def plan(self, chart: Chart, progress: Progress) -> PlanResult:
        ...
