# 情境信号与自动触发（2026-09-08）

用户拍板的形状，原话两句：

> 关于分类我的想法其实是我们提供各种判断用的接口，然后 codex 自己使用这些接口
> 绑定各种功能，只是我们要提供一些自动触发用的工具，就像到家自动发信类型，需要
> 提供给 ai 比如到达某地后满足条件返回信号，或者是起床后满足条件等
>
> 不这样做 codex 每次都会自己创建一个新的工具，会很混乱

所以这不是"又做了两个功能"，是**把一类功能收敛成两个原语**：

| 原语 | 文件 | AI 怎么用 |
|---|---|---|
| 判断接口 | `computer-voice-desktop/situation_signals.py` | `python …\BWReader\situation_signals.py`（`--vocab` 看有哪些） |
| 自动触发 | `computer-voice-desktop/situation_triggers.py` | `--add --name X --when '{…}' --title Y --ai-action Z` |
| 触发能做的事 | `computer-voice-desktop/situation_actions.py` | 触发规则里写 `--do <动作>`（`--list` 看有哪些） |

三个都铺在 `%LOCALAPPDATA%\BWReader\` 稳定路径，AI 直接跑那份。
教 AI 的地方是 `~/.codex/AGENTS.md`（「情境信号与自动触发」一节）。

## 为什么词汇表必须封闭

`situation_signals.SIGNALS` 是一张**封闭**的表（15 个信号）。条件里只能写表里的
名字，写错会当场拒绝并列出可用的名字。这条约束就是整件事的实现手段 ——
开放的取数路径等于允许 AI 每次临场发明一条，而临场发明的路径没有任何人测过。

加信号要改那个文件，同时写清 `summary` 和 `values`：两者都会随 `--vocab`
端到 AI 面前，**写反比不写更糟**（AI 看到错的值域会直接放弃用这个信号）。

## 「到达某地」和「起床后」为什么没有专用谓词

它们是同一件事的**上升沿**：

    到家   = place 信号从非 home 变成 home
    起床后 = awake 信号从 false 变成 true

所以引擎只做一件事：边沿检测。条件写成"状态的合取"，"什么时候算刚发生"由引擎
负责。多一个 `arrived_at` 谓词就是第二条判定路径，而两条路径迟早不一致。

配套的三条语义，都有具体的坑撑着：

- **注册时先记基线，不立刻触发**。注册那刻已成立的记 `lastMatch=true` 但不响 ——
  已经成立的事 AI 当场就能做；"一注册就响"会让每次试探性注册都吵人一次。
- **`known=false` 一律不成立**。读不到位置不等于"不在家"。判定只写在
  `situation_signals.matches` 一处。
- **建通知失败不吃掉上升沿**。第一版 `_fire` 没给 `end`（`NotificationStore`
  拒绝没有终止条件的条目），于是"响过一次但其实什么都没发生"，而 `lastMatch`
  已被置真、下一轮不再重试。现在失败会退回 `lastMatch` 并把原因记进
  `lastError`（`--list` 看得见）。这种失败最贵，因为它看起来像成功。

## 出口有两个：告诉人，和直接做事

用户 2026-09-08 说明思路时补的那半句最关键：

> 我们其实现在在做的 codex 自己也能做，但是在使用时做很不稳定且各种工具调用
> 肯定没有固定化的代码稳定快速……不只是需要及时的获取数据，还牵扯到这些数据
> 变化为某个状态时自动触发的各种行为能力，比如触发某个或者复合条件后停下或者
> 开始某些功能

第一版只有"建一条通知"，也就是**只能告诉人**。`situation_actions.py` 补的是
另一半：机器自己能确定性做完的事，不该绕经 AI。分工：

| 写法 | 什么时候用 |
|---|---|
| `--do <动作>` | 结果确定、机器自己能做完。**注册时就校验**动作名和参数 |
| `--ai-action <话>` | 要判断、要说话、要看情况。经快慢板交给 AI |

只给 `--do` 不给 `--title` 的规则做完**不打扰人**（板上不出条目）；
但动作失败一定留一条通知 —— 静默失败的动作等于没有这个动作。
（第一版这里就有 bug：失败时没标题，通知被 `NotificationStore` 拒掉，
于是"失败也要说"变成了"什么都没说"。测试当场抓到。）

### 动作表的准入条件：说得出它凭什么生效

每个动作都要填 `why`。这一栏不是注释，是准入条件 —— 一个"校验全过、其实什么
都没发生"的动作比没有这个动作糟得多，因为调用方以为做了。

已经被这条规矩挡在表外的例子（2026-09-08 实查）：**改
`readerpc-server.config.json` 的 `voiceEnabled` / `keepPcPreprocessingOnline`
不会当场生效** —— 那个文件只在 ReaderPC 启动时读一次（`load_preferences`），
托盘界面的复选框是靠 `command=` 回调当场应用的，不是靠文件监听。
所以"用触发关掉语音"目前做不到，**别把它写进表里假装能做**。

现有四个动作，都验证过生效路径：

| 动作 | 凭什么生效 |
|---|---|
| `background.hold` / `background.resume` | `readerpc_gate` 读 `background-hold.json`，所有后台计划任务开头都过那道闸 |
| `task.disable` / `task.enable` | `schtasks /Change` 当场改注册状态 |

`background.hold` 正对着用户 2026-09-08 的那个痛点：人在用电脑时后台不该跑
半小时的 AI 作业把机器拖卡。它**必须有上限**（480 分钟）并且过期自动失效 ——
没有上限的"静音"会变成永久停摆，而且没人记得去解除。

⚠ `task.*` 只认白名单，而**看门狗和引导任务永远不在白名单里**：关掉看门狗
等于让一次崩溃变成永久停摆，而排查的人不会想到去翻计划任务。

⚠ 闸在主项目树（`scripts/lib/readerpc_gate.py`），动作在桥那边，两棵 git 树
互相 import 不了，只能靠**文件名**这个协议常量对接。`test_situation_actions.py`
里有一条断言盯着两个常量相等 —— 别指望人记住两处要一致。

## 紧凑是硬要求

用户 2026-09-08：「这些信息要尽可能的紧凑和简洁防止造成混乱」。

`situation_signals.py` **默认输出一行**，逐行版留给 `--full`：

    在家 醒着 23点 窗内 新卡10 到期0 闲264分 PC停 ?headphones 位置已264分钟未更新

三条规矩：
- **紧凑不等于省掉"不知道"**：读不到的信号收尾成 `?名字`，不写会让 AI
  以为那一项是否定的。
- **不重复同一件事**：`PC停` 时 `voice_linked` / `reading_title` 必然不可用，
  就不再列 `?那两个` —— 原因已经说全了。
- **旧到离谱要标出来**：位置超过 60 分钟没更新会附一句，
  拿两小时前的位置当现状是这套东西最容易犯的错。

顺带修掉一个我自己把纪律用反的地方：`reading_title` 在"什么都没在读"时
原本报 `unknown`，而那是**已知**的事实。改成 `known("")` 之后
`{"reading_title": {"not": ""}}`（在读点什么）这类条件才成立得了 ——
之前它永远不成立，因为不可用的信号一律判不成立。

### 出口仍然是封闭的

通知走既有的 `NotificationStore` + deliver 档，默认 12 小时过期；动作只能从
`ACTIONS` 表里挑。**触发永远不执行任意命令** —— 那既是安全问题，也会立刻长出
第二套"AI 自己发明的动作"，而那正是这套设计要消灭的东西。

`--ai-action` 折进通知正文而**不新开字段**：通知的字段表在导出/渲染/板子/侧栏
各有副本，加一个字段要同步好几处。

## 与 `--at-place` 的分工（别混）

`replication_notifications.py create --at-place` 走 **iOS 系统提醒 App** 的地理
围栏，不依赖我们在跑，更可靠，**单纯地点提醒首选它**。
`situation_triggers` 补的是它做不到的：组合条件（到家 *且* 醒着 *且* 卡够多）、
以及绑到起床/耳机/阅读状态这类非位置信号。

## 求值时机与它的限度

`replication_apply` 每轮对账调一次 `situation_triggers.evaluate()`，夹在：

- `export_current_place` **之后** —— 条件里的 `place` 读的就是那个文件，
  顺序反了会拿上一轮的旧位置判这一轮；
- 两个 `export_*` **之前** —— 触发建出来的通知要赶上本轮投影，
  否则侧栏/板子要等下一轮才看见它。

所以触发有**最多一刻钟延迟**，且 **ReaderPC 没在跑时不求值**。
要求秒级响应的事不能指望它 —— 耳机自动静音就是因此没走这条路（见下）。

## 耳机自动静音为什么不在这套机制里

用户要的是「在外面没戴耳机 + App 语音开着 → 自动静音」，并明确保留了位置条件
（「在家里我说的算，而且也只有我一个人」）。这件事必须在 **App 本机即时**完成：
出门那刻要立刻生效，绕一趟 Windows 再回来声音早就出去了。

所以分工是：

- **判断在 App**：`ios/BWReader/App/ReaderPresenceGuard.swift`，
  拿 `AVAudioSession` 的输出走向 + `ReaderLocationProvider` 的当下定位；
  静音落到 `NativeAudioEngine.setOutputMuted`（改 `player.volume`，不停播 ——
  用户要的是静音不是挂断）。引擎在**路由变化**和**开播**两处各判一次：
  只在路由变化时判会漏掉"出门之后才开始说话"。
- **判据由 Windows 下发**：`replication_places.export_voice_zones` 导出
  `voice-zones.json`（几个坐标 + 一个半径 + `home/work/elsewhere`），
  桥在 `/reader-presence/v1` 的响应里顺路带回，App 缓存进 `UserDefaults`。
  缓存是必需的：出门在外往往连不上家里的电脑，而那恰恰最需要这个判断。
  ⚠ 别名→状态的映射（家/自宅→home）**只存在 Python 一处**；让 C# 或 Swift
  各自去认那几个中文名字就是把同一张表抄三份。
- **在场状态回传 Windows**：`/reader-presence/v1` 落 `presence-signal.json`，
  折成 `audio_route` / `headphones` / `app_foreground` 三个信号给 AI 和规则用。
  这份是**副本，不是控制回路**。

静错的代价不对称（错误静音会让语音看起来是坏的且没有提示说得出为什么），
所以每个"不知道"都倒向不静音：定位读不到不静、语音区拿不到不静、
不认识的音频走向算"戴着耳机"。桥读不到 `voice-zones.json` 时**不带这个字段**
而不是给空数组 —— 空数组会被 App 理解成"一个地点都没命名"，于是它认为自己
永远在外面。

## 改动时要数的副本

- 新增 CLI 要动**两份打包清单**：`package_readerpc_server.py` 的
  `RUNTIME_SOURCES`（打进包）+ 稳定路径清单（铺到 `%LOCALAPPDATA%\BWReader`）。
  只加第一份的表现是 `ImportError` 被 `except` 吞掉、规则永远不响
  （`voip_push` 2026-08-29 栽过这一次）。
- 新增桥路由要动**四处**：`DirectBridgeServer.cs` 路由表 + 分发方法、
  `bridge_core.DIRECT_SERVE_SIBLING_PATHS`、`release_preflight.py` 白名单，
  外加 `tailscale serve --set-path`（那是一份逐条清单，没加的新地址一律 404；
  ⚠ 从 PowerShell 跑，Git Bash 会把 `/path` 参数改写成本地路径）。
- **C# 改动不随 ReaderPC 走**。两条流水线互相独立：
  `package_readerpc_server.py` 是 PyInstaller，`package_computer_voice_direct.py`
  才编译 C#。装了 ReaderPC 不等于送到了桥。

## 相关测试

| 文件 | 守什么 |
|---|---|
| `computer-voice-desktop/tests/test_situation_signals.py` | 空世界一律"不知道"、`matches` 各比较器、词汇表封闭 |
| `computer-voice-desktop/tests/test_situation_triggers.py` | 非法规则报错、上升沿只响一次、失败不吃掉边沿、不可用不响 |
| `computer-voice-desktop/tests/test_situation_actions.py` | 动作名注册时就校验、白名单挡住看门狗、按住真拦闸且会过期、动作失败不静默 |
| `tests/test_presence_mute.py` | 语音区导出、位置条件没被简化掉、静音是音量不是停播、端点四处注册 |
| `tests/test_ios_purpose_strings.py` | entitlement 与用途说明配对（漏了只在**上传**才报 90683） |
