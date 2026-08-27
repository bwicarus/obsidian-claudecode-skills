# 手表语音桥（Pi 侧）：为什么它是减震器

2026-08-27 加。这份文档解释**为什么这个进程长这样**。

线格式（帧布局、错误码、握手时序、期限）不写在这里 —— 权威在 Windows 桥的
源码里，`extensions/bw-reader-webext/windows/ComputerVoiceAudio/` 下的
`DirectBridgeProtocol.cs` / `DirectPcmFrame.cs` / `DirectBridgeContract.cs` /
`DirectBridgeServer.cs` / `DirectBridgeAdapters.cs`。抄一份到文档里，只会得到
第二个会过期的真相。要改协议就去读那些文件。

手表侧的取舍（为什么不用 CallKit、为什么音频会话能解禁 WebSocket、
后台中断为什么不可恢复）在 `references/watch-companion.md`，不在这里重复。

---

## 这个进程存在的唯一理由：两个契约不相容

```
手表  ──WSS(Funnel,公网)──→  Pi  ──WSS(tailnet)──→  Windows 语音桥
      容错的一侧                            严格的一侧
```

两头对「掉一帧」的态度是相反的，而且**两头都没得商量**：

- **Windows 侧 fail-closed，没有 resume。** 上行帧的序号必须恰好等于上一帧
  +1、时间戳必须严格递增，违反即整条连接被 abort。这不是可以放宽的参数，
  它是那条链路的安全设计（防止拼接/重放）。
- **手表侧必然掉帧。** 实测：连续跑 153 秒、49 包/秒，中间出现约 **5 秒**的
  空档，原因是息屏切网导致的网络路径变化 —— 射频链路上这是常态，不是故障。

把这两头直接对接，结果是确定的：**第一次息屏就挂断整通电话**。

所以 Pi 不是"顺路转发一下"。它存在的全部意义，就是让这两个不相容的契约
各自成立：

> **严格的一侧永不断，容错的一侧随便断。**

---

## 铁律一：Pi 自己当上行的唯一时钟

### 为什么序号不能来自手表

如果 Pi 把手表帧里的序号原样转给 Windows，那么"手表这一刻有没有网"就直接
决定了"Windows 那条连接活不活"。等于把整条链路上**最不可靠的一段**的抖动，
交给**最不能容错的一段**去承受。

所以 Pi 侧的形状是：

- 序号与时间戳由 Pi 自己生成（`seq = up_seq++`、`ts = up_t0 + seq*20000`），
  按 50Hz 恒定节拍推。这两个公式天然满足"+1 且严格递增"，不需要任何判断，
  也就没有任何一条能判错的分支。
- 手表送来的帧只决定"这一格填什么"。**没来就填静音**（尾部做短淡出，
  否则突然的电平跳变在扬声器上是一声咔哒）。
- **迟到的帧直接丢。** 不回填、不重排、不打乱节拍 —— 回填意味着要么改已
  发出的序号（做不到），要么把后续帧整体推迟（把一次网络抖动变成永久的
  时间偏移）。丢掉 20ms 音频是可闻的；丢掉整通电话是不可接受的。

### 为什么心跳也不能转发

心跳是同一个问题的另一面。Windows 侧 15 秒收不到心跳就判死；手表侧的 5 秒
空档只要撞上心跳时刻，转发过去就是一次误杀。

**Pi 对 Windows 那一侧必须自己按固定间隔发心跳**，跟手表在不在线无关。
手表断连期间，Pi 照发心跳、照发静音帧 —— 通话在 Windows 眼里从未中断，
用户抬腕回来就还在。

⚠ 反过来，Pi 对**手表**那一侧应当宽松：手表的心跳超时要分档（一次超时是
抖动，连续多次才是真断），而且第一次失败**不能**走"通话结束"那条路。
iOS 客户端为此把阈值从 1 次改成 2 次，理由写在 `DirectVoiceSocket.swift`
的注释里：原本比服务端还严，一次 RTT 抖动就误杀整通电话。

⚠ 心跳序号有个不显眼的合同（2026-08-27 读 C# 源码核实）：服务端
`RenewHeartbeatAsync` 拿 `_heartbeatSequence + 1` 逐个比对，而它**只在接受时**
才前进 —— 也就是说心跳序号必须从 1 开始、恰好 +1，跟上行 PCM 一样严。

于是「超时了下一次用哪个号」是个真选择，而且两边都可能错：
- **超时后照常 +1**（当前实现，也是 iOS 的做法）：如果那条心跳其实到了、只是回执
  慢，服务端已经前进过，+1 正好对上。
- **超时后重用同一个号**：如果那条心跳其实到了，服务端就会判 SEQUENCE_INVALID。

选前者，因为 Pi↔Windows 是同一局域网的 TCP 直连（direct 192.168.3.20）：那里
「超时」几乎只可能是对端慢，而不是消息没送到。⚠ 但这也意味着**在消息真的没到达
的那种网络下，两次机会实际只剩一次** —— 第二次必然撞 SEQUENCE_INVALID。这条链路
上不该发生，真发生了就是网络假设错了，别去调 `HEARTBEAT_STRIKES`。

### 由此得到的一条便利

Windows 侧的抖动队列（200ms 上限，满了丢最老的一帧）明确注释说丢弃是安全的，
因为序号已经在入队**之前**校验完。也就是说 Pi 按恒定节拍推过去，即使对端
渲染稍有起伏，损失的只是音频，**不会**变成协议层的序号缺口。

---

## 铁律二：零转发（结构性的，不是白名单）

这条 WebSocket 上复用了同一套消息格式的一大堆能力，其中不少是危险的：删 Anki
卡片、往 Codex 窗口盲发全局热键、改写 Windows 本机的配置文件、写服务模式意图
让收敛循环重启服务、把文本敲进桌面应用……

它们够不到手表，**不是因为 Pi 有一张黑名单**，而是因为：

- 手表能发的只有一个**枚举**（`start` / `stop` / `ping`）和 PCM 音频；
- 发往 Windows 的每一条消息由 Pi 自己拼字面量；
- 两者之间**不共享任何数据结构**。代码里不存在一个"拿手表来的 JSON 塞给
  Windows"的函数。

### 为什么非要做成结构性的

CLAUDE.md 有一条用五次事故换来的教训：**清单类的东西往往有多份副本，
改一处以为改好了**。白名单正是这种东西 —— 它必须被记得、被同步、被审查，
而每一层都长得像"就差这一处了"。

零转发是那条教训的正面版本：

> **不存在的代码路径，不会被顺手加一条绕过。**

一个只会把 3 个枚举值翻译成 3 段固定字节的函数，不会因为某天有人"顺手支持
一下别的 op"就长出新能力 —— 要长出来，得先有人**写一条数据通路**，而那是
一次显眼的结构改动，不是往数组里加个字符串。

⚠ 顺带说明：Windows 那边确实还有一层结构性保护（语音端点在构造协议会话时
没有注入视觉/复制/浏览器控制那几组委托，于是它们退化成"抛未接线"的桩）。
**但那是第二层，不能当第一层。** 另有几类危险消息在当前阶段是可达的。
安全性只能建立在"Pi 结构上发不出去"上，不能建立在"反正对面会拒"上。

---

## 安全边界：Pi 就是那道边界

读代码核实过（2026-08-27）：Windows 桥**没有**密码学配对。真实的闸只有三道：

1. Origin 白名单；
2. 一个由 `tailscale serve` 注入、客户端伪造不了的登录名头，逐字相等；
3. 一条裸 `hello`，只校验协议版本号就算认证通过。

Kestrel 只绑环回地址，唯一入口就是 `tailscale serve`。所以那三道闸合起来
表达的其实是同一件事：

> **真正的安全边界是「你在 tailnet 里」。**

而手表**够不到 tailnet**（watchOS 没有 Tailscale 客户端），这条链路必须经
Funnel 把端点搬到 tailnet 之外。于是边界跟着搬了：

> **Pi 一旦把这条链路暴露到 tailnet 之外，Pi 就成了那道边界 ——
> 因为 Windows 那头不会再校验第二次。**

铁律二由此而来，不是洁癖。任何在 Pi 上"临时放行一下"的改动，等价于在
Windows 本机上开一个未鉴权的命令入口。

配套的两条：

- **Windows 桥的地址与 Origin 是常量，写死在代码里，不从环境变量读。**
  可配置的目标地址是一个能被改指向的攻击面，换来的灵活性这条链路用不上。
- 手表侧的鉴权（Funnel 上的 OAuth）是**唯一**一道认人的闸。它不在这份
  文档的范围里，但它失效等于整条链路失效 —— 没有第二道网可以兜。

---

## 部署

### 归属

| 东西 | 位置 | 谁执行 |
|---|---|---|
| 线格式与节拍的纯函数层（无 socket、无线程） | `_server_deploy/watch_voice_wire.py` | Pi |
| 进程入口（两侧 socket + 自己拼控制面消息） | `_server_deploy/watch_voice_relay.py` | Pi |
| unit 副本 | `references/systemd/watch-voice.service` | Pi |

⚠ 这个切分本身就是铁律二的落地：纯函数层**只做 PCM 帧**，连帧都是重编码而不是
透传（手表帧的 seq/ts/session 三个字段全部丢弃，只有 1920 字节载荷过河）；
控制面消息由进程入口自己拼字面量。把"翻译外来结构"这件事从代码里整个拿掉，
才谈得上"危险消息在结构上够不到"。

⚠ 这三份**都在部署清单内**，走 `scripts/deploy_reader.sh`（自带摘要校验、
原子安装、失败回滚、健康检查）。不要手工重做那套。

⚠ `watch-voice.service` 跑的是**已安装副本** `/home/bwicarus/webapp/` 下的
那份，不是 checkout。只改 checkout 不部署，线上一个字都不会变。这条有
契约测试钉着（`tests/test_reader_deploy_manifest.py`），不是口头约定。

**两个 .py 必须同批安装。** 只上其中一个，表现不是报错，是通话建立后立刻被
对端掐掉 —— 因为 Windows 对上行序号是 fail-closed 的，半旧半新的协议层一定
会在某一帧上违约。所以它们在清单里是 release 不变量，漏掉任何一个，
清单校验直接红。

### 首次上线（一次性，之后交给部署脚本）

⚠ 顺序不能反。`deploy_reader.sh` 会在安装新 unit 文件**之前**先停 unit，
而对一个 systemd 还不认识的 unit 执行 stop 会失败，整次部署 fail-closed 中止。
所以新 unit 必须先让 systemd 认识它：

```bash
# Pi 上，一次性
sudo install -m 0644 -o root -g root \
  ~/claude/references/systemd/watch-voice.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now watch-voice.service
```

之后 unit 文件的内容由部署清单接管（policy=exact，逐字节比对），
不要再手工改 `/etc/systemd/system/` 里的那份 —— 下次部署会把它覆盖回去，
而中间那段时间线上跑的是什么没人说得清。

### Funnel 路径（手工，且**不要碰 nginx 配置**）

Pi 的 nginx 配置与仓库里那份结构完全不同，覆盖会冲掉 Tailscale 证书配置
导致全站挂。这条链路也用不着 nginx —— `tailscale serve/funnel` 自己就能把
一个路径挂到本机端口上。

大意如下（⚠ **未在本机实测**，tailscale 各版本的子命令形态有出入，
执行前先 `tailscale serve --help` 对一遍）：

```bash
sudo tailscale serve --set-path=/watch-voice 8768
sudo tailscale funnel 443 on
tailscale serve status        # 确认路径与端口都在，且是 Funnel 而不只是 serve
```

验收方式只有一种可信的：**从 tailnet 之外**取那个端点，看鉴权闸有没有拦。
⚠ 用公共 DoH 查 `*.ts.net` 会返回 NXDOMAIN，那是**假阴性** —— Funnel 的名字
解析不走那条路，别据此判断没生效。

### 测试跑在哪

两个测试文件跟被测模块同住 `_server_deploy/tests/`，**不在 `tests/` 包里**：

| 文件 | 验什么 |
|---|---|
| `_server_deploy/tests/test_watch_voice_wire.py` | 线格式逐字节、抖动缓冲、上行不变量（纯函数层） |
| `_server_deploy/tests/test_watch_voice_relay.py` | 两条铁律的进程级版本 + 与 wire 的接口一致性 |

```bash
python3 -m unittest discover -v -s _server_deploy/tests -p "test_watch_voice*.py"
```

⚠ **不能加 `-t _server_deploy`**：那个目录没有 `__init__.py`，unittest 会直接报
"Start directory is not importable"。测试文件自己把 `_server_deploy` 插进 `sys.path`。

⚠ 这两个文件曾经**谁都不跑**：`deploy_reader.sh` 的预检跑的是 `-m unittest tests.X`
那一串，按包名点名，扫不到 `tests/` 之外的东西。所以预检里另加了一行 discover，
并把 `_server_deploy/tests` 加进 `hash_validation_inputs` 的输入 —— 否则「预检期间
夹具被改了」这条护栏对它们不成立。一套只在有人想起来时才跑的测试，和没有测试的
区别只在于它让人放心。

### 端口

`8768`。往上依次是 `8767`（voice-rt）、`8766`（mcp-server），
⚠ `8765` 是 AnkiConnect 的默认端口，撞过一次车，别用。

### 「加一个 Pi service」要同步几处

```bash
python3 scripts/contract_sites.py pi-service-registration watch-voice
```

9 处，分布在 4 个代码文件加 2 份文档。**它们互相不知道对方存在**，所以漏掉任何
一处都不会红：只登记代码不登记 unit，unit 就永远是手改的；登记了却没进
`MANAGED_SERVICES`，文件会装上而进程不重启 —— 部署照样退 0。

登进这份表，是为了下一个人不用再靠 grep 把它们一处处撞出来。

---

## unit 里那几行为什么是那样

### `Nice=-5` 一个人不够

cgroup v2 打开 cpu 控制器之后（本机 `yolo-figures` / `figures-describe` 都设了
`CPUWeight`，控制器就是开的），**跨 service 的 CPU 分配由 `cpu.weight` 决定，
`nice` 只在同一个 cgroup 内部分配**。而这个 service 的 cgroup 里只有它自己 ——
光设 `Nice` 等于没设。所以两个都写：`CPUWeight` 管跨服务，`Nice` 管本进程。

这是一个"看起来配了、其实没生效"的典型，符合 `silent-failure-lessons.md`
描述的形状：没有任何地方会告诉你这条设置是空转的。

### `MemorySwapMax=0`：宁可被杀，也不要边跑边换页

Pi 上同时跑着 webapp / anki / OCR / YOLO，内存常年紧张。对一个要按 20ms 出帧的
进程来说，**一次 page-in 就吃光整个抖动预算**，而换页是静默的：日志里什么都
不会写，只表现为通话偶尔卡一下，查都没处查。

禁用 swap 把"内存不够"从一个无声的性能问题，变成一个响亮的、会被健康检查
抓到的事件。

### 有 `MemoryMax`，没有 `MemoryHigh`

`MemoryHigh` 的做法是**回收内存并把进程拖住**，那正是我们要避免的停顿。
所以只留硬上限：超了就 OOM-kill，`Restart=always` 拉起，代价是掉一通电话。

宁可掉一通电话，也不要一个"还活着但一直卡顿、且没人看得出原因"的进程。

⚠ 想过用 `MemoryLow` 保护它的页不被全局回收，**没有采用**：cgroup v2 里
memory 保护是逐层分配的，父 slice 没设保护时子 cgroup 的 `memory.low` 是
空转的。装一个看起来在保护、实际什么都没做的旋钮，比不装更糟。

### 崩溃循环要变成 `failed`

`StartLimitIntervalSec` / `StartLimitBurst` 是刻意写出来的（不靠默认值）：
这是全系统唯一一个"悄悄不在了 = 用户按下按键什么都不发生"的服务。
无限快重启会让它在 `is-active` 上永远显示 active；进 `failed` 才看得见。

---

## 排错

### 先分清是哪一段断了

三种断的修法完全不同，混在一起查会绕很久：

| 现象 | 哪一段 | 依据 |
|---|---|---|
| 手表 UI 停住、Pi 日志显示上行帧不再到达，但对 Windows 的连接还在、心跳照常 | **手表→Pi** | 正常，减震器就是干这个的。若数十秒不恢复，多半是手表侧音频会话已被中断（watchOS 后台不可恢复，见 `watch-companion.md`） |
| Pi 日志出现来自 Windows 的错误码并且连接被关 | **Pi→Windows** | 错误码原文在 `DirectBridgeProtocol.cs` 等文件里可直接搜到，带中文 message |
| `systemctl is-active watch-voice` 不是 active，或 `NRestarts` 在涨 | **Pi 进程自己** | 见下 |

⚠ 第一行那条要特别小心：**手表断了是设计内的**。如果排错时把它当故障去修
Pi，会得出"减震器没生效"的错误结论 —— 恰恰相反，手表断了而 Windows 侧没
挂断，才说明减震器正在工作。

### 常用命令

```bash
systemctl status watch-voice
journalctl -u watch-voice -n 100 --no-pager
journalctl -u watch-voice -f
systemctl show -p MainPID -p NRestarts --value watch-voice.service
```

`NRestarts` 是判断"活着"还是"一直在复活"的唯一可靠信号 —— 一个每两秒重启
一次的进程，`is-active` 看上去和健康的完全一样。部署脚本的健康断言就是靠
比对 `MainPID` + `NRestarts` 来分辨这两者。

### 错误码怎么读

Windows 回来的失败信封里带 `retryable` 字段，**按它分档，不要自己猜**：

- 标了可重试的（对端忙、等待应用就绪超时、上下文模式变化等）→ 可以退避后重来；
- 没标的（本机未启用、目标进程校验失败、媒体未确认启动、协议/字段类错误）
  → **终局**，循环重连只会刷日志。

握手阶段同理：只有连接失败/断开/超时三类是可重试的，schema 与合同版本类
是终局。iOS 客户端就是这么分的，Pi 照抄这条分档即可。

HTTP 层还有两个不是 JSON 的结果：Origin 或登录名头不过是 **403**，
非 GET / 非 Upgrade 是 **426**。403 要单独对待 —— 那是配置或身份问题，
重试一万次也是 403。

### 一通电话 = 一条连接

不要复用连接。Windows 侧"等待启动"的期限一旦设定就不再重置，超时会直接
关连接；复用连接跑第二通电话，症状是 STOP 的回执**根本不会发出**，只收到
一个错误事件然后 socket 关闭。

每通电话：新连接 → 握手 → 限时内 START → 通话 → STOP → 关闭。
没有通话时也不要长期挂着这条连接。

---

## 已知未验证 / 留给下一个人

- **`tailscale serve/funnel` 的确切子命令形态**没在本机跑过（见上）。
  验收必须从 tailnet 之外做，且别用公共 DoH 判断。
- **"复用连接会导致 STOP 回执发不出"是从代码读出来的推论**，没有实跑验证。
  按"一通电话一条连接"做就绕开了这个问题；哪天真要复用，先实测。
- **是否允许手表抢占正在进行的通话**（协议上支持带标志强抢一个健康会话）
  是产品决定，不是协议限制。当前倾向是不抢，让"对端忙"冒到手表 UI 上，
  但这需要用户拍板。
- **电池消耗**只有估算（15–25%/小时），无实测数字。
- 手表在**后台**被系统中断音频后不可恢复，这条无软件侧规避，只能提示用户
  回前台。它属于手表侧，Pi 不需要为它做任何事 —— 但排错时要认得出来，
  否则会当成 Pi 的 bug 查。
