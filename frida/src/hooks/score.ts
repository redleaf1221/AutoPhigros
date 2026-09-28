/* =============================================================================
 * hook 7 / 8：每一次判定，以及终局的账目
 * ========================================================================== */

import { readBoolField, readNumberField, readObjectField } from "../bridge";
import { JUDGE_METHODS, judgeParameterCount, LEVEL_RESULT_METHOD } from "../protocol";
import type { JudgeEvent, JudgeKind, ResultEvent } from "../protocol";
import { state } from "../state";

/* ============ hook 7：判定流水（Perfect / Good / Bad / Miss） ============ */

/* 判决方法形状（第一个参数是 ``ChartNote.noteCode``，不是时间）：
 *   Perfect(Single noteCode, Single judgeTime, Vector3 judgeTransform, Boolean isHold)   0x1d30a84
 *   Good   (Single noteCode, Single judgeTime, Vector3 judgeTransform, Boolean isHold)   0x1d30c3c
 *   Bad    (Single noteCode, Single judgeTime)                                          0x1d30e20
 *   Miss   (Single noteCode)                                                            0x1d30ff0
 * ``judgeTime = nowTime − realTime``（正数 = 晚）：调用点算 ``realTime − nowTime`` 再取反
 * （``ClickControl::Judge`` 0x1d307a0 一带、``DragControl::Judge`` 0x1d315a4）。Miss 不传时间。
 */

/**
 * hook 7：四个判决方法。判决本身仍由原实现去做，这里只挂号，再把号对着音符表翻译成人话发回主机。
 */
function installJudgeHook(ScoreControl: Il2Cpp.Class, kind: JudgeKind): void {
    const parameterCount = judgeParameterCount(kind);
    const method = ScoreControl.method<void>(kind, parameterCount);
    const address = method.virtualAddress;

    method.implementation = function (...args: Il2Cpp.Parameter.Type[]): void {
        const scoreControl = this as Il2Cpp.Object;

        // 先让原实现记分，再读现场
        scoreControl.method<void>(kind, parameterCount).invoke(...args);

        try {
            const event = describeJudge(kind, scoreControl, args);
            send(event);
        } catch (error) {
            send({ event: "warn", reason: `上报 ${kind} 判定失败：${String(error)}` });
        }
    };

    send({
        event: "hooked",
        target: `ScoreControl::${kind}`,
        signature: `${kind}(${method.parameters.map(parameter => parameter.type.name).join(", ")})`,
        address: address.toString(),
        rva: method.relativeVirtualAddress.toString(),
        unityVersion: Il2Cpp.unityVersion
    });
}

/** 把一次判决凑成一条消息：谁被判了、判成什么、早晚多少。 */
function describeJudge(
    kind: JudgeKind,
    scoreControl: Il2Cpp.Object,
    args: Il2Cpp.Parameter.Type[]
): JudgeEvent {
    const noteCode = asNumber(args[0]);
    // Bad 只有两个参数、Miss 只有一个，所以早晚量只有前三个判决有
    const delta = kind === "Miss" ? null : asNumber(args[1]);
    const isHold = kind === "Perfect" || kind === "Good" ? asBoolean(args[3]) : null;

    return {
        event: "judge",
        kind,
        noteCode,
        delta,
        isHold,
        time: readNumberField(readObjectField(scoreControl, "progressControl"), "nowTime"),
        at: Date.now(),
        note: state.noteIndex.get(noteCode) ?? null
    };
}

function asNumber(value: Il2Cpp.Parameter.Type | undefined): number {
    return typeof value === "number" ? value : Number.NaN;
}

function asBoolean(value: Il2Cpp.Parameter.Type | undefined): boolean | null {
    return typeof value === "boolean" ? value : null;
}

/** 装上四个判决 hook。 */
export function installJudgeHooks(ScoreControl: Il2Cpp.Class): void {
    for (const kind of JUDGE_METHODS) {
        installJudgeHook(ScoreControl, kind);
    }
}

/* ============ hook 8：结算账目回流 ============ */

/* ScoreControl 字段（size 0xd0；数值用带下划线的私有名，同名的 ``score`` / ``combo`` 是 UI 的 Text*）：
 *   +0x40 _score float   +0x44 _percent float   +0x4c _combo int   +0x54 maxcombo int
 *   +0x50 isAllPerfect bool   +0x51 isFullCombo bool
 *   +0x58 perfect / +0x5c good / +0x60 bad / +0x64 miss / +0x68 early / +0x6c late
 */

/** hook 8：``ScoreControl::GetLevelResultInfo()``（0x1d3505c）—— 把终局的分数与判定计数回传。
 * 两个调用点（``ProgressControl::Update`` 的断关分支、``_LevelOver__438::MoveNext`` 的协程）都在
 * ``levelOver`` 之后，读到的一定是终局数字，不会是打到一半的。 */
export function installResultHook(ScoreControl: Il2Cpp.Class): void {
    const getResult = ScoreControl.method<Il2Cpp.Object>(LEVEL_RESULT_METHOD, 0);
    const address = getResult.virtualAddress;

    getResult.implementation = function (): Il2Cpp.Object {
        // 先让原实现把 LevelResultInfo 拼出来，再读 —— 这一读是纯旁观，不改它
        const result = (this as Il2Cpp.Object)
            .method<Il2Cpp.Object>(LEVEL_RESULT_METHOD, 0)
            .invoke();

        try {
            const score = this as Il2Cpp.Object;
            if (state.gateSeq > 0 && state.lastResultSeq !== state.gateSeq) {
                state.lastResultSeq = state.gateSeq;
                const event: ResultEvent = {
                    event: "result",
                    seq: state.gateSeq,
                    score: readNumberField(score, "_score"),
                    percent: readNumberField(score, "_percent"),
                    perfect: readNumberField(score, "perfect"),
                    good: readNumberField(score, "good"),
                    bad: readNumberField(score, "bad"),
                    miss: readNumberField(score, "miss"),
                    early: readNumberField(score, "early"),
                    late: readNumberField(score, "late"),
                    combo: readNumberField(score, "_combo"),
                    maxCombo: readNumberField(score, "maxcombo"),
                    allPerfect: readBoolField(score, "isAllPerfect"),
                    fullCombo: readBoolField(score, "isFullCombo")
                };
                send(event);
            }
        } catch (error) {
            send({ event: "warn", reason: `结算回传失败：${String(error)}` });
        }
        return result;
    };

    send({
        event: "hooked",
        target: `ScoreControl::${LEVEL_RESULT_METHOD}`,
        signature: `${LEVEL_RESULT_METHOD}()`,
        address: address.toString(),
        rva: getResult.relativeVirtualAddress.toString(),
        unityVersion: Il2Cpp.unityVersion
    });
}
