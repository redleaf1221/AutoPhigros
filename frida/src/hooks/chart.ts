/* =============================================================================
 * hook 1 / 2：拿到谱面原文，并与游戏自己对一遍音符数
 * ========================================================================== */

import { fnv1a } from "../bridge";
import { CHART_TYPE } from "../protocol";
import type { ChartEvent, ChartParsedEvent } from "../protocol";
import { state } from "../state";

/* ==================== hook 1：谱面反序列化咽喉点 ==================== */

/** hook 1：``UnityEngine.JsonUtility::FromJson(String, Type)``（0x3a5025c）—— 所有谱面反序列化
 * 的唯一咽喉点，拿到**镜像之前**的原始 JSON 文本与目标 Type。 */
export function installFromJsonHook(JsonUtility: Il2Cpp.Class): void {
    const fromJson = JsonUtility.method<Il2Cpp.Object>("FromJson", 2);
    const address = fromJson.virtualAddress;
    const signature = fromJson.parameters.map(parameter => parameter.type.name).join(", ");

    /*
     * implementation 的类型是 (this, ...parameters: Il2Cpp.Parameter.Type[]) => T，
     * 形参必须写成那个联合类型才能过 strictFunctionTypes，所以在函数体内再收窄。
     */
    fromJson.implementation = function (
        jsonArg: Il2Cpp.Parameter.Type,
        typeArg: Il2Cpp.Parameter.Type
    ): Il2Cpp.Object {
        const json = jsonArg as Il2Cpp.String;
        const type = typeArg as Il2Cpp.Object;

        // 先调用原实现，拿到游戏真正解析出来的对象
        const result = (this as Il2Cpp.Class).method<Il2Cpp.Object>("FromJson", 2).invoke(json, type);

        // 只做旁路观测，整体 try/catch，无论发生什么都把原返回值原样返回。
        // 用返回对象的 class 名判断类型，不去解析第二个 System.Type 参数：纯原生调用，更省也更稳。
        try {
            if (!result.isNull() && result.class.type.name === CHART_TYPE) {
                const text = json.isNull() ? "" : (json.content ?? "");
                state.lastChartSeq = ++state.chartSeq;
                const message: ChartEvent = {
                    event: "chart",
                    seq: state.lastChartSeq,
                    chars: text.length,
                    hash: fnv1a(text),
                    context: state.lastContext,
                    at: Date.now(),
                    json: text
                };
                send(message);
            }
        } catch (error) {
            send({ event: "chart-error", reason: String(error) });
        }

        return result;
    };

    send({
        event: "hooked",
        target: "UnityEngine.JsonUtility::FromJson",
        signature: `FromJson(${signature})`,
        address: address.toString(),
        rva: fromJson.relativeVirtualAddress.toString(),
        unityVersion: Il2Cpp.unityVersion
    });
}

/* ==================== hook 2：交叉验证音符数量 ==================== */

/** hook 2：``Chart::GetNoteCount()``（0x1d28918）—— 游戏自己数出来的音符数，与主机从 JSON 数的对账。 */
export function installNoteCountHook(Chart: Il2Cpp.Class): void {
    const getNoteCount = Chart.method<number>("GetNoteCount", 0);

    getNoteCount.implementation = function (): number {
        const notes = (this as Il2Cpp.Object).method<number>("GetNoteCount", 0).invoke();
        try {
            const message: ChartParsedEvent = { event: "chart-parsed", notes, at: Date.now() };
            send(message);
        } catch {
            /* 观测失败不影响游戏 */
        }
        return notes;
    };
}
