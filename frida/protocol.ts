/* =============================================================================
 * 消息协议 —— agent 与主机（``main.py``）之间唯一的约定
 * -----------------------------------------------------------------------------
 * 所有的类型名、方法名、事件名都集中在这里，两边改协议时改的就只有这一个文件。
 *
 * 八个 hook 点
 * -----------------------------------------------------------------------------
 * hook 1（读谱面）：UnityEngine.JsonUtility::FromJson(System.String, System.Type)
 *     所有谱面都会经过的唯一咽喉点，拿到**镜像之前**的原始 JSON 文本。
 *     谱面只读这一遍 —— 主机就对着它规划。
 *
 * hook 2（对数）：Chart::GetNoteCount() —— 和主机从 JSON 数出来的对一遍。
 * hook 3（来源上下文）：SongsItem::GetLevelStartInfo(Int32) —— 歌名、难度、资源 key。
 * hook 4（闸门）：LevelControl::SortForNoteWithFloorPosition()
 *     谱面**真正启动**的位置，在这里把游戏闸住，并把**镜像开关**告诉主机；
 *     主机放行之前游戏一步都走不了。
 * hook 5（音符表）：LevelControl::SetInformation()
 *     全部音符的运行时数值在这一步才算完（``realTime`` 由 ``time`` 与 bpm 换算、
 *     ``positionX`` 按屏幕宽高比缩放）；趁它刚跑完把号与音符对上，
 *     hook 7 才能把"判了哪个音符"说清楚。
 * hook 6（游戏时钟）：ProgressControl::Update() —— 定期把 nowTime 回传，触控对表用。
 * hook 7（判定流水）：ScoreControl::Perfect / Good / Bad / Miss —— 每一个音符的判决。
 * hook 8（结算账目）：ScoreControl::GetLevelResultInfo() —— 终局的分数与四个判定计数。
 *
 * 参考地址（Phigros 4.0 / libil2cpp.so，MD5 4086181c2803dc92561c0e23488fe74c）
 *   JsonUtility::FromJson(String, Type)          0x3a5025c   <- hook 1
 *   Chart::GetNoteCount()                        0x1d28918   <- hook 2
 *   SongsItem::GetLevelStartInfo(Int32)          0x1c9bd80   <- hook 3
 *   LevelControl::SortForNoteWithFloorPosition() 0x1d25350   <- hook 4，闸门就架在这
 *   LevelControl::SetCodeForNote()               0x1d2516c   （发 noteCode，在 hook 5 之前）
 *   LevelControl::SetInformation()               0x1d2563c   <- hook 5，音符表建在这
 *   ProgressControl::Update()                    0x1d3483c   <- hook 6
 *   ScoreControl::Perfect(float,float,Vector3,bool) 0x1d30a84  <- hook 7
 *   ScoreControl::Good(float,float,Vector3,bool)    0x1d30c3c
 *   ScoreControl::Bad(float,float)                  0x1d30e20
 *   ScoreControl::Miss(float)                       0x1d30ff0
 *   ScoreControl::GetLevelResultInfo()           0x1d3505c   <- hook 8
 *
 * agent -> 主机
 * -----------------------------------------------------------------------------
 *   { event: "hooked",               target, signature, address, rva, unityVersion }
 *   { event: "ready",                unityVersion, pid }
 *   { event: "chart",                seq, chars, hash, context, at, json }   <- 镜像前的谱面
 *   { event: "chart-parsed",         notes, at }
 *   { event: "level-context",        context, at }
 *   { event: "level-start",          seq, chartSeq, at, mirror, offset }     <- 已停在闸门上
 *   { event: "level-start-released", seq, at }                               <- 主机已放行
 *   { event: "note-index",           notes, at }                             <- 音符表建好了
 *   { event: "progress",             time }                                  <- 游戏时钟 nowTime
 *   { event: "judge",                kind, noteCode, delta, isHold, x, y, time, at, note }
 *   { event: "result",               seq, score, percent, perfect, good, bad, miss, ... }
 *   { event: "warn" | "fatal" | "chart-error", reason }
 *
 * 主机 -> agent
 * -----------------------------------------------------------------------------
 *   { type: "release", payload: { seq } }   放行第 seq 道闸门
 *
 * 谱面**总是**回传，不存在任何尺寸/开关限制；存不存由主机决定。
 * ========================================================================== */

/* ============================== 类型与方法 ============================== */

/** 谱面根类型名（无命名空间）。 */
export const CHART_TYPE = "Chart";

/** 关卡控制器，闸门与音符表都架在它的方法上。 */
export const LEVEL_CONTROL_TYPE = "LevelControl";

/** 全局游戏信息单例，``_main->levelStartInfo->mirror`` 就是镜像开关。 */
export const GAME_INFORMATION_TYPE = "GameInformation";

/** 每帧驱动整局的组件：``nowTime`` 就是它算出来的，触控播放跟着它走。 */
export const PROGRESS_CONTROL_TYPE = "ProgressControl";

/** 记分板：终局的分数、四个判定计数，以及每一次判定的入口。 */
export const SCORE_CONTROL_TYPE = "ScoreControl";

/** 曲目条目，关卡来源上下文（歌名 / 难度 / 资源 key）从它身上读。 */
export const SONGS_ITEM_TYPE = "SongsItem";

/** UnityEngine.JsonUtility —— 谱面反序列化的唯一咽喉点。 */
export const JSON_UTILITY_TYPE = "UnityEngine.JsonUtility";

/** 开谱协程里第一个无条件调用的"把谱面落地"的方法 —— 闸门。 */
export const LEVEL_START_METHOD = "SortForNoteWithFloorPosition";

/**
 * 音符表的建立点：``LevelControl::SetInformation()``。
 *
 * 为什么不是发号的 ``SetCodeForNote``：**号发下来了，但音符的运行时数值还没算**。
 * ``realTime``（``time`` 与 bpm 的换算）与按屏幕宽高比缩放过的 ``positionX`` 都是
 * ``SetInformation`` 里写的（反编译 0x1d25848 写 ``+0x2C``、0x1d25808 写 ``+0x18``），
 * 而它在发号之后才跑。在 ``SetCodeForNote`` 那里建表，抄到的就是一堆 ``realTime == 0``
 * —— 判决日志于是把每个音符都写成"@ 0.000s"。
 *
 * 两个方法都只由开谱协程调用一次（``0x1d27ef8`` 与 ``0x1d27f00``，中间夹着
 * ``SetCodeForNote``），顺序固定：闸门 -> 发号 -> SetInformation -> 按时间排序。
 */
export const NOTE_TABLE_METHOD = "SetInformation";

/** 结算账目的唯一组装点。 */
export const LEVEL_RESULT_METHOD = "GetLevelResultInfo";

/**
 * 谱面镜像开关：``LevelStartInfo`` 上的属性 ``mirror``，取它的 getter。
 *
 * 为什么不用背后的字段：那是个自动属性，字段全名是 **``<mirror>k__BackingField``**，
 * 带尖括号。IDA 反编译时会把 ``<`` ``>`` 洗成 ``_`` 显示成 ``_mirror_k__BackingField``，
 * 照着它的写法去 ``tryField`` 会**静默查不到**（返回 null，不报错）——
 * 实测就这么翻过一次车。属性 getter 的名字 ``get_mirror`` 是干净的，用它。
 */
export const MIRROR_METHOD = "get_mirror";

/** 判定流水上那四个方法：方法名就是判决名。 */
export const JUDGE_METHODS = ["Perfect", "Good", "Bad", "Miss"] as const;

/** 一个判决的名字。 */
export type JudgeKind = (typeof JUDGE_METHODS)[number];

/** 判决方法的参数个数：``Perfect`` / ``Good`` 带 Vector3 与 isHold，``Bad`` 只有两个。 */
export function judgeParameterCount(kind: JudgeKind): number {
    switch (kind) {
        case "Miss":
            return 1;
        case "Bad":
            return 2;
        default:
            return 4;
    }
}

/** 游戏时钟的采样间隔（毫秒）。100ms 一次：跟得上，又不会把消息通道塞满。 */
export const PROGRESS_INTERVAL_MS = 100;

/** 主机放行闸门的消息类型。 */
export const RELEASE_MESSAGE = "release";

/* ============================== 载荷 ============================== */

/** 关卡来源上下文；某一项读不到就是 null。 */
export interface LevelContext {
    songsId: string | null;
    songsName: string | null;
    songsLevel: string | null;
    songsDifficulty: string | null;
    chartAddressableKey: string | null;
}

/** 游戏生效的延迟（秒）的四个组成部分，详见 ``hooks/gate.ts``。 */
export interface OffsetBreakdown {
    total: number | null;
    chart: number | null;
    user: number | null;
    main: number | null;
}

export interface HookedEvent {
    event: "hooked";
    target: string;
    signature: string;
    address: string;
    rva: string;
    unityVersion: string;
}

export interface ReadyEvent {
    event: "ready";
    unityVersion: string;
    pid: number;
}

/** 一张谱面的原文（**镜像之前**）。 */
export interface ChartEvent {
    event: "chart";
    seq: number;
    chars: number;
    hash: string;
    context: LevelContext | null;
    at: number;
    json: string;
}

export interface ChartParsedEvent {
    event: "chart-parsed";
    notes: number;
    at: number;
}

export interface LevelContextEvent {
    event: "level-context";
    context: LevelContext;
    at: number;
}

/** 游戏已经停在闸门上了。 */
export interface LevelStartEvent {
    event: "level-start";
    seq: number;
    chartSeq: number;
    at: number;
    mirror: boolean | null;
    offset: OffsetBreakdown;
}

export interface ReleasedEvent {
    event: "level-start-released";
    seq: number;
    at: number;
}

export interface ProgressEvent {
    event: "progress";
    time: number;
}

/**
 * 一个音符的身份 —— hook 5 攒出来那张表里的一行。
 *
 * 全部是**纯数字**：表建好之后 agent 不再持有任何 il2cpp 对象，判定那一刻只做一次
 * ``Map.get()``，既有 O(1) 的查询，也没有"引用了已经被回收的对象"的风险。
 */
export interface NoteRef {
    /** ``ChartNote.noteCode``：游戏自己的编号，判决回调里拿到的就是它。 */
    code: number;
    /** 1 Tap / 2 Drag / 3 Hold / 4 Flick（与谱面 JSON 的 ``type`` 同源）。 */
    type: number;
    /** ``realTime``：判定时刻，秒，与 ``nowTime`` 同一个时间基。 */
    time: number;
    /** ``positionX``：沿判定线的横向偏移，**运行时**的世界单位（乘 0.9 得虚拟屏单位）。 */
    x: number;
    /** ``holdTime``：按住时长，秒；只有 Hold 非零。 */
    hold: number;
    /** 第几条判定线（谱面 ``judgeLineList`` 的下标）。 */
    line: number;
    /** 在判定线上面（``notesAbove``）还是下面（``notesBelow``）。 */
    above: boolean;
    /** 在这条线的这个列表里的第几个。 */
    index: number;
}

export interface NoteIndexEvent {
    event: "note-index";
    notes: number;
    at: number;
}

/**
 * 一次判决。
 *
 * ``delta`` 是游戏自己算的早晚量（``nowTime − realTime``，**正数 = 晚**），
 * Miss 那条路径不传时间，所以它是 null；``note`` 是拿 ``noteCode`` 从音符表里查回来的，
 * 查不到（理论上不该发生）就是 null。
 */
export interface JudgeEvent {
    event: "judge";
    kind: JudgeKind;
    noteCode: number;
    delta: number | null;
    isHold: boolean | null;
    time: number | null;
    at: number;
    note: NoteRef | null;
}

/** 一局的终局账目，字段直接抄自 ``ScoreControl``。 */
export interface ResultEvent {
    event: "result";
    seq: number;
    score: number | null;
    percent: number | null;
    perfect: number | null;
    good: number | null;
    bad: number | null;
    miss: number | null;
    early: number | null;
    late: number | null;
    combo: number | null;
    maxCombo: number | null;
    allPerfect: boolean | null;
    fullCombo: boolean | null;
}

export interface WarnEvent {
    event: "warn" | "fatal" | "chart-error";
    reason: string;
    stack?: string | null;
}

/** agent 会发给主机的所有消息。 */
export type AgentEvent =
    | HookedEvent
    | ReadyEvent
    | ChartEvent
    | ChartParsedEvent
    | LevelContextEvent
    | LevelStartEvent
    | ReleasedEvent
    | NoteIndexEvent
    | ProgressEvent
    | JudgeEvent
    | ResultEvent
    | WarnEvent;
