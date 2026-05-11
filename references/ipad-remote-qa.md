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

## 找几个必需信息

| 信息 | 位置 |
|---|---|
| Tailscale IP（本机）| `tailscale ip -4`，或 Windows `ipconfig` 找 100.x.x.x |
| cmd_server API key | `%LOCALAPPDATA%\bwicarus-client\cmd_server_key.txt`（或旧位置 `%LOCALAPPDATA%\截图问答\cmd_server_key.txt`）|
| daemon 端口 | 9091 |
| cmd_server 端口 | 9090（配置在 cfg `cmd_server.port`） |

## iOS 快捷指令配置（推荐流程）

```
1. 拍照（或「选择照片」）
2. 「转换图像」→ 格式：JPEG，质量 80
3. 「编码 Base64」（输出文本）
4. 「获取 URL 内容」
   - URL: http://100.99.9.124:9090/qa?key=<把 key 粘在这>
   - 方法: POST
   - 请求体: JSON
     {"image_b64": <上一步的 Base64 文本>}
   - 头部: Content-Type: application/json
5. 「打开 URL」
   - URL: http://100.99.9.124:9091
   - （这一步让 Safari 自动跳转到 qa_browser 完整页面，看到刚才推上去的截图）
```

替换 IP 为你实际的 Tailscale IP。

## 排错速查

| 现象 | 可能原因 | 解决 |
|---|---|---|
| POST /qa 返回 403 forbidden | key 错或没带 | 核对 cmd_server_key.txt |
| POST /qa 返回 502 qa-daemon 不可达 | daemon 没启起来 | 检查 GUI「iPad 远程截图问答」勾选 + 重启客户端 |
| Claude API 400 "Could not process image" | iPad 发了 HEIC，daemon 转 PNG 失败 | iPad 端确保「转换图像 → JPEG」步骤在 POST 之前 |
| 浏览器开 :9091 看到截图但发送消息无回复 | AI 后端配错 / Claude CLI 未登录 | GUI AI Tab 检查后端、ping 测试 |
| 浏览器开 :9091 显示截图但下方"等待截图注入" | session 没 reset，pollScreenshot 命中旧的 None | 在网页里点重置 / 刷新 |
| iPad 连 :9091 timeout | 防火墙挡 0.0.0.0 监听 / Tailscale 没连 | Windows 防火墙允许 9091 入站 + 确认 Tailscale 已连 |

## 与本地按 `ctrl+shift+q` 的关系

**两个入口共用同一份 daemon + 同一份 state**：

- 本机 `ctrl+shift+q` 触发 `qa_browser.launch()` → 启动**临时** server（随机端口）并设置 state；这台 server 处理完一次会话就退。
- iPad 走 daemon → 调用 `qa_browser.start_server_daemon()` → **常驻** 9091 server，state 由 cmd_server `/qa` 注入。

**同时使用两个入口可能 state 串扰**（截图字段、AISession messages）。单人多端通常撞不到。

## 已废弃路径（避免误读）

- ~~`bwicarus.space/qa/`~~ — 旧公网展示页面，nginx 反代 2026-05-11 已删
- ~~`POST /qa/update` (服务端)~~ — webapp 路由保留但 nginx 不再转发，外部不可达
- ~~`scripts/cmd_server.py /qa`~~ — 旧 launcher exe 路由，被 `_client/core/cmd_server_thread.py` 取代
