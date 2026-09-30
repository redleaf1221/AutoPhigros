/* =============================================================================
 * 消息协议 —— agent 与主机（``main.py``）之间的唯一约定
 * -----------------------------------------------------------------------------
 * 所有类型名、方法名、事件名、载荷都集中在这里：改协议只改这一个文件。
 * 十个 hook 点：1/2 谱面（chart.ts）、3/4 关卡上下文与闸门（gate.ts）、5 音符表（notes.ts）、
 * 6 游戏时钟（clock.ts）、7/8 判定与结算（score.ts）、9/10 播放状态与关卡销毁（level.ts）。
 * ========================================================================== */

/* 参考地址 A：谱面加载链（Phigros 4.0 / libil2cpp.so，MD5 4086181c2803dc92561c0e23488fe74c）
 *   UnityEngine.JsonUtility::FromJson(String, Type)  0x3a5025c   hook 1
 *   Chart::GetNoteCount()                            0x1d28918   hook 2
 *   SongsItem::GetLevelStartInfo(Int32)              0x1c9bd80   hook 3
 *   LevelControl::SortForNoteWithFloorPosition()     0x1d25350   hook 4，闸门
 *   LevelControl::SetCodeForNote()                   0x1d2516c   发 noteCode，在 hook 5 之前
 *   LevelControl::SetInformation()                   0x1d2563c   hook 5，音符表
 */
/* 参考地址 B：时钟 / 判定 / 结算 / 关卡
 *   ProgressControl::Update()                        0x1d3483c   hook 6
 *   ScoreControl::Perfect / Good / Bad / Miss        0x1d30a84 / 0x1d30c3c / 0x1d30e20 / 0x1d30ff0   hook 7
 *   ScoreControl::GetLevelResultInfo()               0x1d3505c   hook 8
 *   ProgressControl::Play(bool)                      0x1d34270   hook 9
 *   LevelControl::OnDestroy()                        0x1d25118   hook 10
 */

/* 消息一览（各字段的类型与含义见下方 interface）
 * agent -> 主机：hooked / ready / chart / chart-parsed / level-context / level-start /
 *   level-start-released / note-index / progress / play-state / level-gone /
 *   judge / result / warn（含 fatal、chart-error）
 * 主机 -> agent：{ type: "release", payload: { seq } } / { type: "gate", payload: { enabled } }
 * 谱面总是回传，没有尺寸或开关限制；存不存由主机决定。
 */

/* ============================== 类型与方法 ============================== */

/** 谱面根类型名（无命名空间）。 */
export const CHART_TYPE = "Chart";

/** 关卡控制器；闸门与音符表都架在它的方法上。 */
export const LEVEL_CONTROL_TYPE = "LevelControl";

/** 全局游戏信息单例；``_main->levelStartInfo->mirror`` 是镜像开关。 */
export const GAME_INFORMATION_TYPE = "GameInformation";

/** 每帧驱动整局的组件；判定用的 ``nowTime``（字段 +0x88）由它算。 */
export const PROGRESS_CONTROL_TYPE = "ProgressControl";

/** 记分板：终局分数、四个判定计数，以及每一次判定的入口。 */
export const SCORE_CONTROL_TYPE = "ScoreControl";

/** 曲目条目；关卡上下文从 SongsItem::GetLevelStartInfo(Int32)（0x1c9bd80）的返回值上读。 */
export const SONGS_ITEM_TYPE = "SongsItem";

/** UnityEngine.JsonUtility，谱面反序列化的唯一咽喉点（见 hooks/chart.ts）。 */
export const JSON_UTILITY_TYPE = "UnityEngine.JsonUtility";

/** 开谱协程里第一个无条件调用的"把谱面落地"的方法，闸门：0x1d25350。 */
export const LEVEL_START_METHOD = "SortForNoteWithFloorPosition";

/** 音符表的建立点：``LevelControl::SetInformation()``（0x1d2563c）—— ``realTime`` 与按屏幕
 * 宽高比缩放过的 ``positionX`` 都在这一步才算完，所以必须挂在它这里（见 hooks/notes.ts）。 */
export const NOTE_TABLE_METHOD = "SetInformation";

/** 结算账目的唯一组装点：0x1d3505c。 */
export const LEVEL_RESULT_METHOD = "GetLevelResultInfo";

/** 暂停 / 恢复 / 退场的那个闸：``ProgressControl::Play(bool)``（0x1d34270）。全 .so 只有四个调用点
 * （``JudgeControl::CheckPause`` 0x1d20a38、``Pause::Update`` 两处、``ProgressControl::Update`` 的
 * leave 分支）；**开谱起播不经过它** —— "现在在不在走"要读 ``isPlaying``（字段偏移 ``0x82``）。 */
export const PLAY_METHOD = "Play";

/** 这一局没了的唯一信号：``LevelControl::OnDestroy()``（0x1d25118）—— 退出到选歌、重开这一关、
 * 结算之后清场都会走到这里（比"多久没收到进度样本"可靠）。 */
export const LEVEL_DESTROY_METHOD = "OnDestroy";

/** 谱面镜像开关：``LevelStartInfo`` 上的属性 ``mirror``，取它的 getter（0x1ca407c）。背后字段全名是
 * ``<mirror>k__BackingField``，IDA 渲染成 ``_mirror_k__BackingField``，照它 ``tryField`` 会静默
 * 查不到；属性 getter 的名字 ``get_mirror`` 是干净的，用它。 */
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

/** 主机开关闸门的消息类型：``{ enabled: boolean }``。关了就不阻塞主线程，只报开谱现场。 */
export const GATE_MESSAGE = "gate";

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
    /** ``ProgressControl.isPlaying``（字段偏移 ``0x82``）：游戏自己在说的"音乐在走吗"。
     * ``Play`` 起播不响（见 ``PLAY_METHOD``），所以状态必须读字段；读不到就是 ``null``。 */
    playing: boolean | null;
}

/** 一个音符的身份 —— hook 5 那张表里的一行，全是纯数字：判定时只做一次 ``Map.get()``，表建好
 * 之后 agent 不再持有任何 il2cpp 对象。字段偏移见 ``hooks/notes.ts``。 */
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

/** ``ProgressControl::Play(bool)`` 的每一次调用：游戏自己在说"我停住了 / 我又走了"。 */
export interface PlayStateEvent {
    event: "play-state";
    /** true = 从暂停恢复（**起播不走这条路**），false = 暂停 / 退场。 */
    playing: boolean;
    /** 调用这一刻游戏的 ``nowTime``（秒）—— 暂停时它就是"停在哪一秒"。 */
    time: number | null;
    at: number;
}

/** ``LevelControl::OnDestroy()``：这一局没了（退出到选歌 / 重开 / 结算清场）。 */
export interface LevelGoneEvent {
    event: "level-gone";
    /** 调用这一刻游戏的 ``nowTime``，读不到就是 null。 */
    time: number | null;
    at: number;
}

/**
 * 一次判决。``delta`` 是游戏自己算的早晚量（``nowTime − realTime``，**正数 = 晚**），
 * Miss 那条路径不传时间所以是 null；``note`` 由 ``noteCode`` 查表得到，查不到就是 null。
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

/** 一局的终局账目，字段直接抄自 ``ScoreControl``（偏移表见 ``hooks/score.ts``）。 */
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
    | PlayStateEvent
    | LevelGoneEvent
    | JudgeEvent
    | ResultEvent
    | WarnEvent;
