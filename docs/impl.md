# auto_phigros 实现说明

这份文件讲**为什么这么做**：每个 hook 为什么选在那儿、判定到底怎么算、踩过哪些坑。
怎么装、怎么用看 [README.md](../README.md)；游戏内部（函数地址、字段偏移、伪代码）
看 [Phigros4.0-音游内核逆向报告.md](Phigros4.0-音游内核逆向报告.md)。

结构、Hook 点的选择、开谱闸门与谱面镜像、垂直判定与判定线坐标、规划器（事件汇率 /
在位够一帧 / Flick 起手 / 缓存）、触控（scrcpy / 时钟同步 / 排期诊断 / 坐标）、
运行时控制台的回显规矩与存活探测、可视化为什么是 qtrle、自检的判据、
消息协议 —— 都在下面。

## 结构

```
auto_phigros/
  frida/            frida agent 的源码（esbuild 打包成 target/_.js）
    index.ts        入口：找类、装 hook、留 RPC 出口
    protocol.ts     消息协议：类型名、方法名、事件名与载荷（改协议只改这一个文件）
    state.ts        agent 的全部可变状态
    bridge.ts       与 il2cpp 打交道的零碎工具（读字段、遍历 List）
    hooks/chart.ts  hook 1/2  谱面原文（FromJson）、音符数（GetNoteCount）
    hooks/gate.ts   hook 3/4  关卡上下文（SongsItem）、闸门（SortForNoteWithFloorPosition）
    hooks/notes.ts  hook 5    音符表：noteCode -> 音符（SetInformation）
    hooks/clock.ts  hook 6    游戏时钟（ProgressControl::Update）
    hooks/score.ts  hook 7/8  判定流水（Perfect/Good/Bad/Miss）、结算（GetLevelResultInfo）
    hooks/level.ts  hook 9/10 播放状态（Play：暂停/恢复）、这一局没了（LevelControl::OnDestroy）
  src/                  源码根：所有 Python 都在这一层（跑法见 README 的"目录一览"）
    main.py           薄入口：读/建 config.json → 起主干（控制台）→ 收工
    planner.py        规划模块（落盘即缓存），可被调用、也可单独运行
    judge.py          算法体检：按游戏真实判定重放规划（丢音 / 蹭键 / 分布 / 分数）
    touch.py          触控模块：时钟、调度器、命令行，可单独运行
    render.py         可视化模块，把 .psap 渲染成视频
    selftest.py       薄入口：代码自检（判据与替身在 tests/ 里）
    runtime/          主干那一套（不可独立运行；包内用相对 import）
      console.py        主干：命令表 + 读输入（跑在主线程上）
      controller.py     设备 / 后端 / 时钟 / 注入 / 播放器 / 收工
      agent.py          一次注入的全都：会话、闸门、消息分发、判决对账、结算
      config.py         固定常量 + 落盘的 config.json（ROOT = src 的上一层）
      options.py        运行时设置（控制台改的就是它）
      output.py         进程里唯一的写者：整行原子输出 + 补回提示符 + 抄一份到 logs/
    formats/
      storage.py        .psap 编解码 + 落盘
    tools/
      device_log.py     运行日志的读回（judge --compare 拿它当"设备标准答案"）
    backends/         触控后端，按名字现 import
      __init__.py     对外只暴露 catalog() / create() / register()
      utils.py        契约：Backend 协议 + 虚拟屏 → 设备像素
      registry.py     后端注册表
      scrcpy.py       scrcpy 控制协议（目前唯一的真后端）
      recording.py    干跑：不连设备，只记录（自检与 backend recording 用）
    tests/            自检包：coverage / archive / runtime / console / liveness / accounting /
                      attach / settings / referee / stubs / cli
    algorithms/
      __init__.py     对外只暴露 catalog() / create() / register()
      utils.py        契约：TouchEvent、PlanResult（含 mirrored()）、Progress、Planner
      chart.py        官谱模型与解析
      geometry.py     虚拟屏幕、音符摆位、判定区（垂直判定见下）
      judging.py      判定规则的唯一出处 + 重放裁判（judge.py 与覆盖率判据都读它）
      track.py        事件时间轴：按毫秒收事件、压掉原地不动的 MOVE
      registry.py     规划器注册表，按名字现 import
      conservative.py / radical.py / geometric.py
  config.json       落盘的配置（第一次跑按默认值建；不进版本库）
  charts/           采集到的谱面（save-chart on）
  plans/            规划结果 = 缓存（默认写）
  renders/          渲染出来的视频
  target/_.js       构建产物
```

agent 拆成模块之后，`../frida/index.ts` 只剩"找类、按顺序装、缺什么报什么"这一件事，
每个 hook 的来龙去脉（为什么选这个点、反编译依据、踩过的坑）都跟着它自己那个文件走 ——
不必在一个一千行的文件里上下来回找。

规划器**按名字现 import**（`registry.create`），所以没被选中的算法连同它的依赖都不会加载；
加一个新规划器只要写一个模块、在 `registry._BUILTIN` 里加一行。

规划器不打印任何东西：它只通过注入进来的 `Progress` 汇报进度（命令行给 tqdm 实现，
自检给 `SilentProgress`），把告警放进 `PlanResult.warnings` 交给调用方决定怎么显示。

## Hook 点的选择

`UnityEngine.JsonUtility::FromJson(System.String, System.Type)`

选它的三条理由（详见 [`../frida/hooks/chart.ts`](../frida/hooks/chart.ts) 顶部注释）：

1. `LevelControl::_Start_d__46::MoveNext`（`0x1d27748`）里只有一句
   `_4__this->chart = JsonUtility::FromJson<Chart>(textAsset.text);`
   —— 普通曲目、第六章解锁用的 `Chart_*_Error.json` 元变体、第九章解密得到的谱面，
   最终都要变成同一个 `Chart` 实例。
2. `Chart` / `ChartNote` / `JudgeLine` / `SpeedEvent` / `JudgeLineEvent` 的构造函数在整份
   `libil2cpp.so` 里**没有任何代码调用者**（只有 `.data.rel.ro` 里的 IL2CPP method 指针槽位），
   二进制里也不存在内联的谱面 JSON 字面量 —— 不存在绕过 `JsonUtility` 的路径。
3. `FromJson<T>` 虽是泛型，但引用类型实参走共享泛型实现，真身就是这个非泛型重载
   （反编译 `0x1f664a4` 可见 `JsonUtility::FromJson(json, typeof(T))`）。所以**只 hook 一个
   非泛型方法**就覆盖所有 `FromJson<T>` 调用，并直接拿到原始字符串。

顺带挂了这些辅助 hook：

| hook | 地址 | 作用 |
|---|---|---|
| `Chart::GetNoteCount()` | `0x1d28918` | 回传游戏真正解析出的音符数，用来交叉验证抓到的 JSON |
| `SongsItem::GetLevelStartInfo(Int32)` | `0x1c9bd80` | 回传歌曲 id / 难度 / Addressable key，给谱面标注来源 |
| `LevelControl::SetInformation()` | `0x1d2563c` | 建音符表：`noteCode` → 音符（判定流水的翻译词典）。**必须在它之后**：`realTime` 与缩放过的 `positionX` 都是它算的 |
| `ProgressControl::Update()` | `0x1d3483c` | 定期回传 `nowTime`，触控播放按它对表 |
| `ScoreControl::Perfect/Good/Bad/Miss` | `0x1d30a84` / `0x1d30c3c` / `0x1d30e20` / `0x1d30ff0` | 每一次判决：判了哪个音符、判成什么、早/晚多少 |
| `ScoreControl::GetLevelResultInfo()` | `0x1d3505c` | 回传终局账目：分数、四个判定计数、最大连击 |
| `ProgressControl::Play(Boolean)` | `0x1d34270` | 游戏自己在说"音乐停了 / 走了"（暂停菜单的每条路都汇到它这里） |
| `LevelControl::OnDestroy()` | `0x1d25118` | 这一局没了：退出到选歌 / 重开 / 结算清场都会走它 |

### 暂停、退场：让游戏自己说，别拿计时去猜

`ProgressControl::Play(bool)`（`0x1d34270`）是**播放状态的唯一总闸**，反编译出来的调用点：

| 谁调它 | 什么时候 | 参数 |
|---|---|---|
| `JudgeControl::CheckPause` `0x1d20a38` | 右上角暂停手势（`pauseTime = 1.2`，见 §6.8） | `false` |
| `Pause::Update` `0x1d33920` 尾部 | 暂停动作（`SetActive(false)` + `SimpleDelay(0.5s)` 之后） | `false` |
| `Pause::Update` 里 tag 为 `"Resume"` 的按钮 | 点"继续" | `true` |
| `ProgressControl::Update` | 开谱起播 | `true` |

`play == false` 的实现体里干的是：`isPlaying = 0`、音量归零、`audioSource.Pause()`、打开
`pauseBar` / `pauseCamera`、把 Guide 的 `Animator.speed` 设 0 —— **游戏自己的时钟就是在这一刻
停住的**。所以"暂停/恢复"根本不用主机去推断（原先靠"250ms 值不变"认，现在有信号了）。

`Pause::Update` 里剩下的分支（同一帧里对触点做 `Physics2D` 射线、读命中物体的 tag）：

* `"Retry"` → `SceneManager.LoadScene("Level")` —— 重开这一关；
* `"Back"` → `GameInformation.turnToScene = "Loading"`、`nextScene = "ChapterSelector"`，
  再 `LoadScene("TurnToScene")` —— 退出到选歌；
* 两者之后都会（经 `Pause::StartNextScene` 的协程）把 `GameInformation._main` 销毁。

而 `LevelControl::OnDestroy()` 是**这一局没了的唯一信号**：退出、重开、结算清场最终都会
销毁关卡对象。主机收到就停手（停触控、清时钟、明说一句）。

> 为什么不看"多久没收到进度样本"：暂停与退出在样本上的差别只差一次抖动那么宽，而且**暂停时
> 样本照样每 100ms 来一次**（`ProgressControl` 还在跑，只有 `nowTime` 钉住不动）。这条推断
> 实现过、也撤了 —— 猜出来的东西不能当判据。

### 裁判与实机逐音符对齐（`judge.py --compare`）

裁判（`../src/algorithms/judging.py`）的职责是**复现游戏会怎么判**。它准不准不该靠感觉：
`judge.py --compare <日志>` 把裁判的每一条判定与实机日志里的**同一个音符**对上
（身份是 `(线, 上/下, 同侧第几个)`，就是游戏自己的 `noteCode` 那套）。

当前基线（`../logs/2026-09-27_19-49-47.log`，AT 谱 + geometric，1156 音符）：

```
设备（日志）：1152P  2G  1B  1M   951099 分 / 99.77%   最大连击 615
裁判（本机）：1152P  2G  1B  1M   951099 分 / 99.77%   最大连击 615
逐音符比出 1156 条：一致 1156，不一致 0
```

**这是验收线**：以后任何改动都要让它保持 0（或把新的不一致解释清楚再落地）。

规则全部逐行对着反编译核过，出处都写在代码注释里：

| 规则 | 出处 | 我原先错在哪 |
|---|---|---|
| 候选窗 `realTime ∈ (nowTime − 0.18, nowTime + 0.22)` | `CheckNote` 第 67-123 行 | 两个常量在 bisect 里**写反**了 —— 于是"未来音符被偷"这类事件裁判看不见 |
| `touchPos >= 1.9` 严格跳过 | 第 189 行 | 用了 `>` |
| `badTime = 0.22 + (touchPos − 0.9) × 0.08 × (−0.5)`，只收紧**早**那侧 | 第 199-212 行 | 没建模 |
| 判档 `\|Δ\| < 0.08 / < 0.18 / 否则 Bad`，**无上界** | `ClickControl::Judge` 第 917-920 行 | 在 0.22 切了一刀（把判档与"能不能判到"混成一条） |
| 挑选度量 `\|noteX − lateral\| + \|normal\|/2.2`，平局**先遇到的胜出** | `CheckNote` 第 344/383/407-408 行 | 分别错在"到音符判定点"与"按线号扫" |
| `minDeltaTime` 每帧被写成 10000.0f → 那条早判守卫是死代码 | 第 124/186/425 行 | 把它当成了"允许多早"的输入参数（报告 §9 曾列为未解之谜） |
| Hold 头判 `\|Δ\| ≥ 0.18` **不判档**，清标记继续等 | `HoldControl::Judge` 第 88-137 行 | 当成 Tap 判 Bad |
| Hold 身体宽限 `_safeFrame = 2`（忍 3 帧，第 4 帧判 Miss），**只在头判之后跑** | 第 241-295 行 | 没建模 → 看不见"头判被蹭掉、手指没留住"的早判 Miss |
| Hold 收尾在 `realTime + holdTime − 0.22`，报的是**头判**的 Δ | 第 180/303-337 行 | 这条一开始就猜对了 |

两个**不是规则、是物理**的量，也写成了常量并注明来源：

* `PROCESS_DELAY = 0.029`：我们发出 → 游戏在处理它的那一帧读 `nowTime`，实测 1149 条同档判定
  的中位差 +28.9ms、分布 12~48ms（一帧量化）。固定部分可建模，**帧相位不可知** —— 这是
  主机侧裁判理论上到不了的地方。
* `METRIC_EPSILON = 0.005`：度量来自游戏的**活** transform，我这边是插值出来的线位置，
  差在千分之几以内应当算"平局"、交给扫描顺序（实机 147.500/152.328 那两处就是这么定的）。


这条路上踩过两个坑，都是实机报回来的，记在这儿免得再踩：

**坑 1：把计划里紧接着的 `MOVE` 改写成 `DOWN` 来"重新按住"。** 那是把 flick 的**位移**吃掉
了：游戏判 flick 靠的是"按下之后有没有那一下跳动"，按在目标位置上不动，`isNewFlick` 永远
不亮。表现是"恢复之后有些 flick 划不出来"。

**坑 2：暂停期间不按住时钟。** 暂停时 `nowTime` 要么完全冻住、要么缓慢爬升；后一种情况下
`min(h−v)`（2 秒窗口）会被窗口里最老的那个样本拖住，对齐能偏到 1~2 秒之前，于是恢复后每个
事件都被算成"迟到太多"（`LATE_SKIP = 0.15s`）直接丢掉 —— 表现是"恢复之后像没在打"。而且它
**不稳定**：完全冻住的暂停反而自愈（值停过 → 重锚），取决于暂停期间时钟怎么动。自检里量过：
爬升速率 0.02 / 0.1 / 0.5 时老做法分别偏 −1862ms / −1710ms / −950ms。

所以现在是**信号驱动**的两步：

| 事件 | 动作 | 为什么 |
|---|---|---|
| `Play(false)` | `clock.hold()` + `player.release_all()` | 暂停期间游戏的时间不是时间源，一个事件都不该发；手指留在屏幕上更糟 —— 暂停菜单是按**手指位置**射线找按钮的，停在那儿可能替人把"重开"按了 |
| `Play(true)` | `player.resume()` + `clock.release()` | 按回**原位**（保住 flick 的位移），并把对齐**清零重来**（暂停期间的样本一律不采信） |

`resume()` 只设旗子并叫醒播放器线程（睡眠用 `Event.wait` 而不是 `sleep`）—— hold 的主体
只有约 67ms 的缺席容忍（`_safeFrame = 2`），等下一个事件到点再顺手按下去就断了。发送本身在
播放器线程里做：后端（一条 scrcpy 连接）只能有一个线程写。一个例外：计划里紧接着那一帧本来
就有同一个指针的 `DOWN` 时不补按 —— 同一个 pointer 连按两次是不合法的输入序列。

兜底：万一 `Play(true)` 那个信号没送到，`GameClock` 会在按住期间看一眼**速率**（1.5 秒窗口里
值走了 0.4 秒以上 = 游戏真在跑），自己放行并**报警**（`take_auto_release`）。看速率而不是
"总共走了多少"是刻意的：缓慢爬升攒一分钟也能攒出 1 秒，按总量判会在暂停中途误放行。


### 判定流水：漏音要能指名道姓

四个判决方法的形状（反编译）：

```
Perfect(Single noteCode, Single judgeTime, Vector3 judgeTransform, Boolean isHold)
Good   (Single noteCode, Single judgeTime, Vector3 judgeTransform, Boolean isHold)
Bad    (Single noteCode, Single judgeTime)
Miss   (Single noteCode)
```

第一个参数是**音符编号**（`ChartNote.noteCode`），不是时间 —— 形参名容易看岔，四个调用点
（`ClickControl::Judge` / `DragControl::Judge` / `FlickControl::Judge` / `HoldControl::Judge`）
写的都是 `noteFoo->noteCode`。`judgeTime = nowTime − realTime`，**正数 = 晚**；Miss 那条路径
不传时间，所以是 `null`。

光有编号说不出人话，所以要把编号与音符对上号（hook 5）。这张表是
**问游戏要**的：遍历它自己的 `judgeLineList` 与每条线的 `notesAbove` / `notesBelow`，读它自己
写在每个音符上的 `noteCode`。不去推那个编码（`行号 × 1000000 + 上下 × 100000 + 序号 × 10`），
因为编码依赖列表**当时的顺序** —— 而列表刚在闸门那里被 `SortForNoteWithFloorPosition` 按
`floorPosition` 排过一遍，主机手里那份 JSON 的原始顺序已经不是它了。自己拿 JSON 推号，就得在
主机上把排序再实现一遍；抄错了还不报错，只会"查出来的音符全都不对"。

**建表挂在 `LevelControl::SetInformation` 上，不是发号的 `SetCodeForNote`。** 号确实在
`SetCodeForNote` 就发完了，但音符的运行时数值要到 `SetInformation` 才算完：`realTime`
（`+0x2C`，`time × 1.875 / bpm`）与按屏幕宽高比缩放过的 `positionX`（`+0x18`）都是它写的。
挂早了抄到的就是一整张 `realTime == 0` 的表 —— 实测过一个 89.969 秒才判掉的 Flick 被记成
"@ 0.000s"。这个坑报告里其实写着（`SetInformation` 对每个音符做什么），选 hook 点时没回头查。

表里存的是**纯数字**，建好之后 agent 不再持有任何 il2cpp 对象，判定那一刻只是查一次表：

```
[judge] Miss    Drag @ 111.062s  线 4   上 第 12   个  x= +2.250  #4100120  判定于 111.212s
[judge] Good    Tap  @  83.625s  线 0   下 第  3   个  x= +1.080  #    30  晚 32ms
```

默认只打 Miss / Good / Bad —— 它们本来就"值得看一眼"，一局也就那么几条。Perfect 一局几百条，
要看得在控制台里 `verbose on`。

**每一条判决都会过一次账**（连不打印的 Perfect 也过）：`delta` 必须等于 `nowTime − realTime`。
这三个数来自三处 —— 游戏判决时算的早晚量、它当时的 `nowTime`、我们从音符表抄来的 `realTime`，
它们之间是个恒等式，笔抄错了当场就会露馅；Miss 那条路径不带早晚量，就退一步查"判定时刻离音符
时刻多远"（正常刚越过窗口 0.1 秒，超过 1 秒就没法解释）。同类问题一局只报第一次，结算那行给总数。

### 结算账目从哪来

`ScoreControl` 上就是那一局的全部账：

```
+0x40 _score       float     +0x54 maxcombo    int      ← 注意是小写 c
+0x44 _percent     float     +0x58 perfect     int
+0x4c _combo       int       +0x5c good  +0x60 bad  +0x64 miss
+0x50 isAllPerfect bool      +0x68 early +0x6c late
+0x51 isFullCombo  bool
```

`ScoreControl::GetLevelResultInfo()` 是这些数字被抄进 `LevelResultInfo` 的唯一组装点，
所以只挂它一个就够（不用把四个判定入口都挂一遍）。它的两个调用者
—— `ProgressControl::Update` 的断关分支、`ProgressControl::_LevelOver_d__438::MoveNext`
的结算协程 —— **都在 `levelOver` 之后**，因此读到的一定是终局数字，不会是打到一半的。
反编译 `0x1d3505c` 可以逐行核对这次抄写：

```c
LevelResultInfo::set_Score(v7, v2->_score);
LevelResultInfo::set_Percent(v7, v2->_percent);
*(v7 + 52) = v2->maxcombo;
*(_OWORD *)(v7 + 28) = *(_OWORD *)&v2->perfect;   // perfect/good/bad/miss 四个连排
*(_QWORD *)(v7 + 44) = *(_QWORD *)&v2->early;     // early/late
```

> 数值字段是带下划线的私有名（`_score` / `_percent` / `_combo`），同名的
> `score` / `combo`（`+0x70` / `+0x78`）是给 UI 用的 `Text*` —— 读错了会拿到一个对象指针。

日志长这样：

```
[result #0001] Glaciaxion [HD]  1000000 分（100.00%）  最大连击 393  All Perfect
              Perfect 393  Good 0  Bad 0  Miss 0（早 0 / 晚 0）
```

读不到的字段显式写 `?` 而不是 0 —— 字段名对不上时 frida 是**静默**返回 null 的
（`<mirror>k__BackingField` 那次就是），所以"读不到"必须和"真的是 0"分得开。

## 开谱闸门与谱面镜像

### 谱面镜像做了什么

开关是 `LevelStartInfo` 上的属性 `mirror`（自动属性，背后字段的**真名**是
`<mirror>k__BackingField`），由 `SongSelector::ToggleChartMirror` 翻转、
存档里的字段名是 `chartMirror`。

> **IDA 会把 `<` `>` 洗成 `_`**，反编译里显示成 `_mirror_k__BackingField`。照着它的
> 写法去 frida 里 `tryField` 会**静默查不到**（返回 null 不报错），实测就这么翻过一次车：
> 每次都报"读不到镜像开关"、然后一律按不镜像打。agent 现在取属性 getter
> `get_mirror`（名字干净），不再碰那个字段。

`Chart::Mirror()` 在整个 `libil2cpp.so` 里**只有一个调用者**：`LevelControl::_Start_d__46::MoveNext`
（`0x1d27748`），也就是关卡启动协程。顺着它看，镜像只动三样东西：

| 对象 | 镜像怎么算 | 反编译依据 |
|---|---|---|
| 判定线移动事件 | `x → 1 − x`（v1 老格式：整数打包里的 `i → 880 − i`） | `vsub_f32(1.0, value)` |
| 判定线旋转事件 | `θ → −θ` | `vneg_f32` |
| 音符 `positionX` | `positionX → −positionX` | `ChartNote +0x18` 取负 |

前两条把判定线整个绕屏幕中心翻过去；第三条**只取负、不减 0.5** —— 这恰好证明了
`positionX` 是**以判定线为原点的沿线上偏移量**，不是屏幕绝对坐标。
`../src/algorithms/chart.py` 里 `point_at = 判定线位置 + 朝向 × positionX × 0.9` 正是这个模型。

顺带证实了 `positionX` 的缩放：`JudgeLineControl::Start` 算出
`moveScale = min(1, (屏宽/屏高) / (16/9))`，而 `LevelControl::SetInformation` 把每个音符的
`positionX` 乘上同一个因子（`0x1d2563c`：`positionX *= (screenW/screenH) / 1.7778`）。
所以 `positionX` → 16×9 虚拟屏的换算是**与设备无关**的 `0.9`，也就是 `NOTE_X_SCALE`。

### 闸门架在哪儿

`LevelControl::SortForNoteWithFloorPosition()`（`0x1d25350`）。

它是启动协程里"把谱面落地"的第一步，**由协程无条件调用、每关只调用一次**
（`XrefsTo` 只有 `0x1d27ef0` 一处）。这一刻：

- 谱面已经从 JSON 解析成 `Chart`；
- **镜像已经应用完**（`Chart::Mirror` 就在它前面几十行）；
- `LevelInformation` 已经填好（`offset` / `noteScale` / `numOfNotes` / `speed` …）；
- 判定线和音符的 GameObject **一个都还没生成**，音乐也还没开始。

卡在这里，游戏就是"万事俱备，只欠东风"；而镜像开关这一刻也已经定下来了
（`Chart::Mirror` 就在它前面几十行），所以主机能拿到"这一局到底镜像没镜像"，
据此把规划结果翻过来 —— 见下一节。

> 它后面紧跟着的 `LevelControl::SetInformation` 会**原地改写**那个 `chart` 对象
> （raw 值换成世界坐标、tick 换成秒）。所以"事后再回读一遍谱面"这条路本来也走不通。

### 怎么闸住的

`LevelControl::Start` 是 Unity 协程，跑在主线程；hook 的实现体也在主线程。于是
"停住不返回"就等于把整个游戏停住。停住用的是 Frida 官方的阻塞式收信
（[`messages.md`](../../frida_docs/messages.md)，"Blocking receives in the target process"）：

```ts
const op = recv("release", () => {});
op.wait();          // 主线程挂起，等主机 script.post()
```

两个细节：

- `recv()` 是**一次性**的，收一条就得重新注册。所以顺序是"先注册 → 再发 `level-start`
  → 再 `wait()`"。反过来写，主机回得足够快时放行消息会落在没有接收者的空档里，
  游戏就永远卡住。
- 放行消息带 `seq`，对不上的（上一关残留、误发）不算数，重新注册接着等 —— 免得把
  下一关悄悄放走。不带 `seq` 的放行一律认，方便手工操作。

主机侧（`../src/main.py`）在 `level-start` **那条消息里**把活干完再放行，一律 `try/finally`：
规划失败、规划器抛异常、甚至采集失败（agent 会送来一条只带 `error` 的 `level-start`），
都必须放行 —— 少了放行游戏就死在闸门上，比规划失败严重得多。

代价：主机卡多久，游戏就冻多久（规划一张谱面几秒）。期间 Android 理论上可能弹 ANR，目前接受。

> 这一条**还没在真机上跑过完整流程**：在 `Interceptor.replace` 的回调里做阻塞式收信是
> Frida 官方文档里的标准用法（例子里就是 `Interceptor.attach` 的 `onEnter` 里 `op.wait()`），
> 但"卡住 Unity 主线程会不会有别的副作用"只能上设备才知道。上设备时重点看两件事：
> 主机回放行之后游戏是不是**同一帧接着往下走**（画面没黑、音乐正常起），以及连续开两关
> 时放行的 `seq` 有没有错位。

### 镜像不重算，翻一下规划结果就行

谱面**只读一遍**：`FromJson` 那个咽喉点抓到的原文（`chart` 事件）。闸门那条
`level-start` 只多带一个布尔量 —— 这一局有没有开镜像。

既然镜像只是把整个画面绕中线左右翻一下，那规划也不用重做：

```
判定线 x → 1 − x      θ → −θ      音符 positionX → −positionX
        └──────────── 合起来 = 判定线上每个点 (x, y) → (16 − x, y) ────────────┘
```

判定点只是翻了个身，手指跟着翻一下当然还是判得中 —— 所以 `planner.plan(mirror=True)`
就是"先对着原文规划，再把结果整体水平翻过去"（`PlanResult.mirrored`），镜像开关原样
记进来源上下文，`.psap` 和 `meta.json` 里都看得见。

> 这条推理有个硬判据：`../src/selftest.py` 会把规划结果翻过来、拿去对 **按 `Chart::Mirror`
> 规则镜像出来的那份谱面**，要求 393/393 全中。实测镜像前后的最大横向偏差 **逐位相同**
> （0.0137 / 0.2490 / 0.0547 / 1.2800）—— 横向判据本身就是镜像不变的，这正是它该有的样子。
> 把 `mirrored()` 改成空操作、翻错轴、或者把中线从 8 挪到 9，都会立刻漏掉两三百个音符。

这样安排还顺手去掉了两个隐患：不用为镜像再读一次谱面（`SetInformation` 会把 `chart`
对象**原地**改成世界坐标，事后再读也读不回官谱格式），也不用维护"同一局到底该信哪一份
谱面"这套判断。

## 垂直判定

Phigros 的判定特色，也是 `../src/algorithms/geometry.py` 里一切的地基。

`JudgeControl::GetFingerPosition` 为每根手指、每条判定线只算两个量：判定线局部坐标下的
**横向分量**与**法向分量**。而 `JudgeControl::CheckNote` 里只把横向分量拿去比
`touchPos >= 1.9`（横向偏得越多，时间窗还收得越紧：
`badTime += (touchPos - 0.9) * PerfectTimeRange * -0.5`），**法向分量算出来了但从来没被用过**。

也就是说判定线"无限细"：触点离判定线多远都无所谓，只看它投到线上落在哪儿。由此：

- 把屏幕外的音符沿**垂直于判定线**的方向拉回屏幕是安全的 —— 横向分量不变。
- 几何算法可以放心按判定区窄带的重心按下去，哪怕重心离判定线很远。
- `../src/selftest.py` 校验的是沿判定线的横向偏差，而不是欧氏距离。

三个容易记错的细节：

- **容差不都是一个数。** Tap / Hold 是 `1.9`，**Drag 与 Flick 是 `2.1`**
  （`DragControl::Judge` 里那句 `fabsf(fingerPositionX[i] − positionX) < 2.1`）。
  折到虚拟屏就是 1.71 与 1.89。
- **Tap / Hold 的头判只在"按下那一帧"发生。** `JudgeControl::Update` 里
  `CheckNote` 的调用条件是手指的 `phase == TouchPhase.Began`（`Fingers + 0x44`
  直接存的就是 `UnityEngine.Touch.get_phase` 的返回值），所以一根早就在屏幕上、
  只是被 MOVE 过来的手指**判不到 tap**。反过来，按下之后马上抬起没关系。
  Drag / Flick 不吃这一条：它们逐帧读 `fingerPositionX`，只看位置。
  **不过 Flick 比这还多一道门 —— 见下。**
- **游戏是逐帧读手指位置的**，手指在两次事件之间不动、判定线却在动。
  所以"摆到位多久"和"摆得准不准"一样要紧 —— 见
  [手指得在位够一帧](#手指得在位够一帧)。

## 判定线坐标（y 千万别搞反）

`judgeLineMoveEvents` 里 `start` / `end` / `start2` / `end2` 都是 0~1 的比例。
`LevelControl::SetInformation` 把它们换算成 Unity 世界坐标：

```
world_x = (raw_x − 0.5) × 10 × A        A = min(屏宽高比, 16/9)
world_y = (raw_y − 0.5) × 10
```

`JudgeLineControl::UpdateInfo` 再把世界坐标**原样**写进 `localPosition`，把
`theta = start + (end − start) · t`（单位：度）**原样**交给 `Quaternion::AngleAxis`。
世界是 `y ∈ [−5, 5]`、`x ∈ [−5A, 5A]`，折算到本项目的 16×9 虚拟屏正好是：

```
虚拟 x = 16 × raw_x          虚拟 y = 9 × raw_y
```

**两条都是干净的线性映射，没有镜像、没有翻转。** 即 `start2 = 0` 是屏幕**下**边缘，
`start2 = 1` 是上边缘；判定线一般落在屏幕下三分之一，音符从上方落下。

> 社区实现（[phisap](https://github.com/kvarenzn/phisap)）这里用的是 `h × (1 − start2)`
> 和 `−radians(deg)`，等于把 y 与角度一起做了垂直镜像。因为 **垂直判定**，这个镜像对
> **水平判定线毫无影响**（官谱里绝大多数事件角度就是 0°），所以它镜像了也照样能打。
> 但只要判定线立起来（90°），横向分量就整体错掉；做可视化时更是整层都对不上。
>
> 这个坑值得记一笔：`../src/algorithms/chart.py` 最初就是从 phisap 照抄过来的，
> 结果渲染出来所有点都挤在画面上半部分。 **判定依据是 `(raw − 0.5) × 10`，不是 `5 − raw × 10`。**

## 规划器

三个都"吸收自" [phisap](https://github.com/kvarenzn/phisap) 的三个算法，但按本项目的
数据结构重写，不共用它的代码。取舍不同：

| 名字 | 思路 | 特点 |
|---|---|---|
| `conservative`（默认） | 每个 note 当整体，需要几押就分几根手指；flick / hold 拆成连续手势 | 最稳，手指不够就报错 |
| `radical` | hold 退化成"开头 tap + 每毫秒一个 drag"；1ms 时间栅格上贪心复用指针 | 事件最少，靠 MOVE 复用已在屏幕上的手指 |
| `geometric` | 125Hz 帧，给每个 note 切一条判定区窄带，同一帧内相交的区域并起来一起按 | 最省手指，判定区宽度是经验值 |

### 事件的"汇率"：一个 MOVE 就是一次注入

规划结果里的每个事件，到了设备上都是一次完整的输入事件
（INJECT → 输入分发 → 应用输入队列），**不是免费的**。所以规划器有两条纪律：

* **采样间隔不往小里调。** `ConservativeConfig.sample_delay = 8`（125Hz）。
  设备那边一帧最多消费一个位置，比 125Hz 更密只是白灌 —— 早先它是 `1`，
  密集段每毫秒一个事件、平均 362 个/秒，足以把 adb/scrcpy 那条注入链和游戏主线程
  一起拖住：主线程一停，游戏时钟的采样就断，触控跟着停，恢复时 `nowTime` 按音频
  往前跳一大截，整个时间轴就和谱面错开了。降到 8ms 后是 43 个/秒，
  **覆盖率与最大偏差逐位没变**。
* **手指没动就别说话。** `../src/algorithms/track.py` 的 `EventTrack` 会丢掉同一指针、
  同一坐标的 MOVE。真实手指不动时本来就不产生事件，所以这更接近真实输入。
  `geometric` 原本 70% 的事件是这样的空报。

三个规划器的事件数（Glaciaxion IN / 729 音符 / 157.7s）：

| 规划器 | 事件 | 事件/秒 |
|---|---|---|
| `conservative` | 5267 | 33 |
| `geometric` | 2397 | 15 |
| `radical` | 2443 | 15 |

### 手指得在位够一帧

第三个纪律不是"少发事件"，而是"别发早了就走"。游戏是**逐帧**读手指位置的
（`JudgeControl::GetFingerPosition` 每帧重算
`fingerPositionX = (手指 − 判定线原点) · 判定线朝向`），而手指在两次事件之间是不动的
—— 判定线却在动，于是横向偏移逐帧漂。所以"一个音符能不能判到"取决于**它的判定窗口里
有没有整整一帧手指都落在容差内**，而不是"有没有那么一瞬间对上"。

摆下一毫秒就走，等于这个位置在时间轴上只占一个点：drag 的 ±0.1s 窗口里能不能撞上一帧
纯看帧落在哪。三个规划器都栽在这上面：

| 规划器 | 症状 | 改法 |
|---|---|---|
| `conservative` | 指针回收补发的 UP 记在"上次用完的下一毫秒"，把上一个音符的覆盖砍成 1–2ms —— `Eradication Catastrophe` IN 上有 4 个 drag 因此进"看运气"状态（60fps 下撞上一帧的概率不到 15%） | UP 排到**重新按下之前的那一毫秒**（`PointerPool.lift_at`）；征用闲置手指时优先挑已经摆够一帧的 |
| `radical` | 同一处写成 `self.now - pointer.age + 1` | 同上，抬到 `now - 1` |
| `geometric` | drag / flick 只占 1 tick（8ms），60fps 下撞上一帧的概率不到一半 | `DRAG_DWELL_TICKS = ceil(MIN_DWELL_MS / FRAME_MS) = 3`（24ms）。副作用是事件数**减半** —— 手指多待一会儿，反倒顺手多覆盖了几个 drag |

一帧有多长由游戏自己的帧率策略定：`GameInformation::CheckFrameRate` 读
`Screen.currentResolution.refreshRateRatio`，刷新率 ≤ 89Hz 时 `targetFrameRate = 60`，
更高则取 `2 × 刷新率`、上限 300，而 `QualitySettings.vSyncCount` 恒为 0。
60fps 是它支持的最低档，`../src/algorithms/utils.py` 的 `MIN_DWELL_MS = 20` 就按这一档兜底，
不用去猜设备。（实机帧间隔比 16.7ms 短，所以上面那些"不足一帧"的覆盖在真机上时中时不中：
**同一个规划结果，不同一局的 miss 个数可以不一样** —— 这条正好可以拿来验证。）

同一件事还有第二条推论：**摆得准不准，和"摆到哪"不是一回事**。
`Screen.remap` 负责把落在屏幕外的判定点沿法向拉回屏幕（横向分量不变，所以判定结果不变），
但它的探针原来只铺一条屏幕对角线那么长。判定线整体跑到很远的地方时（`Eradication
Catastrophe` IN 的线 10 在 119.250s 从 `y=1.8` 跳到 `y=90`），探针够不着屏幕，于是
**静默退回屏幕中心** —— 而屏幕中心的横向分量跟目标差着 2.1，那一下必 Miss。
现在探针长度取"点到屏幕的距离 + 屏幕对角线"，够得着的交点一个都不会漏。

### Flick 光"摆在那儿"不算：它要一次"新起手"

这一条与 drag 完全不同，值得单独说 —— **`FlickControl::Judge`（`0x1d319e4`）自己根本不看手指**：

```c
v5 = note->realTime - progressControl->nowTime;
v6 = note->isJudgedForFlick || this->isJudged;      // ← 只看这个标志
if (v5 < 0.005 && v6) ScoreControl::Perfect(...);
if (v5 < PerfectTimeRange * -1.75) ScoreControl::Miss(...);   // -0.14s
```

点灯的是 `JudgeControl::CheckFlick`（`0x1d21828`），而它只在那**一帧**手指带
`Fingers.isNewFlick` 时才跑（`JudgeControl::Update` 里 `if (finger.isNewFlick) CheckFlick(i)`）。
`isNewFlick` 由 `FingerManagement::Update` 按手指的瞬时速度算：`|nowMove| / deltaTime` 过了
`flickJudgeSpeed × 5` 就算一次"新起手"，阈值本身在 `FingerManagement::Start` 里按 dpi 归一
（`flickJudgeSpeed = 序列化值 / 380 × Screen.dpi`）。**也就是说 flick 必须先"划一下"，
摆着不动是判不到的。**

而 `CheckFlick` 的挑法让事情更麻烦：它在 `nowTime ± 0.14s` 里按时间顺序挑**第一个**还没判过、
且 `|positionX − fingerPositionX| < 2.1` 的 flick，点亮之后就把手指那个标志清掉。两条推论：

1. **一次"新起手"只点亮一个音符** —— 它会被窗口里更早的 flick 抢走；
2. 一个 flick 只划一下 = 只有一次机会，被抢走就必然漏。

症状正是"几个连续的 flick 里随机漏一个，每次漏的还不一样"。改法是每个 flick **划两下**
（`flick_repeats`，三个规划器都有）：第二下从 `+半径` 蹦回去重新划，那个跳变本身就是一次
新的"新起手"，于是每个音符有两次独立机会。事件数的代价很小（`conservative` 在 Dlyrotz HD 上
3384 → 3734；`radical` 靠把滑键采样从 1ms 放到 2ms 抹平，峰值注入率反而没涨）。

自检里有一条 `check_flick` 按同一套规则把整局走一遍，要求每个 flick 最后都被点亮 ——
这是**悲观**假设（只认"手指位置跳变"那种够快的起手），它报问题一定有问题，说没问题时
真机上的机会只会更多。把它掰回"只划一下"，Dlyrotz HD 上立刻报 7 个 flick 点不亮。

### 规划结果就是缓存

规划结果是**谱面的纯函数**，所以缓存不需要另立门户 —— 落盘的那个 `.psap` 就是缓存：

* 位置由 `storage.plan_path_for` 定：`<谱面文件名前缀>_<规划器>.psap`，
  一张谱面 + 一个规划器对应一个文件，天然一一对应；
* 命中判据是 meta 里的 `cache_key` —— `../src/algorithms/**/*.py` 加 `../src/planner.py` 的哈希。
  改了算法、改了谱面解析、改了坐标换算，指纹就变，下次自动重算；
* 镜像、延迟这些**运行时**设置一概不进缓存：`.psap` 存的是规范解（不镜像、不偏移），
  由 `touch.Player` 在执行时临时改。于是一张谱面的缓存在任何局面下都能用，
  也不用为"开着镜像再存一份"。

要强制重算就删掉那个 `.psap`（或者 `--no-cache`）。

## 触控

把 `.psap` 里的触点按时发到设备上。后端都在 `../src/backends/` 包里，照 `../src/algorithms/` 的套路来：
一张注册表按名字现 import，加一个后端 = 写一个模块 + 在 `registry._BUILTIN` 里加一行。

| 后端 | 干什么 |
|---|---|
| `scrcpy`（默认） | 走 scrcpy 的控制通道真发，目前唯一能真打的后端 |
| `recording` | 干跑：不连设备，只把"什么时候发了什么"记下来；没有设备时唯一能验调度器的办法 |

两边都能选：控制台里 `backend <名字>`（整条链：闸门 → 规划 → 放行 → 对表 → 排事件，换了
当场重开）与 `touch.py --backend <名字>`（单机放一个 `.psap`）。

### 为什么借 scrcpy

往 Android 送触摸有两条路：`adb shell input`（慢、单点、抬起有延迟）和
`InputManager.injectInputEvent`（快、多点、能精确到帧）。后者要 INJECT_EVENTS 权限，
而 scrcpy 的 server 已经在设备上把这条路走通了 —— 我们只按它的协议发字节，不重复造。

用的是**只有控制通道**的模式（`video=false audio=false`）+ `tunnel_forward`：

```sh
adb push scrcpy-server-v4.1 /data/local/tmp/scrcpy-server.jar
adb forward tcp:<port> localabstract:scrcpy_<scid>
adb shell CLASSPATH=/data/local/tmp/scrcpy-server.jar app_process / \
    com.genymobile.scrcpy.Server 4.1 scid=<scid> log_level=info \
    video=false audio=false tunnel_forward=true
# 连上 127.0.0.1:<port>，先读掉一个哨兵字节再开始发
```

两个路径是项目里的固定常量（`../src/backends/scrcpy.py` 顶部）：`adb` 用
`C:\UserData\platform-tools\adb.exe`，server 用**与 `scrcpy.py` 同目录**的 `scrcpy-server-v4.1`
（从 scrcpy release 里拿出来、不用改扩展名）。自检：

```sh
cd src
python -m backends.scrcpy      # 只查 adb、server 文件和屏幕尺寸
```

（`../src/backends/` 里的模块是包内相对 import，所以得在 `src/` 里用 `-m` 跑 —— 直接
`python src/backends/scrcpy.py` 不行。）

一条触摸消息 32 字节（大端，字段逐一对过 v4.1 的 `control_msg.h` 与
`ControlMessageReader.java`，见 `../src/backends/scrcpy.py` 顶部）：动作、指针号、x、y、屏宽、屏高、
压力（按下 `0xffff`、抬起 0）、`action_button`、`buttons`。多指索引由 server 自己算 ——
客户端只管发 DOWN/UP/MOVE 和各自的指针号。

> 只开控制通道时 server 的 `displayData` 是 null，`Controller` 走的是
> "**按原始坐标注入**"那条分支：我们发设备像素。所以坐标换算（见下）必须自己算准。

### 同步：跟着游戏的时钟走，而不是自己数拍子

agent 每 100ms 把 `ProgressControl.nowTime` 回传一次，触控模块用这些样本估计
"游戏时间 → 主机时钟"的映射，按它排事件。于是这些全都**自动**对齐了：

* **游戏里的延迟设置** —— `nowTime = audioTime − (mainOffset + chart.offset + 玩家offset)`，
  游戏侧的延迟已经含在 `nowTime` 里，跟着它走就是跟着延迟走；
  **不需要也不能再加一次**（加了就是双份）。`level-start` 里仍会把四项报回来，
  是给"对不上账"时看的：

  ```
  [gate #0001] 游戏延迟 合计 +20ms = 设备音频补偿 +20ms + 谱面 +0ms + 玩家设置 +0ms
  ```

  四项都是**直接读**的（谁也不靠减出来），主机顺手核对三项之和等不等于"合计"，
  不等就直接标出来。三个分量分别是：

  | 分量 | 字段 | 谁定的 |
  |---|---|---|
  | 设备音频补偿 | `GameInformation.mainOffset`（静态） | 游戏按 DSP 缓冲大小算：`dspBufferSize × 0.00016 − 0.02048` |
  | **谱面** | `Chart.offset`（JSON 根上的 `offset`） | **策划在编辑器里定的**，游戏 UI 里看不到 |
  | 玩家设置 | `GameInformation.offset` | 设置里的延迟校准，`SaveManagement::LoadFloat("offset", 0.0f)` |

  > "谱面延迟"容易误会成"每个谱面有一份玩家设置" —— 不是。玩家只有**全局**一个校准；
  > `Chart.offset` 是**谱面文件自带**的字段（`formatVersion` / `offset` / `judgeLineList`
  > 就是 JSON 根上的三个键，运行期 `Chart` 结构体 `+0x10/+0x14/+0x18` 一一对应）。
  > 我们抓的两张谱面它都是 `0.0`，所以日志里恒为 `+0ms`。
* 加载、起播前那 3 秒、掉帧、暂停与恢复 —— 时钟停了事件就停，时钟走事件就走。

样本带的是"读到时的值"，到主机手上必然晚了一小段。设真实关系是 `host = game + τ`，
样本满足 `h − v = τ + d`（`d ≥ 0` 是那一次的传输延迟），所以对最近的样本取
`min(h − v)` 就是 **τ 的一个偏高估计** —— 错也只错在"晚了一点"，不会早。窗口 2 秒，
跟着设备与主机的晶振漂移慢慢挪。

"时钟停了"的判据是**值多久没变**（250ms），不是"多久没收到样本"：暂停时 hook 照常
每 100ms 送一次，只是每次都送同一个数 —— 所以暂停一定看得见。停住期间已经到点却来不及
发的会补上，没到点的一律等；确认停过就把旧的对齐作废重新对表，不然暂停吃掉的那段时间
会被算进去（恢复后会把事件发**早**）。

> 这里踩过一个坑，值得留着：最早的判据是"上次 **值变化**距今超过阈值就当成停过"。
> 于是 **传输打嗝**（采样断了几百毫秒、之后一口气补上一串样本）也会命中 —— 补上来的
> 头一笔本来就带旧值（400ms 只走了 5ms），看数据和暂停一模一样。窗口一被清掉，
> `origin` 就退化成那个样本自己的 `h − v`， **它带多少延迟我们就晚发多少**，要等 2 秒
> 窗口重新填满才自愈。表现是偶发的 late good、每次音符还都不一样，而"最大迟到"那个
> 指标量的是"相对我自己的排期"，排期本身错位它照样报 0 —— 完全看不见。
> 现在只认"看着它停过"这一条证据，`../src/selftest.py` 里有一条传输打嗝的回归用例盯着。

剩下一个固定偏差：主机 → adb → 设备 → InputManager 这条注入链路的耗时。它由
`--latency` 手工补（正数 = 提前发），只能上设备调，默认 0。

### 排期出问题时看什么

日志里除了"最大迟到"，还会带上这几样 —— 追 late good 时对着看：

```
[touch #0001] 打完：发了 6882 个事件，最大迟到 4.8ms（3 帧超过 20ms，最差在谱面 137.20s 迟到 234ms）
            单次发送最长 1.2ms；时钟重锚 1 次，采样最大间隔 123ms
```

| 数字 | 说明 |
|---|---|
| 最大迟到 | 相对**排期**的迟到，含 `send()` 本身的耗时 |
| N 帧超过 20ms | 迟到超标的帧数；配合"最差在谱面 X s"直接对着游戏里的 late 找 |
| 另有 N 个迟到太多没发 | 晚过 `LATE_SKIP`（150ms，已越过 Good 窗口）的事件被丢掉而不是补发 |
| 单次发送最长 | `send()` 有没有被阻塞（TCP 缓冲满、adb 卡住都会露在这） |
| 时钟重锚 | 估计值整体挪过几次；**每次挪都会让接下来一两秒整体偏**，所以一发生就当场报警（`>20ms` 时） |
| 采样最大间隔 | "采样断过"的直接证据。断了几百毫秒 → 上面那个坑的现场 |

### 坐标

虚拟屏（16×9，y 轴向上）换成设备像素，同时补上宽屏时的黑边、翻 y 轴：

```
px = 屏宽/2 + (虚拟x/16 − 0.5) × A × 屏高        A = min(宽高比, 16/9)
py = 屏高 × (1 − 虚拟y/9)
```

设备比 16:9 更宽时画面左右留黑边（`A` 被 16/9 卡住），更窄时化简就是
`px = 虚拟x/16 × 屏宽`。`wm size` 报的是自然方向的物理尺寸，Phigros 只在横屏跑，
所以按长边当宽取。

### 已经验过什么 / 还没验什么

不连设备能验的都验了（`../src/selftest.py` 里那些）：时钟估计的六种情形、坐标换算的三种宽高比、
缓存的命中与失效、播放器按时发送 + 镜像翻转、闸门一局只放一次、控制台的命令解析、
存活探测的三种结局。调度精度用 `--backend recording` 实测**最大迟到 0.5ms**
（原先用 `Event.wait` 睡，Windows 上被 15.6ms 的系统滴答拖到 14ms —— 见
`touch.py` 里 `COARSE_NAP` 那段注释）。

**没验的**：真的把触摸送进设备。协议是照着 v4.1 的源码逐字段核的，但没跑过。
还有 `ScoreControl::Perfect/Good/Bad/Miss` 那四个 hook 与 `SetInformation` 的音符表
—— 反编译依据都在，但**只有上设备才知道**判决回调的参数是不是真的按那个顺序递进来的
（`Perfect` / `Good` 带一个 `Vector3` 结构体参数，是这套 hook 里唯一"不那么确定"的地方）。

## 运行时控制台与存活探测

命令表在 [README.md](../README.md#控制台)。这里讲两条设计：

设置只有一份，就是 `../src/runtime/options.py` 里的 `Options`：控制台改它，规划与播放**每次都读它**。
于是"改了什么时候生效"这个问题根本不存在 —— 延迟改了下一个事件就按新值发，规划器改了
下一关就按新的算。反过来，如果让每个模块各存一份、改的时候挨个同步，就一定会有改漏的那一处。
`inject off` 不是"换成干跑后端"，而是照常排期、照常记迟到，只是最后那一次 `backend.send()`
不发出去 —— 所以关掉注入仍然是一次完整的调度演练。

### 回显：输入之后一定有反应

面板是**第一个**支起来的东西 —— 找设备、注入、等 agent 握手（最长 30s）、推 scrcpy，
这些都排在它后面。之前是反过来，于是启动那几秒里敲什么都没人听，看起来就像"控制台没反应"。

三条规矩，都是为了同一件事：

1. **所有输出都走 `../src/runtime/output.py` 的 `log()`**（`print` 的签名），而且 **默认 `flush=True`**。
   整行不会被别的线程插花；也因为写者只有一个，才谈得上下面第 2 条。flush 不能省：
   不 flush 的话输出攒在哪里、什么时候露面完全由 stdout 是什么决定（终端行缓冲、管道块缓冲），
   表现就是"敲了一条命令没反应，再敲一条，上一条的输出才出来"；
2. **提示符被输出顶掉就补回来** —— agent 每 100ms 就可能在打印，而 `auto> ` 是不带换行
   画在屏幕上的；直接 `print` 会接在它后面，看起来就是"提示符没了"。所以打印前先
   `\r`+空格擦掉那一行，打印完再画一遍；
3. **每条命令都要说话**，包括"看不懂的参数"和"没这个命令"。控制台是运行时唯一的面板，
   它沉默的时候人分不清是"命令没生效"还是"程序卡住了"。

stdin 不是终端（管道 / 重定向）时**照样读**，并把命令自己回显出来（`auto> status`）
好让日志能对上；读到文件尾就说明白然后退出。这里踩过一次坑：原先"不是终端就一声不吭
把自己关掉"，于是敲什么都没反应、连提示符都没有 —— 宁可多说一句，也不要安静地不干活。

> **别让子进程继承 stdin。** 这是最难查的一类"输入被吞"：`../src/backends/scrcpy.py` 里的
> `adb` 子进程原先用默认的 `stdin`，而 **`adb shell` 会把本地 stdin 转发给设备端的 shell**
> —— 用户在控制台里敲的那一行被 adb 半路吃掉了，表现是"敲了没反应，再敲一条上一条才生效"，
> 而且 **永远不报错**。修法是给每一次 `subprocess` 调用都写死 `stdin=subprocess.DEVNULL`
> （scrcpy 那条 `adb shell` 整局都活着，等于一个一直在等着抢输入的家伙）。自检里有一条
> 源码级检查盯着这件事：找出所有"括号里没有 `stdin=`"的 subprocess 调用。

### 活着还是死了

每半秒真的问 agent 一声（`ping`，一次 RPC 往返），有三种结局：

| 结局 | 判据 | 为什么必须单独有这一档 |
|---|---|---|
| `ok` | 答了 `pong` | —— |
| `dead` | 会话断了（进程被杀 / 退出 / 我们主动断的） | frida 会主动报 `detached`，这一档最省事 |
| `hang` | 连接还在，但 `ping` 超时没回来 | **进程被 Android 冻结**（切后台缓存）时 socket 好着、心跳也在，只是不再执行任何东西 —— 不发一条消息等回答，就分不出"还活着"和"冻着" |

一旦不是 `ok` 就**停手**：剩下的排期是给上一个进程的，往一个死掉或冻住的游戏里灌输入只会更糟
（那些触摸会落在别的应用上）。停手之后**不会自作主张重连** —— 重连要选 spawn 还是 attach，
那是人的决定：

```
auto> spawn             # 游戏被关掉了：重新启动它再注入（旧名字 respawn 照样能用）
auto> attach            # 游戏还开着、只是我们的会话没了：附加上去（旧名字 reattach）
```

两者都保留触控后端（它挂在设备上，不挂在游戏进程上）与设置，只换 frida 会话、清空游戏时钟、
停掉旧播放器。重连之后自己点到那首歌，开谱时自动接管。

### 收工：`quit` 与 Ctrl+C 必须是同一件事

两个入口都落到 `Controller.stop()` 上：置 `stopping`、**当场** unload 脚本并 detach 会话、
停播放器、关触控后端。这是刻意的，因为"只置一个标志、等主干自己发现"在这个程序里根本不成立：
主干可能正卡在启动阶段的阻塞调用里（frida 找设备自带 10 秒超时、等 agent 握手最长
`READY_TIMEOUT`、推 scrcpy 要一两秒），那时候置标志等于什么都不做 —— 屏幕上还是那个
`auto> `，游戏上却已经挂着我们的 hook，而人以为已经退出来了。Ctrl+C 看起来干脆，只是因为它
能打断阻塞调用。同理，主干在启动阶段也会逐段问一句 `stopping`：找完设备就不再注入、
`_await_ready` 按 0.1 秒切片等（标志位拦不住一次 30 秒的 `Event.wait`）、注入了就跳过起后端。

三条当初没想到、后来加上的规矩，都在自检里钉住了：

1. **拆除只做一次，后到的等它做完。** `Agent.stop()` 只是起一条线程去 unload/detach
   （进程冻住时它会挂住，所以不能同步等死），主干要是扭头就把进程结束了，设备上就留下一个
   还挂着 hook 的游戏 —— 而"取消注入"正是收工要办的事。
2. **拆除期间屏蔽 SIGINT。** Ctrl+C 常常要按两下（第一下没动静，人就再按一下），第二下会落在
   拆除中途，把 unload/detach 打断 —— 那就等于没取消注入，还甩一份堆栈出来。第一下已经进来了，
   够了。信号只能在主线程装，装完还原。
3. **闸门还开着就先放行再断会话。** Unity 主线程正卡在 `recv("release")` 的 `wait()` 上，
   直接断会话那条 wait 就永远等不到消息，游戏会连着主线程一起冻在那儿，人只能去杀进程。
   agent 那边因此记着 `gate_open`，收工时补一次放行。

**收工只撤我们自己的东西**（脚本、会话、播放器、触控后端），游戏本身一动不动 —— 即使它是我们
spawn 出来、还没 `resume` 的那个。"放不放它跑"不是收工该管的事，自检里有一条专门盯着这件事
（替身设备上任何 `resume`/`kill` 都算越界）。同理，收工之后不再规划、不再架播放器：开谱消息
可能正好在拆除途中到达，那会儿后端已经关了、会话也快断了，排期没有任何去处。

> `ping` 的实现里一个字都不碰 il2cpp：闸门正按着 Unity 主线程的时候它也得立刻返回。
> 另外 frida 的 RPC 没有超时参数，所以它被丢到一条一次性线程里 `join(timeout)`；超时之后
> 那条线程仍然堵在 frida 内部 —— 记着它，在它回来之前不再发第二次，否则每探一次就漏一条线程。

## 可视化：为什么是 qtrle

参数表在 [README.md](../README.md#可视化)。

**为什么是 qtrle / `.mov`**：H.264 没有 alpha 通道，所以透明只能用 QuickTime Animation
（`.mov`，qtrle）或 VP9（`.webm`）。qtrle 是逐行行程编码，对"大片透明 + 几个点"
这种画面特别友好 —— Glaciaxion 全程 1080p60 也就几十 MB，而 ProRes 4444 同样内容要 GB 级。
Premiere / AE / FCP 都直接认。给 `-o xxx.mp4` 会改成 H.264，那时**必须**同时给个底色，
否则会明确报错而不是悄悄给你一坨黑的。

抗锯齿有个坑写在 `../src/render.py` 顶部：OpenCV 的 `LINE_AA` 只有在**单通道**图上才给出正确的
覆盖率。直接往 RGBA 上画，透明像素会被当成黑色参与混合，边缘立刻出现一圈暗边。
所以这里是先出覆盖率掩膜，再用 numpy 自己做直通 alpha 的合成 —— 边缘像素是
`(255, 128, 0, 54)` 而不是 `(54, 27, 0, 54)`。

> **已知现象：部分播放器会把 qtrle 的点显示成青色。** 文件本身没问题 —— qtrle 的 32 位色
> 按 QuickTime 规范存的是 `A,R,G,B`，而有些播放器把它当成 `B,G,R,A` 读，红点
> `(A=255,R=254,G=47,B=47)` 就被解释成 `(B=255,G=254,R=47)`，正好是青色。
> Premiere / AE / FCP 按规范读，不受影响；VLC、Windows 自带播放器之类对这一块的支持一向含糊。
>
> 这类播放器的表现 **不能**用来判断文件对不对。真要验证，用 `ffmpeg` 转一张 PNG 再看，
> 或者干脆拖进 Premiere 叠到一段素材上（QuickTime Animation 的 alpha 是原生识别的，
> 不用做任何 keying）。万一 Premiere 也读错，那说明得换封装 —— `qtrle` 只支持 `argb`
> 这一种 32 位格式，没有字节序可调，只能改 `../src/render.py` 里的 `CONTAINERS`
> （PNG 序列 / ProRes 4444 / VP9 webm）。

## 自检

```sh
python src/selftest.py
```

不连设备也能跑，用的就是 `../charts` 里已采集的谱面。对每张谱 × 每个规划器查五件事：

1. **事件流自洽** —— 每个指针的 DOWN / UP 严格配对，事件按时间有序，没有未抬起的指针。
2. **覆盖完整** —— 分两类判，因为游戏判这两类的方式根本不同（依据见下）：
   * **Tap / Hold 头判**：只在**按下那一帧**判（`JudgeControl::Update` 只为 `phase == Began`
     的手指调 `CheckNote`），所以要求"窗口里有一次**落在容差内的 DOWN**"。一根早就按在屏幕上、
     只是被 MOVE 过来的手指是判不到 tap 的。
   * **Drag / Flick**：逐帧比手指位置，所以要求"判定窗口里**存在整整一帧**手指都在容差内"。
     hold 主体另外按 `HOLD_GRACE_MS` 扫全程。
3. **存得回去** —— 走一遍 `planner.plan(cache=True)` 与 `storage`，`.psap` 编解码往返一致。
4. **镜像也对** —— 把规划结果翻过来，拿去对按 `Chart::Mirror` 规则镜像出来的那份谱面，
   要求全中；顺带查镜像两次回到原样、事件数与音符数不变。
5. **滑键起手** —— 见上面「Flick 光"摆在那儿"不算」：按 `CheckFlick` 的规则把整局走一遍，
   要求每个 flick 都被点亮。悲观假设（只认手指位置的跳变），它报问题一定有问题。

另外几组"整条链的上游下游"自检，都不需要设备：

| 自检 | 查什么 |
|---|---|
| 闸门 | 用假消息喂 `agent.Agent`：一局恰好放行一次、`seq` 对得上、作业抛异常也放行、没收到谱面也放行；喂给规划器的永远是 FromJson 原文，**镜像不进缓存的身份**、而是交给播放器；播放状态（`play-state`）与"这一局没了"（`level-gone`）原样转给主机 |
| 时钟 | 六种情形下 `GameClock` 的估计：稳定推进、延迟抖动、起播前的等待、中途暂停、时钟倒走、传输打嗝补样本。判据只有一条 —— **宁可偏晚，绝不能偏早** |
| 坐标换算 | 16:9 / 20:9（左右留黑边）/ 4:3 三种宽高比，以及 y 轴翻转 |
| 缓存 | 第一次不算命中、第二次命中且事件流一致、算法指纹变了就失效、`cache=False` 既不读也不写；命中时 `stats` / `warnings` 也必须跟着回来（`.psap` 是二进制、这些在 `.meta.json` 里 —— 不补的话"音符数核对"会拿 `None` 去比，喊假警报） |
| 播放器 | 用记录后端跑一遍：每批事件比"游戏时钟走到那一刻"早 `latency` 秒发出（±30ms），`--mirror` 时坐标 `x → 16 − x`；外加**收尾那一发"把按着的手指全抬起来"**：计划自己留了没抬的、两根都还按着的、以及注入关着（一根都没按过，不许凭空发） |
| 控制台 | 命令解析与那几个运行时旋钮：`latency` 的三种写法（`0.02` / `20ms` / `+5ms`）、`inject` / `verbose` / `planner` 生效、换到不存在的规划器时设置不动、`devices` / `device` / `spawn` / `attach [pid]` / `backend` / `cache` / `save-chart` / `host` 各走各的（含"没有会话时换后端不该去重开"）、会落盘的真的落盘而 `inject` / `verbose` 绝不落盘、打错/打空/打注释都不炸；回显三条规矩 —— 每条命令都得有回话（包括 `status_lines()` 给空的时候）、管道模式下命令要回显、别的线程打印完提示符必须画回来、`log()` 必须当场 flush；**空读不退出**：管道读到 EOF 要停靠（不退出、说清楚怎么收工）、终端上一次空读要接着读；再加一条**源码级检查**：任何 `subprocess` 调用都不许继承 stdin |
| 附加目标 | 先问应用列表（包管理器给的 identifier → pid）、再问进程列表（精确名 → `包名:子进程` 前缀）、最后才按名字找；实机那种"进程名是应用标签 `Phigros`、包名不在进程列表里"必须附加得上；给了 pid 就跳过查找；两边都读不到时不瞎猜；失败清单里必须有 pid / 包名 / `--pid` 提示 |
| 配置 | `../config.json` 不存在时按默认值建一份、往返一致、坏文件先备份成 `.bak` 再重建、不认识的字段忽略而认识的照收；`inject` / `verbose` **不许**出现在落盘字段里 |
| 日志 | 抄的是**到达屏幕的一切**（直接 `print` 的也要在里面，`scrcpy.py` 就属于这种）、一行里只留最后一个 `\r` 之后的内容（控制台擦提示符的回车不进文件）、`stop_file_log()` 之后不再写、`log_path()` 跟着开关走 |
| 存活探测 | 三种结局都认得出来：答了 `pong` = `ok`、没有会话 = `dead`、`ping` 抛异常 = `dead`（并记下原因）、`ping` 卡住 = `hang` 且按超时返回；另外钉住"收工只收一次" |
| 收工 | `stop()` 当场拆完且顺序是"停播放器 → unload → detach"、拆除期间 SIGINT 真的是屏蔽的（在替身的 `join` 里当场采一次，不是只看源码）、闸门开着时先放行再断、拆除只做一次且后到的那个等它做完、收工之后不规划也不架播放器、收工不碰游戏本身；再跑**真的** `main()`（只顶掉 `Controller` / `Console`），要求 `quit` 与 Ctrl+C 都以 0 收尾、都落到 `shutdown()`、且收工只做一次 |
| 暂停 / 退场 | 游戏自己报的信号要按对的方式处置：暂停 → 按住时钟 + 抬起按着的手指（但不停播放器）；恢复 → 把抬掉的指针**按回原位**（保住 flick 的位移，不许把计划里的 `MOVE` 改写成 `DOWN`）+ 放开时钟并**重新对表**；这一局没了 → 停播放器 + 清时钟 + 明说一句。时钟那半还量了四种爬升速率（0 / 0.02 / 0.1 / 0.5），要求恢复后的排期偏差在 30ms 以内 —— 老做法在这里偏 1~2 秒，等于"恢复之后一个事件都发不出去"。这一组同时钉住"**不许拿计时去猜**关卡跑没跑" |
| 结算 | 喂假的 `result` 消息：满分局 / 有失误局 / 全连局各一行字都要对，且**读不到的字段必须写成 `?` 而不是 0** |
| 延迟自校准 | 拿这一局 Perfect 的**中位数**调手工补偿：样本不足不给结论、Good/Bad 的离群值不参与、单次挪动有上限（越过就跳过并说明）、开关关着时绝不动设置也不落盘 |
| 存活探测 | 三种结局都认得出来（`ok` / `hang` / `dead`），外加一条**豁免**：我们自己正忙（卡在闸门 / 谱面在通道上传输）时 ping 必然排不上队，那不算"游戏冻住了"；"忙"的理由没了就照常报警 —— 别拿它当万能挡箭牌 |
| 判定对账 | 拿合成的 `judge` 消息喂 `Agent`：`delta` 与 `nowTime − realTime` 对得上时不许吭声、对不上必须**每一条**都当场报（带序号、带是哪个音符）、正常的 Miss 不许被冤枉（含"早于 realTime 触发"那种：下界 `MISS_EARLY = 0.22`）、Hold 的两头（头判 / 收尾 −0.22s）都认、查不到音符时不许乱报 |
| 在位时长 | 覆盖率判据**自己**的回归用例：拿两根合成时间轴喂 `check_coverage`，要求"停 100ms"不报、"只停 5ms"必报。防的是有人把判据"简化"回"那一瞬间在不在点上" |
| 滑键起手 | 按 `CheckFlick` 的规则把整局走一遍（窗口 ±0.14s、时刻最早优先、横向 2.1），要求**每个 flick 都被点亮**。悲观假设（只认手指位置的跳变），所以它报问题一定有问题。把 `flick_repeats` 掰回 1，Dlyrotz HD 上立刻报 7 个点不亮 |

这几组都做过变异测试：把 `min` 换成 `max`（会抢拍）、认不出时钟停住、不重新对表、
镜像不翻坐标、闸门不放行、把传输打嗝当成暂停 —— 全都被抓到。播放器那组正是这样抓出了
一个真 bug（`Player` 拿参数里的 `plan` 建帧表，`--mirror` 翻了 `self.plan` 却没用上）。
覆盖率那组是**双向**做的：把 hold 主体上的手指横移 6 个单位（远超容差）再归位 ——
40ms 和 60ms 放过（在游戏的 67ms 宽容窗口内），500ms 报"连续 72ms 落空"，
既不放水也不冤枉。存活探测那组也是双向的：让"取走播放器"不置空 → 立刻报"停了 2 次、
账报了 2 次"；让 `probe` 永远说活着 → 六条里错五条。回显那组同样是变异出来的：
把"打印完再把提示符画回来"去掉、或者让管道模式不回显命令，立刻报出来 ——
而**第一版判据太松**（只查"结尾不是擦除码"），漏掉了"擦掉就完事、不画回来"这种写法，
正是变异测试把它逼严的。判定对账那组：把核对整个摘掉 → 五条全报；把容差放到没意义 → 立刻报出来。
滑键起手那组是**反向**验证的：把 `flick_repeats` 掰回 1（就是原来那个"只有一次机会"的写法），
自检立刻报出 Dlyrotz HD 上那 7 个点不亮的 flick；改回 2 就全绿。
收工那组是**逐条掰坏**验的：只置标志不当场拆、拆除期间不屏蔽 Ctrl+C、后到的那个不等、
闸门不先放行、不记 `gate_open`、`handle_level_start` / `play` 不认收工、`open()` 失败时
永远返回 2、启动阶段收工后照旧起后端、收工顺手 `resume` 一下游戏 —— 十种改法全部报出来，
一种都没漏。

当前结果（6 张谱 × 3 个规划器，全部通过）：

| 谱面 | 规划器 | 事件 | 覆盖 | 最大横向偏差 |
|---|---|---|---|---|
| Credits HD (v1, 355 notes) | conservative | 1367 | 355/355 | 0.000 |
| | geometric | 798 | 355/355 | 0.011 |
| | radical | 388 | 355/355 | 1.219 |
| Credits IN (535 notes) | conservative | 2169 | 535/535 | 0.001 |
| | geometric | 1150 | 535/535 | 0.076 |
| | radical | 1630 | 535/535 | 1.149 |
| Dlyrotz HD (458 notes, 70 个 flick) | conservative | 3734 | 458/458 | 0.160 |
| | geometric | 1596 | 458/458 | 0.160 |
| | radical | 2956 | 458/458 | 1.250 |
| Eradication Catastrophe HD (200 notes) | conservative | 6119 | 200/200 | 0.000 |
| | geometric | 1919 | 200/200 | 0.001 |
| | radical | 287 | 200/200 | 0.947 |
| Eradication Catastrophe IN (593 notes) | conservative | 4430 | 593/593 | 0.000 |
| | geometric | 2404 | 593/593 | 0.064 |
| | radical | 1488 | 593/593 | 1.125 |
| Glaciaxion IN (729 notes) | conservative | 5478 | 729/729 | 0.160 |
| | geometric | 2601 | 729/729 | 0.377 |
| | radical | 2443 | 729/729 | 1.052 |

滑键起手那一栏（`check_flick`）全都是 0 个点不亮；峰值注入率最高 215 个/秒（`radical` 在
Eradication Catastrophe IN 上），仍在"362 个/秒就会把注入链拖住"那条线以内。

容差：Tap / Hold 是 1.9（`CheckNote` 的阈值）× 0.9（`positionX` 到虚拟屏幕的缩放）= 1.71；
Drag / Flick 是 2.1 × 0.9 = 1.89（`DragControl::Judge` 用的是 2.1，跟 Flick 一样，
**不是** Tap 的 1.9 —— 这一点报告里原先写错了，见下）。那几个 1.x 是 `radical` 有意复用
"横向 1.25 以内"的闲置手指带来的，仍有余量。
**镜像后逐位相同**（横向判据本身就是镜像不变的），所以上表同时就是镜像自检的结果。

> **v1 老谱的镜像模型。** 自检的"游戏行为"模型（`mirror_chart`）原先写着"只伺候
> `formatVersion >= 2`，v1 不伺候"—— 直到采到 `Credits` HD（v1）才发现这句话是个洞：
> 三个规划器在镜像后**全军覆没**，偏差 16~19，看着像规划器错了。其实 `Chart::Mirror`
> 把 `formatVersion == 1` 当 `oldVersion` 传给 `JudgeLine::Mirror`，那边走的是打包整数的
> 分支（0x1d28afc）：`新值 = 880000 − 旧值 + 2 × (旧值 mod 1000)`，代进去就是
> `x → 880 − x`、`y` 不动 —— 仍然是纯粹的左右翻，规划结果翻一下照样全中。
> 补上这一段之后 v1 谱 355/355、镜像后也 355/355。**判据说"我不伺候"的地方，
> 就是它不会替你看住的地方。**

> **判据踩过的坑，值得留着。** 头判查"判定时刻那一瞬间"，hold 主体则**扫描全程**、
> 只要求"没有一段超过 `HOLD_GRACE_MS`（67ms）完全落空"。这不是放水，是照抄游戏：
> `HoldControl::Judge` 里 `_safeFrame` 初值 2、连续落空时每帧减 1、减到 < 0 才判 Miss
> —— 连续 3 帧落空都忍得下来，按 60fps 折算就是 67ms。
>
> 原先的写法是"头查一瞬间、尾也查一瞬间"，于是 Glaciaxion IN 上冤了 `geometric` 三处：
> 那条判定线**每 53ms 在两个位置之间跳一次**（归一化 `0.2 ↔ 0.8`，世界坐标 `3.2 ↔ 12.8`），
> 而那个 hold 的尾正好落在跳变点上（`63.4286s`）。判据用尾时刻算目标（按"落在事件起点取
> 新值"的约定拿到 12.8）、却用尾前 8ms 查手指（还在 3.2）—— 两个不同时刻的东西拿来比，
> 凭空差出 9.6。**实机是 All Perfect。**
>
> 两处教训：一是查任何一瞬间都要让**目标与手指状态同刻**；二是对会跳的目标，
> 单点采样本身就是掷硬币，得按游戏真正的宽容窗口去扫。

## `.psap` 里存了什么

`.psap` 布局（大端）：

```
'APSP' | u8 版本 | u8 名字长度 | utf8 规划器名
f64 屏宽 | f64 屏高 | u32 帧数
每帧: i64 时间戳(ms) | 事件个数 u16 | 每个事件: u8 动作 | u32 指针号 | f64 x | f64 y
```

坐标是虚拟屏幕坐标（官谱 16×9，y 轴向上），`storage.decode_plan()` 直接读回
`PlanResult`；映射到真实分辨率是触控模块的事。

## 消息协议

`../frida/protocol.ts` 与 `main.py` 一一对应，改一侧要同时改另一侧：

| event | 载荷 | 说明 |
|---|---|---|
| `hooked` | `signature, address, rva, unityVersion` | hook 装上了 |
| `ready` | `unityVersion, pid` | agent 初始化完成（主干据此握手） |
| `chart` | `seq, chars, hash, context, at, json` | **谱面正文（镜像之前）** |
| `chart-parsed` | `notes` | 游戏数出的音符数 |
| `level-context` | `context` | 歌曲 id / 难度 / key |
| `level-start` | `seq, chartSeq, at, mirror, offset` | **谱面真正启动**：游戏此刻已停在闸门上。`mirror` 是镜像开关，`offset` 是游戏生效的延迟拆开后的四项（`total` / `main` / `chart` / `user`） |
| `level-start-released` | `seq, at` | 主机已放行，游戏继续 |
| `note-index` | `notes` | 音符表建好了（`SetInformation` 之后），后面 `judge` 里的音符就查得到 |
| `progress` | `time` | 游戏时钟 `ProgressControl.nowTime`，约 100ms 一次；触控模块按它对表 |
| `judge` | `kind, noteCode, delta, isHold, time, at, note` | **每一次判决**。`kind` 是 `Perfect`/`Good`/`Bad`/`Miss`；`delta = nowTime − realTime`（正数=晚，Miss 没有）；`note` 是拿 `noteCode` 从音符表查回来的身份（`type` / `time` / `x` / `hold` / `line` / `above` / `index`），查不到就是 `null` |
| `result` | `seq, score, percent, perfect, good, bad, miss, early, late, combo, maxCombo, allPerfect, fullCombo` | **一局终局账目**，一个 gate 只报一次 |
| `warn` / `fatal` / `chart-error` | `reason` | 异常 |

反向（主机 → agent）只有一条，就是放行：

```python
script.post({"type": "release", "payload": {"seq": 1}})
```

agent 侧还留了 RPC：

```python
script.exports_sync.ping()      # 存活探测：一个字都不碰 il2cpp，闸门按着主线程时也能答
script.exports_sync.status()    # {chartSeq, lastChartSeq, lastContext, gateSeq, releasedCount, notesIndexed, lastResultSeq}
script.exports_sync.revert()    # 还原全部 hook（名单就是 frida/index.ts 里那张表）
```

## 依赖的取舍

Python 侧就 [requirements.txt](../requirements.txt) 里那五行（conda 环境 `auto_phigros`，
Python 3.14），frida 的版本是唯一有讲究的一个。

**刻意没用**的：`rich`（进度条用 tqdm 替了）、`z3-solver`（phisap 只在"指定目标分数"
那套预处理里用到它，而那套代码算完 `perfect/good/miss/combo` 之后什么也没干就
`return chart` —— 是个没写完的功能，这里不缝）、`PyQt5` / `av` / `pyusb` / `lz4`
（GUI、控制器、解包用的）、`Pillow`（透明视频必须走 ffmpeg，绘图就一并交给 OpenCV 了
—— 它的 `LINE_AA` 在单通道图上直接给出正确的覆盖率，不用像 PIL 那样超采样）。

Node 侧：`frida-il2cpp-bridge@0.14.0`、`esbuild`、`@types/frida-gum` 都在
`node_modules`；本地 `tsc` 只用于类型检查，不参与构建（打包是 esbuild 干的，
`../tsconfig.json` 因此配的是 `module: esnext` + `moduleResolution: bundler`）。

