"""裁判自检：`algorithms/judging.simulate` 那套"游戏会怎么判"的模型。

这两条都不是推出来的，是拿**实机日志逐音符比**出来的（`judge.py --compare logs/xxx.log`），
所以各钉一个合成用例 —— 合成谱能精确控制"位置相同、时刻不同"这类构造，实机谱里要靠运气碰上：

1. **候选按时间序扫**，不按线号。AT 谱 34 秒那一簇是一堆判定点完全相同、只是线和时刻不同的
   Tap（线 8/16/17/18 都在 `(+8.00, +1.80)`）。位置一样，挑选度量就并列，胜负全看扫描顺序；
   按线号分组会挑中线号最小的那个 —— 与按下时刻差出一百多毫秒，于是凭空多出 Good 与 Miss
   （实机那一局是 Perfect）。
2. **挑选度量里的 |Δy| 是"指尖到判定线的法向距离"**（报告 §6.3 的 `array_normal`），而且
   和横向一起都在**按下那一帧**上算（`GetFingerPosition` 每帧只算一次）。同一条线上的两个
   候选因此必然并列；原先量的是"指尖到音符判定点"的纵向距离，同一线上的两点就分出大小 ——
   按下会被配给时刻更远的那个（AT 谱 147.5/147.6s 那一对就是这么配错的）。
"""

from __future__ import annotations

from algorithms.chart import Chart, JudgeLine, Note, NoteType, Track
from algorithms.geometry import Screen
from algorithms.judging import Verdict, simulate
from algorithms.utils import PlanResult, Touch, TouchEvent

SCREEN = Screen(16.0, 9.0)
TAP_X = 8.0
"""按下位置（虚拟屏坐标）：横向正好压住那些音符，纵向故意离判定线很远（判定线是无限细的）。"""
PRESS_Y = 4.5


def _line(x: float, y: float, notes: list[Note], *, rise: tuple[float, float, float] | None = None):
    """一条（可以不动的）判定线。`rise = (起点, 终点, 抬升到)` 用来造缓慢平移。"""
    move = Track()
    if rise is None:
        move.cut(0.0, 1000.0, complex(x, y), complex(x, y))
    else:
        start, end, top = rise
        move.cut(0.0, start, complex(x, y), complex(x, y))
        move.cut(start, end, complex(x, y), complex(x, top))
        move.cut(end, 1000.0, complex(x, top), complex(x, top))
    rotate = Track()
    rotate.cut(0.0, 1000.0, 0.0, 0.0)  # 弧度：不转
    return JudgeLine(bpm=120.0, notes=notes, move=move, rotate=rotate)


def _presses(moments: list[float]) -> PlanResult:
    """一批"按下就抬起"的 8ms 点按，一个时刻一根手指。"""
    frames = []
    for index, moment in enumerate(moments):
        stamp = int(round(moment * 1000))
        frames.append((stamp, (TouchEvent(1000 + index, Touch.DOWN, TAP_X, PRESS_Y),)))
        frames.append((stamp + 8, (TouchEvent(1000 + index, Touch.UP, TAP_X, PRESS_Y),)))
    return PlanResult(planner="stub", screen=SCREEN, frames=sorted(frames))


def check_referee() -> list[str]:
    problems: list[str] = []

    def expect(condition: bool, complaint: str) -> None:
        if not condition:
            problems.append(complaint)

    def counts(report) -> dict[Verdict, int]:
        return report.counts

    # 1) 同一位置、不同线与时刻：按下该判**时刻最近**的那个
    #    刻意让"时刻最早的音符在最大的线号上" —— 按线号扫就会先撞上时刻不对的那个。
    notes = {
        0: Note(NoteType.TAP, 34.2, 0.0, 0.0),
        1: Note(NoteType.TAP, 34.1, 0.0, 0.0),
        2: Note(NoteType.TAP, 34.0, 0.0, 0.0),
    }
    chart = Chart(3, 0.0, SCREEN, [_line(TAP_X, 2.0, [notes[line]]) for line in (0, 1, 2)])
    report = simulate(chart, _presses([34.0, 34.1, 34.2]))
    table = counts(report)
    expect(
        table[Verdict.PERFECT] == 3,
        f"三次按下都在自己的音符上，应当三个 Perfect，实际 {table[Verdict.PERFECT]}P "
        f"{table[Verdict.GOOD]}G {table[Verdict.BAD]}B {table[Verdict.MISS]}M"
        f"（候选按线号扫会挑到时隔 100ms+ 的那个）",
    )

    # 2) 同一条线上 86ms 相邻的两点：度量并列，胜负交给扫描顺序
    #    判定线在两点之间缓慢抬起（实机那种"线在动"），于是"到音符判定点"的距离会分出大小，
    #    而"到判定线"的距离对两个音符是同一个数。
    first, second = 58.793, 58.879
    line = _line(
        TAP_X, 1.84, [Note(NoteType.TAP, first, 0.0, 0.0), Note(NoteType.TAP, second, 0.0, 0.0)],
        rise=(58.8, 58.9, 2.0),
    )
    report = simulate(Chart(3, 0.0, SCREEN, [line]), _presses([58.792, 58.872]))
    table = counts(report)
    expect(
        table[Verdict.PERFECT] == 2 and not report.grazes,
        f"相邻 86ms 的两点各按各的，应当两个 Perfect 且没有蹭键，实际 "
        f"{table[Verdict.PERFECT]}P {table[Verdict.GOOD]}G，蹭键 {len(report.grazes)} 处"
        f"（挑选用「到音符判定点」的距离时，按下会被配给时刻更远的那个）",
    )

    # 3) 顺带钉住一条老账：**一次按下只判一个音符**（早先按"把所有够得着的都判掉"写，
    #    Credits IN 上凭空多出 80 个 Good）。两个音符完全同时同位，一次按下只该判掉一个。
    twins = [
        Note(NoteType.TAP, 5.0, 0.0, 0.0, above=True),
        Note(NoteType.TAP, 5.0, 0.0, 0.0, above=False),
    ]
    report = simulate(Chart(3, 0.0, SCREEN, [_line(TAP_X, 2.0, twins)]), _presses([5.0]))
    judged = [j for j in report.judgements if j.verdict is Verdict.PERFECT]
    expect(len(judged) == 1, f"一次按下只该判掉一个音符，实际判掉了 {len(judged)} 个")
    expect(
        len(report.lost) == 1,
        f"另一个同位的音符该留着（计划只按了一次），实际丢了 {len(report.lost)} 个",
    )

    # 4) 判定窗与判档：逐字照抄 `CheckNote` / `ClickControl::Judge` 的反编译
    #    （`minDeltaTime` 那条 `>= 10000.01` 的守卫是死代码，别照它写早判下界）
    from algorithms.judging import GOOD_TIME, tap_window, verdict_of

    cases = [
        (-0.21, 0.0, True, "正中、早 210ms：扫描窗内"),
        (-0.23, 0.0, False, "正中、早 230ms：超出扫描窗"),
        (0.17, 0.0, True, "正中、晚 170ms"),
        (0.19, 0.0, False, "正中、晚 190ms：晚的边界是 0.18"),
        (-0.19, 1.8, False, "贴边（1.8）、早 190ms：边缘把早判收紧到约 184ms"),
        (-0.17, 1.8, True, "贴边（1.8）、早 170ms：仍在收紧后的窗内"),
        (0.0, 1.9, False, "横向正好 1.9：严格比较，判不到"),
        (0.0, 1.89, True, "横向 1.89：判得到"),
    ]
    for delta, touch, want, why in cases:
        got = tap_window(delta, touch)
        if got is not want:
            problems.append(f"tap_window({delta}, {touch}) = {got}，应当是 {want} —— {why}")
    if verdict_of(-0.30).label != "Bad":
        problems.append("判档没有上界：差 300ms 也是 Bad（只要 CheckNote 选中了它）")
    if verdict_of(GOOD_TIME - 0.001).label != "Good":
        problems.append("刚好在 Good 边界内应当是 Good")

    # 5) Hold 的身体：头判只是"挂号"，手指没留住照样 Miss —— 而且**可以早于 realTime**。
    #    实机踩过的那条：AT 谱 99.310s 的 hold 在 99.217s 就判了 Miss（早了 93ms），
    #    成因就是一次"不是瞄它"的按下把头判标上、那根手指随即离开。
    from algorithms.judging import BAD_TIME, HOLD_GRACE_FRAMES, PROCESS_DELAY

    long_hold = Note(NoteType.HOLD, 10.0, 2.0, 0.0)
    line = _line(TAP_X, 2.0, [long_hold])
    chart = Chart(3, 0.0, SCREEN, [line])

    def hold_report(frames):
        return simulate(chart, PlanResult(planner="stub", screen=SCREEN, frames=frames))

    # 5a) 头判被"擦边按下"标记、手指随即离开 → 身体宽限耗尽 → Miss（早于音符自己的时刻）
    thief = [
        (round((10.0 - GOOD_TIME + 0.01) * 1000), (TouchEvent(0, Touch.DOWN, TAP_X, PRESS_Y),)),
        (round((10.0 - GOOD_TIME + 0.05) * 1000), (TouchEvent(0, Touch.UP, TAP_X, PRESS_Y),)),
    ]
    report = hold_report(thief)
    hold_verdicts = [j for j in report.judgements if j.note.kind is NoteType.HOLD]
    if len(hold_verdicts) != 1 or hold_verdicts[0].verdict is not Verdict.MISS:
        problems.append(
            f"头判挂上号、手指没留住应当 Miss，实际 "
            f"{[j.verdict.label for j in hold_verdicts]}"
        )
    elif hold_verdicts[0].at >= 10.0:
        problems.append(
            f"身体判的 Miss 可以早于音符时刻（实机早 93ms），实际判在 {hold_verdicts[0].at:.3f}s"
        )

    # 5b) 头判之后一直按着 → 收尾 Perfect，而且报的是**头判**的 Δ（不是收尾那一刻的）
    head_lead = 0.01
    kept = [
        (round((10.0 - head_lead) * 1000), (TouchEvent(0, Touch.DOWN, TAP_X, PRESS_Y),)),
        (round((10.0 + 2.2) * 1000), (TouchEvent(0, Touch.UP, TAP_X, PRESS_Y),)),
    ]
    report = hold_report(kept)
    hold_verdicts = [j for j in report.judgements if j.note.kind is NoteType.HOLD]
    if len(hold_verdicts) != 1 or hold_verdicts[0].verdict is not Verdict.PERFECT:
        problems.append(
            f"头判之后一直按着应当 Perfect，实际 {[j.verdict.label for j in hold_verdicts]}"
        )
    elif abs(hold_verdicts[0].delta) > GOOD_TIME:
        problems.append(
            f"收尾要报**头判**的 Δ（{hold_verdicts[0].delta * 1000:+.0f}ms 太大了，"
            f"像是拿了收尾那一刻去减）"
        )

    # 5c) 一次按下够得着两个相隔 86ms 的音符、度量并列时：**时刻更早**的那个胜出
    #     （AT 谱 147.500/147.586 那一对；设备按时间序选，我原先按"更小的度量"选反了）
    #     两条线的法向距离刻意差 0.002 —— 千分之几的差别不该推翻扫描顺序（见 METRIC_EPSILON），
    #     否则这一条判据在同一条线的构造下永远成立，等于没测。
    left = Note(NoteType.TAP, 147.500, 0.0, 0.0)
    right = Note(NoteType.TAP, 147.586, 0.0, 0.0)
    chart = Chart(
        3, 0.0, SCREEN, [_line(TAP_X, 2.0, [left]), _line(TAP_X, 2.002, [right])]
    )
    report = simulate(chart, _presses([147.496, 147.584]))
    order = {j.key: (j.verdict, round(j.delta * 1000)) for j in report.judgements}
    first = order.get((0, True, 0))  # 线 0 = 时刻更早的那个（147.500）
    later = order.get((1, True, 0))
    if first is None or later is None:
        problems.append(f"并列挑选那条没判出来：{order}")
    elif first[0] is not Verdict.PERFECT or later[0] is not Verdict.PERFECT:
        problems.append(
            f"度量并列时该各按各的（都是 Perfect），实际 {order} —— "
            f"千分之几的法向差不该推翻扫描顺序"
        )

    return problems
