# iPad 远程截图问答操作指南

## 链路一览

```
[iPad 拍照 / 选相册图]
   ↓ iOS 快捷指令：转换为 JPEG → base64 编码 → HTTP POST
http://<Tailscale-IP>:9090/qa?key=<cmd_server_API_KEY>
   ↓ cmd_server 收到，body 是 {image_b64: "..."} 或纯 base64
cmd_server 转发到 daemon /api/inject-image
   ↓
qa_browser daemon 解码 → 自动转 PNG（含 HEIC）→ 写到 %LOCALAPPDATA%\bwicarus-client\qa-temp\remote-<ts>.png
   ↓ 注入 state["img_b64"/"img_fname"/"temp_path"]，session.reset()

[iPad 切到 Safari]
http://<Tailscale-IP>:9091
   ↓ 完整 qa_browser 页面，pollScreenshot() 轮询拿到刚注入的图
[输入框打字 → 发送 → fetch /api/chat → AI 回复（同本地 ctrl+shift+q）]
```

## 客户端开关

GUI 截图问答 Tab：
- ✓ 远程访问（必须）— `qa_remote_access: true`
- ✓ iPad 远程截图问答（必须）— `qa_remote_daemon: true`

两个都勾选并保存配置后重启客户端，日志应该出现：

```
✓ qa-daemon: http://<本机IP>:9091  (iPad 远程问答；Tailscale 设备直连此 URL)
```

## 找几个必需信息（按实例分）

服务器 / Pi 都跑 qa-server，**两边端口一样（9090/9091）**，但 host 和 key 不同。
**最简办法**：登录任一实例的控制面板 → 设置 → **「iPad 远程触发」** section，
每行已经拼好完整 URL（含 key），点复制按钮即可。

手动拿 host + key 的对照：

| 信息 | VPS | Pi | Windows 客户端（旧路径，迁移期间还在）|
|---|---|---|---|
| Tailscale hostname | `bwicarus-3.taile44d0c.ts.net` | `bwicarus.taile44d0c.ts.net` | `bwicarus-2.taile44d0c.ts.net` 等 |
| Tailscale IP | `100.110.193.39` | `100.101.15.57` | 装 Tailscale 客户端时分配 |
| API key 文件 | `/root/claude/state/qa-server-data/cmd_server_key.txt` | `/home/bwicarus/claude/state/qa-server-data/cmd_server_key.txt` | `%LOCALAPPDATA%\bwicarus-client\cmd_server_key.txt` |
| qa daemon 端口 | 9091 | 9091 | 9091 |
| cmd_server 端口 | 9090 | 9090 | 9090 |

## iOS 快捷指令配置（推荐流程）

```
1. 拍照（或「选择照片」）
2. 「转换图像」→ 格式：JPEG，质量 80
3. 「编码 Base64」（输出文本）
4. 「获取 URL 内容」
   - URL: http://<TS-HOST>:9090/qa?key=<API-KEY>
   - 方法: POST
   - 请求体: JSON
     {"image_b64": <上一步的 Base64 文本>}
   - 头部: Content-Type: application/json
5. 「打开 URL」
   - URL: http://<TS-HOST>:9091
   - （这一步让 Safari 自动跳转到 qa_browser 完整页面，看到刚才推上去的截图）
```

`<TS-HOST>` / `<API-KEY>` 直接从控制面板「iPad 远程触发」复制完整 URL，
按 `?key=` 切开（前半给 URL 字段，整段也可以直接用，iPad 快捷指令支持
带 query string 的 URL）。

## 排错速查

| 现象 | 可能原因 | 解决 |
|---|---|---|
| POST /qa 返回 403 forbidden | key 错或没带 | 核对 cmd_server_key.txt |
| POST /qa 返回 502 qa-daemon 不可达 | daemon 没启起来 | 检查 GUI「iPad 远程截图问答」勾选 + 重启客户端 |
| Claude API 400 "Could not process image" | iPad 发了 HEIC，daemon 转 PNG 失败 | iPad 端确保「转换图像 → JPEG」步骤在 POST 之前 |
| 浏览器开 :9091 看到截图但发送消息无回复 | AI 后端配错 / Claude CLI 未登录 | GUI AI Tab 检查后端、ping 测试 |
| 浏览器开 :9091 显示截图但下方"等待截图注入" | session 没 reset，pollScreenshot 命中旧的 None | 在网页里点重置 / 刷新 |
| iPad 连 :9091 timeout | 防火墙挡 0.0.0.0 监听 / Tailscale 没连 | Windows 防火墙允许 9091 入站 + 确认 Tailscale 已连 |
| 数学公式里出现方框套圆圈（豆腐块） | 自托管 MathJax 用了 CHTML 版但缺字体文件 | `static/qa/mathjax.js` 换成 SVG 版（见下「自托管前端资源」）|

## HTTPS 访问（语音输入前提 + 去掉「不安全」警告）—— 2026-05-22

QA daemon 自身只监听 **HTTP** `:9091`。但浏览器的 **Web Speech API（语音输入麦克风）
要求安全上下文（HTTPS）**，HTTP 下直接禁用，所以麦克风按钮在 `http://...:9091` 下"按了没反应"。

**解法**：经 nginx 的 HTTPS（Tailscale 证书，443）反代 QA daemon。

- **Pi nginx**（`/etc/nginx/sites-available/bwicarus`，**手工维护、不在 git**）443 server 块加：
  ```nginx
  location /qa/ {
      proxy_pass http://127.0.0.1:9091/;
      proxy_http_version 1.1;
      proxy_set_header Host $host;
      proxy_set_header X-Forwarded-Proto https;
      proxy_set_header Connection "";
      proxy_buffering off;          # SSE 流式必需
      proxy_read_timeout 3600s;
      client_max_body_size 50m;     # 截图/粘贴图
  }
  ```
  访问入口：`https://bwicarus.taile44d0c.ts.net/qa/`（页面相对 API 路径自动走 `/qa/api/...`）。
- **混合内容坑**：HTTPS 页面不能加载 `http://` 脚本。qa_browser.py 里 mathjax/marked 的
  URL 改成**协议相对** `//bwicarus.taile44d0c.ts.net/static/qa/...`，HTTP:9091 与 HTTPS 两种访问都不报混合内容。
- **卡片 QA 链接**：`.env` 的 `QA_PUBLIC_URL` 从 `http://...:9091` 改成
  `https://bwicarus.taile44d0c.ts.net/qa`（`.env` 被 gitignore，**换机/重部署要手动改**）。
  改后存量卡片用 AnkiConnect 批量替换 footer 里的旧链接（findNotes `"问 AI / 改进这张卡"`
  → updateNoteFields 把 `http://...:9091/?card=` 换成 `https://.../qa/?card=` → sync）。
- `:9091` 直连仍可用（只是无语音、有「不安全」警告）；iPad 浏览器书签/快捷指令改用 HTTPS 入口即可。

## 自托管前端资源（MathJax / marked）—— 必须用 SVG 版 MathJax

QA 页的 MathJax / marked 不走 jsdelivr CDN（公网/隐私考虑），由 nginx 从
`/var/www/html/static/qa/` 提供（VPS 同名路径）。

**现状（2026-05-31 核实）**：URL 现在**直接硬编码**在 `_client/core/qa_browser.py`
（约 1592-1593 行），是协议相对的自托管地址、且已是 SVG 版：

```html
<script src="//bwicarus.taile44d0c.ts.net/static/qa/mathjax.js?v=svg1" async id="MathJax-script"></script>
<script src="//bwicarus.taile44d0c.ts.net/static/qa/marked.js"></script>
```

源码里已经 grep 不到任何 `jsdelivr` / `tex-chtml` 字串。

**注意（systemd sed 已成 no-op）**：`references/systemd/qa-server.service`
（VPS 版）的 ExecStartPre 仍有两条 sed，想把
`https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js` /
`.../marked@9/marked.min.js` 替换成 `bwicarus.space/static/qa/...`。但这两个
**目标字串源码里早已不存在**，sed 现在每次都命中 0 处、纯属历史遗留兜底（可删）。

**两实例 host 不一致**：源码硬编码的是 **Pi** 的 `bwicarus.taile44d0c.ts.net`
（协议相对，HTTP:9091 与 HTTPS 都不报混合内容）。VPS 历史上靠那条已失效的 sed
想改成 `bwicarus.space`，如今 sed 无效 → VPS 实际也会去加载 Pi 的 ts.net 静态资源
（协议相对地址跨实例都能取到，只要 Pi 在线）；若要让 VPS 走自己的
`bwicarus.space/static/qa/`，需另行处理（直接改源码或在部署侧 patch）。

**坑（2026-05-22 踩过）**：`mathjax.js` 不能用 **CHTML** 版（`tex-chtml.js`）。
CHTML 渲染特殊字形（如 `\underbrace` 的横花括号 `⏟`）要从
`static/qa/output/chtml/fonts/woff-v2/` 加载字体文件——服务器上只放了
`mathjax.js`、没放字体目录，于是这些字形显示成**方框套圆圈（豆腐块/tofu）**。
普通字母数字靠浏览器后备字体凑合显示，所以"大部分公式正常、个别字符是豆腐"。

**正解**：用 **SVG** 版（`tex-svg.js`）。它把所有字形路径内嵌在 JS 里，
零外部字体依赖，永不豆腐。下载替换即可（文件名仍叫 mathjax.js，配置不用改，
渲染器由加载的 bundle 决定）：

```bash
cd /var/www/html/static/qa
sudo cp mathjax.js mathjax.chtml.js.bak       # 备份
sudo curl -so mathjax.js https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-svg.js
# ETag/last-modified 变了，浏览器普通刷新即可拿到新版，无需 qa-server 重启
```

> `static/qa/mathjax.js` 是手放的静态文件、不在 git，git pull 不会覆盖它，
> 但**重新部署/换机时记得放 SVG 版**，别又抓成 CHTML。注意：现在源码已直接硬编码
> 自托管协议相对 URL（见本节开头），所有入口（含 Windows 客户端、本机 ctrl+shift+q）
> 都加载同一份 `//bwicarus.taile44d0c.ts.net/static/qa/mathjax.js?v=svg1`，
> 不再各走各的 CDN——所以这份自托管文件务必是 SVG 版，否则全端一起豆腐。

## 流式回复 + 历史删除（2026-05-15+）

**SSE 流式**：`/api/chat` 检测 `Accept: text/event-stream` 走流式分支，
回复一段段出（前端 `fetch` + `ReadableStream` 边接边渲染，markdown/MathJax
节流 120ms 重渲染）。sending 时「发送」按钮变「中止」，点了 abort fetch →
后端 `BrokenPipe` → `gen.close()` → 杀 AI 子进程 / 关 HTTP 连接，不烧 token。
5 个 backend：claude_cli / claude_api / openai_api / ollama 真流式，
codex_cli 走 fallback（一次性单 chunk）。旧 JSON 模式（无 Accept 头）保留兼容。

**历史删除**：webapp `/history/` 每条对话 hover 出红色「删除」按钮 →
自定义 confirm modal（列三项清理范围 + 「以后不再提醒」localStorage 开关）→
`POST /api/qa-history/<id>/delete`（webapp cookie 鉴权）→ proxy 到 qa-server
`:9091/api/history/delete` → 级联清：
1. SQLite `conversations` 行
2. `state/qa-server-data/images/<fname>` 截图
3. **对应 Obsidian 笔记**（解析 note 字段 `→ /path.md`，Windows 路径
   fallback 到 vault 习题/错题目录按文件名找）
4. 触发 `_export_history_to_webapp` 同步 webapp data
body 可传 `keep_note:true` 只清 db+截图保留 .md（暂未在 UI 暴露）。

**数据目录**：qa history.db / 截图 / qa-temp 不再在 `~/AppData/Local/截图问答/`
（Windows 风格 fallback），改走 `paths.app_dir()`：服务端实例由 systemd
`BWICARUS_APP_DIR` 指到 `state/qa-server-data/`；Windows 客户端走
`%LOCALAPPDATA%\bwicarus-client\`。模块加载时自动迁移旧路径数据。

## 与本地按 `ctrl+shift+q` 的关系

**两个入口共用同一份 daemon + 同一份 state**：

- 本机 `ctrl+shift+q` 触发 `qa_browser.launch()` → 启动**临时** server（随机端口）并设置 state；这台 server 处理完一次会话就退。
- iPad 走 daemon → 调用 `qa_browser.start_server_daemon()` → **常驻** 9091 server，state 由 cmd_server `/qa` 注入。

**同时使用两个入口可能 state 串扰**（截图字段、AISession messages）。单人多端通常撞不到。

## 已废弃路径（避免误读）

- ~~`bwicarus.space/qa/`~~ — 旧公网展示页面，nginx 反代 2026-05-11 已删
- ~~`POST /qa/update` (服务端)~~ — webapp 路由保留但 nginx 不再转发，外部不可达
- ~~`scripts/cmd_server.py /qa`~~ — 旧 launcher exe 路由，被 `_client/core/cmd_server_thread.py` 取代
