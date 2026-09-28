/* =============================================================================
 * hook 6：游戏时钟回流
 * ========================================================================== */

import { readBoolField, readNumberField } from "../bridge";
import { PROGRESS_INTERVAL_MS } from "../protocol";
import type { ProgressEvent } from "../protocol";
import { state } from "../state";

/**
 * ``ProgressControl.isPlaying``（字段偏移 ``0x82``）：``Play(false)`` 把它清 0，构造函数置 1
 * —— 所以起播之后不用等谁调 ``Play``，读它就够了（见 ``protocol.ts`` 的 ``PLAY_METHOD``）。
 */
const PLAYING_FIELD = "isPlaying";

/** hook 6：把 ``ProgressControl.nowTime``（字段 ``+0x88``，``audioTime − (mainOffset + chart.offset +
 * 玩家 offset)``）定期回传，供主机的触控播放对表：它是判定用的时间基，游戏侧的延迟设置已含在
 * 里面。顺带捎一个 ``isPlaying``。 */
export function installProgressHook(ProgressControl: Il2Cpp.Class): void {
    const update = ProgressControl.method<void>("Update", 0);
    const address = update.virtualAddress;

    update.implementation = function (): void {
        const self = this as Il2Cpp.Object;
        // 先跑原实现再读字段：nowTime 是在 Update 里算出来的，读早了拿到的是上一帧
        self.method<void>("Update", 0).invoke();

        try {
            const now = Date.now();
            // 按墙上时间节流 100ms：Update 每帧都跑；用 Date.now() 而不是帧计数，帧率变了间隔不变
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
