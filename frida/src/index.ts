/// <reference types="frida-gum" />
import "frida-il2cpp-bridge";

/* =============================================================================
 * auto_phigros / agent 入口（composition root）：找类、装 hook、留 RPC 出口。
 * -----------------------------------------------------------------------------
 * hook 点地址表与消息协议见 ``protocol.ts`` 顶部；模块分工：
 *     state.ts 状态 · bridge.ts il2cpp 工具 · hooks/ 十个 hook
 *     （1/2 chart，3/4 gate，5 notes，6 clock，7/8 score，9/10 level）。
 * install 与 revert 共用同一份名单 ``INSTALLED_HOOKS``：装了哪些就撤哪些。
 * ========================================================================== */

import { findClass } from "./bridge";
import { installFromJsonHook, installNoteCountHook } from "./hooks/chart";
import { installProgressHook } from "./hooks/clock";
import { installLevelContextHook, installLevelStartHook } from "./hooks/gate";
import { installLevelGoneHook, installPlayStateHook } from "./hooks/level";
import { installNoteTableHook } from "./hooks/notes";
import { installJudgeHooks, installResultHook } from "./hooks/score";
import {
    CHART_TYPE,
    GAME_INFORMATION_TYPE,
    JSON_UTILITY_TYPE,
    JUDGE_METHODS,
    judgeParameterCount,
    LEVEL_CONTROL_TYPE,
    LEVEL_DESTROY_METHOD,
    LEVEL_RESULT_METHOD,
    LEVEL_START_METHOD,
    NOTE_TABLE_METHOD,
    PLAY_METHOD,
    PROGRESS_CONTROL_TYPE,
    SCORE_CONTROL_TYPE,
    SONGS_ITEM_TYPE
} from "./protocol";
import { state } from "./state";

/** 装过的 hook 一览 —— ``revert()`` 照着它还原；与 install 共用同一份名单。 */
const INSTALLED_HOOKS: ReadonlyArray<{ type: string; method: string; parameters: number }> = [
    { type: JSON_UTILITY_TYPE, method: "FromJson", parameters: 2 },
    { type: CHART_TYPE, method: "GetNoteCount", parameters: 0 },
    { type: SONGS_ITEM_TYPE, method: "GetLevelStartInfo", parameters: 1 },
    { type: LEVEL_CONTROL_TYPE, method: LEVEL_START_METHOD, parameters: 0 },
    { type: LEVEL_CONTROL_TYPE, method: NOTE_TABLE_METHOD, parameters: 0 },
    { type: LEVEL_CONTROL_TYPE, method: LEVEL_DESTROY_METHOD, parameters: 0 },
    { type: PROGRESS_CONTROL_TYPE, method: "Update", parameters: 0 },
    { type: PROGRESS_CONTROL_TYPE, method: PLAY_METHOD, parameters: 1 },
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
    installLevelGoneHook(LevelControl);

    state.classes.gameInformation = findClass(GAME_INFORMATION_TYPE);
    if (state.classes.gameInformation === null) {
        send({
            event: "warn",
            reason: `找不到 ${GAME_INFORMATION_TYPE}，镜像开关与延迟读不到`
        });
    }

    const ProgressControl = findClass(PROGRESS_CONTROL_TYPE);
    if (ProgressControl === null) {
        send({ event: "warn", reason: `找不到 ${PROGRESS_CONTROL_TYPE}，游戏时钟读不到` });
    } else {
        installProgressHook(ProgressControl);
        installPlayStateHook(ProgressControl);
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
     * 存活探测：进程被 Android 冻结时连接还在、脚本却不动，只有真调用才问得出来。
     * 所以这里一个字都不碰 il2cpp —— 闸门正按住 Unity 主线程时它也要能立刻返回。
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
