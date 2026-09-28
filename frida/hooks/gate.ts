/* =============================================================================
 * hook 3 / 4：关卡来源上下文（SongsItem），以及在这里把游戏闸住的闸门
 * -----------------------------------------------------------------------------
 * 闸门实现体跑在 Unity 主线程上（``LevelControl::Start`` 是协程）：在里面阻塞收信就能让
 * 整局停住。调用原实现用 ``this.method<名字>(参数个数).invoke(原参数)``，内部是 NativeFunction。
 * ========================================================================== */

import { findClass, readBoolMethod, readNumberField, readObjectField, readStaticNumberField, readStringField } from "../bridge";
import {
    LEVEL_START_METHOD,
    MIRROR_METHOD,
    RELEASE_MESSAGE,
    SONGS_ITEM_TYPE
} from "../protocol";
import type { LevelContext, LevelContextEvent, LevelStartEvent, OffsetBreakdown, ReleasedEvent } from "../protocol";
import { resetForLevel, state } from "../state";

/* ==================== hook 3：关卡上下文 ==================== */

/** hook 3：``SongsItem::GetLevelStartInfo(Int32)``（0x1c9bd80）—— 记下歌名 / 难度 / 资源 key。 */
export function installLevelContextHook(): void {
    const SongsItem = findClass(SONGS_ITEM_TYPE);
    if (SongsItem === null) {
        send({ event: "warn", reason: `找不到 ${SONGS_ITEM_TYPE} 类，跳过关卡上下文 hook` });
        return;
    }

    const getLevelStartInfo = SongsItem.method<Il2Cpp.Object>("GetLevelStartInfo", 1);

    getLevelStartInfo.implementation = function (levelArg: Il2Cpp.Parameter.Type): Il2Cpp.Object {
        const level = levelArg as number;
        const info = (this as Il2Cpp.Object).method<Il2Cpp.Object>("GetLevelStartInfo", 1).invoke(level);

        try {
            if (!info.isNull()) {
                const context: LevelContext = {
                    songsId: readStringField(info, "songsId"),
                    songsName: readStringField(info, "songsName"),
                    songsLevel: readStringField(info, "songsLevel"),
                    songsDifficulty: readStringField(info, "songsDifficulty"),
                    chartAddressableKey: readStringField(info, "chartAddressableKey")
                };
                state.lastContext = context;
                const message: LevelContextEvent = { event: "level-context", context, at: Date.now() };
                send(message);
            }
        } catch (error) {
            send({ event: "warn", reason: `读取关卡上下文失败: ${String(error)}` });
        }

        return info;
    };
}

/* ============ hook 4：谱面真正启动 + 闸门 ============ */

/** ``GameInformation._main``，拿不到就是 null。 */
function gameInformationMain(): Il2Cpp.Object | null {
    const klass = state.classes.gameInformation;
    if (klass === null) {
        return null;
    }
    try {
        const main = klass.tryField<Il2Cpp.Object>("_main")?.value;
        if (main === null || main === undefined || main.isNull()) {
            return null;
        }
        return main;
    } catch {
        return null;
    }
}

/* 延迟构成（``LevelControl::_Start_d__46::MoveNext`` @ 0x1d27e28）：
 *   levelInformation.offset = GameInformation.mainOffset + chart.offset + gameInformation.offset
 *   字段：LevelInformation +0x20 / Chart +0x14 / GameInformation +0xac / mainOffset 静态 +0x10
 */

/** 采集"开谱前一刻"的现场：一个镜像开关 + 一组延迟。谱面正文已由 hook 1 在镜像之前抓走，镜像
 * 不必重算；三个延迟都报是为了对账（``total`` 才是游戏真正用的），``nowTime`` 里已含 ``total``。 */
function collectLevelStart(levelControl: Il2Cpp.Object, gate: number): LevelStartEvent {
    const main = gameInformationMain();
    const startInfo = readObjectField(main, "levelStartInfo");
    const chart = readObjectField(levelControl, "chart");
    const information = readObjectField(levelControl, "levelInformation");

    const offset: OffsetBreakdown = {
        total: readNumberField(information, "offset"),
        chart: readNumberField(chart, "offset"),
        user: readNumberField(main, "offset"),
        main: readStaticNumberField(state.classes.gameInformation, "mainOffset")
    };

    return {
        event: "level-start",
        seq: gate,
        chartSeq: state.lastChartSeq,
        at: Date.now(),
        // 谱面镜像开关：Chart::Mirror 唯一看的就是 LevelStartInfo.mirror 这个属性
        mirror: readBoolMethod(startInfo, MIRROR_METHOD),
        offset
    };
}

/** 一次性放行接收者：**先注册 recv()、再 announce、最后阻塞 wait()**。顺序反了，主机回得足够
 * 快时放行消息会落在没有接收者的空档里，游戏永远卡住。带 seq 的只认本关，不带 seq 的一律认。 */
function waitForRelease(gate: number, announce: () => void): void {
    let announced = false;
    for (;;) {
        let answered = false;
        let granted = false;
        const op = recv(RELEASE_MESSAGE, message => {
            answered = true;
            const payload = (message as { payload?: { seq?: number } } | null)?.payload;
            granted = payload?.seq === undefined || payload.seq === gate;
        });

        if (!announced) {
            announced = true;
            announce();
        }

        op.wait();

        if (answered && granted) {
            return;
        }
    }
}

/** 通知主机"游戏已经停在闸门上了"并附上现场。无论如何都要发出一条 ``level-start``（采集失败就
 * 发一条 mirror / offset 全 null 的）：主机就是靠它才知道该放行的。 */
function announceLevelStart(levelControl: Il2Cpp.Object, gate: number): void {
    try {
        const message: LevelStartEvent = collectLevelStart(levelControl, gate);
        send(message);
    } catch (error) {
        send({ event: "warn", reason: `读取开谱现场失败：${String(error)}` });
        send({
            event: "level-start",
            seq: gate,
            chartSeq: state.lastChartSeq,
            at: Date.now(),
            mirror: null,
            offset: { total: null, chart: null, user: null, main: null }
        });
    }
}

/** hook 4：闸门架在 ``LevelControl::SortForNoteWithFloorPosition()``（0x1d25350）。开谱协程
 * **无条件调用、每关一次**（XrefsTo 只有 0x1d27ef0）：谱面已解析、镜像已应用、参数已填，而判定线
 * 与音符的 GameObject 一个都没 Instantiate、音乐也没起 —— 停在这里游戏一步都走不了。 */
export function installLevelStartHook(LevelControl: Il2Cpp.Class): void {
    const sortForNote = LevelControl.method<void>(LEVEL_START_METHOD, 0);
    const address = sortForNote.virtualAddress;

    sortForNote.implementation = function (): void {
        const levelControl = this as Il2Cpp.Object;
        const gate = ++state.gateSeq;
        // 上一关的音符表从这一刻起作废；新的那张由 hook 5 在放行之后重建
        resetForLevel();

        waitForRelease(gate, () => announceLevelStart(levelControl, gate));

        state.releasedCount++;
        const released: ReleasedEvent = { event: "level-start-released", seq: gate, at: Date.now() };
        send(released);

        // 放行之后照常执行原实现，游戏从刚才那一帧继续往下走
        return levelControl.method<void>(LEVEL_START_METHOD, 0).invoke();
    };

    send({
        event: "hooked",
        target: `LevelControl::${LEVEL_START_METHOD}`,
        signature: `${LEVEL_START_METHOD}()`,
        address: address.toString(),
        rva: sortForNote.relativeVirtualAddress.toString(),
        unityVersion: Il2Cpp.unityVersion
    });
}
