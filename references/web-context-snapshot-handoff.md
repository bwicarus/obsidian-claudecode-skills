# 网页上下文快照：最终架构与交接（2026-08-06）

从 iPad 上的任意网页，把用户正在看的内容送到 Windows 快照。
2026-08-05/06 期间重写并打通，本文是交接说明。

排查过程中发现的十处静默失败与由此定下的规则，见
[`silent-failure-lessons.md`](silent-failure-lessons.md) —— **改动这条链路前先读那份**。

---

## 一、链路

```
content.js  采集正文与位置
   ↓ postMessage（同页，不跨进程）
call.js     内嵌框，扩展 origin
   ↓ 一次 POST /reader-context/snapshot（无长连接、无握手、无会话）
Windows 桥  覆盖快照，最后一发赢
```

**为什么是 POST 而不是 WebSocket**：一页的上下文用一次就作废。
长连接的全部机制（hello、context-open、会话 id、重连退避）都是为了
让一段对话跨时间存活，这里一样用不上。而 socket 必须由活着的文档持有，
iOS 上每种扩展文档都短命 —— 那才是真正的代价。
POST 不需要任何东西活着：框只需在发送的那一瞬间存在。

**为什么由内嵌框发而不是 content script 直发**：桥的 origin 白名单只认
`safari-web-extension://` 与 Reader 站点，网页 origin 一律 403。
这是整条链路上**唯一真正的硬约束**，其余复杂度都是自找的。

---

## 二、关键设计

### 位置与正文分开对待

| | 性质 | 规则 |
|---|---|---|
| 位置（url/title/page/selection） | **状态** | 每次都必须为真 |
| 正文 | **内容** | 给过一次就不必再挤占助手上下文 |

正文重复时，不是整条不报，而是把正文换成
「正文与 HH:MM:SS 送出的相同，未重复发送」，
并在 `page_context` 标明 `text_source=extension-page-repeat`。位置照常更新。

**原因**：为省掉正文重复而整条不报，位置也跟着陈旧了。
而陈旧的位置不读作"未变化"，它读作"仍在此处" —— 那是另一个论断，有时是假的。
助手一直把快照当作"用户此刻正在看的东西"。

只有位置与正文都没变时才真正跳过。

### 只有聚焦的页面上报

`document.visibilityState` 把每个未最小化的标签都算作 `visible`，
于是用户没在看的标签也在上报 —— 覆盖式下最后一发赢，
曾出现登录页盖掉正在读的文章。

判定加上 `document.hasFocus()`：**区分"用户面前那一页"与"屏幕上碰巧还在的几页"的是焦点，不是可见性。**

### 正文提取

先挑最像文章的子树，再从中取文：

1. **声明式地标优先** —— 页面写了 `<article>` / `role=main` / `<main>` 就不必猜
2. **否则打分** —— 导航是许多短串分布在许多链接里，文章是少量块里的长段落。
   文本长度 ÷ 链接密度即可分开，不需要认识具体站点。链接占比 > 55% 一律判为菜单
3. **取文时跳过装饰** —— `nav/header/footer/aside` 与对应 `role`
4. **回退** —— 提取不足 200 字而整页远多于此，说明选错子树，退回全页

⚠ **取文必须遍历实况树，不能用 `cloneNode`**：
克隆不在文档树里，`innerText` 会退化成 `textContent`，
把 `display:none` 藏起来的内容一并算入 —— 曾出现"提取结果比整页还长"。

---

## 三、诊断

统一通道 `src/bw-probe.js`，**默认关闭**：

```
任意页面加 ?bwdebug=1  →  开启，全站生效（存扩展存储）
任意页面加 ?bwdebug=0  →  关闭
```

新功能接入：

```js
var P = window.__bwProbe;
if (P) P.probe("模块名", "发生了什么");
if (P) P.skip("模块名", "为什么提前返回");
```

桥侧另有两个文件，重启不丢：

| 文件 | 内容 |
|---|---|
| `runtime/reader-context-post.log` | 每次 POST 尝试，含被拒与预检 |
| `runtime/computer-voice-direct.failures.jsonl` | 每次故障 |

---

## 四、Windows 侧（0.1.95）

- `POST /reader-context/snapshot` + **`OPTIONS` 预检**
  ⚠ 缺预检时浏览器报 `Load failed`，看着像网络不通，
  而**真正的请求根本没离开 Safari**，桥的日志一行都没有
- `Access-Control-Allow-Origin` 只回给已通过白名单的 origin，未放宽边界
- Tailscale serve 需要单独的路由：
  `/reader-context/snapshot → 127.0.0.1:43128`
  ⚠ 端点在 43128 上是好的、外部却 404 时，先查这里，这个坑踩过两次

---

## 五、未完成

### 1. 30 秒断连看门狗（Windows 侧，未做）

远程直接断线时走不到 STOP，音频路由不会归还。
目前的归还只覆盖正常挂断路径。
需要：断连 30 秒后，确认确无活跃连接，再恢复默认音频路由。

（`voiceSettled` 那个条件已在 0.1.93 解除 —— 归还路由不再等待 Codex 语音收口，
因为这一侧自 0.1.89 起就不再关闭它了。）

### 2. 每个变化的元素分开上报（用户提出，未做）

当前仍是"位置 + 正文"两类。用户建议更彻底：
**每一个变化的元素各自独立上报**（位置、正文、选区、滚动进度……），
各有各的新鲜度，互不牵连。

现在的两分法已经解决了最要命的那个混淆，但选区仍与位置共用一个签名 ——
选中文字变化会触发一次位置上报，而位置其实没变。

### 3. 临时探针的去留

`content.js` 中的 `probeLine` 调用（采集/找框/投递/正文提取命中率）
目前保留。它们走统一通道、默认关闭，调试时有用。
若认为已无必要可摘除，但**统一通道本身应当留下**。

---

## 六、相关文件

| 文件 | 作用 |
|---|---|
| `extensions/bw-reader-webext/content.js` | 采集、正文提取、投递、焦点判定 |
| `extensions/bw-reader-webext/call.js` | 框内 POST、位置/正文分离、框内诊断 |
| `extensions/bw-reader-webext/src/bw-probe.js` | 统一诊断通道与调试开关 |
| `extensions/bw-reader-webext/package_safari.py` | host 权限（含 Windows 桥这台具名主机） |
| `windows/ComputerVoiceAudio/DirectBridgeServer.cs` | POST 端点、CORS 预检、请求日志 |
| `windows/ComputerVoiceAudio/DirectBridgeAdapters.cs` | 故障留痕 |
