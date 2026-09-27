# Phigros 4.0 音游内核逆向报告

**目标**：`C:\UserData\phigros\libil2cpp.so`
ELF64 ARM64 · 71,413,296 B · MD5 `4086181c2803dc92561c0e23488fe74c` · 216,700 个函数
**工具**：IDA Pro（idb 已由 Il2CppInspector 处理，注入了完整 IL2CPP 类型系统与 demangle 符号）

> 这份报告讲**游戏内部**：函数地址、字段偏移、判定公式、反编译伪代码。
> 工程实现（怎么用这些结论、踩过哪些坑）在 [`impl.md`](impl.md)，
> 安装与用法在 [`README.md`](README.md)。

---

## 0. 摘要

| 问题 | 结论 |
|---|---|
| **谱面如何加载** | Addressables 取 `Assets/Tracks/<songsId>/Chart_<EZ\|HD\|IN\|AT>.json` 的 `TextAsset`，然后 **`UnityEngine.JsonUtility.FromJson<Chart>(text)`** |
| **谱面镜像** | `if (levelStartInfo.mirror) Chart::Mirror()`：移动事件 `x → 1 − x`、旋转事件 `θ → −θ`、音符 `positionX → −positionX`。音符**只取负、不减 0.5**，说明 `positionX` 是沿判定线、以判定线为原点的偏移量。见 [§3.7](#37-谱面镜像chartmirror--0x1d286ac) |
| **开谱时机**（可下闸门处） | `LevelControl::SortForNoteWithFloorPosition` @ `0x1d25350` —— 谱面解析完、镜像应用完、`LevelInformation` 填好，但判定线与音符的 GameObject 一个都还没生成、音乐还没开始。`auto_phigros` 的闸门就架在这里 |
| **音符如何落下** | `localPosition.y = ±(note.floorPosition − lineFloor) × note.speed × Speed`，上轨取正、下轨取负；`Speed` 是玩家流速设置（默认 6.0） |
| **轨道如何滚动** | `lineFloor = speedEvent.floorPosition + (nowTime − speedEvent.startTime) × speedEvent.value` |
| **判定时间基准** | `nowTime = AudioSource.time − (mainOffset + chart.offset + 用户offset)`；三个分量与字段偏移见 [§4](#4-时间基准音频时钟) |
| **判定窗口（普通）** | Perfect `0.08 s` / Good `0.18 s` / Bad `0.22 s` |
| **判定窗口（课题模式）** | `0.04 / 0.09 / 0.14` **加上半帧时长**（帧率自适应放宽） |
| **音符 X 容差** | Tap/Hold `1.9` 世界单位；**Drag/Flick `2.1`**。**只比沿判定线的横向分量** |
| **判定时机** | `CheckNote`（Tap/Hold 头判）只在手指 `phase == TouchPhase.Began` 的那一帧被调用；Drag/Flick 逐帧比 `fingerPositionX`。见 [§6.6](#66-四类音符的判定实现) |
| **帧率策略** | `vSyncCount = 0`，`targetFrameRate` 由 `GameInformation::CheckFrameRate` 按刷新率定：≤ 89Hz → `60`，更高 → `min(2 × 刷新率, 300)`。判定是逐帧的，所以"手指在一个位置待多久"和"摆得准不准"一样要紧 |
| **判定线坐标** | `world = ((raw − 0.5) × 10) × (A, 1)`，`A = min(屏宽高比, 16/9)`；世界 `y ∈ [−5,5]`、`x ∈ [−5A,5A]`。**无翻转**，见 [§5.7](#57-raw-值--世界坐标y-朝向的关键容易搞反) |
| **垂直判定** | 判定线"无限细"：触点离判定线多远都无视，只看它投影到线上落在哪儿。见 [§6.3](#63-垂直判定x-轴容差与边缘放宽) |
| **音符类型** | `type`：1 = Tap，2 = Drag，3 = Hold，4 = Flick |
| **时间刻度** | 谱面 `time` 单位是 **1/32 拍**；`realTime = time × 1.875 / bpm`（1.875 = 60/32） |
| **计分** | `900000 × (perfect + 0.65×good)/N + 100000 × maxCombo/N` |

### 排查过程中最容易走弯路的一点

`.fake_strings`（516 KB 字符串字面量表）与 `il2cpp` 段（35 MB）里**都查不到** `judgeLineList` / `positionX` / `floorPosition` / `formatVersion` 这些键名——0 命中。

原因：**谱面就是明文 JSON，字段名走 IL2CPP 元数据反射**。`JsonUtility` 按字段名反射填充，编译产物里不保存键名字符串。所以"没有字段名字符串 ⇒ 用自定义二进制格式"这个推断是错的。

---

## 1. 二进制结构速览

| 段 | 起始 | 说明 |
|---|---|---|
| `.rodata` | `0xbea760` | 只读数据 |
| `.eh_frame` | `0x12ffe70` | 异常帧 |
| `.text` | `0x192b244` | **代码段，至 `0x1c4c924`，约 3.1 MB** |
| `il2cpp` | `0x1c4c924` | 只读数据区，**不含** global-metadata.dat 字符串 |
| `.data.rel.ro` | `0x3e57530` | IL2CPP 元数据使用表（method pointer / TypeInfo 槽位） |
| `.bss` | `0x44264b0` | |
| `.fake_strings` | `0x4650390` – `0x46ce390` | 516 KB 字符串字面量表，按字典序排列 |

> `.data.rel.ro` 中 `0x41e59xx` 一带是 method 指针槽位（`Chart::ctor` / `ChartNote::ctor` / `JudgeLine::ctor` 的地址都在那里），所以 IDA 的 xref 会显示成 `?`。这属正常现象，**不代表函数没被调用**。

Il2CppInspector 是从**外部 `global-metadata.dat`** 读取类型名的，元数据没有内嵌进 `.so`。

### 1.1 游戏代码分布

游戏自身的类全部没有命名空间前缀（顶层类），集中在 `0x1c70000 – 0x1d80000`，核心玩法在 `0x1d1f000 – 0x1d37000`：

```
0x1d1a1b0  HPProvider                 血量
0x1d1ebb0  FingerManagement           手指管理（Touch → Fingers）
0x1d206b0  JudgeControl               触摸 → 判定
0x1d22834  JudgeLineControl           轨道：事件插值、音符生成
0x1d24e00  LevelControl               关卡总控、谱面加载
0x1d28f5c  LevelInformation           关卡运行时数据
0x1d30358  ClickControl / DragControl / FlickControl / HoldControl
0x1d30a84  ScoreControl               计分
0x1d32ce8  NoteUpdateManager          每帧驱动
0x1d34270  ProgressControl            音频时钟
```

---

## 2. 谱面数据结构（含 JSON Schema）

以下结构由 IDA 中 Il2CppInspector 恢复的类型直接读出，**字段偏移即真值**。

### 2.1 `Chart`（根，size 0x28）

| 偏移 | 类型 | 字段 |
|---|---|---|
| 0x10 | int32 | `formatVersion` |
| 0x14 | float | `offset` |
| 0x18 | `List<JudgeLine>` | `judgeLineList` |
| 0x20 | `List<GameInformation.BlockArea>` | `blockAreaList` |

### 2.2 `JudgeLine`（size 0x48）

| 偏移 | 类型 | 字段 |
|---|---|---|
| 0x10 | float | `bpm` |
| 0x18 | `List<SpeedEvent>` | `speedEvents` |
| 0x20 | `List<ChartNote>` | `notesAbove` |
| 0x28 | `List<ChartNote>` | `notesBelow` |
| 0x30 | `List<JudgeLineEvent>` | `judgeLineDisappearEvents` |
| 0x38 | `List<JudgeLineEvent>` | `judgeLineMoveEvents` |
| 0x40 | `List<JudgeLineEvent>` | `judgeLineRotateEvents` |

### 2.3 `ChartNote`（size 0x40）

| 偏移 | 类型 | 字段 | 备注 |
|---|---|---|---|
| 0x10 | int32 | `type` | 1=Tap 2=Drag 3=Hold 4=Flick |
| 0x14 | **int32** | `time` | **1/32 拍为单位的整数刻度** |
| 0x18 | float | `positionX` | 轨道内横向位置 |
| 0x1c | float | `holdTime` | Hold 时长，**同样是 1/32 拍刻度** |
| 0x20 | float | `speed` | 谱师给的流速倍率 |
| 0x24 | float | `floorPosition` | 谱师预先算好的"楼层位置" |
| 0x28 | bool | `isJudged` | 运行期 |
| 0x29 | bool | `isJudgedForFlick` | 运行期 |
| 0x2c | float | `realTime` | **运行期**：换算成秒的判定时刻 |
| 0x30 | int32 | `judgeLineIndex` | **运行期**：`2×轨道序号 + (下方?1:0)` |
| 0x34 | int32 | `noteIndex` | **运行期**：列表内下标 |
| 0x38 | float | `noteCode` | **运行期**：唯一编号 |

### 2.4 `SpeedEvent`（size 0x20）

`+0x10 startTime` · `+0x14 endTime` · `+0x18 floorPosition` · `+0x1c value`

### 2.5 `JudgeLineEvent`（size 0x28）

`+0x10 startTime` · `+0x14 endTime` · `+0x18 start` · `+0x1c end` · `+0x20 start2` · `+0x24 end2`

- `judgeLineMoveEvents`：`start→end` 映射到 X，`start2→end2` 映射到 Y
- `judgeLineRotateEvents`：`start` / `end` 是角度
- `judgeLineDisappearEvents`：`start` / `end` 是不透明度（alpha）

### 2.6 `GameInformation.BlockArea`（size 0x50，第九章 ARG 用）

`topRightPercentage` / `bottomLeftPercentage`（Vector2）、`appearTime` / `enableTime` / `disableTime` / `disappearTime`、`isSubtract`、`rotateEvents` / `moveEvents` / `scaleEvents`。

### 2.7 由结构反推出的 JSON 骨架

`JsonUtility` 要求 **JSON 键名 == 字段名**，且必须是扁平对象（不支持 RPE 那种"数组压缩"写法）：

```jsonc
{
  "formatVersion": 3,
  "offset": 0.0,
  "judgeLineList": [
    {
      "bpm": 120.0,
      "speedEvents": [
        { "startTime": 0.0, "endTime": 0.0, "floorPosition": 0.0, "value": 1.0 }
      ],
      "notesAbove": [
        { "type": 1, "time": 0, "positionX": 0.0, "holdTime": 0.0,
          "speed": 1.0, "floorPosition": 0.0 }
      ],
      "notesBelow": [],
      "judgeLineDisappearEvents": [
        { "startTime": 0.0, "endTime": 0.0, "start": 1.0, "end": 1.0,
          "start2": 0.0, "end2": 0.0 }
      ],
      "judgeLineMoveEvents":   [ /* 同上 */ ],
      "judgeLineRotateEvents": [ /* 同上 */ ]
    }
  ],
  "blockAreaList": []
}
```

> `isJudged` … `noteCode` 是运行期字段，谱面文件里一般不写。`holdTime` 为 0 时表示非 Hold。
> `speed` 与 `floorPosition` **由谱师工具预先算好并直接写进 JSON**——运行时只读不改（见 §3.4）。

---

## 3. 谱面加载链（问题一）

### 3.1 生成 Addressable key

`SongsItem::GetLevelStartInfo(int level)` @ **`0x1c9bd80`**

```c
// "Assets/Tracks/" + songsId + "/Chart_" + levels[level] + ".json"
judgeLineImages = String::Concat("Assets/Tracks/", this->songsId,
                                 "/Chart_", this->levels[level], ".json");
v5->chartAddressableKey = judgeLineImages;

// 音乐：hasDifferentMusic ? "Assets/Tracks/<id>/music_<level>.wav" : "Assets/Tracks/<id>/music.wav"
```

`LevelStartInfo`（size 0x78）关键字段：

```
+0x48 musicAddressableKey
+0x50 chartAddressableKey      <-- 谱面 key
+0x58 illustrationKey
+0x60 <mirror>k__BackingField   <-- IDA 显示成 _mirror_k__BackingField，取 get_mirror
+0x68 judgeLineImages
+0x70 levelMods
```

**证据**：`.fake_strings` 中 `0x4658f52` 处是字符串 `"/Chart_"`，`0x4658e82` 处是 `".json"`，二者均只被 `SongsItem::GetLevelStartInfo` 与 `SplashScene::NextScene` 引用。同一区域还有成品路径：

```
Assets/Tracks/望影の方舟Six.SeURa.0/Chart_EZ_Error.json
Assets/Tracks/望影の方舟Six.SeURa.0/Chart_HD_Error.json
Assets/Tracks/望影の方舟Six.SeURa.0/Chart_IN_Error.json
```

这是该曲目谱面加载失败时的兜底谱（`_Error` 变体）。

### 3.2 加载与反序列化

`LevelControl::_Start_d__46::MoveNext` @ **`0x1d27748`**（第 255–283 行）

```c
chartAddressableKey = levelStartInfo->chartAddressableKey;
this = AssetStore::Get<TextAsset>(chartAddressableKey);          // Addressables
v2->_chartAssetRef_5__4 = this;
...
text = UnityEngine::TextAsset::get_text(assetRef->obj);
this = UnityEngine::JsonUtility::FromJson<Chart>(text);          // <<< 谱面解析
_4__this->chart = (Chart *)this;
AssetStore::AssetRef<TextAsset>::ReleaseAll(...);
```

### 3.3 加载后处理（同函数，第 290–403 行）

```c
if (levelStartInfo->mirror) Chart::Mirror(chart);                     // 0x1d286ac
DoppelgangerLevelEffect::TryStripHdUnlockNote(chart, levelStartInfo);  // HD 解锁音符剥离
AddressableAudioSource::set_Clip(progressControl->audioSource,
                                 levelStartInfo->musicAddressableKey);

// 校准与缩放
screenH = gameInformation->screenH;  screenW = gameInformation->screenW;
if (screenH == 0 || screenW == 0) { screenH = Screen.height; screenW = Screen.width; }

levelInformation->offset    = GameInformation.mainOffset + chart.offset + gameInformation.offset;
levelInformation->noteScale = gameInformation.noteScale;
ratio = screenW / screenH;
if (ratio < 1.7778) noteScale *= ratio / 1.7778;      // 非 16:9 屏幕音符等比缩小
levelInformation->scale     = noteScale;
levelInformation->musicVol  = gameInformation.musicVol;
levelInformation->hitFxIsOn = gameInformation.hitFxIsOn;
levelInformation->numOfNotes = Chart::GetNoteCount(chart);            // 0x1d28918

LevelInformation.Speed = gameInformation.speed;                       // 全局流速（静态）
levelInformation->judgeLineList = chart->judgeLineList;
levelInformation->stopBeforeBegan = gameInformation.stopBeforeBegan;

LevelControl::SortForNoteWithFloorPosition();   // 0x1d25350  按 floorPosition 排序
LevelControl::SetCodeForNote();                 // 0x1d2516c  分配 noteCode
LevelControl::SetInformation();                 // 0x1d2563c  核心后处理
LevelControl::SortForAllNoteWithTime();         // 0x1d26ee8  生成全局按时间排序表

judgeControl->chartNoteSortByTime = levelInformation->chartNoteSortByTime;

// 为每条轨道实例化 GameObject 并接线
for (i = 0; i < judgeLineList.Count; i++) {
    go  = Object::Instantiate(levelControl->judgeLine, ...);
    go.transform.SetParent(gameCanvas.transform);
    jlc = go.GetComponent<JudgeLineControl>();
    jlc->index            = i;
    jlc->levelInformation = levelInformation;
    jlc->progressControl  = progressControl;
    jlc->scoreControl     = scoreControl;
    jlc->Click = ...; jlc->Drag = ...; jlc->Hold = ...; jlc->Flick = ...;  // 音符预制体
    jlc->ClickHL/HoldHL0/HoldHL1/DragHL/FlickHL = ...;                     // 高亮精灵
    judgeControl->judgeLines.Add(go);
    judgeControl->judgeLineControls.Add(jlc);
    levelInformation->judgeLines.Add(go);
}
```

`LevelControl::Awake` @ `0x1d24e00` 另外通过 `Addressables::InstantiateAsync` 实例化 `LevelStartInfo.levelMods` 中的关卡 Mod 预制体（Doppelganger / Luminescence / Retribution / SecretChallengeLife 等）。

### 3.4 `SetInformation` 对每个音符做什么

`LevelControl::SetInformation` @ **`0x1d2563c`**（伪代码 102 KB，全二进制最大的函数之一）

外层循环 `for (i = 0; i < judgeLineList.Count; i++)`，`v9 = 2*i`；内层分别遍历 `notesAbove` 与 `notesBelow`（两支逻辑对称）：

```c
// 239 行：realTime = time * 1.875 / bpm
*((float *)&note->realTime) = (float)(note->time * 1.875) / judgeLine->bpm;

// 253 行：holdTime 从 1/32 拍刻度换算成秒
v17 = (float)(int)(holdTime + 0.0001) * 1.875;
note->holdTime = v17 / judgeLine->bpm;

// 224 行：非 16:9 屏幕按宽高比收缩横向偏移（与 JudgeLineControl::Start 的 moveScale 同一个因子）
if (screenW / screenH < 1.7778) note->positionX *= (screenW / screenH) / 1.7778;   // 见 §3.7

// 257-260 行：运行期索引 + 复位判定标记
note->judgeLineIndex = v9;          // notesAbove: 2*i
note->isJudged       = false;

// 262-283 行：加入全局按时间排序表
levelInformation->chartNoteSortByTime.Add(note);
```

`notesBelow` 分支的 `note->judgeLineIndex = v9 | 1`（即 `2*i + 1`）。

> **`speed` 与 `floorPosition` 没有被改写**——直接来自 JSON。

### 3.5 `SetCodeForNote`：noteCode 编码

`LevelControl::SetCodeForNote` @ **`0x1d2516c`**

```
notesAbove[k].noteCode = lineIdx × 1_000_000 + k × 10
notesBelow[k].noteCode = lineIdx × 1_000_000 + 100_000 + k × 10
```

（`v4` 从 0 起，每轨道 `+= 1_000_000`；`v8 = v4` 逐音符 `+= 10`；`v6` 从 100_000 起，每轨道 `+= 1_000_000`。）

`noteCode` 是 float，全程作为音符的**唯一 ID**，用于去重、特效归属与回放对齐（§8）。

### 3.6 调用链总览

```
SongsItem::GetLevelStartInfo(level)            0x1c9bd80  产出 chartAddressableKey
LevelControl::Awake                            0x1d24e00  实例化 levelMods
LevelControl::Start → _Start_d__46::MoveNext   0x1d27748
   ├─ AssetStore::Get<TextAsset>(key)                      Addressables
   ├─ JsonUtility::FromJson<Chart>(text)         <<< 谱面解析
   ├─ Chart::Mirror                              0x1d286ac   仅当 levelStartInfo.mirror
   ├─ DoppelgangerLevelEffect::TryStripHdUnlockNote
   ├─ Chart::GetNoteCount                        0x1d28918
   ├─ LevelControl::SortForNoteWithFloorPosition 0x1d25350   <<< 闸门（★）
   ├─ LevelControl::SetCodeForNote               0x1d2516c
   ├─ LevelControl::SetInformation               0x1d2563c  realTime / holdTime / judgeLineIndex
   ├─ LevelControl::SortForAllNoteWithTime       0x1d26ee8
   └─ 循环 Instantiate 每条 JudgeLine 并接线到 JudgeLineControl
```

> **★ 为什么把闸门架在这儿。** 它是启动协程里"把谱面落地"的第一步，
> **无条件调用、每关只调用一次**（`XrefsTo` 只有 `0x1d27ef0` 一处）。
> 此刻 `Chart` 已解析完、镜像已应用完、`LevelInformation` 已填好，而判定线 / 音符的
> GameObject 一个都还没 `Instantiate`、音乐也还没起 —— 卡在这里游戏就是"万事俱备，
> 只欠东风"。更关键的是紧跟其后的 `SetInformation` 会**原地改写** `chart` 对象
> （raw → 世界坐标、tick → 秒），改写之后 `JsonUtility::ToJson` 就不再是官谱格式了，
> 所以**回读镜像后的谱面必须赶在 `SetInformation` 之前**。

### 3.7 谱面镜像（`Chart::Mirror` @ `0x1d286ac`）

开关是 `LevelStartInfo` 上的属性 `mirror`（自动属性，背后字段的真名是
**`<mirror>k__BackingField`**，`get_mirror` @ `0x1ca407c`），由
`SongSelector::ToggleChartMirror` @ `0x1d571a0` /
`Chapter8SelectMusicControl::ToggleChartMirror` @ `0x1cf0cf0` 翻转，存档里的字段名是
`chartMirror`（`0x46b5d59`）。

`Chart::Mirror()` 在整个 `libil2cpp.so` 里**只有一个调用者**：`LevelControl::_Start_d__46::MoveNext`
（`0x1d27d24`）。它遍历 `judgeLineList` 调 `JudgeLine::Mirror(oldVersion = formatVersion == 1)`，
再遍历 `blockAreaList` 调 `BlockArea::Mirror()`。

`JudgeLine::Mirror` @ `0x1d28afc` 只动三样东西：

| 对象 | 新值 | 反编译依据 |
|---|---|---|
| `notesAbove` / `notesBelow` 里每个音符的 `positionX`（`ChartNote +0x18`） | `−positionX` | `*(float *)&cur[1].monitor = -*(float *)&cur[1].monitor` |
| `judgeLineMoveEvents` 的 `start` / `end` | `1 − x`；v1 老格式为 `(880000 − p) + 2 × (p mod 1000)`，即整数打包里的 `i → 880 − i` | `vsub_f32(1.0, value)` / 常量 `0x4956D800 = 880000.0f` |
| `judgeLineRotateEvents` 的 `start` / `end` | `−θ` | `vneg_f32` |

前两条把判定线整体绕屏幕中心翻过去；第三条**只取负、不减 0.5** —— 这恰好证明了
`positionX` 是**以判定线为原点的沿线上偏移量**，而不是屏幕绝对坐标。这一点与
`JudgeControl::GetFingerPosition` 只算"判定线局部坐标下的横向分量"是同一件事（§6.3），
也是 `algorithms/chart.py` 里 `point_at = 判定线位置 + 朝向 × positionX × 0.9` 的依据。

**三条合起来，镜像就是整个局面绕中线左右翻一次。** 判定线上任一点的虚拟屏幕坐标
从 `p = L + R(θ)·offset` 变成：

```text
p' = (16 − Lx, Ly) + R(−θ)·(−offset)
   = (16 − (Lx + offset·cosθ), Ly + offset·sinθ)
   = (16 − p.x, p.y)
```

也就是说"按哪儿"只是翻了个身，判定依据（沿判定线的横向分量，§6.3）本身也是镜像不变的
—— 所以镜像**不需要重算**：把原来的解整体水平翻过来就是镜像后谱面的解。
`auto_phigros` 正是这么做的（`PlanResult.mirrored`），谱面因此只需要读一遍。

顺带确证了 `positionX` 的缩放（`NOTE_X_SCALE = 0.9` 的来源）：

```c
// JudgeLineControl::Start @ 0x1d2407c
moveScale = (Screen.height / Screen.width <= 0.5625) ? 1.0
           : (Screen.width * 0.5625 / Screen.height);      // = min(1, (w/h)/(16/9))

// LevelControl::SetInformation @ 0x1d2563c（对每个 ChartNote）
if (screenW / screenH < 1.7778)
    note->positionX *= (screenW / screenH) / 1.7778;        // 同一个因子
```

于是 `positionX` → 16×9 虚拟屏的换算**与设备无关**：
`positionX × (A/(16/9)) × (16/(10A)) = 0.9 × positionX`。

---

## 4. 时间基准：音频时钟

`ProgressControl::Update` @ **`0x1d3483c`**

```c
// _offset 初值 -100.0 作为哨兵
if (fabsf(_offset + 100.0) < 1e-6 && audioSource->ready) {
    _offset      = levelInformation->offset;   // mainOffset + chart.offset + user offset
    nowTime      = 0.00001;
    _audioLength = audioSource->ClipLength;
}
if (Time.timeSinceLevelLoad >= _startTime
    && audioSource->status == 0 && _playPrepared && _volumeSet) {
    audioTime = audioSource->time;
    nowTime   = audioTime - _offset;
    if (nowTime < 0.00001) nowTime = 0.00001;
}
```

**所有位移与判定都以 `nowTime` 为唯一时间源**，即"音频播放秒数 − 校准偏移"。

偏移由三部分相加（`MoveNext` @ 0x1d27748）：

```
levelInformation.offset = GameInformation.mainOffset + chart.offset + GameInformation.offset
```

> 反编译器在这里把 `chart` 那个基址认丢了（渲染成 `*((float *)&this->__1__state + 1)`，看着像在读
> 状态机的填充字节）。**以汇编为准**，三处都点了名：
>
> ```asm
> 0x1d27e08  LDR  X0,  [X20,#0x140]   ; chart            (LevelControl+0x140 = Chart*)
> 0x1d27e10  LDR  X8,  [X19,#0x28]    ; gameInformation
> 0x1d27e18  LDR  X21, [X20,#0x20]    ; levelInformation (LevelControl+0x20)
> 0x1d27e28  LDR  S1,  [X0, #0x14]    ; chart.offset          <- 谱面自带
> 0x1d27e2c  LDR  S2,  [X8, #0xAC]    ; gameInformation.offset <- 玩家设置
> 0x1d27e34  FADD S1,  S1, S2
> 0x1d27e3c  LDR  S3,  [X9, #0x10]    ; GameInformation::mainOffset（静态）
> 0x1d27e44  FADD S1,  S3, S1
> 0x1d27e48  STR  S1,  [X21,#0x20]    ; levelInformation->offset
> ```
>
> 三个字段的归属也逐个核过类型：`Chart +0x10 formatVersion / +0x14 offset / +0x18 judgeLineList`
> 与 JSON 根上的三个键**一一对应**（`JsonUtility.FromJson<Chart>` 按名字填），
> `LevelControl +0x140 = chart`、`LevelControl +0x20 = levelInformation`、
> `GameInformation +0xAC = offset`。

**注意 `chart.offset` 不是玩家设置**，是**谱面文件自带**的字段（策划在编辑器里定的整体校准），
游戏 UI 里看不到它；玩家能调的是 `GameInformation.offset`（设置里的延迟校准）。我们抓到的
两张谱面这个值都是 `0.0`，所以日志里恒显示 `谱面 +0ms`。单位是**秒**。

- `GameInformation.mainOffset` — 在 `GameInformation::Awake` @ `0x1c9cca4`（第 999 行）由 DSP 缓冲大小推出，是设备音频延迟补偿：
  ```c
  mainOffset = (float)dspBufferSize * 0.00016f - 0.02048f;
  ```
- `GameInformation.offset` — 玩家设置，`SaveManagement::LoadFloat("offset", 0.0f)`
- `GameInformation.speed` — 玩家流速，**默认 6.0**，`SaveManagement::LoadFloat("speed", 6.0f)`
- `GameInformation.noteScale` — 默认 1.0

字段位置（开谱闸门那一刻都读得到）：

| 值 | 所在 | 偏移 |
|---|---|---|
| 生效总量 | `LevelInformation.offset` | `+0x20` |
| 谱面自带 | `Chart.offset`（JSON 根上的 `offset`） | `+0x14` |
| 玩家设置 | `GameInformation.offset` | `+0xac` |
| **判定用时钟** | `ProgressControl.nowTime` | `+0x88` |
| 生效偏移（副本） | `ProgressControl._offset` | `+0x90` |

`mainOffset` 是 `GameInformation__StaticFields + 0x10`，**元数据里有名字**（早先误以为"没有符号
可查"，所以在 `auto_phigros` 里拿 `total − chart − user` 反推过一阵子；现在直接读，并顺手核对
三项之和等不等于 `total`）。

> `auto_phigros` 在闸门那一刻读前三个报给主机对账（"到底是哪个旋钮偏了"），
> 之后每 100ms 从 `ProgressControl::Update` 把 `nowTime` 回传，触控播放**直接跟着
> `nowTime` 走**。因为游戏侧的偏移已经含在 `nowTime` 里了，跟着它就等于跟着延迟设置走 ——
> **不能也不该再加一次**。

### 4.1 判定窗口每帧被重写

同一函数（`ProgressControl::Update`）：

```c
if (progressControl->_inChallengeMode) {
    // 最近 10 帧 deltaTime 的平均值 × 0.5
    avg = (_timeSum / min(_nowFrameIndex, 10)) * 0.5;
    JudgeControl.InChallengeMode  = true;
    JudgeControl.PerfectTimeRange = avg + 0.04f;   // 0x3D23D70A
    JudgeControl.GoodTimeRange    = avg + 0.09f;   // 0x3DB851EC
    badTimeRange                  = avg + 0.14f;
} else {
    JudgeControl.InChallengeMode  = false;
    JudgeControl.PerfectTimeRange = 0.08f;         // 0x3DA3D70A
    JudgeControl.GoodTimeRange    = 0.18f;         // 0x3E3851EC
    badTimeRange                  = 0.22f;
}
JudgeControl.BadTimeRange = badTimeRange;
```

静态字段定义见 `JudgeControl::cctor` @ **`0x1d227d8`**：

```c
*(_QWORD *)&static_fields->PerfectTimeRange = 0x3E3851EC3DA3D70ALL;   // 0.08 / 0.18
static_fields->BadTimeRange = 0.22f;
```

`JudgeControl__StaticFields` 布局：`+0x00 InChallengeMode(bool)` · `+0x04 PerfectTimeRange` · `+0x08 GoodTimeRange` · `+0x0c BadTimeRange`。

> **课题模式（Challenge Mode）基础窗口更严（0.04/0.09/0.14），但会加上半帧时长做帧率补偿。**
> 60 fps 时 `avg ≈ 0.00833`，即 Perfect ≈ 0.0483 / Good ≈ 0.0983 / Bad ≈ 0.1483。掉帧时窗口自动放宽。

### 4.2 关卡结束

```c
if (nowTime + min(_offset, 0) > _audioLength + 0.2 && levelInformation->canLevelOver) {
    audioSource->Stop();
    levelInformation->levelOver = true;
}
```

之后 `_overTime` 计时，1.2 s 后收尾切场景。

---

## 5. 音符如何落下（问题二）

### 5.1 位置公式

`ClickControl::NoteMove` @ **`0x1d30358`**
（`DragControl::NoteMove` @ `0x1d31174`、`FlickControl::NoteMove` @ `0x1d31768`、`HoldControl::NoteMove` @ `0x1d31e2c` 三者基础公式同构）

```c
// v9 = note.judgeLineIndex % 2   →  0 = 上方, 1 = 下方
// j  = note.judgeLineIndex / 2   →  轨道序号（floorPositions 的下标）

if (v9 == 0) {                                  // notesAbove
    v22 = (note->floorPosition - floorPositions[j]) * note->speed;
} else {                                        // notesBelow
    v22 = note->speed * -(note->floorPosition - floorPositions[j]);
}

v35.z = 0.0;
v35.y = v22 * TypeInfo::LevelInformation->static_fields->Speed;   // 全局流速
v35.x = note->positionX;
Transform::set_localPosition(transform, v35);
```

即：

> **y = ±(note.floorPosition − lineFloor) × note.speed × Speed**
> 上轨取 `+`，下轨取 `−`。

判定线位于 `y = 0`：音符从远处（或下方）移动过来，`y` 穿过 0 的时刻就是判定时刻。

### 5.2 轨道自身 floorPosition 的积分

`LevelInformation.floorPositions` 是 `Single[]`，按轨道序号索引。`JudgeLineControl::UpdateInfo` @ **`0x1d24214`**：

```c
// floorPositions[index] = speedEvent.floorPosition
//                       + (nowTime - speedEvent.startTime) * speedEvent.value
floorPositions->vector[index] = v115
                              + (float)((nowTime - v118) * *((float *)speedEvent + 7));
//   v114[6] / +0x18  = speedEvent.floorPosition
//   v117[4] / +0x10  = speedEvent.startTime
//   +7       / +0x1c = speedEvent.value
```

轨道用当前生效的 `SpeedEvent` **线性积分**出自己的"已滚动距离"。`nowSpeedIndex` 由 `JudgeLineControl::UpdateJudgeLineEventIndex` @ **`0x1d22834`** 维护（逐个比较 `event.endTime > nowTime` 推进）。

**谱面 `floorPosition` 与运行时轨道 `floorPosition` 使用同一套单位**，这正是差值能直接当屏幕坐标用的原因。

### 5.3 音符是**一次性全部实例化**的

`JudgeLineControl::UpdateProductionIndex` @ **`0x1d22a98`**（全文只有这几行）：

```c
v2->nowProductIndexAbove = notesAbove->_size - 1;   // 直接就是 Count - 1
v2->nowProductIndexBelow = notesBelow->_size - 1;
```

`UpdateInfo` 随后：

```c
JudgeLineControl::UpdateProductionIndex(this, v5);
if (this->lastProductIndexAbove != this->nowProductIndexAbove) {
    for (i = lastProductIndexAbove + 1; i <= nowProductIndexAbove; ++i)
        JudgeLineControl::CreateNote(this, i, true);
    this->lastProductIndexAbove = nowProductIndexAbove;
}
// notesBelow 同理
```

> **每条轨道的全部音符在关卡开始前一次性创建完毕**，不存在"提前 N 秒预生成"的窗口机制。
> 之后每帧只更新位置与显隐；超出屏幕的音符不是被销毁，而是被挪到 `(0, 0, −50)`。

这也解释了 `NoteUpdateManager::Update` 里为什么是"遍历全部控件 + 两个剔除条件"。

### 5.4 每帧驱动顺序

`NoteUpdateManager::Update` @ **`0x1d32ce8`**：

```c
// 1) 先更新所有轨道（推 floorPositions、推进事件索引、创建音符）
for (i = 0; i < judgeControl->judgeLineControls.Count; i++)
    JudgeLineControl::UpdateInfo(judgeLineControls[i]);

// 2) 再按类型遍历所有音符控件
foreach (clickControl) { ...; ClickControl::NoteMove();
                         if (note->realTime <= nowTime + 2.0) ClickControl::Judge(); ... }
foreach (dragControl)  { ...; DragControl::NoteMove();  ... DragControl::Judge();  ... }
foreach (holdControl)  { ...; HoldControl::NoteMove();  ... HoldControl::Judge();  ... }
foreach (flickControl) { ...; FlickControl::NoteMove(); ... FlickControl::Judge(); ... }
```

两个关键条件（以 Click 为例，第 150–163 行）：

```c
// (a) 时间提前量：超出 2 秒预判窗口就完全跳过 Judge
if (note->realTime <= nowTime + 2.0) { ... Judge() ... }

// (b) 屏幕外剔除（20 个世界单位）
v15 = note->floorPosition - floorPositions[j];
if (v15 < -fmaxf(note->floorPosition / 6000000.0, 0.001)) goto skip;
if ((v15 * note->speed) * LevelInformation.Speed <= 20.0) { 屏幕内，正常绘制 }
else { transform.localPosition = (0, 0, -50); }              // 挪到屏幕外
```

### 5.5 淡出

`ClickControl::NoteMove` 结尾：

```c
if (nowTime > note->realTime) {
    color.a = 1.0 - (nowTime - note->realTime) / (JudgeControl.GoodTimeRange - 0.02);
    spriteRenderer.color = color;
}
```

Tap 在过判定时刻后 `0.18 − 0.02 = 0.16 s` 内淡出。Drag 用 `/ -0.1`（0.1 s），Flick 用 `/ -0.18`（0.18 s）。

### 5.6 轨道移动 / 旋转事件如何插值

`JudgeLineControl::UpdateInfo` @ `0x1d24214`，第 843–914 行：

```c
// 移动事件（nowMoveIndex）
ev = judgeLineMoveEvents[nowMoveIndex];
pos.x = ((v94 - v90) * (v92 - v89)) / (v129 - v81) + v130;   // start → end 线性插值 → X
pos.y = ((v85 - v134) * (v132 - v131)) / (v91 - v95) + ev[8]; // start2 → end2 → Y
pos.z = 0.0;
Transform::set_localPosition(lineTransform, pos);

// 旋转事件（nowRotateIndex）
ev = judgeLineRotateEvents[nowRotateIndex];
this->theta = ((v97 - v99) * (v100 - v102)) / (v104 - v106) + ev[6];  // 角度线性插值
Transform::set_localRotation(lineTransform, Quaternion::AngleAxis(...));
```

**全部是线性插值，没有任何缓动函数**（整个 `UpdateInfo` 里搜不到 `Lerp` / `SmoothStep` / `AnimationCurve`）。

> 这是 Phigros 官方格式的一个重要特征：**缓动是谱师工具在导出时把曲线拆成大量线性小段实现的**，所以官方谱面的 `judgeLineMoveEvents` 动辄上千条。运行时只做线性插值。

`theta` 同时被 `JudgeControl::CheckNote` 用于坐标变换（见 §6.4）。

### 5.7 raw 值 → 世界坐标（y 朝向的关键，容易搞反）

`LevelControl::SetInformation` 里有两条常量（第 169–170 行、686–687 行两处重复，分别对应 v2+ 与 v1 两条路径）：

```c
v6 = -0.5;
v7 = 10.0;
```

每个 move 事件的四个坐标分量都按这同一个模式换算：

```c
// X（start / end）—— 带屏幕宽高比
// 窄屏 (screenW/screenH <= 1.7778)：
ev[?] = screenW * (((raw + v6) * v7) / screenH);
// 宽屏：
ev[?] = (((raw + v6) * v7) / 9.0) * 16.0;

// Y（start2 / end2）—— 没有宽高比
ev[?] = (raw + v6) * v7;
```

（X 的四处在第 1185 / 1273–1274 / 1284 / 1370–1371 行，Y 的三处在第 1228 / 1326 / 1415 行。）

于是：

```
world_x = (raw_x - 0.5) * 10 * A        A = min(screenW / screenH, 16/9)
world_y = (raw_y - 0.5) * 10
```

**世界是 `y ∈ [-5, 5]`、`x ∈ [-5A, 5A]`** —— 这也和 `JudgeControl::CheckPause`
里硬编码的暂停键范围 `y ∈ [4.05, 4.85]`、以及窄屏公式里的 `(w/h) * -5.0`
（`-5.0` 正是半高）对得上。

> ⚠️ **`−0.5` 的偏移意味着 `raw = 0` 对应 y 的负半轴（屏幕下方），`raw = 1` 对应上方。
> 没有翻转。** 再结合 §5.6：`localPosition` 直接吃这个结果，角度也直接吃 `theta` 的正值，
> 所以**整个坐标系只有一次 `-0.5` 平移和一次 `×10` 缩放，没有任何镜像**。

折算到 16×9 的虚拟屏（正好是把世界按 `0.9` 缩放、再平移半个屏）：

```
虚拟 x = ((raw_x - 0.5) * 10A + 5A) * 0.9 = 16 * raw_x
虚拟 y = ((raw_y - 0.5) * 10  + 5 ) * 0.9 =  9 * raw_y
```

两条都是干净的线性映射。音符的横向偏移 `positionX` 是沿判定线的**世界单位**，
换算到虚拟屏要乘 `16 / (10A) = 0.9`（A = 16/9 时）。

> **常见陷阱**：社区实现（例如 phisap）用的是 `h * (1 - start2)` 和 `-radians(deg)`，
> 等于把 y 与角度一起做了垂直镜像。因为 Phigros 是**垂直判定**（§6.3），这个镜像对
> **水平判定线完全无影响**，而官谱里绝大多数判定线事件的角度就是 0°，所以镜像了也照样能打。
> 但只要判定线立起来（90°），镜像就会让横向分量整体错掉；做可视化时更是整层都对不上。
> **判定依据是 `(raw - 0.5) * 10`，不是 `5 - raw * 10`。**

---

## 6. 判定系统（问题三）

### 6.1 输入采集

`FingerManagement`（`Start` `0x1d1ebb0` / `Update` `0x1d1ebf4` / `SyncFingers` `0x1d1eee0`）把 `UnityEngine.Input.touches` 同步成 `List<Fingers>`：

```
Fingers (size 0x48)
 +0x10 int      fingerId
 +0x14 Vector2  lastMove
 +0x1c Vector2  nowMove
 +0x28 Vector2[] lastPositions
 +0x30 Vector2  lastPosition
 +0x38 Vector2  nowPosition
 +0x40 bool     isNewFlick
 +0x41 bool     stopped
 +0x44 int      phase
```

`JudgeControl::Update` @ **`0x1d207e4`**：

```c
this->nowTime = progressControl->nowTime;
foreach finger in fingerManagement->fingers {
    if (finger.pressed)                        // HIDWORD(finger[2].m_CachedPtr) == 0
        if (levelInformation->canPause) JudgeControl::CheckPause(fingerIndex);
}
if (previewElementUpdateControl->_HasBlocks) JudgeControl::CheckBlocks();
JudgeControl::GetFingerPosition();             // 所有手指都算，不看 phase
foreach finger {
    if (finger.pressed)                JudgeControl::CheckNote(fingerIndex);
    if (finger.isNewFlick /*LOBYTE*/)  JudgeControl::CheckFlick(fingerIndex);
}
```

> **`finger.pressed` 就是 `phase == 0`，也就是 `TouchPhase.Began`。** 取的是
> `Fingers + 0x44`（`SyncFingers` 里那句 `HIDWORD(current[4].klass) =
> UnityEngine::Touch::get_phase(...)` 直接存的就是 `Touch.get_phase()` 的返回值），
> 而 Unity 的 `TouchPhase` 是 `Began = 0, Moved = 1, Stationary = 2, Ended = 3, Canceled = 4`。
> 于是**`CheckNote`（Tap 与 Hold 的头判）只在"按下事件被处理的那一帧"跑一次**：
> 一根早就按在屏幕上、只是被 MOVE 过来的手指判不到 Tap；反过来，按下之后马上抬起没关系。
> Drag / Flick 不吃这一条 —— `DragControl::Judge`、`CheckFlick` 逐帧读
> `fingerPositionX`，只看位置。

`JudgeControl::GetFingerPosition` @ **`0x1d20d10`**：遍历 `judgeLines`（`judgeLines->_size`）与 `fingers`（`fingers->_size`），用 `Transform::get_position()` 取位置，把每个手指的世界坐标换算到各轨道的局部坐标系，写入

```
JudgeLineControl +0xd8  fingerPositionX  (Single[])
JudgeLineControl +0xe0  fingerPositionY  (Single[])
JudgeLineControl +0xe8  numOfFingers     <-- 由 fingers->_size 直接赋来
```

两个分量各是一句算术。设判定线世界坐标 `L`、朝向角 `θ`（度）、手指世界坐标 `F`，记
`d = F − L`，则写入的横向分量正好是

```
fingerPositionX[i] = (L.y − F.y)·sin(−θ·π/180) − (L.x − F.x)·cos(−θ·π/180)
                   =  d.x·cos θ + d.y·sin θ
                   =  (手指 − 判定线原点) · 判定线朝向
```

**这就是全篇横向判定的地基**：所有 `|… − positionX| < 1.9` 比的都是"手指相对判定线原点、
沿线的偏移"，而不是屏幕绝对坐标 —— 这也解释了为什么 `positionX` 在镜像里只取负、不减 0.5
（§3.7）。副作用是它**跟着判定线平移**：线整体移动多少，站着不动的手指在这个量上就漂多少。
Glaciaxion IN 有一条线每 53ms 在 `0.2 ↔ 0.8`（世界坐标 `3.2 ↔ 12.8`）之间跳，正是靠下面
§6.6 的 `_safeFrame` 宽容才判得过去。

### 6.2 候选音符滑动窗口

`JudgeControl::CheckNote` @ **`0x1d21104`** 开头（第 68–126 行）：

```c
this->endIndex = -1;
while (endIndex < chartNoteSortByTime.Count - 1) {
    n = chartNoteSortByTime[endIndex + 1];
    if (n->realTime >= nowTime + JudgeControl.BadTimeRange) break;   // +0.22
    this->endIndex = ++endIndex;
}
this->startIndex = endIndex;
while (endIndex >= 1) {
    n = chartNoteSortByTime[endIndex - 1];
    if (n->realTime > nowTime - JudgeControl.GoodTimeRange) break;   // -0.18
    --endIndex;
}
this->startIndex = endIndex;
```

每帧只扫描 `realTime ∈ (nowTime − 0.18, nowTime + 0.22)` 的音符。`JudgeControl.startIndex` / `endIndex` 是跨帧保留的增量游标（单调推进，不回头）。

### 6.3 垂直判定：X 轴容差与"边缘放宽"

**这是 Phigros 判定系统最本质的一条，本节及后续一切位置判据都建立在它上面。**

`JudgeControl::GetFingerPosition` @ **`0x1d212b4`** 为每根手指、每条判定线算出**两个**量
（分别写进 `JudgeLineControl` 的两个 `List<float>`，成员偏移 `+0xD8` 与 `+0xE0`）：

```c
// dx = fingerX - lineX, dy = fingerY - lineY, θ = 判定线角度（度）
array_lateral[i]  = dy * sinf(θ / -180.0 * π) - dx * cosf(θ / -180.0 * π);   // = -(dx·cosθ + dy·sinθ)
array_normal [i]  = dx * sinf(θ /  180.0 * π) - dy * cosf(θ /  180.0 * π);   // = -(dy·cosθ - dx·sinθ)
```

令 `z = dx + i·dy`：前者是 `-Re(z·e^{-iθ})`，**沿判定线的横向分量**；
后者是 `-Im(z·e^{-iθ})`，**垂直于判定线的法向分量**。两者只是符号相反，量纲一致。

而 `CheckNote` 里**只取了横向那一个**（`*((_QWORD *)jlc + 27)`，即 `+0xD8` 那个数组）：

```c
v18 = note->positionX;                          // note 作为 float* 的第 6 个 float = +0x18
jlc = judgeControl->judgeLineControls[note->judgeLineIndex / 2];
this->touchPos = fabsf(v18 - jlc->fingerPositionX[fingerIndex]);    // 只用横向分量

if (note->isJudged)                                    goto skip;
if (note->realTime - nowTime >= minDeltaTime + 0.01)   goto skip;   // 还没到时机
if (this->touchPos >= 1.9)                             goto skip;   // 横向超限

badTimeRange = JudgeControl.BadTimeRange;
if (touchPos > 0.9)
    badTimeRange += (touchPos - 0.9) * JudgeControl.PerfectTimeRange * (-0.5);
this->badTime = badTimeRange;
if (note->realTime - nowTime > this->badTime) goto skip;
```

**法向分量算出来了，但整个 `CheckNote`（以及 `CheckFlick`）从头到尾没有读过它一次。**

> 判定线是"无限细"的：手指离判定线多远都无所谓，只看它**投影到判定线上**落在哪儿。
> 我们把这个性质叫作**垂直判定**，它是 Phigros 区别于"判定区是二维矩形"的那类音游的核心特色。
>
> 三个直接推论：
>
> 1. 判定线可以被拉到屏幕外，而线上的音符依然可在屏幕内的对应位置触发 —— 只要横向分量对上。
> 2. 反过来说，把屏幕外的音符**沿垂直于判定线的方向**平移回屏幕，判定结果不变。
> 3. 判定区的"宽度"只存在于横向；法向的延展完全不影响能否判定（只影响画出多宽的一条带）。

要点：

- **横向容差硬阈值 1.9 世界单位**（`CheckNote` 走的是 Tap / Hold；**Drag 与 Flick 走各自
  的 2.1**，见 [§6.6](#66-四类音符的判定实现)）。手指投影到判定线上的位置必须落在音符横向范围内。
- `touchPos ∈ (0.9, 1.9)` 时，允许的**时间**窗被轻微收紧/放宽：
  `badTime = 0.22 + (touchPos − 0.9) × 0.08 × (−0.5)` —— 越靠音符边缘，迟到容忍度越低。
- 多候选时用距离度量挑选最近的一个（第 405–409 行）：
  ```
  |Δx₁| + |Δy₁| / 2.2   >=   |Δx₂| + |Δy₂| / 2.2
  ```
  取度量较小的那个（`v55 = 2.2` 是 Y 方向的归一化系数）。
  注意：这个**挑最近候选**的度量里 `|Δy|` 是参与的，但那只用来在多押里选一个，不构成判据。

### 6.4 Flick 的独立窗口

`JudgeControl::CheckFlick` @ **`0x1d21828`**：

```c
if (n->realTime >= nowTime + JudgeControl.PerfectTimeRange * 1.75) break;   // +0.14
...
if (n->realTime <= nowTime + JudgeControl.PerfectTimeRange * -1.75) break;  // -0.14
...
if (fabsf(noteX - jlc->fingerPositionX[fingerIndex]) >= 2.1) goto skip;
...
// 同样用 |Δx| + |Δy|/2.2 挑最近音符
```

> **Flick 的候选窗口是 ±0.14 s**（`PerfectTimeRange × 1.75`），比 Tap 的 −0.18/+0.22 更窄。
> 且必须带 `Fingers.isNewFlick`（新产生的滑动），单纯按住无效。

### 6.5 音符类型分派

`JudgeLineControl::CreateNote(int index, bool ifAbove)` @ **`0x1d22afc`**

```c
note = (ifAbove ? this->notesAbove : this->notesBelow)[index];
switch (note->type) {                         // *(_DWORD *)(note + 16)
  case 1: go = Instantiate(this->Click, transform, true);
          c  = go.GetComponent<ClickControl>();  break;
  case 2: go = Instantiate(this->Drag,  ...);
          c  = go.GetComponent<DragControl>();   break;
  case 3: go = Instantiate(this->Hold,  ...);
          c  = go.GetComponent<HoldControl>();   break;
  case 4: go = Instantiate(this->Flick, ...);
          c  = go.GetComponent<FlickControl>();  break;
}
c->levelInformation = this->levelInformation;   // +0x30
c->progressControl  = this->progressControl;    // +0x28
c->scoreControl     = this->scoreControl;       // +0x20
c->judgeLine        = this;                     // +0x38/0x40
c->noteInfor        = note;
```

实例化后先把 GameObject 挪到 `(1000, 0, 0)`（屏幕外），再由 `NoteMove` 摆正。

`chordSupport` 打开时（`JudgeLineControl::Start` @ `0x1d2407c`，`SaveManagement::LoadBool("chordSupport", true)`，**默认开**），还会在 `chartNoteSortByTime` 里做 `IndexOf(note)` 记录全局排序下标，用于渲染层级与判定顺序。

### 6.6 四类音符的判定实现

#### Tap — `ClickControl::Judge` @ `0x1d3060c`

```c
v5 = note->realTime - progressControl->nowTime;      // >0 = 早, <0 = 晚
this->isJudged = note->isJudged || this->isJudged;
if (this->isJudged) {                                // 已被触摸标记
    v7 = fabsf(v5);
    if (v7 < JudgeControl.PerfectTimeRange)     ScoreControl::Perfect(noteCode, -v5, pos, false);
    else if (v7 < JudgeControl.GoodTimeRange)   ScoreControl::Good   (noteCode, -v5, pos, false);
    else                                        ScoreControl::Bad    (noteCode, -v5);  // + noteBad 特效
    ProgressControl::CheckSpecifiedNoteHit(note->noteIndex);
    return true;
}
// 未被触摸：
if (v5 >= -JudgeControl.BadTimeRange)   return false;   // v5 >= -0.22，继续等待
ScoreControl::Miss(note->noteCode);                     // 迟到 > 0.22s → Miss
judgeLine->notesAbove[note->noteIndex].isJudged = true;
return true;
```

#### Drag — `DragControl::Judge` @ `0x1d313f0`

```c
Δ = note->realTime − progressControl->nowTime;
// ① 只在"音符前后 0.1 秒"这个窗口里才去比手指
if (fabsf(Δ) <= 0.1f && !this->isJudged) {
    if (levelInformation-><+0x80 的某个开关> == 0) {   // 正常游玩必为 0，否则 Drag 全判不了
        for (i = 0; i < judgeLine->numOfFingers; i++)
            if (fabsf(judgeLine->fingerPositionX[i] − note->positionX) < 2.1f)
                this->isJudged = true;                 // ← X 容差是 2.1，与 Flick 同一个数
        ProgressControl::CheckSpecifiedNoteHit(note->noteCode);
    }
}
if (Δ < 0.005f && this->isJudged) {
    SEPlayer.PlayHitFx(8);
    transform.localPosition = (note->positionX, 0, 0);
    ScoreControl::Perfect(noteCode, -Δ, position, /*isHold=*/false);
} else {
    if (Δ >= -0.1f || this->isJudged) return false;    // 还早，或者已经挂上号了
    ScoreControl::Miss(note->noteCode);                // 迟到 > 0.1s 且始终没挂上号
}
```

关键在**两个 0.1 不是一回事**：

- `fabsf(Δ) <= 0.1` 里的 0.1 是**时间**（秒）—— 判定窗口 `nowTime ∈ [realTime − 0.1, realTime + 0.1]`；
- 手指那一条比的是 `2.1`，**不是 0.1**（`0x1d31498` 取的常量就是 Flick 那个 2.1）。

> **勘误。** 本报告早先把这行读成"手指 X 在音符 X 的 ±0.1 内"，把**时间窗**当成了**位置
> 容差**，于是"Drag 的横向容差只有 0.1"这个错结论流传开来（`auto_phigros` 的自检也一直
> 拿 Tap 的 1.9 去量 Drag，比真值严 10%）。照 0.1 去做，Drag 会显得比 Tap 还严 19 倍，
> 把注意力引到错的地方去。**Drag 与 Flick 的 X 容差都是 2.1 世界单位
> （折成虚拟屏 2.1 × 0.9 = 1.89）。**

判定流程因此是"**挂上号 + 到点**"两段：

1. 音符进入 `|Δ| ≤ 0.1` 之后，只要有**任何一根**手指的 `fingerPositionX` 落在
   `note->positionX ± 2.1` 内，`isJudged` 就置位（`isJudged` 是组件字段，置位后不再复位）；
2. 之后任意一帧只要 `Δ < 0.005`（也就是音符到点或已经过点）就结算 Perfect。

所以 **Drag 只有 Perfect 或 Miss**，成功的条件是"判定窗口里至少有一帧手指在位"，
而不是"某一生效瞬间手指在位"。这一点在工程上有直接后果：手指在窗口里只停 1 毫秒的话，
能不能撞上游戏的一帧纯看帧相位 —— 见 `auto_phigros/impl.md` 的
「手指得在位够一帧」。

#### Flick — `FlickControl::Judge` @ `0x1d319e4`

```c
v5 = note->realTime - progressControl->nowTime;
v6 = note->isJudgedForFlick || this->isJudged;      // 由 CheckFlick 置位
this->isJudged = v6;
if (v5 < 0.005 && v6) {
    SEPlayer.PlayHitFx(9);
    transform.localPosition = (note->positionX, 0, 0);
    ScoreControl::Perfect(noteCode, -v5, position, false);
    ProgressControl::CheckSpecifiedNoteHit(note->noteCode);
    return true;
}
...
if (v5 >= -JudgeControl.BadTimeRange) return false;
ScoreControl::Miss(note->noteCode);
```

> **Flick 也只有 Perfect 或 Miss。** 候选窗口已在 `CheckFlick` 收窄到 ±0.14 s。

#### Hold — `HoldControl::Judge` @ `0x1d32668`

```c
if (!judged) {
    if (missed) ...;
    v8 = note->realTime - nowTime;
    isJudged = note->isJudged || this->isJudged;
    // 头部判定：与 Tap 的窗口一致
    //   |v8| < PerfectTimeRange  → judged = true; isPerfect = true
    //   |v8| < GoodTimeRange     → judged = true; isPerfect = false
    this->_judgeTime = v8;
}
if (v8 >= -BadTimeRange) ...                  // -0.22
    missed = true; ScoreControl::Miss(...)
...
// 中途松手（丢帧/滑手保护）：只要有一根手指还在这条线上就算按着
if (!missed && !judgeOver) {
    missed = true;
    for (i = 0; i < judgeLine->numOfFingers; i++) {
        if (fabsf(judgeLine->fingerPositionX[i] - note->positionX) < 1.9) {
            missed = false;
            this->_safeFrame = 2;                       // 手指在位，宽限重置为 2 帧
        }
    }
    if (一根都不符合) {
        if (--_safeFrame < 0) { judgeOver = true; ScoreControl::Miss(note->noteCode); }
        else                    missed = false;         // 连续 3 帧落空都忍得下来，第 4 帧才判
    }
}
// 完整按住到尾部
if (nowTime > (note->realTime + note->holdTime - 0.22) && judged && !judgeOver) {
    if (isPerfect) ScoreControl::Perfect(noteCode, ..., /*isHold=*/true);
    else           ScoreControl::Good   (noteCode, ..., /*isHold=*/true);
    judgeOver = true;
}
// 兜底超时
if (nowTime > (note->realTime + note->holdTime + 0.25)) {
    if (judged || missed || judgeOver) return true;
    ScoreControl::Miss(noteCode); return true;
}
```

要点：

- Hold 的**头判**用与 Tap 相同的窗口决定 `isPerfect`，但**不立即结算**（`isHold = true` 时 `ScoreControl::Perfect/Good` 不生成判定特效）。
- 整条按住到 `realTime + holdTime − 0.22` 才结算：头判是 Perfect 就 Perfect，否则 Good。
- `_safeFrame = 2`：中途落空能连忍 3 帧，第 4 帧才判 Miss（掉帧/滑手保护）。容忍量是**帧**
  不是毫秒 —— 60fps 下约 67ms，120fps 下只有 33ms。这一条是"判定线快速闪烁"类谱面能打
  过去的关键：`fingerPositionX` 跟着线平移（§6.1），线一跳，站着不动的手指就落空一个相位。
- 主体期间的横向判据是 `|fingerPositionX[i] − note.positionX| < 1.9`，与 Tap 同一个 1.9，
  但对象是**谱面里那个静态 `positionX`**（`+0x1c`），不是"判定线当前时刻的世界位置"。

### 6.7 判定结果一览（普通模式）

| 类型 | Perfect | Good | Bad | Miss | X 容差 | 说明 |
|---|---|---|---|---|---|---|
| Tap | \|Δ\|<0.08 | \|Δ\|<0.18 | \|Δ\|≥0.18 | Δ<−0.22 未触碰 | 1.9 | 唯一有 Bad 的类型 |
| Drag | 挂上号且 Δ<0.005 | — | — | Δ<−0.1 未挂上号 | 2.1 | 窗口 ±0.1s，窗口内任一帧手指在位即可 |
| Flick | 到达且 `isJudgedForFlick` | — | — | Δ<−0.22 | 2.1 | 候选窗 ±0.14，需 `isNewFlick` |
| Hold | 头判 Perfect 且按满 | 头判 Good 且按满 | — | 早松手 / 超时 | 1.9 | `_safeFrame = 2` 帧宽限 |

> `Δ = note.realTime − nowTime`，`Δ > 0` 表示音符还没到（早），`Δ < 0` 表示已经过了（晚）。
> 传给 `ScoreControl` 的第二参数是 `−Δ`，符号即"早/晚"。

### 6.8 暂停区域

`JudgeControl::CheckPause` @ `0x1d20a38`：手指落在右上角（世界坐标 `x ∈ [−8.8, −8.0]`、`y ∈ [4.05, 4.85]`，窄屏另有换算）触发暂停，`pauseTime = 1.2`；长按则继续播放。

---

## 7. 计分

`ScoreControl`（size 0xd0）关键字段：

```
+0x40 float  _score          总分
+0x44 float  _percent        完成度
+0x48 float  scoreOfNote     本次计算出的分数
+0x4c int    _combo
+0x50 bool   isAllPerfect
+0x51 bool   isFullCombo
+0x54 int    maxcombo
+0x58 int    perfect    +0x5c good    +0x60 bad    +0x64 miss
+0x68 int    early      +0x6c late
+0x88 List<float> _noteCodes
+0x98..0xb0  Action _OnMiss / _OnBad / _OnGood / _OnPerfect
```

### 7.1 计数器维护

| 函数 | 地址 | 行为 |
|---|---|---|
| `ScoreControl::Perfect` | `0x1d30a84` | `++perfect; ++_combo;` 若 `!isHold` 则实例化 `perfectJudge` 特效；`_noteCodes.Add(noteCode)` |
| `ScoreControl::Good` | `0x1d30c3c` | `isAllPerfect = false; ++_combo; ++good;` 若 `judgeTime <= 0` 则 `++early` 否则 `++late`；`!isHold` 时实例化 `goodJudge` |
| `ScoreControl::Bad` | `0x1d30e20` | `_combo = 0; ++bad;`（`isAllPerfect = false`） |
| `ScoreControl::Miss` | `0x1d30ff0` | `_combo = 0; ++miss;` |

`judgeTime` 就是传入的 `−Δ`：`judgeTime <= 0` ⇔ `nowTime <= realTime` ⇔ **早**。

### 7.2 分数公式（`ScoreControl::Update` @ `0x1d3601c`）

```c
numOfNotes = (float)levelInformation->numOfNotes;
v5 = ((good * 0.65f) + perfect) / numOfNotes;          // 准确率

if (JudgeControl.InChallengeMode) {
    v8 = v5 * 1000000.0f;
    scoreOfNote = v8;
    if (_combo > maxcombo) maxcombo = _combo;
    _score   = v8;
    _percent = v8 / 10000.0f;                          // 0..100
} else {
    v11 = v5 * 900000.0f;
    scoreOfNote = v11;
    if (_combo > maxcombo) { maxcombo = _combo; }
    _score   = v11 + (float)(maxcombo * 100000.0f) / numOfNotes;
    _percent = (v11 / 900000.0f) * 100.0f;
}
```

整理成公式（`N = numOfNotes`）：

**普通模式**

```
准确率  acc = (perfect + 0.65 × good) / N
分数    score = 900000 × acc + 100000 × maxCombo / N
完成度  percent = acc × 100
```

**课题模式**

```
分数    score   = 1000000 × acc
完成度  percent = score / 10000        （等价于 acc × 100）
```

> 即经典的 Phigros 结构：**90 万来自准确率，10 万来自最大连击**。
> `bad` 与 `miss` 不进分子（只通过 `_combo = 0` 拉低 `maxCombo` 来扣分）。

`ScoreControl::Update` 第 132 行还有 `v17 = _score + 0.5;`，即四舍五入成整数分写入 `LevelResultInfo._IntScore`。

### 7.3 Combo 显示

同一函数第 108–114 行：`combo >= 3` 才把 `comboText` 设为 `"COMBO"`（配合 `comboLabelProvider` / `comboValueProvider` 两个可覆盖的委托，供 `ScoreControl::SetComboDisplayOverride` 使用）。

### 7.4 结算

`ScoreControl::GetLevelResultInfo` @ **`0x1d3505c`** 填充 `LevelResultInfo`（size 0x40）：

| 偏移 | 字段 |
|---|---|
| 0x10 | `_IntScore_k__BackingField` |
| 0x14 | `score` |
| 0x18 | `percent` |
| 0x1c–0x28 | `perfect` / `good` / `bad` / `miss` |
| 0x2c / 0x30 | `early` / `late` |
| 0x34 | `maxCombo` |
| 0x38 | `ghostNoteJudged`（`Dictionary<int,bool>`） |

由 `Assets.Phigros2.Utils.Tools.DifficultyToIndex(songsLevel)` 取难度下标，从 `ProgressControl.ghostNoteJudged[difficultyIndex]` 取该难度的命中表。

派生判定（都是极简 getter）：

```c
bool get_FullCombo()        { return bad + miss < 1; }
bool get_AllPerfect()       { return bad + miss + good < 1; }
int  get_FailedNoteCount()  { return bad + miss; }
bool get_Passed()           { return _IntScore_k__BackingField > 699999; }   // 即 ≥ 700000
```

> **注意 `FullCombo` 的定义是"没有 Bad 也没有 Miss"，而 `AllPerfect` 是"连 Good 都没有"** —— Phigros 里 Bad 会断连，所以 FC 允许 Good。

---

## 8. 其它已确认细节

- **noteCode 与回放**：`ProgressControl::CheckSpecifiedNoteHit(float noteCode)` @ `0x1d30f14`
  ```c
  if (ghostNoteJudged[difficultyIndex].ContainsKey((int)noteCode))
      ghostNoteJudged[difficultyIndex][(int)noteCode] = true;
  ```
  记录本局哪些音符被命中，供 `VisionReplay`（回放）与课题模式做幻影音符对齐。`ScoreControl::SetNoteCodeList` @ `0x1d3517c` 把 `_noteCodes` 推给 `GameInformation.noteCodes`。

- **镜像模式**：`Chart::Mirror` `0x1d286ac`，`JudgeLine::Mirror` `0x1d28afc`，`GameInformation.BlockArea::Mirror` `0x1ca32a8`。

- **和弦支持**：`JudgeLineControl::Start` `0x1d2407c` → `SaveManagement::LoadBool("chordSupport", true)`，默认开。

- **宽高比**：
  ```c
  moveScale = (Screen.height / Screen.width <= 0.5625) ? 1.0
            : (Screen.width * 0.5625) / Screen.height;
  ```
  且 `SetInformation` 中 `noteScale *= (ratio / 1.7778)`（`ratio = screenW / screenH`）。

- **血量**：`HPProvider::OnJudge(float)` @ `0x1d1a238`，订阅 ScoreControl 的判定事件；`HPProvider::MapToDisplayHP` @ `0x1d1a4e4`。

- **关卡 Mod 钩子**：`DoppelgangerLevelEffect::TryStripHdUnlockNote`、`LuminescenceLevelEffect`、`TheChariotEffect`、`RetributionEffect(SecondPhase)::OnJudge`、`SecretChallengeLifeMod::OnNonPerfect` 等通过 `ScoreControl` 的 `Action` 事件介入。

---

## 9. 尚未确认 / 存疑

1. **`JudgeControl.minDeltaTime`（+0xac）与 `touchPos`（+0xb8）的初值来源。**
   `touchPos` 在 `CheckNote` 里每帧被写入，但 `minDeltaTime` 找不到任何赋值点。推测二者是 Unity 预制体上的**序列化（Inspector）字段**，值在 AssetBundle 里，`.so` 中不可见。
2. **`JudgeLineControl.lastProductIndexAbove/Below` 的初值。**
   按 `for (i = last + 1; i <= now; ++i)` 的写法，若初值为 0 则下标 0 的音符永远不会被创建。合理推测预制体里序列化为 `-1`，但未经证实。
3. **`GetFingerPosition` 的坐标换算细节**（是否用到 `theta` 做旋转逆变换、是否有额外的 DPI/宽高比补偿）未逐行核对。
4. **Hold 的 `noteImages` / `holdHead` / `holdEnd` 三段精灵的长度计算**只确认到 `(holdTime / 3.8) × 0.2` 这一处参与 Y 缩放，完整几何未展开。
5. `JudgeControl::CheckBlocks`（第九章 ARG 阻挡方块）对判定的具体影响未展开。

---

## 10. 关键函数地址速查

| 功能 | 函数 | 地址 |
|---|---|---|
| 拼谱面路径 | `SongsItem::GetLevelStartInfo` | `0x1c9bd80` |
| **谱面反序列化** | `LevelControl::_Start_d__46::MoveNext` | `0x1d27748` |
| 关卡后处理 | `LevelControl::SetInformation` | `0x1d2563c` |
| noteCode 分配 | `LevelControl::SetCodeForNote` | `0x1d2516c` |
| 按 floorPosition 排序 | `LevelControl::SortForNoteWithFloorPosition` | `0x1d25350` |
| 全局时间排序 | `LevelControl::SortForAllNoteWithTime` | `0x1d26ee8` |
| 拍→秒换算 | `LevelControl::GetRealTime` | `0x1d26ed4` |
| 谱面镜像 | `Chart::Mirror` | `0x1d286ac` |
| 镜像开关（属性） | `LevelStartInfo::get_mirror` / `set_mirror` | `0x1ca407c` / `0x1ca4084` |
| 镜像开关（UI） | `SongSelector::ToggleChartMirror` | `0x1d571a0` |
| 谱面回读成 JSON | `UnityEngine.JsonUtility::ToJson` | `0x3a500e4` |
| 音符总数 | `Chart::GetNoteCount` | `0x1d28918` |
| 音乐与计时开跑 | `ProgressControl::Play` | `0x1d34270` |
| **判定用时钟** | `ProgressControl::Update`（算 `nowTime`） | `0x1d3483c` |
| 音符屏幕缩放基准 | `JudgeLineControl::Start`（算 `moveScale`） | `0x1d2407c` |
| 每帧总驱动 | `NoteUpdateManager::Update` | `0x1d32ce8` |
| 轨道更新 | `JudgeLineControl::UpdateInfo` | `0x1d24214` |
| 音符实例化 | `JudgeLineControl::CreateNote` | `0x1d22afc` |
| 生产索引 | `JudgeLineControl::UpdateProductionIndex` | `0x1d22a98` |
| 事件索引推进 | `JudgeLineControl::UpdateJudgeLineEventIndex` | `0x1d22834` |
| **音符位置** | `ClickControl::NoteMove` | `0x1d30358` |
| **Tap 判定** | `ClickControl::Judge` | `0x1d3060c` |
| **Drag 判定** | `DragControl::Judge` | `0x1d313f0` |
| **Flick 判定** | `FlickControl::Judge` | `0x1d319e4` |
| **Hold 判定** | `HoldControl::Judge` | `0x1d32668` |
| 触摸扫描 | `JudgeControl::CheckNote` | `0x1d21104` |
| 滑动扫描 | `JudgeControl::CheckFlick` | `0x1d21828` |
| 手指坐标分配 | `JudgeControl::GetFingerPosition` | `0x1d20d10` |
| 判定窗口初始化 | `JudgeControl::cctor` | `0x1d227d8` |
| 判定窗口每帧重写 | `ProgressControl::Update` | `0x1d3483c` |
| 音频时钟 | `ProgressControl::Update` | `0x1d3483c` |
| 计分 | `ScoreControl::Perfect/Good/Bad/Miss` | `0x1d30a84` / `0x1d30c3c` / `0x1d30e20` / `0x1d30ff0` |
| 分数与完成度 | `ScoreControl::Update` | `0x1d3601c` |
| 结算 | `ScoreControl::GetLevelResultInfo` | `0x1d3505c` |
| 幻影音符记录 | `ProgressControl::CheckSpecifiedNoteHit` | `0x1d30f14` |

---

## 附：本次分析产出的伪代码

全部反编译产物已保存在 `C:\UserData\phigros\_re\`（约 30 个 `.c` 文件），文件名即 `<类名>_<方法名>.c`，可直接对照本报告的地址与行号。
