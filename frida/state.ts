/* =============================================================================
 * agent 的全部可变状态
 * -----------------------------------------------------------------------------
 * hooks/ 里那几个模块都往这里读写，而不是各自在自己模块里放一份模块级变量 ——
 * 这样"现在到底处在什么状态"只有一个地方能看，`status` 之类的出口也不会漏掉某一项。
 * ========================================================================== */

import type { LevelContext, NoteRef } from "./protocol";

export const state = {
    /** 本会话解析过的第几张谱面（FromJson 的计数器）。 */
    chartSeq: 0,
    /** 最近一次 FromJson 抓到的谱面序号，供随后的 level-start 关联。 */
    lastChartSeq: 0,
    /** 最近一次读到的关卡上下文，随下一张谱面一起发（第九章写死的谱面也能带上）。 */
    lastContext: null as LevelContext | null,

    /** 闸门计数；每一关开谱 +1，主机按 seq 放行。 */
    gateSeq: 0,
    /** 已经放行了多少道闸门。 */
    releasedCount: 0,

    /** 上一次回传游戏时钟的墙上时间，用来节流。 */
    lastProgressSent: 0,

    /** 已经报过账的那一关；结算方法可能被调两次（断关分支 + 结算协程），只报一次。 */
    lastResultSeq: -1,

    /**
     * noteCode -> 音符。每次 ``SetCodeForNote`` 重建一份。
     *
     * 存的是纯数字（见 ``protocol.ts`` 的 :interface:`NoteRef`），所以判定那一刻
     * 只是查一次表，不去碰任何 il2cpp 对象。
     */
    noteIndex: new Map<number, NoteRef>(),

    /** 安装 hook 时留下的类引用，采集时直接复用，不重复查表。 */
    classes: {
        gameInformation: null as Il2Cpp.Class | null
    },

    /**
     * 游戏自己在说"音乐在走还是停着"（``ProgressControl::Play(bool)`` 的最近一次调用）。
     *
     * 有了它，主机不必再靠"值多久没变"去猜暂停 —— 这一项是**观测结果**，不是推断。
     */
    playing: false
};

/** 换一关：把只属于上一局的账清掉。 */
export function resetForLevel(): void {
    state.lastProgressSent = 0;
    state.noteIndex.clear();
}
