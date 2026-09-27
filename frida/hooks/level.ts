/* =============================================================================
 * hook 9 / 10：播放状态（暂停 / 恢复）与"这一局没了"
 * -----------------------------------------------------------------------------
 * 这两个 hook 的存在理由只有一句：**这些事游戏自己知道，主机不该猜**。
 *
 * 之前主机是靠"多久没收到进度样本"推断"这一局是不是跑掉了"的 —— 那条路撤了：暂停
 * 与退出在样本上的差别只有一次抖动那么宽，而且**暂停时样本照样每 100ms 来一次**
 * （`ProgressControl` 还在跑，只是 `nowTime` 钉住不动）。推断出来的东西没法当判据。
 *
 * 反编译依据（详见 protocol.ts 里两个常量的注释）：
 *
 *     ProgressControl::Play(bool)   0x1d34270   ← 暂停 / 恢复 / 退场三条路汇到这里
 *       play == false: isPlaying = 0 / 音量归零 / audioSource.Pause() / 开 pauseBar
 *       play == true : 音量恢复 / audioSource 继续 / 关 pauseBar
 *
 *     LevelControl::OnDestroy()     0x1d25118   ← 退出、重开、结算清场都走它
 *
 * **别把它当成"音乐在走吗"的唯一来源**：全 .so 里只有四个调用点，**开谱起播一个都不是**
 * （`isPlaying` 在构造函数里就是 true）。所以"现在在不在走"要看 ``progress`` 采样里带的
 * 那个字段，"刚刚变了"才看这里的事件。踩过：一局 All Perfect，主机却一直在报"音乐没在走"。
 *
 * 顺带解决了一件计时上的老问题：**"时钟为什么停住"不用再靠 250ms 的值不变去猜** ——
 * 游戏在这一刻明说了。
 * ========================================================================== */

import { findClass, readNumberField } from "../bridge";
import { LEVEL_DESTROY_METHOD, PLAY_METHOD, PROGRESS_CONTROL_TYPE } from "../protocol";
import type { LevelGoneEvent, PlayStateEvent } from "../protocol";

/** `ProgressControl` 上那个"当前游戏时间"，暂停时它就钉住不动。 */
const NOW_TIME_FIELD = "nowTime";

function nowTime(instance: Il2Cpp.Object | null): number | null {
    return instance === null ? null : readNumberField(instance, NOW_TIME_FIELD);
}

/**
 * 播放状态：`ProgressControl::Play(bool)`。
 *
 * 这个 hook **一个字都不碰 il2cpp 的写操作**，只读一个 `nowTime` 当附注；即使读失败也照样
 * 把状态报出去 —— "停了/走了"这件事本身就是全部价值。
 */
export function installPlayStateHook(ProgressControl: Il2Cpp.Class): void {
    const play = ProgressControl.method<void>(PLAY_METHOD, 1);
    const address = play.virtualAddress;

    play.implementation = function (playing: Il2Cpp.Parameter.Type): void {
        // 先让原实现干活：它是"真的把音乐停掉/放起来"的那一步，现场要以它之后的为准
        (this as Il2Cpp.Object).method<void>(PLAY_METHOD, 1).invoke(playing);

        try {
            const message: PlayStateEvent = {
                event: "play-state",
                playing: Boolean(playing),
                time: nowTime(this as Il2Cpp.Object),
                at: Date.now()
            };
            send(message);
        } catch (error) {
            send({ event: "warn", reason: `上报播放状态失败：${String(error)}` });
        }
    };

    send({
        event: "hooked",
        target: `ProgressControl::${PLAY_METHOD}`,
        signature: `${PLAY_METHOD}(Boolean)`,
        address: address.toString(),
        rva: play.relativeVirtualAddress.toString(),
        unityVersion: Il2Cpp.unityVersion
    });
}

/**
 * 这一局没了：`LevelControl::OnDestroy()`。
 *
 * 为什么用它而不是 `Chart::Mirror` 那条链上的东西：退出到选歌、重开这一关、结算后清场
 * 最终都会销毁关卡对象，而**它一定发生在这三者里的每一个上**。主机收到就该停手：
 * 剩下的排期没有去处了。
 */
export function installLevelGoneHook(LevelControl: Il2Cpp.Class): void {
    const destroy = LevelControl.method<void>(LEVEL_DESTROY_METHOD, 0);
    const address = destroy.virtualAddress;

    destroy.implementation = function (): void {
        // 先报再让原实现跑：销毁过程里 `nowTime` 可能已经被清掉，报出来就没意义了
        try {
            const progress = findClass(PROGRESS_CONTROL_TYPE);
            const message: LevelGoneEvent = {
                event: "level-gone",
                time: progress === null ? null : nowTime(progress.tryField<Il2Cpp.Object>("_main")?.value ?? null),
                at: Date.now()
            };
            send(message);
        } catch (error) {
            send({ event: "warn", reason: `上报关卡销毁失败：${String(error)}` });
        }
        (this as Il2Cpp.Object).method<void>(LEVEL_DESTROY_METHOD, 0).invoke();
    };

    send({
        event: "hooked",
        target: `LevelControl::${LEVEL_DESTROY_METHOD}`,
        signature: `${LEVEL_DESTROY_METHOD}()`,
        address: address.toString(),
        rva: destroy.relativeVirtualAddress.toString(),
        unityVersion: Il2Cpp.unityVersion
    });
}
