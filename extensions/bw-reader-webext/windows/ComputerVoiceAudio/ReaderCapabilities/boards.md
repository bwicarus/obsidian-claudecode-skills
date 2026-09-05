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
- `html` 见下面的「卡片怎么写」。

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

一张卡是一个**正方形**（320×320 逻辑像素，2x 渲染），你的 HTML 会被放进一个
深色底、浅色字、内边距 14px、内容垂直居中的壳子里。壳子只做这三件事，
**不改写你的样式** —— 卡片长什么样是你决定的。

- **放得下就行**：最多 12 张卡（最大号小组件放 8 张），每张 `html` 最多 4000 字。
  超了直接被拒（`detail` 会说超了多少），不会悄悄截断。
- **只用页内样式**（`style="…"` 或 `<style>`）。**外链一律无效**：`src`、`href`、
  `@import`、`javascript:` 入库时就被去掉，渲染环境断网 —— 别指望图片、字体、
  外部 CSS 能加载。要图标就用 emoji 或纯 CSS。
- **这些标签整段扔掉**：`script`、`iframe`、`object`、`embed`、`link`、`meta`、`base`、
  `form`、`input`、`button`、`textarea`、`svg`、`math`、`video`、`audio`、`canvas`。
  `on*` 事件属性一并去掉。内容被整段扔掉的卡会被拒（`html 洗掉之后是空的`）。
- **字别太小**：小组件上一张卡只有几厘米。正文 14px 起，标题 18–22px，
  一张卡最多三四行 —— 写不下就拆成两张卡，不要缩字。
- **不要放时间戳**。板子自带"数据时刻"，你再写一遍只会占地方，
  两个时间不一致时用户会以为板子坏了。
- 内容变了 `sha` 才变，设备端按 `sha` 取图并长缓存 —— 所以**没变的卡不要重发**
  （重发同样的内容无害，只是白费一次调用）。

## 一个完整的例子（固定程序每小时跑一次）

```json
{"op":"register","slug":"deploy-watch","title":"发布看板",
 "note":"用户要求盯 X 产品发布","autoClear":{"kind":"afterHours","hours":12}}
{"op":"card","code":"bd_…","id":"status","alt":"尚未发布，官网仍是 4.2",
 "html":"<div style=\"font:700 20px system-ui\">尚未发布</div><div style=\"margin-top:6px;font:14px system-ui;opacity:.8\">官网版本号仍是 4.2<br>下一次检查 1 小时后</div>"}
{"op":"card","code":"bd_…","id":"signals","alt":"两条已确认的信号",
 "html":"<div style=\"font:600 16px system-ui;margin-bottom:6px\">已确认的信号</div><ul style=\"margin:0;padding-left:18px;font:13px system-ui;line-height:1.6\"><li>开发者论坛出现 4.3 beta 讨论</li><li>官方 changelog 未更新</li></ul>"}
```

发布真的发生时：更新 `status` 那张卡，**并按原有通知能力通知用户** ——
板子是"随时可看"，不是"通知的替代品"。用户的原话是「这个作为一个 ai 可选的通知功能
就好」：板子补的是他主动想看的那一刻。
