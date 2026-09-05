# 展示板（iOS 小组件上那块方格板）

给「一段时间里要**反复看状态**」的任务用：每日某个方面的新闻、某个产品的发布、
某个长跑任务的实际进展。变化发生时的通知已经另有一套；这块板子解决的是
「我现在想扫一眼它到哪了」。

它是一块**方格板**：每张卡一个方块，一张卡一条信息。**卡片长什么样由你决定** ——
你写一段 HTML（可带页内 CSS），电脑上把它渲成图，小组件与 App 直接显示那张图。
每张卡右上角固定有一个删除键，用户随时能撤掉任何一张。

## 什么时候用（这一条最重要）

**只有用户在任务里明确说了要用展示板，才用。** 不要自己判断该不该开一块板子，
不要"顺手也放一份到板子上"。用户的原话：

> 这个展示板的使用是用户在创建任务时明确说明需要开启的，所以不需要 ai 自行判断
> 是否开启。

同理，**`enable` 这个 op 是用户的开关，你永远不要调它**。用户在 App 里开关，
关掉的板子就不会出现在小组件上 —— 那是他的决定，不是你的。

## 接口

一个端点，POST JSON，body 里的 `op` 决定做什么：

```
POST https://bwicarus-2.taile44d0c.ts.net/reader-board/v1
```

本机上的固定程序也走这个地址（自己的 tailnet 名字），跟设备同一条路、同一套鉴权。

回执一律 `{"ok":true, …}`；失败是 `{"ok":false,"error":"BW_BOARD_…","detail":"…"}`，
`detail` 会说清是哪个字段、超了多少 —— **按它改，不要原样重试**。

### 第一步：注册，拿编码

```json
{"op":"register", "slug":"daily-ai-news", "title":"每日 AI 新闻",
 "note":"用户 9/5 的任务要求", "autoClear":{"kind":"dailyAtLocal","hhmm":"04:00"}}
→ {"ok":true, "code":"bd_1f9c3a7b28d4e05f", "created":true, "enabled":true}
```

- `slug` 是**你自己起的稳定名字**（同一个任务每次用同一个）。
- **register 对同一个 slug 是幂等的**：再注册一次拿回同一个 `code`。所以固定程序
  每次运行都可以先 register 再放卡，**不需要自己保存 code**（自己存就会丢，
  丢了就再也更新不了那块板子）。
- `note` 写一句"这块板子为什么存在"。用户在 App 里看到的就是这句。
- `autoClear` 可选，见下。

### 放一张卡（同 id 再发一次 = 整张替换）

```json
{"op":"card", "code":"bd_…", "id":"status",
 "alt":"当前状态：尚未发布",
 "html":"<div style=\"font:700 20px system-ui;color:#7ee787\">发布看板</div>
         <div style=\"margin-top:8px;font:14px system-ui;line-height:1.6\">
           <span style=\"background:#1f6feb;color:#fff;padding:2px 8px;border-radius:999px\">进行中</span><br>
           官网版本号仍是 4.2
         </div>"}
→ {"ok":true, "code":"bd_…", "id":"status", "sha":"43931a46f573caea", "replaced":false}
```

- `id` 是你给这张卡起的稳定名字。**反复刷同一张卡是常态用法**：拿同一个 `id` 再
  `card` 一次就是整张替换。不给 `id` 会随机生成一个（那就没法再更新它了）。
- `alt` 是这张卡的一句话文字版：图还没渲出来、或设备取不到图时显示它；
  也是用户在 App 里辨认"这张是什么"的依据。**每张卡都写**。
- `html` 见下面的「卡片怎么写」。⚠ 上面例子里「标题 + 徽章 + 两行字」只是示意接口形状，
  **不要把它当版式模板** —— 画面按「画面要丰富，不要套模板」那节设计。

### 整块换掉所有卡

```json
{"op":"cards", "code":"bd_…", "cards":[
  {"id":"headline", "alt":"…", "html":"…"},
  {"id":"detail",   "alt":"…", "html":"…"}
]}
```

### 删一张 / 清空 / 删板子 / 查询

```json
{"op":"cardDelete", "code":"bd_…", "id":"status"}   // 删一张（小组件上的删除键走的就是它）
{"op":"clear",  "code":"bd_…"}                      // 清掉所有卡（板子还在）
{"op":"delete", "code":"bd_…"}                      // 整块删掉
{"op":"get",    "code":"bd_…"}                      // 取回这块板子（含每张卡的 html/sha）
{"op":"list"}                                        // 列出所有板子（不含卡片正文）
```

任务结束、板子不再有意义时 **`delete`** 掉。留一块永不更新的板子在用户桌面上，
比没有更糟。

### 自动消除（可选）

```json
"autoClear":{"kind":"never"}                       // 默认
"autoClear":{"kind":"afterHours","hours":6}         // 某张卡 6 小时没更新就撤掉那一张
"autoClear":{"kind":"dailyAtLocal","hhmm":"04:00"}  // 每天本地 04:00 整块清空
```

- `afterHours` 撤的是**卡**，不是板子 —— 适合"运行状态"这类过期就无意义的内容。
- `dailyAtLocal` 适合"每日新闻"：早上清空，当天重新填。
- 结算发生在**每次读写**时，不靠定时器（小组件不在这个进程里，定时器帮不了它）。

## 卡片怎么写（HTML 规则）

一张卡有**两种形状**：方（320×320 逻辑像素）和宽（640×320），电脑上两种都渲，
小组件按当时放了几张卡挑一种 —— 卡少时用宽卡把面积吃满，卡多时用方卡。
你的 HTML 会被放进一个深色底、浅色字、内边距 14px、内容垂直居中的壳子里，
壳子还会把整段内容**等比放大到刚好填满**（最多 3 倍，只放大不缩小）。
壳子只做这几件事，**不改写你的样式** —— 卡片长什么样是你决定的。

### 画面要丰富，不要套模板

用户 2026-09-05 原话：「我想要的是更丰富的 html 画面而不是固定格式的卡片」。
一张卡是一块**小海报**，不是"标题 + 徽章 + 两行字"。同一块板子上四张卡长得一模一样，
用户一眼就看出是模板。按信息的形状挑画面：

| 信息是什么样 | 画成什么 |
|---|---|
| 一个关键数字（到期张数、剩余天数、价格） | 占半张卡的大数字 + 一行小标签 + 一条纯 CSS 进度条 |
| 几步流程 / 时间点（发车→到达、发布前后） | 横向时间线：flex 排几个圆点，中间 `border-top` 连线，当前节点换色 |
| 周期性的日子（垃圾回收、复习日） | 一周七个小方块，今天高亮、有事的日子标色 |
| 几项并列状态 | 2×2 或 3×2 色块网格，每格一个词 + 一个 emoji |
| 两三列对照 | 迷你 `<table>`，斑马纹，表头加粗 |
| 比例 / 进度 / 趋势 | 内联 `<svg>` 画环形进度或简单折线（只用内联路径，不能引用外部资源） |

三条硬规则：

- **用满整张画布**。要全出血的色块背景就写 `<style>body{padding:0}</style>`；
  尺寸可以用 `:root` 上的 `--card-w` / `--card-h`（逻辑像素）来算。
- **两种形状各渲一次**，同一段 HTML 要在 1:1 和 2:1 里都成立。要区分时用
  `@media (min-width: 600px)`（宽卡）或 `body.shape-wide` / `body.shape-square`。
- **对比度**：深色底上浅色字；色块上的字要么白要么近黑，不要灰。

一个大数字卡（方卡里数字在上、进度条在下；宽卡里数字在左、进度条在右）：

```html
<style>
  body{padding:0}
  .k{box-sizing:border-box;min-height:var(--card-h);display:flex;flex-direction:column;
     justify-content:center;padding:22px;background:linear-gradient(135deg,#1f6feb,#0d419d)}
  .n{font:800 96px/1 system-ui;color:#fff}
  .l{font:600 16px system-ui;color:#cae8ff;margin-top:8px}
  .bar{margin-top:16px;height:10px;border-radius:5px;background:#ffffff33}
  .bar i{display:block;height:100%;width:62%;border-radius:5px;background:#7ee787}
  body.shape-wide .k{flex-direction:row;align-items:center;gap:28px}
  body.shape-wide .n{font-size:128px}
  body.shape-wide .bar{margin-top:0;flex:1}
</style>
<div class="k">
  <div><div class="n">31</div><div class="l">张卡到期 · 已复习 62%</div></div>
  <div class="bar"><i></i></div>
</div>
```

其余规则：

- **放得下就行**：最多 12 张卡（最大号小组件放 8 张），每张 `html` 最多 8000 字。
  超了直接被拒（`detail` 会说超了多少），不会悄悄截断。
- **只用页内样式**（`style="…"` 或 `<style>`）。**外链一律无效**：`src`、`href`、
  `@import`、`javascript:` 入库时就被去掉，渲染环境断网 —— 别指望图片、字体、
  外部 CSS 能加载。要图标就用 emoji、纯 CSS 或内联 SVG。
- **这些标签整段扔掉**：`script`、`iframe`、`object`、`embed`、`link`、`meta`、`base`、
  `form`、`input`、`button`、`textarea`、`foreignObject`、`video`、`audio`、`canvas`。
  `on*` 事件属性一并去掉。内容被整段扔掉的卡会被拒（`html 洗掉之后是空的`）。
  内联 `<svg>` 和 `<math>` 可以用。
- **字号只决定相对比例**：内容会被放大到填满，所以 14px 正文 / 20px 标题这种
  写法照旧可用，要紧的是各部分之间的比例。**写多了不会缩小**，超出的部分直接
  被裁掉 —— 写不下就拆成两张卡。
- **不要放时间戳**。板子自带"数据时刻"，你再写一遍只会占地方，
  两个时间不一致时用户会以为板子坏了。
- 内容变了 `sha` 才变，设备端按 `sha` 取图并长缓存 —— 所以**没变的卡不要重发**
  （重发同样的内容无害，只是白费一次调用）。

## 一个完整的例子（固定程序每小时跑一次）

```json
{"op":"register","slug":"deploy-watch","title":"发布看板",
 "note":"用户要求盯 X 产品发布","autoClear":{"kind":"afterHours","hours":12}}
{"op":"card","code":"bd_…","id":"status","alt":"尚未发布，官网仍是 4.2",
 "html":"<style>body{padding:0}.w{box-sizing:border-box;min-height:var(--card-h);display:flex;flex-direction:column;justify-content:center;padding:22px;background:#0d1117}.t{font:800 28px system-ui;color:#f0b429}.tl{display:flex;align-items:center;margin-top:20px}.tl b{width:20px;height:20px;border-radius:50%;background:#30363d;border:3px solid #8b949e}.tl b.on{background:#f0b429;border-color:#f0b429}.tl i{flex:1;border-top:3px dashed #484f58}.lb{display:flex;justify-content:space-between;font:600 13px system-ui;color:#c9d1d9;margin-top:8px}</style><div class=\"w\"><div class=\"t\">4.3 尚未发布</div><div class=\"tl\"><b class=\"on\"></b><i></i><b class=\"on\"></b><i></i><b></b></div><div class=\"lb\"><span>beta 讨论</span><span>changelog 未更</span><span>正式发布</span></div></div>"}
{"op":"card","code":"bd_…","id":"signals","alt":"两条已确认的信号",
 "html":"<div style=\"font:600 16px system-ui;margin-bottom:6px\">已确认的信号</div><ul style=\"margin:0;padding-left:18px;font:13px system-ui;line-height:1.6\"><li>开发者论坛出现 4.3 beta 讨论</li><li>官方 changelog 未更新</li></ul>"}
```

发布真的发生时：更新 `status` 那张卡，**并按原有通知能力通知用户** ——
板子是"随时可看"，不是"通知的替代品"。用户的原话是「这个作为一个 ai 可选的通知功能
就好」：板子补的是他主动想看的那一刻。
