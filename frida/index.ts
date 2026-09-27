/// <reference types="frida-gum" />
import "frida-il2cpp-bridge";

/* =============================================================================
 * auto_phigros / 谱面采集与判定观测 agent —— 入口
 * -----------------------------------------------------------------------------
 * 这个文件只干三件事：**装 hook**、**留 RPC 出口**、**把状态报给主机**。
 * 每个 hook 的来龙去脉都在它自己的模块里：
 *
 *     protocol.ts        消息协议：类型名、方法名、事件名与载荷（改协议只改这一个文件）
 *     state.ts           agent 的全部可变状态
 *     bridge.ts          与 il2cpp 打交道的零碎工具（读字段、遍历 List）
 *     hooks/chart.ts     hook 1/2  谱面原文（FromJson）、音符数（GetNoteCount）
 *     hooks/gate.ts      hook 3/4  关卡上下文（SongsItem）、闸门（SortForNoteWithFloorPosition）
 *     hooks/notes.ts     hook 5    音符表（SetInformation）：noteCode -> 音符
 *     hooks/clock.ts     hook 6    游戏时钟（ProgressControl::Update）
 *     hooks/score.ts     hook 7/8  判定流水（Perfect/Good/Bad/Miss）、结算（GetLevelResultInfo）
 *
 * 八个 hook 点的清单与完整消息协议见 ``protocol.ts`` 顶部。这个文件是 composition root：
 * 找类、按顺序装、缺了什么就报什么。
 * ========================================================================== */

import { findClass } from "./bridge";
import { installFromJsonHook, installNoteCountHook } from "./hooks/chart";
import { installProgressHook } from "./hooks/clock";
import { installLevelContextHook, installLevelStartHook } from "./hooks/gate";
import { installNoteTableHook } from "./hooks/notes";
import { installJudgeHooks, installResultHook } from "./hooks/score";
import {
    CHART_TYPE,
    GAME_INFORMATION_TYPE,
    JSON_UTILITY_TYPE,
    JUDGE_METHODS,
    judgeParameterCount,
    LEVEL_CONTROL_TYPE,
    LEVEL_RESULT_METHOD,
    LEVEL_START_METHOD,
    NOTE_TABLE_METHOD,
    PROGRESS_CONTROL_TYPE,
    SCORE_CONTROL_TYPE,
    SONGS_ITEM_TYPE
} from "./protocol";
import { state } from "./state";

/**
 * 装过的 hook 一览 —— ``revert()`` 照着它还原。
 *
 * 表放在这里而不是各 hook 模块里各留一份，是因为"装了什么"与"撤什么"必须是同一份名单。
 */
const INSTALLED_HOOKS: ReadonlyArray<{ type: string; method: string; parameters: number }> = [
    { type: JSON_UTILITY_TYPE, method: "FromJson", parameters: 2 },
    { type: CHART_TYPE, method: "GetNoteCount", parameters: 0 },
    { type: SONGS_ITEM_TYPE, method: "GetLevelStartInfo", parameters: 1 },
    { type: LEVEL_CONTROL_TYPE, method: LEVEL_START_METHOD, parameters: 0 },
    { type: LEVEL_CONTROL_TYPE, method: NOTE_TABLE_METHOD, parameters: 0 },
    { type: PROGRESS_CONTROL_TYPE, method: "Update", parameters: 0 },
    ...JUDGE_METHODS.map(kind => ({
        type: SCORE_CONTROL_TYPE,
        method: kind as string,
        parameters: judgeParameterCount(kind)
    })),
    { type: SCORE_CONTROL_TYPE, method: LEVEL_RESULT_METHOD, parameters: 0 }
];

function install(): void {
    const JsonUtility = findClass(JSON_UTILITY_TYPE);
    if (JsonUtility === null) {
        send({ event: "fatal", reason: `找不到 ${JSON_UTILITY_TYPE}` });
        return;
    }
    installFromJsonHook(JsonUtility);

    const Chart = findClass(CHART_TYPE);
    if (Chart === null) {
        send({ event: "warn", reason: `找不到 ${CHART_TYPE} 类，跳过交叉验证` });
    } else {
        installNoteCountHook(Chart);
    }

    installLevelContextHook();

    const LevelControl = findClass(LEVEL_CONTROL_TYPE);
    if (LevelControl === null) {
        send({ event: "fatal", reason: `找不到 ${LEVEL_CONTROL_TYPE}，闸门装不上` });
        return;
    }
    installLevelStartHook(LevelControl);
    installNoteTableHook(LevelControl);

    state.classes.gameInformation = findClass(GAME_INFORMATION_TYPE);
    if (state.classes.gameInformation === null) {
        send({
            event: "warn",
            reason: `找不到 ${GAME_INFORMATION_TYPE}，镜像开关与延迟读不到`
        });
    }

    const ProgressControl = findClass(PROGRESS_CONTROL_TYPE);
    if (ProgressControl === null) {
        send({ event: "warn", reason: `找不到 ${PROGRESS_CONTROL_TYPE}，游戏时钟跟不上` });
    } else {
        installProgressHook(ProgressControl);
    }

    const ScoreControl = findClass(SCORE_CONTROL_TYPE);
    if (ScoreControl === null) {
        send({ event: "warn", reason: `找不到 ${SCORE_CONTROL_TYPE}，判定流水与结算账目拿不到` });
    } else {
        installJudgeHooks(ScoreControl);
        installResultHook(ScoreControl);
    }

    send({ event: "ready", unityVersion: Il2Cpp.unityVersion, pid: Process.id });
}

/** 供主机（以及手工排障）用的 RPC 表面。 */
rpc.exports = {
    /**
     * 应答一声。
     *
     * 主机拿它当**存活探测**：进程被杀了 frida 会自己报 detached，但进程被 Android
     * 冻结（切后台缓存）时连接还在、脚本却不动了 —— 那种情况下只有一次真调用才问得出来。
     * 所以这个函数必须**一个字都不碰 il2cpp**：闸门正按着 Unity 主线程的时候它也要能立刻返回。
     */
    ping(): string {
        return "pong";
    },
    /** 还原全部 hook。 */
    revert(): boolean {
        for (const hook of INSTALLED_HOOKS) {
            findClass(hook.type)
                ?.method(hook.method, hook.parameters)
                .revert();
        }
        return true;
    },
    status(): Record<string, unknown> {
        return {
            chartSeq: state.chartSeq,
            lastChartSeq: state.lastChartSeq,
            lastContext: state.lastContext,
            gateSeq: state.gateSeq,
            releasedCount: state.releasedCount,
            notesIndexed: state.noteIndex.size,
            lastResultSeq: state.lastResultSeq
        };
    }
};

Il2Cpp.perform(() => {
    try {
        install();
    } catch (error) {
        send({ event: "fatal", reason: String(error), stack: (error as Error).stack ?? null });
    }
});
