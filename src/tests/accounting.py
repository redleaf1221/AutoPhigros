"""账目检查：判决流水与音符表的对账，以及一局的终局结算。"""

from __future__ import annotations

import contextlib
import io
from pathlib import Path


def check_judge() -> list[str]:
    """判定对账自检：``delta`` 与 ``nowTime − realTime`` 必须对得上。

    这三个数来自三处 —— 游戏判决时算的早晚量、游戏当时的 ``nowTime``、我们从音符表抄来的
    ``realTime``，它们之间有个恒等式。表抄错了在这里就该露馅，而不必等到"Miss 出现在一个
    不可能的时刻"再靠人眼看出来（真出过一次：表建早了，``realTime`` 全是 0，
    89.969 秒判掉的音符被记成 "@ 0.000s"）。
    """
    problems: list[str] = []
    try:
        from runtime import agent as agent_module
        from runtime import config
        from runtime import controller as controller_module
        import main as main_module
        import planner
        import touch
    except ImportError as error:  # noqa: BLE001 - frida 没装也不该让整份自检跑不起来
        return [f"导入 main.py 失败，跳过判定对账自检：{error}"]

    agent = agent_module.Agent(Path("unused.js"))
    # 一个真实形状的音符：Dlyrotz 里第 7 条线上方第 0 个，89.969 秒
    note = {
        "code": 7000000,
        "type": 4,
        "time": 89.969,
        "x": 6.0,
        "hold": 0.0,
        "line": 7,
        "above": True,
        "index": 0,
    }

    def judge(**fields) -> str:
        payload = {"kind": "Miss", "noteCode": 7000000, "delta": None, "note": dict(note), "at": 0}
        payload.update(fields)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            agent._on_judge(payload)  # noqa: SLF001
        return buffer.getvalue()

    def expect(condition: bool, complaint: str) -> None:
        if not condition:
            problems.append(complaint)

    # 1) 对得上：Good，晚 32ms
    text = judge(kind="Good", delta=0.032, time=90.001)
    expect("对不上账" not in text, f"明明对得上却报了账不平：{text.strip()!r}")
    expect("[judge] Good" in text, f"Good 那行没打出来：{text.strip()!r}")

    # 2) Miss 的正常情形：刚越过窗口（0.1 秒多一点），不该被冤枉
    expect("对不上账" not in judge(time=89.969 + 0.12), "正常的 Miss 被冤枉了")

    # 3) 表抄早了：realTime 是 0，而游戏说这个音符在 89.969 秒
    stale = judge(time=89.969, note=dict(note, time=0.0))
    expect("对不上账" in stale, "realTime 抄成 0 却没被发现")
    expect("差 +89.969s" in stale or "89.969" in stale, f"抱怨里没说清差了多少：{stale.strip()!r}")
    expect(agent.judge_mismatches == 1, f"账不平的条数不对：{agent.judge_mismatches}")

    # 4) 对不上账的**每一条**都要报（原先"同类只报一次"：知道有 8 条对不上，
    #    却不知道是哪 8 条、各自差多少 —— 那诊断不了任何东西）
    again = judge(time=89.969, note=dict(note, time=0.0))
    expect("对不上账" in again, "第二条同类问题也该当场报出来，不许省略")
    expect("警告 #2" in again, f"每条都该带上序号：{again.strip()!r}")
    expect("线 7" in again and "7000000" in again, f"抱怨里要带上是哪个音符：{again.strip()!r}")
    expect(agent.judge_mismatches == 2, f"第二次没计上数：{agent.judge_mismatches}")

    # 5) 早晚量与 nowTime 不搭（另一条路径也得被抓住）
    agent.judge_mismatches = 0  # noqa: SLF001
    skewed = judge(kind="Good", delta=0.032, time=120.0)
    expect("对不上账" in skewed, "早晚量与 nowTime 差得离谱却没被发现")
    expect(agent.judge_mismatches == 1, f"换了一条路径却没计上数：{agent.judge_mismatches}")

    # 6) 查不到音符（表里没有它）时不该乱报账不平
    expect("对不上账" not in judge(note=None), "没有音符可查时不该报账不平")

    # 7) Hold 的收尾判决：早晚量是从"按住结束 − 0.22s"量的，不是从头量的 ——
    #    拿头判的恒等式去对，每条 hold 都会被冤枉（实测 Dlyrotz HD 上正好冤枉了那 8 个 hold）
    hold_note = dict(note, type=3, time=10.0, hold=2.416)
    settle = 10.0 + 2.416 - agent_module.HOLD_SETTLE_LEAD
    agent.judge_mismatches = 0  # noqa: SLF001
    text = judge(kind="Perfect", delta=0.012, time=settle + 0.012, note=hold_note)
    expect("对不上账" not in text, f"Hold 的收尾判决被冤枉了：{text.strip()!r}")
    expect(agent.judge_mismatches == 0, "Hold 的合法收尾不该计成账不平")

    # 8) Miss 可以**早于** realTime：手指一直没碰、hold 中途松手都会这样。
    #    下界原先只有 0.05（等于"早一点点都不行"），这类正常的 Miss 全被当成"表抄错了"。
    for early in (0.05, 0.15, 0.2):
        agent.judge_mismatches = 0  # noqa: SLF001
        text = judge(time=89.969 - early)
        expect("对不上账" not in text, f"早 {early}s 的 Miss 被冤枉了：{text.strip()!r}")

    # 8) 但 Hold 的**头判**照样要认（同一条恒等式，参考点是音符时刻）
    text = judge(kind="Perfect", delta=0.010, time=10.010, note=hold_note)
    expect("对不上账" not in text, f"Hold 的头判被冤枉了：{text.strip()!r}")

    # 9) Hold 也不许蒙混：两头的参考点都对不上，照样得报
    text = judge(kind="Perfect", delta=0.012, time=settle + 0.9, note=hold_note)
    expect("对不上账" in text, "Hold 两头都对不上却没报账不平")

    return problems


def check_calibration() -> list[str]:
    """延迟自校准：拿这一局 Perfect 的**中位数**调手工补偿，而且要守三条规矩。

    为什么要钉住：这东西是**会自己改设置**的（还落盘），判错了以后每一局都跟着错，
    而"错在哪"极难看出来 —— 所以四条规矩都得是硬的：

    1. 样本不足不给结论（不是校准，是猜）；
    2. 只认 Perfect、取中位数（Good/Bad 里混着被蹭掉、卡顿补判的离群值）；
    3. 单次挪动有上限（越过就说明不是"送达延迟"，得先看 `[judge]`）；
    4. **只在完整打完时**动手（结算消息到达），中途退出不动设置。
    """
    problems: list[str] = []
    try:
        from runtime import agent as agent_module
        from runtime import config
        from runtime import controller as controller_module
    except ImportError as error:  # noqa: BLE001 - frida 没装也不该让整份自检跑不起来
        return [f"导入失败，跳过延迟自校准自检：{error}"]

    from .stubs import make_config, make_controller

    def expect(condition: bool, complaint: str) -> None:
        if not condition:
            problems.append(complaint)

    # 1) 样本不足：不给结论
    agent = agent_module.Agent(Path("unused.js"), attach=True)
    for index in range(agent_module.MIN_LATENCY_SAMPLES - 1):
        agent._on_judge({"kind": "Perfect", "delta": 0.03, "noteCode": index})  # noqa: SLF001
    expect(agent.latency_sample() is None, "样本不足时不该给结论")

    # 2) 只认 Perfect、取中位数：混进一堆离谱的 Good/Bad 也不该动结论
    agent = agent_module.Agent(Path("unused.js"), attach=True)
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        for index in range(41):
            agent._on_judge({"kind": "Perfect", "delta": 0.030, "noteCode": index})  # noqa: SLF001
        agent._on_judge({"kind": "Perfect", "delta": 0.041, "noteCode": 99})  # noqa: SLF001
        for index in range(20):  # noqa: SLF001 - 离群值：被蹭掉的那种
            agent._on_judge({"kind": "Good", "delta": -0.150, "noteCode": 200 + index})
    sample = agent.latency_sample()
    expect(sample is not None, "样本够了却不肯给结论")
    if sample is not None:
        middle, count = sample
        expect(abs(middle - 0.030) < 1e-9, f"中位数不对：{middle}（应当是 0.030）")
        expect(count == 42, f"只该数 Perfect：{count}")

    # 3) 应用：手工补偿往上加，并且落盘
    controller = make_controller(make_config(auto_latency=True, latency=0.001))
    controller.agent = agent
    saved: list[float] = []
    controller.persist = lambda: saved.append(controller.config.latency)  # type: ignore[method-assign]
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        controller.handle_result(1)
    expect(
        abs(controller.options.latency - 0.031) < 1e-9,
        f"自校准没把中位数加进补偿：{controller.options.latency}",
    )
    expect(saved and abs(saved[-1] - 0.031) < 1e-9, f"自校准没有落盘：{saved}")

    # 4) 关掉开关就不许动设置
    controller = make_controller(make_config(auto_latency=False, latency=0.001))
    controller.agent = agent
    controller.persist = lambda: saved.append(controller.config.latency)  # type: ignore[method-assign]
    before = len(saved)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        controller.handle_result(1)
    expect(
        abs(controller.options.latency - 0.001) < 1e-9 and len(saved) == before,
        "自校准关着的时候不该动设置、也不该落盘",
    )

    # 5) 离谱的中位数要拒绝并说清楚（那多半是时钟映射坏了，不是送达延迟）
    crazy = agent_module.Agent(Path("unused.js"), attach=True)
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        for index in range(40):
            crazy._on_judge({"kind": "Perfect", "delta": 0.25, "noteCode": index})  # noqa: SLF001
    controller = make_controller(make_config(auto_latency=True, latency=0.0))
    controller.agent = crazy
    controller.persist = lambda: saved.append(controller.config.latency)  # type: ignore[method-assign]
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        controller.handle_result(1)
    expect(
        abs(controller.options.latency) < 1e-9,
        f"超过单次上限的中位数不该被采纳：{controller.options.latency}",
    )
    expect("自校准跳过" in buffer.getvalue(), f"拒绝时要说明理由：{buffer.getvalue()!r}")

    return problems


def check_result() -> list[str]:
    """结算那两行：数字要一个不差，读不到的字段要显式写成 `?`。

    字段名对不上时 frida 是**静默**返回 null 的（`<mirror>k__BackingField` 那次就是），
    所以"读不到"必须和"真的是 0"在输出里区分得出来 —— 这一条就是盯着这个。
    """
    problems: list[str] = []
    try:
        from runtime import agent as agent_module
        from runtime import config
        from runtime import controller as controller_module
        import main as main_module
        import planner
        import touch
    except ImportError as error:  # frida 没装也不该让整份自检跑不起来
        return [f"导入 main.py 失败，跳过结算自检：{error}"]

    agent = agent_module.Agent(Path("unused.js"), attach=True)
    agent.level_label = "SelfTest [EZ]"

    def capture(payload: dict) -> str:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            agent._on_result(payload)
        return buffer.getvalue()

    perfect = capture(
        {
            "event": "result",
            "seq": 1,
            "score": 1000000.0,
            "percent": 100.0,
            "perfect": 93,
            "good": 0,
            "bad": 0,
            "miss": 0,
            "early": 0,
            "late": 0,
            "maxCombo": 93,
            "allPerfect": True,
            "fullCombo": True,
        }
    )
    for fragment in ("1000000 分", "100.00%", "最大连击 93", "All Perfect", "Perfect 93", "Miss 0"):
        if fragment not in perfect:
            problems.append(f"满分局的结算行里少了 {fragment!r}：{perfect.strip()!r}")
    if "?" in perfect:
        problems.append(f"满分局不该出现读不到的 ?：{perfect.strip()!r}")

    messy = capture(
        {
            "event": "result",
            "seq": 2,
            "score": 987654.0,
            "percent": 98.77,
            "perfect": 80,
            "good": 9,
            "bad": 3,
            "miss": 1,
            "early": 5,
            "late": 8,
            "maxCombo": 40,
            "allPerfect": False,
            "fullCombo": False,
        }
    )
    for fragment in ("987654 分", "Good 9", "Bad 3", "Miss 1", "早 5 / 晚 8"):
        if fragment not in messy:
            problems.append(f"有失误那局少了 {fragment!r}：{messy.strip()!r}")

    # 全连但不是全 Perfect：判定标签必须是 Full Combo
    full_combo = capture(
        {"event": "result", "seq": 3, "score": 999000.0, "maxCombo": 93,
         "allPerfect": False, "fullCombo": True}
    )
    if "Full Combo" not in full_combo:
        problems.append(f"全连那局没标 Full Combo：{full_combo.strip()!r}")

    # 字段全读不到：一个都不许伪装成 0
    missing = capture({"event": "result", "seq": 4})
    if missing.count("?") < 8:
        problems.append(f"字段读不到时应当处处是 ?：{missing.strip()!r}")

    return problems

