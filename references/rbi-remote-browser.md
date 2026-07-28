# 实况网页 RBI（已退役，保留恢复资料）

> 状态：2026-07-25 按用户选择 4A 退役。PWA 不再抓取、代理或远程渲染第三方网页；
> 普通网页阅读能力由浏览器扩展直接注入原网页。本文余下内容是历史实现与恢复资料，
> 不是当前部署指引。

退役边界：

- `/pdf/web` 与旧 `/pdf/html/view?file=__web__` 返回书架；
- `/pdf/web/live?url=` 只在 URL 是无凭据、无控制字符的 `http(s)` 地址时，短期兼容跳转到原站；
- `/pdf/web/proxy`、`/pdf/web/frame`、`/pdf/web/p/*`、`/pdf/web/r/*`、`/pdf/web/res`、
  `/pdf/api/web-fetch`、`/pdf/api/web-cookie`、`/pdf/web/rbi`、`/pdf/web/rbi-live` 和
  `/pdf/api/rbi-ticket` 不再执行，返回 `410 pwa_web_reader_retired`（代理传输端点也可能先被旧
  capability 门禁以 `403` 拒绝）；
- 本地 HTML/Markdown 阅读器、`html-highlights`、网页翻译/生词/词典/助手/图片代理等通用服务保留；
- `web:<URL>` 资料身份和已有网页缓存保留为只读迁移来源，不再由 PWA 产生新抓取缓存。

退役前已做非破坏性备份：

- 位置：`state/retired-web-backups/20260724T155933Z/`
- 归档：`pwa-web-rbi-state.tar.gz`（约 72 MB）
- 清单：`inventory.tsv`（4,326 个文件）与 `original-files.sha256`
- 校验：`archive.sha256` 已通过

原始 `state/rbi-profile*`、`state/web-cookies`、`state/web-cache`、`state/web-rescache`、
`state/web-trcache` 和 `state/web-last*` 均未删除或清空。恢复前应先核对清单与哈希，不要直接
覆盖当前状态。

## 历史方案：rrweb 选型与分阶段实施

> 2026-07-19 定案。用户诉求:iframe 代理"服务端假装浏览器"打不过认证/每站打补丁,要换成
> **Pi 跑真 Chrome 执行页面 → DOM 流式桥接到 iPad**。经调研选型 + Pi 实测,定 **rrweb live mode**。

## 为什么是 rrweb(选型)

7 条判据:①保留 DOM(查词/选区前提)②流式增量 ③真浏览器身份(过 Cloudflare/登录态)
④自托管(数据不出 Pi)⑤arm64/8GB ⑥集成现有 web-adapter ⑦成熟活跃。

**唯一同时满足 ①∧②∧③ 的只有 DOM-mirroring 学派 = rrweb**:

| 方案 | ①DOM | ②流式 | ③真浏览器 | ④自托管 | 结论 |
|---|---|---|---|---|---|
| **rrweb live mode + CDP Input 回传** | ✅真DOM重建 | ✅快照+mutation增量 | ✅Pi真Chrome | ✅MIT | **首选** |
| 渲染代理(Playwright 渲染吐整页) | ✅ | 🟡近似非增量 | ✅ | ✅ | 备选/过渡/降级档 |
| Neko/KasmVNC/Selkies/CDP screencast | ❌只有像素 | 🟡画面帧 | ✅ | ✅ | **①否决**(查词失效) |
| Menlo/Cloudflare NVR/Webfuse/Browserbase | 部分 | ✅ | ✅ | ❌闭源SaaS | **④否决** |
| CDP DOMSnapshot 轮询 + morphdom | ✅ | 🟡轮询粗且耗 | ✅ | ✅ | 兜底 |

- **像素流派**(Neko/Kasm/screencast)结构性丢 DOM → web-adapter 的 getContext/captureSelection
  无处附着,查词整链报废;且 Pi5 无硬件 H.264,软编 1080p 吃满 ~80% 四核。一票否决。
- **业界反信号需辩证**:Steel.dev / Browserbase 放弃 rrweb 改 WebRTC 像素——但那是**服务 AI agent**
  (agent 不需要客户端 DOM);我们需求相反(要 DOM 查词),所以它们的转向**不适用**,反而印证
  "要可查 DOM 只能走 rrweb"。**Webfuse** 证明"远程真浏览器 + 客户端真 DOM"商业成立 →
  我们就是**自托管版 Webfuse**。
- rrweb:19.7k★,v2.1.0 活跃,PostHog/Sentry/Highlight 三大厂生产 fork 背书;MIT 纯 JS 零原生依赖。

## Pi 实测(6 脚本全跑通,verdict=可行)

- **保留 DOM + 可选中**:Replayer 把 4MB Cat 快照重建成 **13403 元素**真 DOM,在重建 iframe 里
  成功选中正文("The cat (Felis catus)…")→ 查词前提坐实。
- **流式增量**:注入的 DOM 变更被增量应用(13403→13565);live-mode(startLive+addEvent)边收边应用。
- **交互闭环**:CDP `Input.dispatchMouseEvent/dispatchKeyEvent` 打字进真 Chrome → 页面真实响应
  (#echo→"ECHO:hello")→ rrweb 捕获这次交互的 14 个增量 → 闭环成立。
- **内存**:浏览器基线 258MB + 每 heavy tab ~330MB = **585MB/tab**;8GB Pi(available 4.5GB)→
  **安全并发 1-2 会话**。
- **重建性能**:construct 8-9ms,整帧渲染 265ms,叠 30 增量共 1.4s。
- **快照体积**:CSS 重页(Cat,inlineStylesheet)4.1MB;example.com 2.7KB。初始快照是首屏延迟/带宽主成本。

## 架构 + 设计不变量(用户拍板)

```
Pi 真 Chrome(Playwright 驱动,带 profile 过 CF)
  └ 注入 rrweb record → fullSnapshot + mutation 增量事件
       │ (下行:DOM 事件流,复用 reader_events SSE 或 WS)
       ▼
iPad rrweb Replayer(liveMode)→ 在 iframe 里重建真 DOM
       │ (上行:仅"网页原生交互"回传)
       ▼
Pi CDP Input.dispatch → 真 Chrome 执行 → 新 mutation 顺下行流回(闭环)
```

**不变量 1 — 镜像层 = 纯网页 DOM**:Pi↔iPad 用 rrweb 同步的**只有网页本身**。

**不变量 2 — 功能层 = 客户端独立叠加**:高亮/翻译双语/选区工具条是叠在重建 DOM 之上的
**我们自己的层**,直连我们的 API,**既不进 rrweb 同步、也不回传 Pi 浏览器**(高亮是学习痕迹不是网页的一部分)。

**不变量 3 — 事件分流**(用户洞察,砍掉 rrweb 唯一难点的大部分):
- **网页原生交互**(点 `<a>`/`<button>`/`<input>`/表单/SPA 内部)→ 回传 Pi,CDP 派发,等网页反应;
- **学习功能**(选区/查词/高亮/翻译/制卡)→ **完全跳过 Pi 浏览器**,客户端直处理 + 直连我们 API,**零延迟**。
- 大部分高频操作是查词/高亮/翻译 → 零延迟;只有真·网页交互才走那个回传来回。
- 这让"要自建的上行交互"只覆盖网页原生交互一小部分,不是所有交互。

## 要自己补的最小部分(rrweb 已免费提供快照+mutation序列化+DOM重建+liveMode buffer)

1. **上行交互回传**(唯一真难点,但被事件分流缩小):iPad 捕获网页交互 → WS 上行 → Pi
   `page.context().newCDPSession()` 握 Input 域 dispatch。**不自写 WS JSON-RPC 栈**(Playwright 已有 CDP)。
2. **视口坐标映射**:replayer iframe 视口 ↔ 真浏览器视口(缩放/滚动偏移换算)。
3. **两条通道**:下行 DOM 流(复用 `reader_events.py` SSE 总线);上行交互(加 WS,注意 SSE/WS 每流
   独占线程,受 reader_events 舱壁保护,别多开——见 [[sse-thread-starvation]])。
4. **web-adapter 跨 iframe 适配**:内容在 replay iframe 内,getContext/captureSelection 加一层跨 iframe
   转发(复用非重写)。

## 分阶段实施

- **阶段 0(已有)**:`scripts/rbi_render.py` + `/pdf/web/rbi`(subprocess 渲染快照塞 iframe)= 渲染代理雏形,留作**降级档**(canvas/video 重页走它)。
- **阶段 1 — rrweb 单向骨架**:Pi 常驻真 Chrome(persistent context 过 CF)+ 注入 record + 下行流 +
  iPad Replayer 重建 + **查词/翻译照常**(不变量 2)。网页导航先用现有剥壳(点链接→新 URL→重开会话)。
  ✅ 此阶段已能:过 Cloudflare、看到完整动态页、查词翻译、图片正常(资源经真 Chrome)。
- **阶段 2 — 交互回传**:CDP Input 派发,网页原生交互(按钮/表单/SPA)回传(不变量 3 落地)。
- **阶段 3 — 降载/护栏**:rrweb sampling/blockClass/只录可视区抗 mutation 洪泛;并发 1-2 会话上限;
  canvas/video 页自动降级到阶段 0 渲染代理。

## 阶段 1a/2 已实施(2026-07-19,交互回传打通)

Pi 端 `_server_deploy/rbi_server.py`(独立 asyncio 进程,systemd `rbi-server`,WS 8769,nginx `/rbi-ws`)+
前端 `_server_deploy/templates/rbi_live.html`(`/pdf/web/rbi-live?url=`)。入口:iframe 代理版顶栏「🖥」切换。

**交互回传(把 rrweb"录像回放"改造成"能交互浏览器",这是选型说的唯一要自建部分)** —— 每一步都真机反馈驱动:
- **点击不用坐标用 node id**:坐标因客户端/Pi 布局差错位(Main_Page 客户端 y=115/Pi y=14)。改
  客户端 `replayer.getMirror().getId(节点)` → 回传 → Pi `window.__rbiMirror.getNode(id).click()`
  (`__rbiMirror = rrweb.record.mirror`,roundtrip 实测 true)。
- **id 必须一致 = 每页单次 record**:重复注入 record(open 手动 + load 钩子)重置 id 空间 → 客户端 id
  ≠ Pi id → 全错。改 `domcontentloaded` 钩子每页单次录(去掉 open 手动录)。
- **表单/搜索当请求转发**(用户拍板,比逐字键盘可靠):GET 表单回车 → 客户端收集具名字段构造搜索 URL
  → Pi `open`(复用跳转链路);POST/SPA → `submitform`(getNode 设值 + form.requestSubmit)。逐字 `setinput` 同步。
- **滚动 = iframe 撑到内容全高 + 外层 #stage 滚**:rrweb 固定 iframe=录制 viewport(900)、内容几万 px
  靠 iframe 内滚(iOS 坑 + rrweb 锁定)。`startFit()` 定时把 iframe.height 设成内容 scrollHeight。
- **无限滚动 = 滚动位置同步 Pi**:客户端滚 #stage,Pi 不知道 → 不触发懒加载。`#stage` scroll → 回传 y
  → Pi `window.scrollTo(0,y)` → 页面 JS 触发加载更多 → 新内容 record 流回 → iframe 变高(实测 quote 10→50)。

**关键坑(实测锤出)**:
- rrweb Replayer 回放模式给 iframe 设 `pointer-events:none` → 真实点击进不去;强制 `auto` + 假鼠标层 `none`。
- **CSP 挡 rrweb 注入 → 白屏零事件**(ddg-lite/GitHub/搜索引擎):`launch_persistent_context(bypass_csp=True)`。
- MB 级事件不能当 `page.evaluate` 参数(aarch64 CDP 序列化卡死);`expose_binding` + `JSON.stringify` 字符串回传 OK。
- context 崩溃/被杀要自愈(`_ctx_alive` + open 重试重建);⚠ 清理测试残留别 `pkill -9 headless_shell`(会杀服务的浏览器)。
- rrweb 重建元素 `innerText` 为空(不在渲染树),取文本/选区用 `textContent`/Range。

## 账号与网络隔离护栏(2026-07-24 已实施)

RBI 不能把普通 App session 直接等同于浏览器 profile 身份，更不能信前端传来的 `uid`。当前唯一
身份链如下：

1. 已登录页面向 `POST /pdf/api/rbi-ticket` 请求票据；请求必须带 `X-BW-RBI: 1`，接口**不接收 uid**，
   只按当前服务端 session 签发，并在签发当下查询该 uid 仍存在；删除账户留下的旧 session 不能
   续签。响应为 `Cache-Control: no-store`。
2. HTML 不嵌入 bearer 票据。页面每次连接/重连前都从上述接口取得新的 `rbit-v1` 短期票。
3. WebSocket 首帧必须是 `{"cmd":"auth","ticket":"rbit-v1..."}`。服务端验证成功后才返回
   `{"t":"ready"}`；后续 `open`/交互消息不再包含、也不再读取客户端 uid。
4. 票据 HMAC 绑定一个正整数用户 id、独立 nonce 与独占式过期时间；默认有效 300 秒，验证端拒绝
   已过期票和超过 900 秒未来窗口的票。共享密钥优先读 `READER_RBI_SECRET`，未配置时由两个进程
   原子创建/共用 `state/rbi-ticket-secret`（0600），并处理 Flask 与 RBI 同时首次启动的竞争。
   nonce 在首帧认证时原子消费且只能使用一次；连接保留完整 claims，达到独占式过期点会主动关闭，
   不是只在握手时检查一次。
5. 验证结果生成不可变 `RbiIdentity`；`state/rbi-profiles/<uid>` 和
   `state/web-cookies/<uid>.json` 只能由这个身份构造。目录为 0700，并拒绝路径穿越及指向其它
   账户文件/目录的符号链接。由此不同登录账户不会误用彼此的 Chrome profile 或导入 cookie。
   `state/rbi-profile/<uid>` 旧目录只读复制到统一的复数目录，不回写旧目录。live 在 Chromium
   context 全生命周期持有 `.locks/<uid>.lock` 跨进程锁；demo 若锁空闲直接复用主 profile，若
   live 正占用则复制同 uid 的 0700 临时 profile（跳过 symlink、特殊文件及 Chromium 锁文件），
   结束清理且不回写主 profile。

WebSocket 还必须满足来源边界：

- 默认只接受 `Origin` 为 HTTPS，且规范化后与握手的 `Host`（优先 `X-Forwarded-Host`）完全一致；
  缺失、重复/异常 header、跨站 Origin 均 fail-closed。
- 测试或本地 HTTP 环境必须显式设置 `READER_RBI_ALLOWED_ORIGINS`；生产也可把它设为正式 origin
  （多个值以逗号分隔），不能用通配符。
- 入站 WS 帧限制为 64KB；rrweb 大快照是服务端下行，不需要放大未认证入站上限。

`_server_deploy/rbi_access.py` 是常驻 RBI、阶段 0 `scripts/rbi_render.py` 与 HTML 入口共用的唯一
公网判断实现：

- 顶层 `open`/`nav` 只接受公网 `http(s)`，拒绝 `file:`、localhost、内网/链路本地/保留地址、
  内嵌账号、控制字符、反斜杠解析歧义和异常端口。
- DNS 的**所有**返回地址都必须是 global；公共地址与私网地址混合也整体拒绝。
- Playwright context 启动时先 `offline=True`，安装 HTTP 请求、重定向、子资源和 WebSocket
  守卫、禁用 service worker、关闭恢复出来的旧 tab 后才联网，避免 profile 恢复窗口绕过守卫。
- Chromium 保持正常 sandbox 与 site isolation；不得恢复 `--no-sandbox`、
  `--disable-setuid-sandbox` 或禁用站点隔离的启动参数。
- 每一次跳转、重定向和子请求都重新经过共享守卫；`ws/wss` 单独由
  `route_web_socket` 检查。`data:`/`blob:`/`about:` 只作为不出网的浏览器本地资源放行。

### 部署要求

- `html_reader.py`、`rbi_server.py` 部署时必须同步 `_server_deploy/rbi_access.py` 和
  `_server_deploy/web_cookie_store.py` 到同一 Python import 路径；漏掉任一文件应视为部署失败，
  不能退回信任 uid 或父域 Cookie 扩展的旧协议。
- Flask 与 `rbi-server` 必须使用同一个 `CLAUDE_PROJECT`/共享密钥来源。更新后同时重启 webapp 与
  `rbi-server`，旧页面会自动刷新短票并按新协议重连。
- nginx 的 `/rbi-ws` 反代必须保留当前站点 Host，至少设置
  `proxy_set_header Host $host`（建议同时设置 `X-Forwarded-Host $host`）；否则为 systemd 服务显式
  配置 `READER_RBI_ALLOWED_ORIGINS=https://正式域名`。正式环境不允许加入 HTTP 或第三方 origin。

### 离线验证

- `tests/test_rbi_access.py` 覆盖票据签名/过期/跨 uid、nonce 并发单次消费、到期主动断线、并发首次
  密钥创建、profile/cookie 路径穿越与符号链接、跨进程 profile 锁与 demo 临时回退、
  `file`/localhost/私网/混合 DNS、重定向与 WebSocket 请求守卫、Origin/Host、当前安装的
  websockets 握手 API、Flask session 票据刷新/删除用户拒签及页面不发送 uid；当前共 22 项。
- 联合回归：
  `python3 -m unittest -v tests.test_rbi_access tests.test_web_proxy_capability
  tests.test_reader_private_storage tests.test_reader_provider_identity`；另以 `py_compile` 检查
  `rbi_access.py`、`rbi_server.py`、`html_reader.py`、`web_cookie_store.py` 与
  `scripts/rbi_render.py`。

**下一步**:阶段 1b 查词/翻译接到重建 DOM(选区不回传 Pi,客户端直连 API——不变量 2);真机继续打磨延迟/手感。

## v1 达不到的(诚实)

- **视频/canvas/WebGL**:rrweb 记录盲区 → 这类页面退渲染代理或 embed(视频本就走官方 embed)。
- **首屏非逐字节渐进**:要等第一个 fullSnapshot 整包到达(~265ms-1.4s)才起画,之后才是真增量。
- **交互一个 round-trip 延迟**(网页交互;学习功能不受影响)。
- **并发 1-2 会话**(单用户 OK,多用户要排队)。

## 实测踩坑(集成时必避)

- ⚠ **绝不能把 MB 级事件/快照当 `page.evaluate` 参数传** —— aarch64 Playwright 的 CDP 参数序列化会
  无限卡死(4MB 卡到 timeout);改用 `add_script_tag(content=…)` 或走 WS(生产本就走 WS,天然规避)。
- `inlineStylesheet:true` 让 CSS 重页快照膨胀到 4MB;用 sampling/blockClass/只录可视区控制。
- live-mode `addEvent` 按事件原始时间戳排程,别批量补灌陈旧事件(只会部分应用)。
- 浏览器子进程继承 python stdout fd,管道到 head 收不到 EOF 会假死;重定向到文件 + 每轮 pkill 清理。
- 静态资源由 nginx/443 服务(不是 :5000),rrweb bundle 要部署到 `/var/www/html/static/pdf/`。

延伸:[[epub-reader-architecture]](统一控制层铁律)、[[sse-thread-starvation]](通道舱壁)、
[[prefer-big-company-solutions]](优先成熟方案)。
