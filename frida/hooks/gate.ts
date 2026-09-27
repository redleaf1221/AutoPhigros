/* =============================================================================
 * hook 3 / 4：关卡来源上下文，以及"游戏在这里停住等人"的闸门
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

/** 把 ``SongsItem::GetLevelStartInfo`` 读到的歌名 / 难度 / 资源 key 记下来。 */
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

/**
 * 采集"开谱前一刻"的现场：一个镜像开关 + 一组延迟。
 *
 * 谱面正文已经由 hook 1（FromJson）在**镜像之前**抓走了，主机就对着那份原文规划；
 * 镜像不必重算，把规划结果整体水平翻过来就是镜像后谱面的解（见 ``PlanResult.mirrored``）。
 * 所以这里不回读谱面，只报"运行时才知道、而且规划用不上但同步必须知道"的东西。
 *
 * 延迟的构成（``LevelControl::_Start_d__46::MoveNext`` 里那一行，汇编 @ 0x1d27e28）：
 *
 *     ``levelInformation.offset = mainOffset + chart.offset + gameInformation.offset``
 *                                （设备音频补偿）  （谱面自带）   （玩家在设置里的延迟）
 *
 * 三个都报，是因为"到底是哪个旋钮偏了"对不上账的时候很有用；``total`` 是游戏真正用的那个。
 * 触控模块跟着 ``nowTime`` 走，``nowTime`` 里已经含了 ``total``，所以它**不需要也不能**
 * 再加一次 —— 这几个数在这里是给对账用的。
 *
 * ``mainOffset`` 是个**静态字段**（``GameInformation`` 的静态区 ``+0x10``），元数据里有名字，
 * 所以直接读、不用拿 ``total − chart − user`` 反推 —— 主机就能顺手核对三项之和等不等于 ``total``。
 *
 * 读不到就是 null，主机据此不镜像 / 不偏移并报警告。
 */
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

/**
 * 架好一次性放行接收者，**先注册、再执行 announce、最后阻塞**。
 *
 * 顺序不能反：``recv()`` 是一次性的，先发数据后注册的话，主机回得足够快时放行
 * 消息就会落在没有接收者的空档里，游戏永远卡住。反过来则绝对安全 —— 即使主机在
 * ``op.wait()`` 之前就回了，消息也会立刻投递给已注册的接收者，``wait()`` 直接返回。
 *
 * 放行消息带 seq。对不上的（上一关残留、手工误发）不算数，重新注册接着等，
 * 免得把下一关悄悄放走。不带 seq 的放行一律认，方便手工操作。
 */
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

/**
 * 通知主机"游戏已经停在闸门上了"，并附上现场。
 *
 * 无论如何都要发出一条 ``level-start``：主机就是靠它才知道该放行的，采集失败就换成
 * 一条"什么也没读到"的 level-start（``mirror`` / ``offset`` 全是 null）外加一条 warn。
 * 少了那条 level-start，游戏会永远停在闸门上。
 */
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

/**
 * 闸门：``LevelControl::SortForNoteWithFloorPosition()``。
 *
 * 为什么选它
 * -----------------------------------------------------------------------------
 * 谱面镜像（``Chart::Mirror``）在整个 libil2cpp.so 里**只有一个调用者**：
 * ``LevelControl::_Start_d__46::MoveNext``，也就是 ``LevelControl::Start`` 那个协程。
 * 顺着它就能看到关卡启动的全部顺序：
 *
 *     Get<TextAsset>(chartAddressableKey)
 *     JsonUtility::FromJson<Chart>(text)      <- hook 1 在这里
 *     levelControl.chart = chart
 *     if (levelStartInfo.mirror) Chart::Mirror(chart)     <- 镜像在这里生效
 *     DoppelgangerLevelEffect::TryStripHdUnlockNote(...)
 *     ... 填 LevelInformation（offset / noteScale / numOfNotes / speed ...）
 *     LevelControl::SortForNoteWithFloorPosition()        <- 闸门在这里
 *     LevelControl::SetCodeForNote()                      <- 音符表在这里
 *     LevelControl::SetInformation()
 *     LevelControl::SortForAllNoteWithTime()
 *     ... 逐个 Instantiate 判定线 -> 建音符 -> 起音乐（ProgressControl::Play）
 *
 * 选最前面那一个的理由：
 *
 * * 它由启动协程**无条件调用且每关只调用一次**（XrefsTo 只有 0x1d27ef0 一处）；
 * * 此刻 Chart 已经解析完、镜像已经应用完、LevelInformation 已经填好，
 *   但判定线和音符的 GameObject 一个都还没 Instantiate，音乐也还没开始 ——
 *   卡在这里，游戏就是"万事俱备，只欠东风"；
 * * 镜像开关在这一刻已经定下来（``Chart::Mirror`` 就在它前面几十行），所以主机
 *   能拿到"这一局到底镜像没镜像"，据此把规划结果翻过来。
 *
 * 闸门只负责**同步**与**报开关**，不负责搬数据：谱面正文 hook 1 已经给过了，
 * 运行参数（speed / noteScale / offset …）规划根本用不上。少读一遍就少一个
 * "到底该信哪一份"的判断，也就少一处能出错的地方。
 *
 * 闸门是怎么闸住的
 * -----------------------------------------------------------------------------
 * ``LevelControl::Start`` 是 Unity 协程，跑在**主线程**上；闸门的实现体也就跑在主线程上。
 * 于是只要在实现体里"停住不返回"，主线程就不动了：渲染停帧、协程不再推进、音乐不会开始、
 * 判定线一根都不会生成。这正是"main 说话之前，游戏不能开"。
 *
 * 停住用的是 Frida 官方的阻塞式收信（frida_docs/messages.md，"Blocking receives in the
 * target process"），方子就是官方那个例子：
 *
 *     const op = recv("release", () => {});
 *     op.wait();          // 主线程在此挂起，直到主机 script.post()
 *
 * 附带的风险：主线程被按住太久，Android 可能弹 ANR。规划一张谱面要几秒，忍了；
 * 主机侧一律用 try/finally 保证"无论规划成功与否都放行"。
 *
 * 关于"替换实现后如何调用原方法"
 * -----------------------------------------------------------------------------
 * 在 method.implementation 的实现体内，通过
 *     ``this.method<...>("名字").invoke(原参数...)``
 * 同步调用原实现即可拿到原返回值，再原样 return。依据 Frida 官方文档
 * （frida_docs/javascript-api.md, Interceptor.replace 一节）：
 *     "If you want to chain to the original implementation you can
 *      synchronously call `target` through a NativeFunction inside your
 *      implementation, which will bypass and go directly to the original
 *      implementation."
 * 而 ``Il2Cpp.Method.invoke`` 内部正是 ``new NativeFunction(this.virtualAddress, ...)``。
 */
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
