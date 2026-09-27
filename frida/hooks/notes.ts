/* =============================================================================
 * hook 5：音符表 —— 把 noteCode 与音符对上号
 * -----------------------------------------------------------------------------
 * 判决回调（hook 7）只拿得到一个 ``noteCode``，光有号说不出"判了哪个音符"：
 *
 *     ChartNote.noteCode  第 i 行判定线 × 1000000 + 上/下 × 100000 + 列表内序号 × 10
 *
 * 反编译依据（``LevelControl::SetCodeForNote`` 0x1d2516c）：两条内层循环分别用
 * ``v4 = i*1000000`` 与 ``v6 = 100000 + i*1000000`` 起头，每个音符 ``+= 10``，写入的都是
 * ``STR S0, [X0, #0x38]`` —— 也就是 ChartNote 的 ``noteCode`` 字段（IDA 给这个偏移贴的
 * 字段名 ``judgeControl`` 是错的，ChartNote 里没有这个字段）。
 *
 * **但这里不去推那个编码。** 音符表是"问游戏要"的：遍历它自己的 ``judgeLineList`` 与每条线的
 * ``notesAbove`` / ``notesBelow``，读它自己写在每个音符上的 ``noteCode``。理由是编码依赖
 * 列表**当时的顺序** —— 而列表刚在闸门那里被 ``SortForNoteWithFloorPosition`` 按
 * ``floorPosition`` 排过一遍，主机手里那份 JSON 的原始顺序已经不是它了。自己拿 JSON 推号，
 * 就得在主机上把排序再实现一遍，等于把游戏内部逻辑抄一份出来；抄错了还不会报错，
 * 只会"查出来的音符全都不对"。
 *
 * 建表必须在 SetInformation 之后，不能在发号之后
 * -----------------------------------------------------------------------------
 * 发号的 ``SetCodeForNote`` 跑在 ``SetInformation`` **前面**，而音符的运行时数值是后者算的：
 *
 * * ``realTime``（``+0x2C``）＝ ``time × 1.875 / bpm``，反编译 0x1d25840 一带；
 * * ``positionX``（``+0x18``）在这一步按 ``screenW / screenH`` 缩放（0x1d25800 一带）。
 *
 * 在 ``SetCodeForNote`` 那里建表，抄到的就是一整张 ``realTime == 0`` 的表 —— 实测的表现是
 * 判决日志把每个音符都写成 ``@ 0.000s``（一个在 89.969s 被判掉的 Flick，报出来是"0.000 秒"）。
 * 所以挂钩点选 ``SetInformation``：号已经发完，数值也刚刚算完。
 *
 * 表里存的是纯数字，建好之后 agent 不再持有任何 il2cpp 对象 —— 判定那一刻只是查一次表。
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

/**
 * 重建音符表。在 ``LevelControl::SetInformation`` **跑完之后**调用。
 *
 * 只遍历 ``LevelInformation.judgeLineList`` 与每条线的 ``notesAbove`` / ``notesBelow``：
 * 这三条列表就是游戏发号时遍历的那三条，顺序与之一致；``line`` / ``above`` / ``index``
 * 三个下标是遍历出来的，与游戏自己的 ``judgeLineIndex``（上下） / ``noteIndex``（列表内序号）
 * 语义对应。
 *
 * 整块 try/catch：表建不出来最多是判决日志里少几个字，绝不能让游戏崩在开谱路上。
 */
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

/** 走一遍谱面，把每个音符的号与身份抄进 :data:`state.noteIndex`。返回抄到几个。 */
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
