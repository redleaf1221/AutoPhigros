/* =============================================================================
 * hook 6：游戏时钟回流
 * ========================================================================== */

import { readBoolField, readNumberField } from "../bridge";
import { PROGRESS_INTERVAL_MS } from "../protocol";
import type { ProgressEvent } from "../protocol";
import { state } from "../state";

/**
 * ``ProgressControl`` 上"音乐在走吗"那个字段。``Play(false)`` 把它清 0，构造函数置它 1
 * —— 所以**起播之后不用等谁调 ``Play``**，读它就够了（见 protocol.ts 的 ``PLAY_METHOD``）。
 */
const PLAYING_FIELD = "isPlaying";

/**
 * 把 ``ProgressControl.nowTime`` 定期回传，供主机的触控播放对表。
 *
 * 为什么是它：``nowTime = audioTime − (mainOffset + chart.offset + 用户offset)``，
 * 是**判定用的时间基**，也正是规划结果里那些时刻的坐标系。跟着它走，游戏侧的延迟设置、
 * 加载、起播前那三秒、掉帧、暂停恢复就全都自动对齐了，主机一个都不用自己算。
 *
 * 顺带捎一个 ``isPlaying``：它是"音乐在走吗"唯一的**观测**（那个 ``Play`` hook 起播时不响，
 * 主机单靠事件会一直以为没在走）。两件事共用同一份节流，不额外花一次注入。
 *
 * 两个细节：
 *
 * * **先跑原实现再读字段** —— ``nowTime`` 是在 ``Update`` 里面算出来的，读早了拿到的是上一帧。
 * * **按墙上时间节流**（100ms）—— ``Update`` 每帧都跑，不节流会把消息通道灌满；
 *   节流用 ``Date.now()`` 而不是帧计数，帧率变了采样间隔也不会跟着变。
 */
export function installProgressHook(ProgressControl: Il2Cpp.Class): void {
    const update = ProgressControl.method<void>("Update", 0);
    const address = update.virtualAddress;

    update.implementation = function (): void {
        const self = this as Il2Cpp.Object;
        self.method<void>("Update", 0).invoke();

        try {
            const now = Date.now();
            if (now - state.lastProgressSent < PROGRESS_INTERVAL_MS) {
                return;
            }
            state.lastProgressSent = now;
            const time = readNumberField(self, "nowTime");
            if (time !== null) {
                const message: ProgressEvent = {
                    event: "progress",
                    time,
                    playing: readBoolField(self, PLAYING_FIELD)
                };
                send(message);
            }
        } catch {
            /* 时钟采样失败不影响游戏 */
        }
    };

    send({
        event: "hooked",
        target: "ProgressControl::Update",
        signature: "Update()",
        address: address.toString(),
        rva: update.relativeVirtualAddress.toString(),
        unityVersion: Il2Cpp.unityVersion
    });
}
