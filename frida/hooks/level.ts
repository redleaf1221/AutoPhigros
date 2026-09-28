/* =============================================================================
 * hook 9 / 10：播放状态（暂停 / 恢复 / 退场）与"这一局没了"
 * -----------------------------------------------------------------------------
 * 这两个 hook 报的是游戏自己知道的事实：暂停与退出在"进度样本"上分不开 —— 暂停时样本照样
 * 每 100ms 来一次（只是值不变），与退出的差别只有一次抖动那么宽。
 * 地址与调用点见 ``protocol.ts`` 的 ``PLAY_METHOD`` / ``LEVEL_DESTROY_METHOD``。
 * ========================================================================== */

import { findClass, readNumberField } from "../bridge";
import { LEVEL_DESTROY_METHOD, PLAY_METHOD, PROGRESS_CONTROL_TYPE } from "../protocol";
import type { LevelGoneEvent, PlayStateEvent } from "../protocol";

/** ``ProgressControl`` 上那个"当前游戏时间"（字段 ``+0x88``），暂停时它就钉住不动。 */
const NOW_TIME_FIELD = "nowTime";

function nowTime(instance: Il2Cpp.Object | null): number | null {
    return instance === null ? null : readNumberField(instance, NOW_TIME_FIELD);
}

/** hook 9：``ProgressControl::Play(bool)``（0x1d34270）。这里不碰任何 il2cpp 写操作，只读一个
 * ``nowTime`` 当附注；即使读失败也照样把状态报出去 —— "停了 / 走了"本身就是全部价值。 */
export function installPlayStateHook(ProgressControl: Il2Cpp.Class): void {
    const play = ProgressControl.method<void>(PLAY_METHOD, 1);
    const address = play.virtualAddress;

    play.implementation = function (playing: Il2Cpp.Parameter.Type): void {
        // 先让原实现干活：它是"真的把音乐停掉 / 放起来"的那一步，现场以它之后的为准
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

/** hook 10：``LevelControl::OnDestroy()``（0x1d25118）。退出到选歌、重开这一关、结算后清场最终
 * 都会销毁关卡对象，而它一定发生在这三者里的每一个上；主机收到就该停手。 */
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
