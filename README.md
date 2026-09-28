# auto_phigros

自动打 Phigros。整条链已经打通：**闸住开谱 → 采集谱面 → 规划（带缓存）→ 放行 →
跟着游戏的时钟把触控发到设备上**。架构上 **frida 是主干**：`main.py` 负责注入、采集、
开谱闸门与打歌现场；规划（`planner.py`）、触控（`touch.py`）、可视化（`render.py`）
都是被它调用的模块，每个都能被主干调用、**也都能单独跑**。

| 想知道什么 | 看哪儿 |
|---|---|
| 怎么装、怎么用、控制台有哪些命令 | 就是这份 README |
| 为什么这么设计、判定怎么算、踩过哪些坑 | [impl.md](docs/impl.md) |
| 游戏内部：函数地址、字段偏移、伪代码 | [Phigros4.0-音游内核逆向报告.md](docs/Phigros4.0-音游内核逆向报告.md) |

## 装一遍

### 1. Python 环境（conda）

```sh
conda create -n auto_phigros python=3.14 -y
conda activate auto_phigros
pip install -r requirements.txt
```

[requirements.txt](requirements.txt) 里就六行，但 **frida 的版本是有讲究的**：
客户端与设备上的 frida-server 必须同一个版本，且**都不能是 17.19.0**
（那一版 attach 任何进程都会抛 `TransportError: agent connection closed unexpectedly`，
`spawn` 却是好的 —— 详见下面的"环境要求"）。

### 2. 设备侧

* **frida-server**：与客户端同版本（`17.10.1` 或 `17.17.0` 都实测可用），
  push 到设备、以 root 跑起来。Phigros 是 arm64。
* **adb**：scrcpy 后端要用它。路径是 `src/backends/scrcpy.py` 顶部的 `ADB` 常量
  （默认 `C:\UserData\platform-tools\adb.exe`）—— 不在这个位置就改那一行。
* **scrcpy server**：从 [scrcpy](https://github.com/Genymobile/scrcpy) v4.1 的 release
  里取 `scrcpy-server`，放到 `src/backends/scrcpy-server-v4.1`。
  这个文件故意不进 git（见 `.gitignore`），得自己捞一份。

### 3. agent（Node）

```sh
npm install
npm run build          # esbuild frida/index.ts --bundle --outfile=./target/_.js
```

没构建过 `target/_.js` 就 `spawn` 会直接告诉你"先构建"。

## 入口

```sh
conda activate auto_phigros

# 主干：没有命令行参数 —— 一切都在控制台里（它就是主干，跑在主线程上）
python src/main.py
#   devices                列设备；只有一台时自动选中
#   device <id>            选一台（记住）
#   spawn / attach [pid]   注入（游戏关掉了用 spawn，还开着用 attach）
#   其余设置见下面的"控制台"

# 规划模块：吃一张采集下来的谱面、吐一份规划结果（落盘的那份同时就是缓存）
python src/planner.py charts/Glaciaxion.SunsetRay.0_HD_93215ea2.npz
python src/planner.py Chart.npz --options              # 这个规划器能调什么
python src/planner.py Chart.npz --planner radical --set flick_repeats=1
python src/planner.py Chart.npz -o /tmp/out --no-cache  # 既不吃也不写缓存

# 触控模块：把一份规划结果发到设备上（自己跑 = 不接游戏，按本地时钟打）
python src/touch.py plans/Glaciaxion.SunsetRay.0_HD_93215ea2_conservative.npz
python src/touch.py plans/x.npz --mirror --latency 0.02
python src/touch.py plans/x.npz --backend recording   # 不连设备，只跑调度器看迟到量

# 可视化：把规划结果渲染成能叠到录屏上的视频
python src/render.py plans/Glaciaxion.SunsetRay.0_HD_93215ea2_conservative.npz
python src/render.py plans/x.npz --size 1280x720 --fps 30 --background chroma
python src/render.py plans/x.npz --no-paths --point-color "#00ff88" --point-radius 12

# 看 npz 里到底有什么（谱面原文 / 规划结果 / meta 都是它）
python src/npz.py charts/Glaciaxion.SunsetRay.0_HD_93215ea2.npz
python src/npz.py plans/x.npz --member meta
python src/npz.py charts/x.npz --member chart -o chart.json   # 把谱面原文取出来

# 算法体检：按**游戏真实的判定规则**把规划重放一遍，报丢音、蹭键、判定分布与分数
# （和 selftest 分工：那边抓代码 bug，这边揪算法问题；慢，而且谱面越多越慢）
python src/judge.py
python src/judge.py --planner geometric
python src/judge.py --chart Dlyrotz
python src/judge.py --plan plans/x.npz -v
python src/judge.py --planner geometric --set flick_repeats=1   # 拿一组参数体检
python src/judge.py --json out.json
```

只有 `main.py` 没有参数，因为它是"一台常驻的面板"：选设备、开关设置都该在**跑起来之后**
做，而且该被记住。**记住的去处是 `config.json`**（第一次跑自动按默认值建一份，删了就能
回到初始状态）—— planner / latency / auto-latency / backend / cache / save-chart / device / host
都在里面。

**落盘的规划结果就是缓存**：一张谱面 + 一个规划器对应一个 `.npz`。命中与否看它 meta 里的
``cache_key``（算法源码 + 规划器名 + **这一套参数**），所以换了参数一定重算；参数不进文件名，
一个规划器就一个文件。`cache off` 表示既不吃也不写。

谱面与规划结果都是 **npz**：原文（或事件流）与那段 `meta`（来源、统计、算法指纹）**装在
同一个文件里**，没有陪嫁的 `.meta.json`。想用眼睛看，就 `python src/npz.py <那个文件>`。

## 第一次上设备：按这个顺序

真的把触摸送进设备之前，先按这个顺序把能离线验的都过一遍 —— 每一步都能独立看到结果，
出问题也好定位到底是哪一段：

1. `cd src && python -m backends.scrcpy` —— 只查 adb、server 文件和屏幕尺寸（包内相对
   import，得在 `src/` 里用 `-m` 跑）；
2. `python src/touch.py <某个短谱的 .npz> --backend recording` —— 调度器；
3. `python src/main.py`，然后在控制台里 `backend recording` + `spawn` —— **整条链**（闸门、
   缓存、对表、排事件）都不碰设备地跑一遍，对着日志看 `[gate]`、`[plan]`、
   `[touch] 打完：发了 N 个事件，最大迟到 X ms` 合不合意；
4. 开一次谱但**先别注入**（控制台里 `inject off`）—— 音符一个都不会被按到，判定流水于是
   会把整张谱**逐个报成 Miss / Bad**：这正好把音符表从头到尾走一遍，不用碰设备就能验两件事
   —— `[notes] 音符表就绪：N 个音符` 的 N 等于这张谱的音符数，以及 `[judge]` 那几行里
   **没有一句**"（音符表里没有它）"。对得上再 `inject on`；
5. `python src/touch.py <同一份 .npz>` —— 真发。先用一首短谱看能不能点到，再调 `latency`；
6. `spawn`（或 `attach`）—— 全自动。第一次要 `devices` 看列表、`device <id>` 选一台
   （USB 下那个 id 就是 adb 的序列号），之后就记住了。

## 控制台

主干跑起来之后，那个终端就是控制台 —— **主线程就在读它**（所以 Ctrl+C 天然是"收工"）。
一边打歌一边改设置，不用重启；会"记住"的那些当场落盘进 `config.json`：

```
auto> help
命令：
  devices                  列出设备（USB、本机、远程 server）
  device <id>              选中一台设备
  spawn                    在选中的设备上启动游戏并注入
  attach [pid]             附加到已经在跑的游戏（pid 可选）
  detach                   断开注入（设备与后端留着，可再 attach）
  status                   现在什么情况：设备、agent、后端、时钟、这一局
  planner [名字]            看可用规划器 / 换一个（下一关生效）
  option [名字 值]          看 / 改当前规划器的参数（下一关生效）
  latency [值]             看 / 改注入补偿：0.02、20ms、+5ms（手动给数会关掉自校准）
  latency auto on|off      每局完整打完，按这一局 Perfect 的中位数自动校准补偿
  backend [名字]            看 / 换触控后端（换了当场重开）
  cache on|off             把 plans/ 里的规划当缓存用
  save-chart on|off        顺便把谱面原文存到 charts/
  log on|off               把输出抄一份到 logs/<时间>.log
  host add <地址>            加 / 看远程 frida-server
  inject on|off            是否真的把触控发给设备（仅本次会话）
  verbose on|off           是否把每一个 Perfect 也打出来（仅本次会话）
  help                     列出这些命令
  quit                     收工：断开注入并退出（等同 Ctrl+C）
```

改了就是立刻生效：`latency` 下一个事件起、`planner` / `option` 下一关起、`inject` / `verbose`
当场起、`backend` 当场重开。除了 `inject` / `verbose`，其余都落盘进 `config.json`。

**规划器的参数是配置项**：`option` 不带参数列出当前规划器能调什么（名字、默认值、说明），
`option flick_repeats 3` 改一个、`option flick_repeats reset` 还原。同一份东西也在
`config.json` 的 `planner_options` 里（`{"radical": {"flick_repeats": 3}}`），可以照着改。
换参数会让那份规划结果的缓存失效并重算 —— 参数算进了 cache_key，但**不进文件名**。

**`inject` 与 `verbose` 故意不落盘**：一个持久化的 `inject off` 会让人下一次以为在打歌、
其实一根手指都没发出去。

**延迟会自己校准**（默认开）：每局**完整打完**之后，拿这一局 Perfect 的早晚量**中位数**
加进 `latency` 并落盘 —— 我们发出触摸、游戏在下一帧才处理，这 20~30ms 的差该由补偿吃掉。
只收 Perfect 是为了躲开被蹭掉、卡顿补判那些离群值；单次最多挪 100ms，超过就跳过并说明
理由（那多半不是送达延迟）。`latency <值>` 手动给数会**自动关掉**它。

**日志默认开着**：一次运行一份 `logs/<时间>.log`，路径在启动第一行就念出来（`[main] 日志：…`），
`status` 里也看得到。抄的是**到达屏幕的一切**（包括 `scrcpy.py` 那种直接 `print` 的），
只按"行的语义"落盘 —— 一行里只有最后一个 `\r` 之后的内容算数，所以控制台擦提示符的那些
回车不会跑进文件。事后要比"这次和上次差在哪"，就在 `logs/` 里按文件名排。

"agent 还活着吗"是怎么判断的（`ok` / `dead` / `hang`），写在
[impl.md](docs/impl.md#运行时控制台与存活探测)；`quit` 为何与 Ctrl+C 等价、它到底拆了些什么，
见那节的[收工小节](docs/impl.md#收工quit-与-ctrlc-必须是同一件事)。

## 可视化

```sh
python src/render.py plans/Glaciaxion.SunsetRay.0_HD_93215ea2_conservative.npz
```

把规划结果里的手指运动画成视频：手指是点，运动轨迹是线。默认输出**带 alpha 的 `.mov`**，
因为这东西的用途是叠到游戏录屏上做对照，能直接拖进 Premiere / AE。

| 参数 | 说明 |
|---|---|
| `plan` | 输入的 `.npz` |
| `-o, --output` | 输出文件（默认 `renders/<名字>.mov`） |
| `--size` | 分辨率，如 `1920x1080`（默认）；宽高会向上取到 8 的倍数 |
| `--fps` | 帧率，默认 60 |
| `--background` | 底色。默认 `none` = 透明；绿幕用 `chroma`（`#00b140`，广播标准绿） |
| `--no-points` / `--point-radius` / `--point-color` | 点的开关 / 半径（像素，默认 8）/ 颜色（默认 `#ff3030`） |
| `--no-paths` / `--path-width` / `--path-color` | 轨迹的开关 / 粗细（像素，默认 3）/ 颜色（默认 `#ff3030`） |
| `--path-window MS` | 轨迹最多回溯多久，默认 400；`0` 表示从按下画到当下 |

颜色写 `#rrggbb`、`rrggbb`、`#rgb`、`r,g,b` 或颜色名（`red` / `green` / `chroma` / `blue` …）。

## 自检与体检

```sh
python src/selftest.py     # 代码有没有 bug —— 5 秒，与 charts/ 里几张谱无关
python src/judge.py        # 算法有没有问题 —— 逐张谱 × 每个规划器，慢
```

分工是刻意的：`selftest.py` 只在"代码本身可能错"的地方报警（闸门放不放行、时钟会不会
偏早、播放器排期、控制台回显、收工、附加目标的解析…），**不逐张谱做规划**（唯一用到谱面
的是缓存自检，只挑最小那张）——谱面一多它就会慢到没人愿意跑，而"算法漏了一个音符"本来
就该由另一条线来报。

`judge.py` 是那条线：把规划按游戏真实的判定规则（窗口、容差、扫描窗、hold 结算提前量，
全部出自 [impl.md](docs/impl.md#自检) 里那份逆向报告）重放一遍，另外还跑原有的那五项判据
（**事件流自洽**、**覆盖完整**、**存得回去**、**镜像也对**、**滑键起手**），并报

* **丢音** —— 哪些音符一次判定都没碰到；
* **蹭键** —— 一次不是为某个音符按下的动作把它判掉了，并区分"计划本来按对了却被先碰掉
  （纯丢分）"与"计划本来也没按到"；
* **判定分布与分数** —— `900000 × acc + 100000 × maxCombo / N`，拿实机那局对过账（1156
  音符 / 1152 Perfect / 2 Good / 连击 613 → 950926 分，与游戏报的一分不差）。

`selftest.py` 里另有十八组"整条链上下游"的检查：闸门、时钟、坐标换算、播放器、控制台、
附加目标、存活探测、收工、结算、延迟自校准、判定对账、日志对账、裁判、规划器、在位时长、配置、日志、缓存。
每组的判据、以及它们各自做过哪些变异测试，见 [impl.md](docs/impl.md#自检) 的「自检」一节
与它的附录「黑历史」；`charts/` 空着时缓存自检会跳过。

## 输出

```
charts/
  Glaciaxion.SunsetRay.0_HD_93215ea2.npz        # chart = FromJson 抓到的原文；meta = 来源上下文 + 长度 + 哈希 + 音符数比对
plans/
  Glaciaxion.SunsetRay.0_HD_93215ea2_conservative.npz   # 事件流 + meta（规划参数、统计、告警、cache_key）
renders/
  Glaciaxion.SunsetRay.0_HD_93215ea2_conservative.mov   # 可视化视频（带 alpha）
```

谱面只有一份，文件名里的哈希就是它的**内容**哈希；`<名字>.npz` 与 `<名字>_<规划器>.npz`
前缀永远一致（规划结果自己的 meta 里也记着它对应哪张谱面）。镜像与延迟**不落盘**
（它们是运行时的事），所以同一份规划结果对所有局面通用。

> `<名字>` = **歌曲 + 难度 + 内容哈希**，**不含 `seq`**。`seq` 是 agent 的会话内计数器
> （这一局进程里解析的第几张谱面），换个会话同一张谱面就会拿到另一个号。曾经把它拼进
> 文件名，于是同一张 Glaciaxion HD 攒出过 0001/0002/0003 三份、而第二次开谱照样得在
> 闸门里现算几十秒 —— 缓存永远落空。`seq` 仍然写进 meta、仍然出现在日志的
> `[gate #0002]` 里，只是不参与身份。

谱面 meta 里 `notes_in_json`（主机从原文数出来的）与 `notes_reported`（游戏自己
`Chart::GetNoteCount` 数出来的）应当一致，`notes_match` 给出结论 —— 这是"抓到的就是
游戏真正用的那一份"的证据。规划结果 meta 里的 `cache_key` 则是"这份结果是哪一版算法算出来的"。

## 环境要求

**frida 客户端与设备上的 frida-server 必须同一个版本，且都不能是 `17.19.0`。**

| frida 版本 | 结果 |
|---|---|
| 17.19.0 | `attach` 任何进程都抛 `TransportError: agent connection closed unexpectedly` |
| 17.10.1 / 17.17.0 | 正常（`main.py` 的 `TESTED_FRIDA` 就是这两个；`requirements.txt` 钉的是 17.17.0） |

踩坑记录：17.19.0 的表现很有迷惑性 —— `spawn` 本身成功，进程 `resume` 后也能正常跑，
只有注入环节挂掉。判别方法是 **attach 一个无关进程**：

```python
dev.attach("com.android.systemui")   # 同样失败 => 与 Phigros 无关，是 frida 层的问题
```

已排除的可能：frida-server 版本号/架构与设备不匹配（17.19.0 / arm64 / ABI arm64-v8a 都对）、
SELinux 拦截（`dmesg` 里 `u:r:ksu:s0` 域零拒绝）、Frida Launcher 的问题（手动 root 启动同样复现）。

测试设备：OnePlus CPH2491，Android 15（API 35），arm64，KernelSU。

### `attach` 说找不到进程，可游戏明明开着

**进程名不等于包名。** 实测（2026-09-27，OPPO/MTK 那台）：

```
enumerate_processes()      → (30501, 'Phigros')               ← 进程名是应用标签
enumerate_applications()   → ('com.PigeonGames.Phigros', 30501)  ← 包名在这里
device.attach('com.PigeonGames.Phigros')  → ProcessNotFoundError: unable to find process ...
```

所以 `attach` 的解析顺序是：**先问应用列表（包管理器给的 identifier → pid），再问进程列表
（精确名 → `包名:子进程` 前缀），最后才交给 frida 按名字找**。两条路都走不通时，它会把
frida 当前看得见的应用与"名字像它的"进程列出来，并提示直接给 pid：

```
auto> attach 30501
```

要自己看一眼现场，跑：

```sh
python -c "import frida; d=frida.get_usb_device(); print([(p.pid,p.name) for p in d.enumerate_processes()]); print([(a.identifier,a.pid) for a in d.enumerate_applications() if a.pid])"
```

## 目录一览

```
auto_phigros/
  src/                  **源码根**：所有 Python 都在这一层（PyCharm 里标成 Sources Root）
    main.py             薄入口：读/建 config.json → 起主干（控制台）→ 收工
    planner.py          规划模块（落盘即缓存），可被调用、也可单独运行
    touch.py            触控模块：时钟、调度器、命令行，可单独运行
    render.py           可视化：把规划结果渲染成视频
    judge.py            算法体检：按游戏真实判定重放规划（丢音 / 蹭键 / 分布 / 分数）
    npz.py              查看工具：把谱面 / 规划结果的 npz 读成 JSON
    selftest.py         薄入口：代码自检（真正的东西在 tests/ 里）
    algorithms/         规划算法与契约（registry 按名字现 import）；judging.py 是判定规则唯一出处
    backends/           触控后端（scrcpy / recording）
    formats/            npz 的读写：谱面与规划结果的布局都在 storage.py
    runtime/            主干那一套
      console.py        主干：命令表 + 读输入（跑在主线程上，Ctrl+C 落在这里）
      controller.py     这一把的全部家当：设备、后端、时钟、注入、播放器、收工
      agent.py          一次注入的全都：会话、闸门、消息分发、判决对账、结算
      config.py         项目内固定常量 + 落盘的 config.json（默认值在代码里）
      options.py        运行期旋钮（控制台改的就是它）
      output.py         进程里唯一的写者（整行原子 + 补回提示符 + 抄一份到 logs/）
    tools/
      device_log.py     把一次运行的日志读回来（设备实际判了什么，`judge --compare` 用它）
    tests/              自检包：判据、替身与入口（详见 tests/__init__.py）
  frida/                agent 源码（esbuild 打包成 target/_.js）
  docs/                 逆向报告与实现笔记
  config.json           落盘的配置（第一次跑自动建；不进版本库）
  charts/ plans/ renders/ logs/   采集到的谱面 / 规划结果=缓存 / 渲染出来的视频 / 运行日志
```

`src` 是**唯一的 Python 导入根**，三件事是配套的：

* **跑法**一律 `python src/<模块>.py`（在项目根敲）。脚本自己的目录会进 `sys.path`，而它就是
  导入根，于是 `from algorithms.chart import …`、`import planner` 这些顶层名字直接成立 ——
  不用装包，也不用设 `PYTHONPATH`。PyCharm 里把 `src` 标成 Sources Root 之后，
  Run Configuration 用 Script path = `src/xxx.py`（或 Module name）都能跑；仓库本身不依赖 `-m`。
* **"根"有两个，别混**：**项目根**（`src/` 的上一层）放数据与产物 —— `config.json`、`charts/`、
  `plans/`、`renders/`、`logs/`、`target/_.js`；**源码根**是 `src/`。在 `src/` 顶层的模块往上
  一层（`parents[1]`），`src/runtime/` 里的往上两层（`parents[2]`）；别再拿 `Path(__file__).parent`
  往上去拼 —— 搬进 `src/` 之后它会指到源码树里去。唯一的例外是缓存指纹：它要的就是**源码根**
  （`planner.SOURCE_ROOT`），因为数据目录挪窝不算算法改过。
* **包内的兄弟模块一律相对 import**（`from .config import …`）：包被搬走或改名都不会断；
  `from config import …` 那种写法其实要靠 `sys.path` 才成立 —— 上次按职责细分目录时就断在这里，
  自检直接 `ModuleNotFoundError`，别再写回去。
* **可调的东西写成配置项，不写成常量**：规划器参数进 `config.json` 的 `planner_options`
  （控制台 `option` 改，`planner.py/judge.py --set` 也能临时给），运行期旋钮进 `Options`。
  只有游戏/引擎的事实（判定窗口、时间栅格、地址偏移）才留作常量。

每个模块的职责与设计取舍见 [impl.md](docs/impl.md#结构)。

## 还没做

* 真后端只有 scrcpy 一个。要加别的（比如 frida 侧注入、minitouch），
  在 `src/backends/registry.py` 的 `_BUILTIN` 里加一行、写一个模块即可。
* human-like 算法：草案在 [human-like.md](docs/human-like.md)，一行代码还没写。
* geometric 合并拖拽那条路上还有一处可以更省的线性扫描。
