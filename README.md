# auto_phigros

自动打 Phigros。整条链已经打通：**闸住开谱 → 采集谱面 → 规划（带缓存）→ 放行 →
跟着游戏的时钟把触控发到设备上**。架构上 **frida 是主干**：`main.py` 负责注入、采集、
开谱闸门与打歌现场；规划（`planner.py`）、触控（`touch.py`）、可视化（`render.py`）
都是被它调用的模块，每个都能被主干调用、**也都能单独跑**。

| 想知道什么 | 看哪儿 |
|---|---|
| 怎么装、怎么用、控制台有哪些命令 | 就是这份 README |
| 为什么这么设计、判定怎么算、踩过哪些坑 | [impl.md](impl.md) |
| 游戏内部：函数地址、字段偏移、伪代码 | [Phigros4.0-音游内核逆向报告.md](Phigros4.0-音游内核逆向报告.md) |

## 装一遍

### 1. Python 环境（conda）

```sh
conda create -n auto_phigros python=3.14 -y
conda activate auto_phigros
pip install -r requirements.txt
```

[requirements.txt](requirements.txt) 里就五行，但 **frida 的版本是有讲究的**：
客户端与设备上的 frida-server 必须同一个版本，且**都不能是 17.19.0**
（那一版 attach 任何进程都会抛 `TransportError: agent connection closed unexpectedly`，
`spawn` 却是好的 —— 详见下面的"环境要求"）。

### 2. 设备侧

* **frida-server**：与客户端同版本（`17.10.1` 或 `17.17.0` 都实测可用），
  push 到设备、以 root 跑起来。Phigros 是 arm64。
* **adb**：scrcpy 后端要用它。路径是 `backends/scrcpy.py` 顶部的 `ADB` 常量
  （默认 `C:\UserData\platform-tools\adb.exe`）—— 不在这个位置就改那一行。
* **scrcpy server**：从 [scrcpy](https://github.com/Genymobile/scrcpy) v4.1 的 release
  里取 `scrcpy-server`，放到 `backends/scrcpy-server-v4.1`。
  这个文件故意不进 git（见 `.gitignore`），得自己捞一份。

### 3. agent（Node）

```sh
npm install
npm run build          # esbuild frida/index.ts --bundle --outfile=./target/_.js
```

没构建过 `target/_.js` 就 `python main.py` 会直接告诉你"先构建"。

## 四个入口

```sh
conda activate auto_phigros

# 主干：注入游戏；每次开谱游戏会停在闸门上，规划完自动放行，然后跟着游戏时钟打
python main.py
python main.py --attach                     # 注入到已在运行的游戏
python main.py --planner radical            # 换规划器
python main.py --backend recording          # 换触控后端：不碰设备，整条链照样跑一遍
python main.py --no-cache                   # 不吃也不写规划缓存
python main.py --latency 0.02               # 注入链路的手工补偿（正数=提前发）
python main.py --save-chart                 # 顺便把谱面原文存下来
python main.py -H 192.168.1.10:27042        # 走远程 frida-server
python main.py -D <device-id>               # 指定设备（USB 下就是 adb 序列号）
# 跑起来之后终端就是控制台：help / status / planner / latency / inject / verbose /
# respawn / reattach（详见下面的"控制台"）

# 规划模块：吃一个谱面文件、吐一个规划结果文件（落盘的那份同时就是缓存）
python planner.py charts/0002_Glaciaxion.SunsetRay.0_HD_93215ea2.json
python planner.py Chart.json --no-cache     # 既不吃也不写缓存
python planner.py Chart.json --planner radical -o /tmp/out

# 触控模块：把一个 .psap 发到设备上（自己跑 = 不接游戏，按本地时钟打）
python touch.py plans/0002_..._conservative.psap
python touch.py plans/x.psap --mirror --latency 0.02
python touch.py plans/x.psap --backend recording   # 不连设备，只跑调度器看迟到量

# 可视化：把规划结果渲染成能叠到录屏上的视频
python render.py plans/0002_..._conservative.psap
python render.py plans/x.psap --size 1280x720 --fps 30 --background chroma
python render.py plans/x.psap --no-paths --point-color "#00ff88" --point-radius 12
```

**落盘的规划结果就是缓存**：一张谱面 + 一个规划器对应一个 `.psap`，下次同样的组合
直接读回来（算法源码动过自动失效）。`--no-cache` 表示既不吃也不写。

`planner.py` 单独跑时会先找同目录下的 `<名字>.meta.json`（采集时留下的那份），
从里面恢复序号、来源上下文与内容哈希，所以单独规划出来的结果和主干规划的落在同一个命名下。

## 第一次上设备：按这个顺序

真的把触摸送进设备之前，先按这个顺序把能离线验的都过一遍 —— 每一步都能独立看到结果，
出问题也好定位到底是哪一段：

1. `python -m backends.scrcpy` —— 只查 adb、server 文件和屏幕尺寸；
2. `python touch.py <某个短谱的 .psap> --backend recording` —— 调度器；
3. `python main.py --backend recording` —— **整条链**（闸门、缓存、对表、排事件）都不碰设备地跑一遍，
   对着日志看 `[gate]`、`[plan]`、`[touch] 打完：发了 N 个事件，最大迟到 X ms` 合不合意；
4. 开一次谱但**先别注入**（控制台里 `inject off`，或直接 `--backend recording`）——
   音符一个都不会被按到，判定流水于是会把整张谱**逐个报成 Miss / Bad**：这正好把音符表
   从头到尾走一遍，不用碰设备就能验两件事 —— `[notes] 音符表就绪：N 个音符` 的 N 等于
   这张谱的音符数，以及 `[judge]` 那几行里**没有一句**"（音符表里没有它）"。
   对得上再开 `inject on`；
5. `python touch.py <同一个 .psap>` —— 真发。先用一首短谱看能不能点到，再调 `--latency`；
6. `python main.py` —— 全自动。这时 `-D` 要填 adb 认的序列号（USB 下与 frida 的设备 id 是同一个）。

## 控制台

主干跑起来之后，那个终端就是控制台 —— 一边打歌一边改设置，不用重启：

```
auto> help
命令：
  status               现在什么情况：agent、后端、时钟、这一局
  planner [名字]       看可用规划器 / 换一个（下一关生效）
  latency [值]         看 / 改注入补偿：0.02、20ms、+5ms（立即生效）
  inject on|off        是否真的把触控发给设备
  verbose on|off       是否把每一个 Perfect 也打出来
  respawn              重新启动游戏并注入（游戏被关掉后用）
  reattach             注入到已经在跑的游戏
  help                 列出这些命令
  quit                 结束（等同 Ctrl+C）
```

改了就是立刻生效：`latency` 下一个事件起、`planner` 下一关起、`inject` / `verbose` 当场起。
`respawn` / `reattach` 怎么用、以及"agent 还活着吗"是怎么判断的（`ok` / `dead` / `hang`），
写在 [impl.md](impl.md#运行时控制台与存活探测)。

## 可视化

```sh
python render.py plans/0002_..._conservative.psap
```

把 `.psap` 里的手指运动画成视频：手指是点，运动轨迹是线。默认输出**带 alpha 的 `.mov`**，
因为这东西的用途是叠到游戏录屏上做对照，能直接拖进 Premiere / AE。

| 参数 | 说明 |
|---|---|
| `plan` | 输入的 `.psap` |
| `-o, --output` | 输出文件（默认 `renders/<名字>.mov`） |
| `--size` | 分辨率，如 `1920x1080`（默认）；宽高会向上取到 8 的倍数 |
| `--fps` | 帧率，默认 60 |
| `--background` | 底色。默认 `none` = 透明；绿幕用 `chroma`（`#00b140`，广播标准绿） |
| `--no-points` / `--point-radius` / `--point-color` | 点的开关 / 半径（像素，默认 8）/ 颜色（默认 `#ff3030`） |
| `--no-paths` / `--path-width` / `--path-color` | 轨迹的开关 / 粗细（像素，默认 3）/ 颜色（默认 `#ff3030`） |
| `--path-window MS` | 轨迹最多回溯多久，默认 400；`0` 表示从按下画到当下 |

颜色写 `#rrggbb`、`rrggbb`、`#rgb`、`r,g,b` 或颜色名（`red` / `green` / `chroma` / `blue` …）。

## 自检

```sh
python selftest.py
```

不连设备也能跑，用的就是 `charts/` 里已采集的谱面。对每张谱 × 每个规划器查五件事：
**事件流自洽**（指针 DOWN/UP 配对、时间有序）、**覆盖完整**（每个音符被判的那一刻
确实有手指在容差内，Tap/Hold 头判与 Drag/Flick 分开判）、**存得回去**（`.psap`
编解码往返一致）、**镜像也对**（把规划结果翻过来，拿去对按 `Chart::Mirror` 规则镜像
出来的谱面）、**滑键起手**（每个 flick 在游戏那套 `CheckFlick` 规则下都点得亮）。

再往外还有十组"整条链上下游"的自检：闸门、时钟、坐标换算、缓存、播放器、控制台、
存活探测、结算、判定对账、在位时长。每组的判据、以及它们各自做过哪些变异测试，
写在 [impl.md](impl.md#自检)。

`charts/` 空着的时候它只会跳过规划相关的部分（退出码 2）。全绿才动设备。

## 输出

```
charts/
  Glaciaxion.SunsetRay.0_HD_93215ea2.json        # 谱面 JSON（FromJson 抓到的原文）
  Glaciaxion.SunsetRay.0_HD_93215ea2.meta.json   # 来源上下文 + 长度 + 哈希 + 音符数比对
plans/
  Glaciaxion.SunsetRay.0_HD_93215ea2_conservative.psap      # 规划结果 = 缓存（规范解，不镜像不偏移）
  Glaciaxion.SunsetRay.0_HD_93215ea2_conservative.meta.json # 规划参数、统计、告警、cache_key
renders/
  Glaciaxion.SunsetRay.0_HD_93215ea2_conservative.mov       # 可视化视频（带 alpha）
```

谱面只有一份，文件名里的哈希就是它的内容哈希；`<名字>.json` + `<名字>.meta.json` +
`<名字>_<规划器>.psap` 三者前缀永远一致。镜像与延迟**不落盘**（它们是运行时的事），
所以同一份 `.psap` 对所有局面通用。

> `<名字>` = **歌曲 + 难度 + 内容哈希**，**不含 `seq`**。`seq` 是 agent 的会话内计数器
> （这一局进程里解析的第几张谱面），换个会话同一张谱面就会拿到另一个号。曾经把它拼进
> 文件名，于是同一张 Glaciaxion HD 攒出过 0001/0002/0003 三份、而第二次开谱照样得在
> 闸门里现算几十秒 —— 缓存永远落空。`seq` 仍然写进 meta、仍然出现在日志的
> `[gate #0002]` 里，只是不参与身份。

`meta.json` 里 `notes_in_json`（主机从 JSON 数出来的）与 `notes_reported`（游戏自己
`Chart::GetNoteCount` 数出来的）应当一致，`notes_match` 给出结论 —— 这是"抓到的就是
游戏真正用的那一份"的证据。`cache_key` 则是"这份结果是哪一版算法算出来的"。

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

## 目录一览

```
auto_phigros/
  frida/            frida agent 源码（esbuild 打包成 target/_.js）
  main.py           主干：注入 + 采集 + 闸门上规划 + 放行 + 驱动触控 + 存活探测
  console.py        运行时控制台
  options.py        运行时设置（控制台改的就是它）
  output.py         进程里唯一的写者（整行原子 + 补回提示符）
  planner.py        规划模块（落盘即缓存），可被调用、也可单独运行
  touch.py          触控模块：时钟、调度器、命令行，可单独运行
  render.py         可视化：把 .psap 渲染成视频
  storage.py        .psap 编解码 + 落盘
  selftest.py       不连设备也能跑的自检
  algorithms/       规划算法与契约（registry 按名字现 import）
  backends/         触控后端（scrcpy / recording）
  charts/ plans/ renders/   采集到的谱面 / 规划结果=缓存 / 渲染出来的视频
```

每个模块的职责与设计取舍见 [impl.md](impl.md#结构)。

## 还没做

* 真后端只有 scrcpy 一个。要加别的（比如 frida 侧注入、minitouch），
  在 `backends/registry.py` 的 `_BUILTIN` 里加一行、写一个模块即可。
* `--latency` 只能手工调，还没有"打完一局看判定结果自动回填"的闭环 —— 现在有判定流水了，
  这个闭环的原料（每个音符判成什么、早晚多少）已经齐了。
