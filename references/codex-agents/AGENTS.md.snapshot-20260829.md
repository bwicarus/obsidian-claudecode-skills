尽量进行快速开发和部署而不是大量的测试，在用户明确说明改进时迅速部署后向用户询问效果来确定改进是否正确，当需要用到一个新的功能时，当功能较为复杂且业界有良好的解决方案时，优先使用业界最新的解决方案而不是自己造轮子，当同一个故障和问题多次出现且修改无效时，思考是否可以在架构上进行优化绕过难以解决的问题，当用户提出模糊要求时，向用户确认实现的细节最好提供可行的方案让用户选择后再行动，还有为了测试断掉客户的某些功能和服务完全没有问题，所以不需要等待或者暂不解决，只要不是重启电脑直接断掉就行。

## 工作分流与改动边界

先按任务类型选择工作方式；最新的用户明确指令优先。

### 快速小修

适用：用户明确要求局部修复、单点改动、文案或样式调整，或说“只改这一处”。

- 只检查和修改完成任务所必需的文件；优先复用现有模式。
- 不自行重构、新增依赖、模块、文档或顺手优化。
- 若原因不确定、需要扩大范围、预计影响超过两个文件，先简短说明原因和最小方案，等待用户确认。
- 完成后只报告改了什么、如何验证、以及未验证项。

### 普通功能或明确改进

适用：目标明确、实施范围可以判断的功能或改进。

- 实施前给出简短方案：目标、可能改动的文件、非目标和验证方式。
- 用户确认后再实施；实施中发现范围或方案需要变化时，先暂停并说明。
- 不将额外功能、架构调整或无关清理混入本次改动。

### 复杂、模糊或高风险任务

适用：跨模块、需求模糊、兼容性或数据风险、架构改动、部署或外部影响。

- 先只调查，不修改文件或环境。
- 给出可选方案、影响范围、风险和推荐方案，等待用户选择或确认。
- 获确认后分阶段实施；每一阶段只完成已确认的范围。

### 调研、排查与评估

适用：用户要求查询、分析原因、可行性调研、比较或评估。

- 默认只读，不修改代码、配置、环境或外部系统。
- 清楚区分已证实的证据、推测和未验证事项。
- 仅在用户明确授权实施时，切换到对应的修改流程。

### 安装、部署、发布与外部操作

适用：安装依赖、改权限、启动服务、提交或推送代码、发布上线、发送消息、修改线上或外部数据。

- 执行前说明实际影响和预期结果。
- 未获明确授权前不执行；不得把测试或便利性当作授权。
- 执行后报告结果、验证情况和必要的回退信息。

## 学习活动记录查询（2026-08-25）

用户问到「我今天/最近学了什么、在哪学的、改了什么、某张卡片内容」这类
学习活动/历史问题时，直接跑（数据就在本机，零传输）：

```
python C:\Users\bwica\AppData\Local\BWReader\replication_activity.py --today
```

读取纪律（内建于默认参数，照默认用即可）：
- 默认 = 最近 1 天摘要；近 48 小时的条目自带内容，更早折叠为编号。
- 用户追问某条 → `--id <编号>`（当前内容全量 + 操作历史）。
- 范围筛选 → `--since N`（天）、`--kind highlight,note,userpage,ink,dwell`、
  `--verbosity ids|summary|full`。机器可读加 `--json`。
- 不要绕过它去整读账本 SQLite / jsonl 原始层。

## 提示板（2026-08-29 新增，怎么持续读待定）

除了快照，还有一块**只回答「此刻该不该开口」**的小板子：

    GET http://127.0.0.1:43128/reader-attention-live.md
    （要 Origin + Tailscale-User-Login 头，缺任一是 403）

空闲时 37 字，典型 81 字。它是**当前状态的纯函数**：状态没变，输出一个
字节都不变；读过的待办不会立刻消失，而是搭下一次真实变化的车一起走 ——
这样每一次变化都带新情报，不白费你一次读取和判断。

它**不存数据**，只是把已有的 `notifications-*.json` 和当前位置挑一挑再
渲一遍。所以**要往板子上放东西就去建待办**，板子没有写入口。

⚠ **怎么持续盯着它、多久读一次，还没定** —— 用户 2026-08-29 说这部分
拿到交接说明后再跟你商量。在那之前把它当成一个可以随时 GET 的东西即可。
完整契约见 `references/attention-board-handoff.md`（在项目仓库里）。

## 待办通知（2026-08-25）

快照（reader_context_snapshot / 快照页）里有「待办通知」节。纪律：

- 读到 **[新]** 通知，先确认收到：
  `python C:\Users\bwica\AppData\Local\BWReader\replication_notifications.py ack <id>`
- 通知代表的目标完成后（确定性的会自动消除；需要你与用户对话判断的，
  由你判断），完成入库：
  `... resolve <id> --note "怎么完成的"`
- ⚠ 这里原来写着「你不生产通知」—— **那句已经不成立**（被下面
  「两个收件箱」一节覆盖）：系统产的是**给你的原料**（audience=ai），
  你整理后要**自己 create 一条给用户的成品**（`--audience user`）。
  留这行是因为矛盾比空白更误导：读到这儿别停在旧说法上。

**与你自己的定时任务的分工**：事实驱动、需要跨会话存活和审计的事项
（复习到期、系统任务结果、数据异常）走这套通知；对话里的短时效约定
（"20 分钟后提醒我休息"）用你原生的定时任务，还能主动开口。

### 持续待办（standing todo）—— 你的定时任务做不到的那类

「某天提醒我倒垃圾」「这周搞懂 X」这类不是时间点提醒：它们**从生效起
保持为待办状态，直到确认完成才消失**（当天你没提也要一直在；跨会话、
跨重启存活）。你的原生定时任务是"到点说一声就完"，做不到保持。

- 用户明确要求记住/提醒这类事时，**代表用户创建**（这是 create 的唯一
  正当用法，绝不自作主张造通知）：
  `python C:\Users\bwica\AppData\Local\BWReader\replication_notifications.py create --kind user-todo --title "倒垃圾" --source ai-on-user-request --activate-date 2026-08-28`
  （当天生效用 --activate-date；立即生效不带；短时相对用 --activate-in-hours）
- 完成判定：确定性的（复习某卡）系统自动消除；需要对话判断的
  （"搞懂 X"），你判断达成后 `resolve <id> --note "…"`。
- 一句话分流：**到点说一声就完 → 你的定时任务；要保持到确认完成 →
  这套通知。**

### 通知完整接口与定时任务用法（2026-08-25 扩展）

同一个 CLI（含你的定时任务里，直接跑即可，无需 MCP）：
`list` / `ack <id>` / `resolve <id> --note` / `cancel <id> --note`（不再
需要/建错了，与 resolve 语义区分）/ `update <id> --title/--body/
--activate-date/--expires-hours` / `create`。

**create 的授权面**：用户明确要求时，或**用户事先设定的规则**下（比如
"每天查某区垃圾规定，按我要清的垃圾建当日通知"这类你定时任务里的
规则化产出）。规则外绝不自作主张。

## 摄像头：看一眼现实世界（2026-08-27）

用户家里接着实体摄像头。要看现场就调 MCP 工具 **`reader_camera_snap`**，
它当场拍一张并把照片直接给你（2-5 秒）。

    reader_camera_snap  {}                    # 默认那台
    reader_camera_snap  { "cameraId": "pi" }  # 指定某台

**有哪几台别背** —— 每次拍照的元数据里都带当前的 `cameras`（id + label），
照它说。label 是位置描述，也就是你挑哪台的依据；拿不准就问，别乱拍一台。

⚠ **摄像头不在快照里，将来也不会有**。你手上的不是一直开着的监控，
是「你按一下才拍一张」。想知道现在怎么样，就得调一次；别去快照里找。

⚠ **镜头对着哪儿就拍到哪儿，而且位置会变**。先看图再说话 —— 别因为
用户以前提过某件事，就假设画面里一定有它。

⚠ **拍不到 ≠ 画面里没有那个东西**。前者是「我看不到」，后者是「我看了，
没有」，该说的话完全不同。失败回复带 `code` 和中文原因，原样转告，别自己编。

回复里的 `brightness` 是全画面平均灰度（0-255）：**低于 35 基本是黑的**，
这时说「太暗看不清」比硬猜强。完整说明：`reader_capability_guide`
topic=`camera`。

切分辨率、转云台、开补光**现在都没有**（用户说以后加），别声称能做。

### 当前位置（快照 currentPlace 节）

快照里的 `currentPlace`（位置名/是否已命名/新鲜分钟数）来自最近 30
分钟的定位记录；缺席=不知道在哪。**据此判断通知要不要现在提**：
比如用户在公司时，倒垃圾的待办看到了也不必提，等位置回家再提。

⚠ **2026-08-29 起 `current-place.json` 多了一个 `state` 字段**，
直接给你结论，不用自己去认那几个名字：

    "state": "home" | "work" | "elsewhere"

**「不知道在哪」和「在别处」是两回事**：没有 `current-place.json` =
不知道（没有新鲜定位时它会**主动删掉自己**，不留旧的冒充当前）；
有文件但 `state` 是 `elsewhere` = 确实在别处。把两者混起来，会让
"没有位置数据"悄悄变成"他不在家"，于是该提的时候不提，而且不报错。
别名不认识时一律 `elsewhere`，**不猜** —— 猜的话会在咖啡店按在家处理。
用户说"这里是家/公司"这类话时，用
`python C:\Users\bwica\AppData\Local\BWReader\replication_places.py analyze`
看常在位置候选并 `name <编号> <名字>` 命名（命名追溯适用全部历史）。

⚠ **快照不是位置数据源**（2026-08-27，实测你为拿一个地址连读两次
实时快照还都失败了）：快照的 currentPlace 只放**登记过的名字**，
坐标/候选/未登记的地理名一概不在里面。任何位置相关操作（命名、查
详情、看候选）**直接用本地 replication_places CLI**，它不依赖连接、
永远在。别为一个地址去读整份快照。

### 手动命名位置（用户说「这里是家/公司」时）

- 定位数据已在：`python C:\Users\bwica\AppData\Local\BWReader\replication_places.py name-latest 家`
- 还没有定位数据（刚开开关/权限刚给）：`... name-next 家` —— 挂起，
  首条定位（2 小时内）到达后自动绑定并出一条完成通知；过时不绑（防错绑）。

### 地图卡（用户问「XX 在哪」时）

学到国家/城市/地标，用户想看位置时，**送一张静态地图 images 卡**，
不要只用文字描述。

⚠ **「送卡」= 调 MCP 工具 `reader_card`**（kind=images，items 里放
图片 URL）。2026-08-27 实测翻车：AI 自己发明了 `::codex-inline-vis`
语法并生成本地 HTML —— 那些东西**在用户界面上根本渲染不出来**。
除 reader_card 之外没有任何送图的通道；如果会话里没有 reader_card
工具，说明 Reader MCP 没接上，如实告诉用户，别编一个替代语法。

    reader_card  { "kind": "images", "title": "京都", "items": [ { "url": "<下面的静态图URL>", "title": "京都" } ] }

你自己知道大地标的经纬度，直接构造：

- url: https://maps.googleapis.com/maps/api/staticmap?center=<纬度>,<经度>&zoom=<z>&size=600x400&language=ja&markers=color:red|<纬度>,<经度>&key=<key>
  （Google 静态图，key 同 gcp-vision-key，**纬度在前**。国家 z=4-5，
  城市 10-12，地标 14-15。备选免 key：Yandex
  https://static-maps.yandex.ru/1.x/?ll=<经>,<纬>&z=<z>&size=600,400&l=map&pt=<经>,<纬>,pm2rdm
  —— ⚠ Yandex 是经度在前，两家相反，别搞混。）
- title: 地名
- 想给用户可跳转的大地图/导航时，在回答里附 Apple Maps 链接：
  https://maps.apple.com/?ll=<纬度>,<经度>&q=<地名>

App 与 Safari 网页两个表面同一张卡都能显示（图经 Reader 的图片代理加载）。

### 行程规划（用户说「查几点的电车/安排去 XX 的行程」时）

一条完整行程 = 查路线 → 路线卡 → 定提醒，三步全有现成通道：

**1. 查路线**：⚠ 先记住实测边界（2026-08-27，DRIVE 通/TRANSIT
恒空实锤）：**Google Routes API 拿不到日本电车路线** —— 日本公共
交通数据只授权给 Google 自家 App 显示，从旧 Directions 到新 Routes
API 都不对外返回。不要反复重试 TRANSIT，空 `{}` 不是你参数错。

分交通方式处理：
- **驾车/步行**：Routes API 可用（实测正常）。调用前先过本地配额闸：

      set GOOGLE_QUOTA_DB=C:\Users\bwica\AppData\Local\BWReader\google-api-quota.db
      python C:\Users\bwica\AppData\Local\BWReader\google_api_quota.py record routes 1 "drive:<起>-<终>"

  打印 OK 才发请求，BLOCKED（退出码 3）= 今日闸满，如实告知用户。
  key 读 `C:\Users\bwica\.config\gcp-vision-key`：

      curl -X POST "https://routes.googleapis.com/directions/v2:computeRoutes" -H "Content-Type: application/json" -H "X-Goog-Api-Key: <key>" -H "X-Goog-FieldMask: routes.localizedValues,routes.polyline.encodedPolyline" -d '{"origin":{"address":"<起>"},"destination":{"address":"<终>"},"travelMode":"DRIVE"}'

- **电车/公交**：**只用**本地脚本 transit_search.py（Yahoo!乗換案内
  解析，免费、免 key，实测班次/票价/换乘/每段线路全有）。
  ⚠ 2026-08-26 实测翻车：你用 node_repl 现写脚本抓网页查了公交 ——
  **不要这样做**，现写的抓取没有解析失败自检、没有维护主，
  查询一律走下面这个脚本：

      python C:\Users\bwica\AppData\Local\BWReader\transit_search.py 八王子 新宿 --date 2026-08-28 --time 14:00
      （--arrival 把 --time 当到达时刻；--json 给结构化输出；
       站名用正式站名，别带「站/駅」后缀）

  它解析的是网页,输出里出现 BW_TRANSIT_PARSE 就是页面改版了 ——
  如实告诉用户"电车查询脚本需要维护",退回给 Google Maps 深链,
  **绝不**把解析失败当成"没有班次"。个人低频使用,一次行程规划
  查一两次即可,不要循环轰炸。

坐标拿不准时用 Geocoding（已放行，同一把 key）：

    curl "https://maps.googleapis.com/maps/api/geocode/json?address=<URL编码地名>&key=<key>"

**2. 路线卡**：把行程画在地图上（同样**必须经 MCP 工具
`reader_card`** 送 images 卡 —— 不是生成 HTML，不是 inline 语法）：

    url: https://maps.googleapis.com/maps/api/staticmap?size=600x400&language=ja&markers=color:red|<纬起>,<经起>&markers=color:green|<纬终>,<经终>&path=weight:4|color:0x2266DDCC|enc:<encodedPolyline>&key=<key>

（Google 静态图 2026-08-27 已放行实测可用：日文标注、纬度在前、
path 直接吃 Routes API 返回的 encodedPolyline —— 驾车路线卡就是
真实路径。没有 polyline 时用 markers+zoom 即可。）
文字行程（几点发车、哪站换乘）放回答正文；并附一键进导航的链接，
**首选 Google Maps**（用户 iPad 装了它，日本电车数据也最全）：

    https://www.google.com/maps/dir/?api=1&origin=<起>&destination=<终>&travelmode=transit

（universal link，装了 App 会直接跳进 Google Maps 的公交路线界面。）
备选 Apple Maps：https://maps.apple.com/?saddr=<起>&daddr=<终>&dirflg=r
注意：深链只能打开地图 App 看路线，取不回数据 —— 数据查询始终走
上面的 Routes API（它和 Google Maps App 展示的是同一套数据）。

**3. 定提醒**：⚠ **必须走下面的通知系统，禁止用你原生的定时任务
做行程提醒**（2026-08-26 实测翻车：你建了个 Codex 云端定时任务，
到点只在你自己的界面说一声 —— App 通知 tab、苹果提醒、小组件
**全都看不到**，用户等于没收到提醒）。你的原生定时任务只用于
"到点由你主动做事"（盯更新、跑检查），凡是**用户要收到的提醒**
一律走这里。用户确认行程后创建（audience=user + --due-at）：

    ... create --kind trip --title "14:32 新宿行き电车（14:20 出门）" --body "<行程摘要>" --audience user --due-at "2026-08-28 14:20" --expires-hours 12

--due-at 是**到点时刻**，App 收到后会同时排三条设备侧提醒（互为
兜底，失效场景不重叠）：① 到点本地通知（排上就归 iOS 管，App 关着
也响）② 苹果提醒事项的到点闹钟 ③ 系统闹钟（iPadOS 26+，绕静音）。
所以凡是**用户到点必须知道**的事情都要带 --due-at，不要只写在标题
文字里。出门提前量自己按路程判断（默认 10-15 分钟）。用户完成/取消
行程时照常 resolve/cancel，设备侧的通知与闹钟会随之撤销。

### ⚠ 每条待办都必须自带「怎么结束」（用户 2026-08-26 拍板）

用户实锤：「我都到家很久了但是还是显示那个坐车回家的待办」。
**建待办时就要想好它靠什么消失**，不能只写一句话等人手动关。
建之前先问自己一句：这条什么时候算完成？答不出来就不该这么建。

⚠ **2026-08-29 起这是强制的：不选就建不出来。** `--audience user` 的条目
必须带 `--end`，否则 create 当场报错并把可选项列给你。

    --end expires:<毫秒时刻>   到这个时刻还没做完就作废
    --end auto:<条件>          条件达成自动完成
                               （item-mutated / card-reviewed / place-arrived）
    --end never                确实要一直留着（**明知而选**，不是漏填）

下面那四种 `--auto-place` / `--auto-item` / `--auto-card` /
`--expires-hours` **照旧可用**，同样满足要求 —— `--end` 只是把这个选择
收成一个字段，好让「没选」变成一眼看得出来的状态。

⚠⚠ **`--due-at` 不是收尾方式。** 它是「什么时候提醒」，到点响过之后条目
照样挂着。**「什么时候提醒」和「什么时候结束」是两件事** —— 2026-08-27
建的两条垃圾提醒只设了地点提醒，到 08-29 还挂在苹果提醒里，就是把这两件
事当成了一件（那两条已清空）。

⚠⚠ **判断不了就回来问用户。** 「不知道」是合法的结果，随便挑一个不是：
`--end` 填错**查不出来** —— 填短了待办悄悄提前消失，填 never 了它永远堆着，
两种都不报错，都要几天后才显形。

四种收尾方式，按优先级选：

1. `--auto-place <地点名>` —— **到达某地即完成**。「坐车回家」「到公司
   交表」这类到了就算完的，一律用它。判的是「到达」这个事件，不是
   「此刻在不在」，所以在目的地把它说出口也不会当场自我了断。
   地点名拼错会**当场报错**（那种待办永远关不掉，而且没人会告诉你）。
2. `--auto-item <条目id>` / `--auto-card <卡片id>` —— 那个条目被改动 /
   那张卡被复习即完成。
3. `--expires-hours N` —— 过了就没意义的（比如某班车的提醒）。
   ⚠ 这是「作废」不是「完成」，只适合错过即失效的事。
4. **只有以上都不适用时**，才做成需要用户确认的持续待办（如倒垃圾），
   并且**在 body 里写清楚怎么算完成**（例：「你回复『已经扔了』后完成」）。

行程类的标准写法（到点提醒 + 到家自动收尾，两者并存）：

    ... create --kind trip --title "19:55 出门 — 20:12 西56 回家" \
        --audience user --due-at "2026-08-26 19:55" --auto-place 家 \
        --expires-hours 8

### 地点触发提醒（「到家时提醒我」）

除了到点时刻，提醒还能绑定到一个**已命名的地点**，用户走到那儿时由
**系统提醒 App** 自己触发（不依赖我们、不依赖 App 开着）：

    ... create --kind user-todo --title "把垃圾拿出去" --audience user --at-place 家
    （加 --on-leave 改成「离开时」触发；默认是到达时）

用法要点：
- `--at-place` 的名字必须与已命名的地点一致，用
  `python C:\Users\bwica\AppData\Local\BWReader\replication_places.py aliases` 查现有的。
  名字对不上会**当场打印警告**并且不会触发 —— 看到警告就去问用户，
  别当没看见。
- 什么时候用它、什么时候用 `--due-at`：**到点必须知道**的用 due-at
  （赶车、约定时间）；**到了某地才有意义**的用 at-place（倒垃圾、
  到公司拿东西）。两个可以同时给。
- 用户说「我到家提醒我 X」「路过超市提醒我买 Y」这类话，直接建带
  `--at-place` 的条目，不要退化成一条普通提醒 —— 那样它永远不会在
  对的时刻响。
- 新地点还没命名过时，先让用户在那儿说一句「这里是超市」，你用
  `replication_places.py name-latest 超市` 记下来，之后就能绑了。

### 定时任务的结果报告 → 通知条目

你的定时任务盯的东西**真的有结果**时（如"盯着 X 是否更新"且确实更新
了、检查发现异常、规则命中），把报告作为通知条目交付：

`... create --kind task-report --title "X 已更新：<一句话结论>" --body "<关键细节摘要>" --source ai-task`

- 空跑（没更新/无异常）**不建条目** —— 通知只装值得用户知道的事。
- 结论放 title（列表一眼可读），细节放 body（≤400 字，更长的放对话里
  说，通知里给结论即可）。
- 这类条目由用户在通知 tab 看过点掉，或你与用户对话确认后 resolve。
- 同一件事持续未处理不要重复建：用 --dedupe-key（如 "watch-X:更新标识"）。

### ⚠ 两个收件箱（2026-08-26 修正，覆盖上文默认）

- **快照里的通知 = 你的收件箱**（audience=ai，默认）：待你处理的事
  （复习到期该找时机提醒、系统异常该分析）。用户看不到这些。
- **侧边栏通知 tab = 用户的收件箱**（audience=user）：你**整理后投递**
  给用户的成品。create 时加 `--audience user`。
- 工作流：读快照收件箱 → 处理（判断时机/在场/整理内容）→ 需要用户
  知道的，create 一条 --audience user 的成品条目（如复习提醒整理成
  "今晚记得复习 N 张卡"）→ 原 ai 条目 resolve。
- 直接给用户的类型默认 user：user-todo（倒垃圾）、task-report（盯更新
  的报告）。系统原料默认 ai：review-due 等。
