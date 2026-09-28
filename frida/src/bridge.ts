/* =============================================================================
 * 与 il2cpp 打交道的零碎工具
 * -----------------------------------------------------------------------------
 * 读字段一律走 ``tryField``：字段名对不上（换版本、类型认错）就安静返回 null，
 * 绝不让观测把游戏搞崩；读不到就是 null，由调用方决定怎么退化。
 * ========================================================================== */

/** 遍历所有程序集找类，避免硬编码程序集名。 */
export function findClass(fullName: string): Il2Cpp.Class | null {
    for (const assembly of Il2Cpp.domain.assemblies) {
        const klass = assembly.image.tryClass(fullName);
        if (klass !== null) {
            return klass;
        }
    }
    return null;
}

/** 读一个 Il2Cpp.String 字段，失败/为 null 返回 null。 */
export function readStringField(obj: Il2Cpp.Object, fieldName: string): string | null {
    try {
        const value = obj.field<Il2Cpp.String>(fieldName).value;
        return value.isNull() ? null : value.content;
    } catch {
        return null;
    }
}

/** 读一个对象字段（失败、字段不存在、值为 null 都返回 null）。 */
export function readObjectField(obj: Il2Cpp.Object | null, fieldName: string): Il2Cpp.Object | null {
    if (obj === null) {
        return null;
    }
    try {
        const value = obj.tryField<Il2Cpp.Object>(fieldName)?.value;
        if (value === null || value === undefined || value.isNull()) {
            return null;
        }
        return value;
    } catch {
        return null;
    }
}

/** 读一个布尔字段；读不到返回 null。 */
export function readBoolField(obj: Il2Cpp.Object | null, fieldName: string): boolean | null {
    if (obj === null) {
        return null;
    }
    try {
        const value = obj.tryField<boolean>(fieldName)?.value;
        return typeof value === "boolean" ? value : null;
    } catch {
        return null;
    }
}

/** 读一个布尔属性（调它的 getter）；读不到返回 null。 */
export function readBoolMethod(obj: Il2Cpp.Object | null, methodName: string): boolean | null {
    if (obj === null) {
        return null;
    }
    try {
        const value = obj.method<boolean>(methodName, 0).invoke();
        return typeof value === "boolean" ? value : null;
    } catch {
        return null;
    }
}

/** 读一个数值字段（float / int）；读不到返回 null。 */
export function readNumberField(obj: Il2Cpp.Object | null, fieldName: string): number | null {
    if (obj === null) {
        return null;
    }
    try {
        const value = obj.tryField<number>(fieldName)?.value;
        return typeof value === "number" ? value : null;
    } catch {
        return null;
    }
}

/** 读某个类的静态数值字段；读不到返回 null。 */
export function readStaticNumberField(klass: Il2Cpp.Class | null, fieldName: string): number | null {
    if (klass === null) {
        return null;
    }
    try {
        const value = klass.tryField<number>(fieldName)?.value;
        return typeof value === "number" ? value : null;
    } catch {
        return null;
    }
}

/** 遍历 ``List<T>`` 的前 ``_size`` 项（容量 ``_items.length`` 通常更大），返回遍历到的个数。
 * 直接走 ``_items`` / ``_size`` 而不调托管 ``get_Item``：一张谱面上千个音符，省掉每个音符一次
 * 托管调用与一次装箱；拿不到列表就是 0。 */
export function forEachInList(
    list: Il2Cpp.Object | null,
    visit: (item: Il2Cpp.Object) => void
): number {
    if (list === null) {
        return 0;
    }
    let items: Il2Cpp.Array<Il2Cpp.Object> | null;
    let size: number;
    try {
        items = list.tryField<Il2Cpp.Array<Il2Cpp.Object>>("_items")?.value ?? null;
        size = readNumberField(list, "_size") ?? 0;
    } catch {
        return 0;
    }
    if (items === null) {
        return 0;
    }

    const count = Math.min(size, items.length);
    for (let i = 0; i < count; i++) {
        let item: Il2Cpp.Object;
        try {
            item = items.get(i);
        } catch {
            return i;
        }
        if (item === null || item.isNull()) {
            continue;
        }
        visit(item);
    }
    return count;
}

/** FNV-1a 32 位，供主机识别重复谱面。 */
export function fnv1a(text: string): string {
    let hash = 0x811c9dc5;
    for (let i = 0; i < text.length; i++) {
        hash ^= text.charCodeAt(i);
        hash = Math.imul(hash, 0x01000193);
    }
    return (hash >>> 0).toString(16).padStart(8, "0");
}
