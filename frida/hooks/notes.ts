/* =============================================================================
 * hook 5：音符表 —— 把 noteCode 与音符对上号
 * -----------------------------------------------------------------------------
 * 判决回调（hook 7）只拿得到一个 ``noteCode``，光有号说不出"判了哪个音符"。
 * 表建在 ``LevelControl::SetInformation()``（0x1d2563c）跑完之后：``realTime``（``+0x2C``，
 * ``time × 1.875 / bpm``，0x1d25848）与 ``positionX``（``+0x18``，按屏幕宽高比缩放，0x1d25808）
 * 都是这一步才算完，早一步抄到的是一整张 ``realTime == 0``；表里只存纯数字。
 * ========================================================================== */

import { forEachInList, readNumberField, readObjectField } from "../bridge";
import { NOTE_TABLE_METHOD } from "../protocol";
import type { NoteIndexEvent, NoteRef } from "../protocol";
import { state } from "../state";

/** 一条判定线里那两条音符列表的字段名。 */
const NOTE_LISTS: ReadonlyArray<{ field: string; above: boolean }> = [
    { field: "notesAbove", above: true },
    { field: "notesBelow", above: false }
];

/* noteCode 编码（``LevelControl::SetCodeForNote`` 0x1d2516c，写入 ChartNote ``+0x38``）：
 *   第 i 行判定线 × 1000000 + 上/下 × 100000 + 列表内序号 × 10
 * 不按公式自己推号，而是读游戏写在每个音符上的 ``noteCode``：编码依赖列表当时的顺序，
 * 而列表刚在闸门处按 ``floorPosition`` 排过，主机手里那份 JSON 的顺序已经不是它了。
 */

/** hook 5：重建音符表。只遍历 ``levelInformation.judgeLineList`` 与每条线的 ``notesAbove`` /
 * ``notesBelow``（游戏发号时遍历的正是这三条，顺序一致）；整块 try/catch，建不出来最多是判决
 * 日志里少几个字。 */
export function installNoteTableHook(LevelControl: Il2Cpp.Class): void {
    const setInformation = LevelControl.method<void>(NOTE_TABLE_METHOD, 0);
    const address = setInformation.virtualAddress;

    setInformation.implementation = function (): void {
        // 先让游戏把每个音符的数值算完（realTime / positionX），再照着它算好的抄
        (this as Il2Cpp.Object).method<void>(NOTE_TABLE_METHOD, 0).invoke();

        try {
            const count = buildNoteIndex(this as Il2Cpp.Object);
            const message: NoteIndexEvent = { event: "note-index", notes: count, at: Date.now() };
            send(message);
        } catch (error) {
            send({ event: "warn", reason: `建立音符表失败：${String(error)}` });
        }
    };

    send({
        event: "hooked",
        target: `LevelControl::${NOTE_TABLE_METHOD}`,
        signature: `${NOTE_TABLE_METHOD}()`,
        address: address.toString(),
        rva: setInformation.relativeVirtualAddress.toString(),
        unityVersion: Il2Cpp.unityVersion
    });
}

/** 走一遍谱面，把每个音符的号与身份抄进 :data:`state.noteIndex`；返回抄到几个。 */
function buildNoteIndex(levelControl: Il2Cpp.Object): number {
    state.noteIndex.clear();

    const information = readObjectField(levelControl, "levelInformation");
    const lines = readObjectField(information, "judgeLineList");

    let line = 0;
    forEachInList(lines, judgeLine => {
        const current = line++;
        for (const { field, above } of NOTE_LISTS) {
            let index = 0;
            forEachInList(readObjectField(judgeLine, field), note => {
                const position = index++;
                const info = readNote(note, current, above, position);
                if (info !== null) {
                    state.noteIndex.set(info.code, info);
                }
            });
        }
    });

    return state.noteIndex.size;
}

/** 从 ``ChartNote`` 上抄一份身份下来；号读不到（NaN / 缺失）就返回 null。 */
function readNote(note: Il2Cpp.Object, line: number, above: boolean, index: number): NoteRef | null {
    const code = readNumberField(note, "noteCode");
    if (code === null || !Number.isFinite(code)) {
        return null;
    }
    return {
        code,
        type: readNumberField(note, "type") ?? 0,
        time: readNumberField(note, "realTime") ?? 0,
        x: readNumberField(note, "positionX") ?? 0,
        hold: readNumberField(note, "holdTime") ?? 0,
        line,
        above,
        index
    };
}
