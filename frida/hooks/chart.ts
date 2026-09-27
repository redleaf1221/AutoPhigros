/* =============================================================================
 * hook 1 / 2：拿到谱面原文，并与游戏自己对一遍音符数
 * ========================================================================== */

import { fnv1a } from "../bridge";
import { CHART_TYPE } from "../protocol";
import type { ChartEvent, ChartParsedEvent } from "../protocol";
import { state } from "../state";

/* ==================== hook 1：谱面反序列化咽喉点 ==================== */

/**
 * 为什么 ``JsonUtility::FromJson`` 是"所有谱面都会经过"的唯一咽喉点
 * -----------------------------------------------------------------------------
 * 1. 游戏里所有谱面（普通曲目、第六章解锁用的 ``_Error.json`` 元变体、第九章解密
 *    得到的谱面）最终都要变成同一个 Chart 实例，而
 *    ``LevelControl::_Start_d__46::MoveNext``（0x1d27748）里只有一句：
 *        ``_4__this->chart = JsonUtility::FromJson<Chart>(textAsset.text);``
 *
 * 2. Chart / ChartNote / JudgeLine / SpeedEvent / JudgeLineEvent 的构造函数在
 *    整个 libil2cpp.so 里**没有任何代码调用者**（只有 .data.rel.ro 里的
 *    IL2CPP method 指针槽位），二进制里也不存在任何内联的谱面 JSON 字面量。
 *    所以不存在"绕过 JsonUtility 把 Chart 直接拼出来"的路径。
 *
 * 3. ``FromJson<T>`` 虽是泛型方法，但引用类型实参走的是共享泛型实现
 *    ``FromJson<System.Object>``，其真身是非泛型的 ``FromJson(String, Type)``：
 *    ``typeof(T)`` 从 rgctx 取出后作为第二个参数传入。反编译 0x1f664a4 可见：
 *        ``v7 = JsonUtility::FromJson(json, Type::GetTypeFromHandle(typeof(T)));``
 *    所以只 hook 这一个非泛型重载，就能拿到**原始 JSON 文本**与**目标 Type**。
 */
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

        /*
         * 只做旁路观测。观测块整体 try/catch，无论发生什么都把原返回值原样返回，
         * 保证不影响游戏行为。
         *
         * 用返回对象的 class 名判断类型，而不是去解析第二个参数 System.Type ——
         * 读对象自身的 class 是纯原生调用，不需要再 invoke 托管方法，更安全也更便宜。
         */
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

/** ``Chart::GetNoteCount()``：游戏自己数出来的音符数，用来和主机从 JSON 数出来的对账。 */
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
