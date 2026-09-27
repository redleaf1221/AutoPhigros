/// <reference types="frida-gum" />
import "frida-il2cpp-bridge";

/* =============================================================================
 * auto_phigros / 谱面采集 agent
 * -----------------------------------------------------------------------------
 * 六个 hook 点
 * -----------------------------------------------------------------------------
 * hook 1（读谱面）：UnityEngine.JsonUtility::FromJson(System.String, System.Type)
 *     所有谱面都会经过的唯一咽喉点，拿到**镜像之前**的原始 JSON 文本。
 *     谱面只读这一遍 —— 主机就对着它规划。
 *
 * hook 2（闸门）：LevelControl::SortForNoteWithFloorPosition()
 *     谱面**真正启动**的位置，在这里把游戏闸住，并把**镜像开关**告诉主机；
 *     主机放行之前游戏一步都走不了。
 *
 * hook 3（音符数）：Chart::GetNoteCount() —— 和主机从 JSON 数出来的对数。
 * hook 4（来源上下文）：SongsItem::GetLevelStartInfo(Int32) —— 歌名、难度、资源 key。
 * hook 5（游戏时钟）：ProgressControl::Update() —— 定期把 nowTime 回传，触控对表用。
 * hook 6（结算账目）：ScoreControl::GetLevelResultInfo() —— 终局的分数与四个判定计数。
 *
 * hook 1 为什么是"所有谱面都会经过"的唯一咽喉点
 * -----------------------------------------------------------------------------
 * 1. 游戏里所有谱面（普通曲目、第六章解锁用的 _Error.json 元变体、第九章解密
 *    得到的谱面）最终都要变成同一个 Chart 实例，而
 *    LevelControl::_Start_d__46::MoveNext（0x1d27748）里只有一句：
 *        _4__this->chart = JsonUtility::FromJson<Chart>(textAsset.text);
 *
 * 2. Chart / ChartNote / JudgeLine / SpeedEvent / JudgeLineEvent 的构造函数在
 *    整个 libil2cpp.so 里**没有任何代码调用者**（只有 .data.rel.ro 里的
 *    IL2CPP method 指针槽位），二进制里也不存在任何内联的谱面 JSON 字面量。
 *    所以不存在"绕过 JsonUtility 把 Chart 直接拼出来"的路径。
 *
 * 3. FromJson<T> 虽是泛型方法，但引用类型实参走的是共享泛型实现
 *    FromJson<System.Object>，其真身是非泛型的 FromJson(String, Type)：
 *    typeof(T) 从 rgctx 取出后作为第二个参数传入。反编译 0x1f664a4 可见：
 *        v7 = JsonUtility::FromJson(json, Type::GetTypeFromHandle(typeof(T)));
 *    所以只 hook 这一个非泛型重载，就能拿到**原始 JSON 文本**与**目标 Type**。
 *
 * hook 2 为什么选 LevelControl::SortForNoteWithFloorPosition()
 * -----------------------------------------------------------------------------
 * 谱面镜像（Chart::Mirror）在整个 libil2cpp.so 里**只有一个调用者**：
 * LevelControl::_Start_d__46::MoveNext，也就是 LevelControl::Start 那个协程。
 * 顺着它就能看到关卡启动的全部顺序：
 *
 *     Get<TextAsset>(chartAddressableKey)
 *     JsonUtility::FromJson<Chart>(text)      <- hook 1 在这里
 *     levelControl.chart = chart
 *     if (levelStartInfo.mirror) Chart::Mirror(chart)     <- 镜像在这里生效
 *     DoppelgangerLevelEffect::TryStripHdUnlockNote(...)
 *     ... 填 LevelInformation（offset / noteScale / numOfNotes / speed ...）
 *     LevelControl::SortForNoteWithFloorPosition()        <- hook 2 在这里
 *     LevelControl::SetCodeForNote()
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
 * * 镜像开关在这一刻已经定下来（`Chart::Mirror` 就在它前面几十行），所以主机
 *   能拿到"这一局到底镜像没镜像"，据此把规划结果翻过来。
 *
 * 闸门只负责**同步**与**报开关**，不负责搬数据：谱面正文 hook 1 已经给过了，
 * 运行参数（speed / noteScale / offset …）规划根本用不上。少读一遍就少一个
 * "到底该信哪一份"的判断，也就少一处能出错的地方。
 *
 * 参考地址（Phigros 4.0 / libil2cpp.so，MD5 4086181c2803dc92561c0e23488fe74c）
 *   JsonUtility::FromJson(String, Type)          0x3a5025c   <- hook 1
 *   JsonUtility::FromJson<Object>(String)        0x1f664a4   共享泛型实现，内部调上面那个
 *   Chart::GetNoteCount()                        0x1d28918   <- 交叉验证
 *   SongsItem::GetLevelStartInfo(Int32)          0x1c9bd80   <- 关卡上下文
 *   Chart::Mirror()                              0x1d286ac   谱面镜像
 *   LevelStartInfo::get_mirror()                 0x1ca407c   镜像开关
 *   LevelControl::_Start_d__46::MoveNext()       0x1d27748   关卡启动协程（镜像的唯一调用点）
 *   LevelControl::SortForNoteWithFloorPosition() 0x1d25350   <- hook 2，闸门就架在这
 *   LevelControl::SetInformation()               0x1d2563c   原地改写坐标，别指望事后再读谱面
 *   ProgressControl::Play(Boolean)               0x1d34270   音乐与计时真正开跑
 *
 * 关于"替换实现后如何调用原方法"
 * -----------------------------------------------------------------------------
 * 在 method.implementation 的实现体内，通过
 *     this.method<...>("名字").invoke(原参数...)
 * 同步调用原实现即可拿到原返回值，再原样 return。依据 Frida 官方文档
 * （frida_docs/javascript-api.md, Interceptor.replace 一节）：
 *     "If you want to chain to the original implementation you can
 *      synchronously call `target` through a NativeFunction inside your
 *      implementation, which will bypass and go directly to the original
 *      implementation."
 * 而 Il2Cpp.Method.invoke 内部正是 new NativeFunction(this.virtualAddress, ...)。
 *
 * 闸门是怎么闸住的
 * -----------------------------------------------------------------------------
 * LevelControl::Start 是 Unity 协程，跑在**主线程**上；hook 2 的实现体也就跑在
 * 主线程上。于是只要在实现体里"停住不返回"，主线程就不动了：渲染停帧、协程不再
 * 推进、音乐不会开始、判定线一根都不会生成。这正是"main 说话之前，游戏不能开"。
 *
 * 停住用的是 Frida 官方的阻塞式收信（frida_docs/messages.md, "Blocking receives
 * in the target process"），方子就是官方那个例子：
 *
 *     const op = recv("release", () => {});
 *     op.wait();          // 主线程在此挂起，直到主机 script.post()
 *
 * recv() 是**一次性**的：收一条就要重新注册一次。所以每次开谱都重新注册，并且
 * 先注册后发数据 —— 反过来写的话，主机回得足够快时放行消息会落在没有接收者的
 * 空档里，游戏就永远卡住了。放行消息带 seq，收到的不是本道闸门的 seq 就继续等，
 * 免得上一关残留的放行把下一关悄悄放走。
 *
 * 附带的风险：主线程被按住太久，Android 可能弹 ANR。规划一张谱面要几秒，忍了；
 * 主机侧一律用 try/finally 保证"无论规划成功与否都放行"。
 *
 * 消息协议（与 main.py 一一对应，勿单方面改动）
 * -----------------------------------------------------------------------------
 *   { event: "hooked",              target, signature, address, rva, unityVersion }
 *   { event: "ready",               unityVersion, pid }
 *   { event: "chart",               seq, chars, hash, context, at, json }  <- 镜像前的谱面
 *   { event: "chart-parsed",        notes, at }
 *   { event: "level-context",       context, at }
 *   { event: "level-start",         seq, chartSeq, at, mirror, offset }   <- 已停在闸门上，等放行
 *   { event: "level-start-released",seq, at }                            <- 主机已放行
 *   { event: "progress",            time }                               <- 游戏时钟 nowTime
 *   { event: "warn" | "fatal" | "chart-error", reason }
 *
 * 主机 -> agent：
 *   { type: "release", payload: { seq } }   放行第 seq 道闸门
 *
 * 谱面**总是**回传，不存在任何尺寸/开关限制；存不存由主机决定。
 * ========================================================================== */

/* ============================== 常量 ============================== */

/** 谱面根类型名（无命名空间）。 */
const CHART_TYPE = "Chart";

/** 关卡控制器，闸门就架在它的开谱方法上。 */
const LEVEL_CONTROL_TYPE = "LevelControl";

/** 全局游戏信息单例，``_main->levelStartInfo->mirror`` 就是镜像开关。 */
const GAME_INFORMATION_TYPE = "GameInformation";

/** 开谱协程里第一个无条件调用的"把谱面落地"的方法。 */
const LEVEL_START_METHOD = "SortForNoteWithFloorPosition";

/**
 * 谱面镜像开关：``LevelStartInfo`` 上的属性 ``mirror``，取它的 getter。
 *
 * 为什么不用背后的字段：那是个自动属性，字段全名是 **``<mirror>k__BackingField``**，
 * 带尖括号。IDA 反编译时会把 ``<`` ``>`` 洗成 ``_`` 显示成 ``_mirror_k__BackingField``，
 * 照着它的写法去 ``tryField`` 会**静默查不到**（返回 null，不报错）——
 * 实测就这么翻过一次车。属性 getter 的名字 ``get_mirror`` 是干净的，用它。
 */
const MIRROR_METHOD = "get_mirror";

/** 每帧驱动整局的组件：``nowTime`` 就是它算出来的，触控播放跟着它走。 */
const PROGRESS_CONTROL_TYPE = "ProgressControl";

/** 记分板：终局的分数、四个判定计数、最大连击都挂在它身上。 */
const SCORE_CONTROL_TYPE = "ScoreControl";

/**
 * 结算账目的唯一组装点。
 *
 * 它把 ``ScoreControl`` 的 ``_score`` / ``_percent`` / ``maxcombo`` / ``perfect`` /
 * ``good`` / ``bad`` / ``miss`` / ``early`` / ``late`` 抄进一个 ``LevelResultInfo``。
 * 两个调用点（``ProgressControl::Update`` 的断关分支、``_LevelOver_d__438::MoveNext``
 * 的协程）都在 ``levelOver`` 之后 —— 读到的一定是终局数字，不会是打到一半的。
 */
const LEVEL_RESULT_METHOD = "GetLevelResultInfo";

/** 游戏时钟的采样间隔（毫秒）。100ms 一次：跟得上，又不会把消息通道塞满。 */
const PROGRESS_INTERVAL_MS = 100;

/** 主机放行闸门的消息类型。 */
const RELEASE_MESSAGE = "release";

/* ============================== 状态 ============================== */

let chartSeq = 0;
let lastContext: Record<string, string | null> | null = null;

/** 最近一次 FromJson 抓到的谱面序号，供 level-start 关联。 */
let lastChartSeq = 0;

/** 上一次回传游戏时钟的墙上时间，用来节流。 */
let lastProgressSent = 0;

/** 闸门计数；每一关开谱 +1，主机按 seq 放行。 */
let gateSeq = 0;
let releasedCount = 0;

/** 已经报过账的那一关；结算方法可能被调两次（断关分支 + 结算协程），只报一次。 */
let lastResultSeq = -1;

/** 安装 hook 时留下的类引用，采集时直接复用，不重复查表。 */
let gameInformationClass: Il2Cpp.Class | null = null;

/* ============================== 工具 ============================== */

/** 遍历所有程序集找类，避免硬编码程序集名。 */
function findClass(fullName: string): Il2Cpp.Class | null {
    for (const assembly of Il2Cpp.domain.assemblies) {
        const klass = assembly.image.tryClass(fullName);
        if (klass !== null) {
            return klass;
        }
    }
    return null;
}

/** 读一个 Il2Cpp.String 字段，失败/为 null 返回 null。 */
function readStringField(obj: Il2Cpp.Object, fieldName: string): string | null {
    try {
        const value = obj.field<Il2Cpp.String>(fieldName).value;
        return value.isNull() ? null : value.content;
    } catch {
        return null;
    }
}

/**
 * 读一个对象字段（失败、字段不存在、值为 null 都返回 null）。
 *
 * 全部用 ``tryField``：字段名一旦对不上（换版本、IDA 认错类型）就安静地跳过，
 * 绝不让观测把游戏搞崩。
 */
function readObjectField(obj: Il2Cpp.Object | null, fieldName: string): Il2Cpp.Object | null {
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
function readBoolField(obj: Il2Cpp.Object | null, fieldName: string): boolean | null {
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
function readBoolMethod(obj: Il2Cpp.Object | null, methodName: string): boolean | null {
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
function readNumberField(obj: Il2Cpp.Object | null, fieldName: string): number | null {
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

/** FNV-1a 32 位，供主机识别重复谱面。 */
function fnv1a(text: string): string {
    let hash = 0x811c9dc5;
    for (let i = 0; i < text.length; i++) {
        hash ^= text.charCodeAt(i);
        hash = Math.imul(hash, 0x01000193);
    }
    return (hash >>> 0).toString(16).padStart(8, "0");
}

/* ==================== hook 1：谱面反序列化咽喉点 ==================== */

function installFromJsonHook(JsonUtility: Il2Cpp.Class): void {
    const fromJson = JsonUtility.method<Il2Cpp.Object>("FromJson", 2);
    const address = fromJson.virtualAddress;
    const signature = fromJson.parameters.map(parameter => parameter.type.name).join(", ");

    /*
     * implementation 的类型是 (this, ...parameters: Il2Cpp.Parameter.Type[]) => T，
     * 形参必须写成那个联合类型才能过 strictFunctionTypes，所以在函数体内再收窄。
     */
    fromJson.implementation = function (jsonArg: Il2Cpp.Parameter.Type, typeArg: Il2Cpp.Parameter.Type): Il2Cpp.Object {
        const json = jsonArg as Il2Cpp.String;
        const type = typeArg as Il2Cpp.Object;

        // 先调用原实现，拿到游戏真正解析出来的对象
        const result = (this as Il2Cpp.Class).method<Il2Cpp.Object>("FromJson", 2).invoke(json, type);

        /*
         * 只做旁路观测。观测块整体 try/catch，无论发生什么都把原返回值原样返回，
         * 保证不影响游戏行为。
         *
         * 用返回对象的 class 名判断类型，而不是去解析第二个参数 System.Type ——
         * 读对象自身的 class 是纯原生调用，不需要再 invoke 托管方法，更安全也更便宜。
         */
        try {
            if (!result.isNull() && result.class.type.name === CHART_TYPE) {
                const text = json.isNull() ? "" : (json.content ?? "");
                lastChartSeq = ++chartSeq;
                send({
                    event: "chart",
                    seq: lastChartSeq,
                    chars: text.length,
                    hash: fnv1a(text),
                    context: lastContext,
                    at: Date.now(),
                    json: text
                });
            }
        } catch (error) {
            send({ event: "chart-error", reason: String(error) });
        }

        return result;
    };

    send({
        event: "hooked",
        target: "UnityEngine.JsonUtility::FromJson",
        signature: `FromJson(${signature})`,
        address: address.toString(),
        rva: fromJson.relativeVirtualAddress.toString(),
        unityVersion: Il2Cpp.unityVersion
    });
}

/* ==================== hook 2：交叉验证音符数量 ==================== */

function installNoteCountHook(Chart: Il2Cpp.Class): void {
    const getNoteCount = Chart.method<number>("GetNoteCount", 0);

    getNoteCount.implementation = function (): number {
        const notes = (this as Il2Cpp.Object).method<number>("GetNoteCount", 0).invoke();
        try {
            send({ event: "chart-parsed", notes, at: Date.now() });
        } catch {
            /* 观测失败不影响游戏 */
        }
        return notes;
    };
}

/* ==================== hook 3：关卡上下文 ==================== */

function installLevelContextHook(): void {
    const SongsItem = findClass("SongsItem");
    if (SongsItem === null) {
        send({ event: "warn", reason: "找不到 SongsItem 类，跳过关卡上下文 hook" });
        return;
    }

    const getLevelStartInfo = SongsItem.method<Il2Cpp.Object>("GetLevelStartInfo", 1);

    getLevelStartInfo.implementation = function (levelArg: Il2Cpp.Parameter.Type): Il2Cpp.Object {
        const level = levelArg as number;
        const info = (this as Il2Cpp.Object).method<Il2Cpp.Object>("GetLevelStartInfo", 1).invoke(level);

        try {
            if (!info.isNull()) {
                const context = {
                    songsId: readStringField(info, "songsId"),
                    songsName: readStringField(info, "songsName"),
                    songsLevel: readStringField(info, "songsLevel"),
                    songsDifficulty: readStringField(info, "songsDifficulty"),
                    chartAddressableKey: readStringField(info, "chartAddressableKey")
                };
                // 缓存下来随下一张谱面一起发（第九章写死的谱面也能带上最近一次上下文）
                lastContext = context;
                send({ event: "level-context", context, at: Date.now() });
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
    if (gameInformationClass === null) {
        return null;
    }
    try {
        const main = gameInformationClass.tryField<Il2Cpp.Object>("_main")?.value;
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
 * 延迟的构成（`LevelControl::_Start_d__46::MoveNext` 里那一行，汇编 @ 0x1d27e28）：
 *
 *     levelInformation.offset = mainOffset    + chart.offset + gameInformation.offset
 *                               （设备音频补偿）  （谱面自带）    （玩家在设置里的延迟）
 *
 * 三个都报，是因为"到底是哪个旋钮偏了"对不上账的时候很有用；`total` 是游戏真正用的那个。
 * 触控模块跟着 `nowTime` 走，`nowTime` 里已经含了 `total`，所以它**不需要也不能**
 * 再加一次 —— 这几个数在这里是给对账用的。
 *
 * `mainOffset` 是个**静态字段**（`GameInformation` 的静态区 `+0x10`），元数据里有名字，
 * 所以直接读、不用拿 `total − chart − user` 反推 —— 主机就能顺手核对三项之和等不等于 `total`。
 *
 * 读不到就是 null，主机据此不镜像 / 不偏移并报警告。
 */
function collectLevelStart(levelControl: Il2Cpp.Object, gate: number): Record<string, unknown> {
    const main = gameInformationMain();
    const startInfo = readObjectField(main, "levelStartInfo");
    const chart = readObjectField(levelControl, "chart");
    const information = readObjectField(levelControl, "levelInformation");

    return {
        event: "level-start",
        seq: gate,
        chartSeq: lastChartSeq,
        at: Date.now(),
        // 谱面镜像开关：Chart::Mirror 唯一看的就是 LevelStartInfo.mirror 这个属性
        mirror: readBoolMethod(startInfo, MIRROR_METHOD),
        offset: {
            total: readNumberField(information, "offset"),
            chart: readNumberField(chart, "offset"),
            user: readNumberField(main, "offset"),
            main: readStaticNumberField("mainOffset")
        }
    };
}

/** 读一个静态数值字段；读不到返回 null。 */
function readStaticNumberField(fieldName: string): number | null {
    if (gameInformationClass === null) {
        return null;
    }
    try {
        const value = gameInformationClass.tryField<number>(fieldName)?.value;
        return typeof value === "number" ? value : null;
    } catch {
        return null;
    }
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
 * 一条只有错误的 level-start。少了这条消息，游戏会永远停在闸门上。
 */
function announceLevelStart(levelControl: Il2Cpp.Object, gate: number): void {
    let payload: Record<string, unknown>;
    try {
        payload = collectLevelStart(levelControl, gate);
    } catch (error) {
        payload = { event: "level-start", seq: gate, at: Date.now(), error: String(error) };
    }
    try {
        send(payload);
    } catch (error) {
        send({ event: "warn", reason: `level-start 回传失败：${String(error)}` });
    }
}

function installLevelStartHook(LevelControl: Il2Cpp.Class): void {
    const sortForNote = LevelControl.method<void>(LEVEL_START_METHOD, 0);
    const address = sortForNote.virtualAddress;

    sortForNote.implementation = function (): void {
        const levelControl = this as Il2Cpp.Object;
        const gate = ++gateSeq;

        waitForRelease(gate, () => announceLevelStart(levelControl, gate));

        releasedCount++;
        send({ event: "level-start-released", seq: gate, at: Date.now() });

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

/* ============ hook 5：游戏时钟（nowTime）回流 ============ */

/**
 * 把 `ProgressControl.nowTime` 定期回传，供主机的触控播放对表。
 *
 * 为什么是它：`nowTime = audioTime − (mainOffset + chart.offset + 用户offset)`，
 * 是**判定用的时间基**，也正是规划结果里那些时刻的坐标系。跟着它走，游戏侧的延迟设置、
 * 加载、起播前那三秒、掉帧、暂停恢复就全都自动对齐了，主机一个都不用自己算。
 *
 * 两个细节：
 *
 * * **先跑原实现再读字段** —— `nowTime` 是在 `Update` 里面算出来的，读早了拿到的是上一帧。
 * * **按墙上时间节流**（100ms）—— `Update` 每帧都跑，不节流会把消息通道灌满；
 *   节流用 `Date.now()` 而不是帧计数，帧率变了采样间隔也不会跟着变。
 */
function installProgressHook(ProgressControl: Il2Cpp.Class): void {
    const update = ProgressControl.method<void>("Update", 0);
    const address = update.virtualAddress;

    update.implementation = function (): void {
        const self = this as Il2Cpp.Object;
        self.method<void>("Update", 0).invoke();

        try {
            const now = Date.now();
            if (now - lastProgressSent < PROGRESS_INTERVAL_MS) {
                return;
            }
            lastProgressSent = now;
            const time = readNumberField(self, "nowTime");
            if (time !== null) {
                send({ event: "progress", time });
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

/* ============ hook 6：结算账目回流 ============ */

/**
 * 把终局的分数与判定计数回传。
 *
 * 字段名逐个对着 IL2CPP 类型信息核过（``ScoreControl``）：
 *
 * ```
 * +0x40 _score         float     +0x54 maxcombo      int     ← 注意是小写 c
 * +0x44 _percent       float     +0x58 perfect       int
 * +0x4c _combo         int       +0x5c good / +0x60 bad / +0x64 miss
 * +0x50 isAllPerfect   bool      +0x68 early  / +0x6c late
 * +0x51 isFullCombo    bool
 * ```
 *
 * 数值字段用的是带下划线的私有名（``_score`` / ``_percent`` / ``_combo``），
 * 而同名的 ``score`` / ``combo`` 是给 UI 用的 ``Text*`` —— 读错了会拿到一个对象指针。
 */
function installResultHook(ScoreControl: Il2Cpp.Class): void {
    const getResult = ScoreControl.method<Il2Cpp.Object>(LEVEL_RESULT_METHOD, 0);
    const address = getResult.virtualAddress;

    getResult.implementation = function (): Il2Cpp.Object {
        // 先让原实现把 LevelResultInfo 拼出来，再读 —— 这一读是纯旁观，不改它
        const result = (this as Il2Cpp.Object)
            .method<Il2Cpp.Object>(LEVEL_RESULT_METHOD, 0)
            .invoke();

        try {
            const score = this as Il2Cpp.Object;
            if (gateSeq > 0 && lastResultSeq !== gateSeq) {
                lastResultSeq = gateSeq;
                send({
                    event: "result",
                    seq: gateSeq,
                    score: readNumberField(score, "_score"),
                    percent: readNumberField(score, "_percent"),
                    perfect: readNumberField(score, "perfect"),
                    good: readNumberField(score, "good"),
                    bad: readNumberField(score, "bad"),
                    miss: readNumberField(score, "miss"),
                    early: readNumberField(score, "early"),
                    late: readNumberField(score, "late"),
                    combo: readNumberField(score, "_combo"),
                    maxCombo: readNumberField(score, "maxcombo"),
                    allPerfect: readBoolField(score, "isAllPerfect"),
                    fullCombo: readBoolField(score, "isFullCombo")
                });
            }
        } catch (error) {
            send({ event: "warn", reason: `结算回传失败：${String(error)}` });
        }
        return result;
    };

    send({
        event: "hooked",
        target: `ScoreControl::${LEVEL_RESULT_METHOD}`,
        signature: `${LEVEL_RESULT_METHOD}()`,
        address: address.toString(),
        rva: getResult.relativeVirtualAddress.toString(),
        unityVersion: Il2Cpp.unityVersion
    });
}

/* ============================== 入口 ============================== */

function install(): void {
    const JsonUtility = findClass("UnityEngine.JsonUtility");
    if (JsonUtility === null) {
        send({ event: "fatal", reason: "找不到 UnityEngine.JsonUtility" });
        return;
    }

    installFromJsonHook(JsonUtility);

    const Chart = findClass(CHART_TYPE);
    if (Chart === null) {
        send({ event: "warn", reason: `找不到 ${CHART_TYPE} 类，跳过交叉验证` });
    } else {
        installNoteCountHook(Chart);
    }

    installLevelContextHook();

    const LevelControl = findClass(LEVEL_CONTROL_TYPE);
    if (LevelControl === null) {
        send({ event: "fatal", reason: `找不到 ${LEVEL_CONTROL_TYPE}，闸门装不上` });
        return;
    }
    installLevelStartHook(LevelControl);

    gameInformationClass = findClass(GAME_INFORMATION_TYPE);
    if (gameInformationClass === null) {
        send({ event: "warn", reason: `找不到 ${GAME_INFORMATION_TYPE}，镜像开关读不到` });
    }

    const ProgressControl = findClass(PROGRESS_CONTROL_TYPE);
    if (ProgressControl === null) {
        send({ event: "warn", reason: `找不到 ${PROGRESS_CONTROL_TYPE}，游戏时钟跟不上` });
    } else {
        installProgressHook(ProgressControl);
    }

    const ScoreControl = findClass(SCORE_CONTROL_TYPE);
    if (ScoreControl === null) {
        send({ event: "warn", reason: `找不到 ${SCORE_CONTROL_TYPE}，结算账目拿不到` });
    } else {
        installResultHook(ScoreControl);
    }

    send({ event: "ready", unityVersion: Il2Cpp.unityVersion, pid: Process.id });
}

/** 供后续步骤（自动打歌等）复用的 RPC 表面。 */
rpc.exports = {
    ping(): string {
        return "pong";
    },
    /** 还原全部 hook。 */
    revert(): boolean {
        findClass("UnityEngine.JsonUtility")?.method("FromJson", 2).revert();
        findClass(CHART_TYPE)?.method("GetNoteCount", 0).revert();
        findClass("SongsItem")?.method("GetLevelStartInfo", 1).revert();
        findClass(LEVEL_CONTROL_TYPE)?.method(LEVEL_START_METHOD, 0).revert();
        findClass(PROGRESS_CONTROL_TYPE)?.method("Update", 0).revert();
        findClass(SCORE_CONTROL_TYPE)?.method(LEVEL_RESULT_METHOD, 0).revert();
        return true;
    },
    status(): Record<string, unknown> {
        return { chartSeq, lastContext, gateSeq, releasedCount, lastProgressSent };
    }
};

Il2Cpp.perform(() => {
    try {
        install();
    } catch (error) {
        send({ event: "fatal", reason: String(error), stack: (error as Error).stack ?? null });
    }
});
