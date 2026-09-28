"""规划器的契约层。

规划器只依赖本模块、``screen`` 与 ``chart``：不 import frida、不打印、不关心谱面从哪来。
主机用 ``registry`` 造出规划器、注入 ``Progress``、把结果交给 ``storage``。

规划器的可调参数是**配置项**，不是散落在模块里的常量：每个规划器声明一个配置
dataclass（字段默认值 + 一句 ``metadata={"help": ...}``），:func:`build_options` 负责
把用户写的覆盖项盖上去。``planner.py`` / ``judge.py`` / 控制台都从这条路上走。
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import IntEnum
from typing import TYPE_CHECKING, Any, Iterable, Iterator, Mapping, NamedTuple, Protocol, TypeVar

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

游戏是**逐帧**读手指位置的（``JudgeControl::GetFingerPosition``），两次触控事件之间手指
不动、判定线在动，所以"能不能判到"取决于窗口里有没有整整一帧手指都落在容差内。
取 20ms = 游戏支持的最低帧率（60fps）的一帧多一点。推导见 impl.md 的"在位时长"。
"""


class PlanningError(RuntimeError):
    """规划器给不出可行解，例如同时按下的音符超过十指上限。"""


WARNING_LIMIT = 20
"""每个规划器最多写多少条 ``warnings``：同一类问题成片出现时，再多日志就没法看了。"""


class Touch(IntEnum):
    """触控动作。编号是项目内部的约定，下发前由触控后端映射成 Android 的 MotionEvent 码
    （那边 MOVE / UP 与这里**对调**，见 ``backends/scrcpy.py`` 的 ``_ACTIONS``）。
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


@dataclass(frozen=True, slots=True)
class Parameter:
    """规划器的一个可调参数：名字、默认值、一句话说明。"""

    name: str
    default: Any
    help: str


def parameters(config_type: type[Any]) -> tuple[Parameter, ...]:
    """从配置 dataclass 里读出参数表 —— 说明就写在字段的 ``metadata`` 里。"""
    return tuple(
        Parameter(item.name, item.default, str(item.metadata.get("help", "")))
        for item in fields(config_type)
    )


def build_options(config_type: type[T], options: Mapping[str, Any] | None = None) -> T:
    """把覆盖项盖到默认值上。

    认不得的名字、类型不对的值都当场报错 —— 配置写错了不该悄悄按默认值跑完整首歌。
    """
    if not options:
        return config_type()
    defaults = {item.name: item.default for item in fields(config_type)}
    unknown = sorted(set(options) - set(defaults))
    if unknown:
        raise ValueError(f"不认识的参数：{'、'.join(unknown)}")
    return config_type(**{name: _coerce(name, defaults[name], value) for name, value in options.items()})


def _coerce(name: str, default: Any, value: Any) -> Any:
    """按默认值的类型收一下用户给的值（命令行与控制台都只能写字）。"""
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ValueError(f"{name} 要 true / false，给的是 {value!r}")
        return value
    if isinstance(default, int):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} 要整数，给的是 {value!r}")
        if float(value) != int(value):
            raise ValueError(f"{name} 要整数，给的是 {value!r}")
        return int(value)
    if isinstance(default, float):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{name} 要数，给的是 {value!r}")
        return float(value)
    if not isinstance(value, type(default)):
        raise ValueError(f"{name} 要 {type(default).__name__}，给的是 {value!r}")
    return value


def parse_option(text: str) -> Any:
    """把命令行 / 控制台里写的一个值变成 Python 值：``1`` / ``1.5`` / ``true`` / 其余当字符串。"""
    lowered = text.strip().lower()
    if lowered in ("true", "on", "yes"):
        return True
    if lowered in ("false", "off", "no"):
        return False
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            pass
    return text


def options_from_args(pairs: Iterable[str] | None) -> dict[str, Any]:
    """把 ``名字=值`` 这样的命令行参数解析成覆盖项（类型由 :func:`build_options` 收）。"""
    options: dict[str, Any] = {}
    for pair in pairs or ():
        name, _, text = pair.partition("=")
        if not name.strip() or not text:
            raise ValueError(f"要写成 名字=值，收到的是 {pair!r}")
        options[name.strip()] = parse_option(text)
    return options


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

        ``Chart::Mirror`` 把画面绕中线左右翻（判定线 ``x → 1−x``、``θ → −θ``、音符
        ``positionX → −positionX``），合起来就是判定线上每个点 ``(x, y) → (16−x, y)``，
        所以镜像**不需要重算**，翻一下坐标就是镜像后谱面的解。推导与验收见 impl.md。
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
    """规划器接口。``registry`` 按名字造出实例，主机只认这个形状。"""

    name: str

    def plan(self, chart: Chart, progress: Progress) -> PlanResult:
        ...
