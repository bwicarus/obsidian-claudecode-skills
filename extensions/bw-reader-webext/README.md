# BW 网页伴读 — Safari/Chromium WebExtension

在**真实网页**(Safari 原生打开,登录/视频/cookie 全不动)里加查词/翻译/解释等学习功能。
是 RBI 远程浏览器方案之外的另一条路(用户 2026-07-20 拍板改试):不代理网页、不搬网页,
只往用户自己的浏览器里注入一个学习工具条,交互全在真浏览器发生。

## 架构(信号流)

```
Safari/Chromium 原网页 —选中文字→ content.js(读选区+标题+URL+附近段落)
   → runtime.sendMessage → background.js(从 storage.local 取 Bearer token)
   → HTTPS → Pi 现有 /pdf/api/* 学习接口 → 页面内 Shadow DOM 结果卡
```

- **content.js**:页面内固定底部工具条(Shadow DOM 隔离)。iOS Safari 不支持扩展 contextMenus,
  所以用工具条不用右键菜单。只在用户主动查词时才发选区,不自动上传整页。
- **background.js**:唯一接触 token 和 Pi API 的地方。只认固定操作名(PING/LOOKUP/TRANSLATE/EXPLAIN),
  网页脚本拿不到 token、也不能传任意 URL(防恶意网页借扩展打内网)。
- **popup**:存/测 Bearer token。

## 已验证(2026-07-20)

三个前提全核实通过 + 核心闭环实测:
- ✅ **无 Mac 打包**为真:iOS 26/iPadOS 26 起,上传 ZIP 到 App Store Connect 自动转换打包
  (仍需付费开发者账号 $99/年)。[Apple 官方](https://developer.apple.com/documentation/safariservices/packaging-and-distributing-safari-web-extensions-with-app-store-connect)
- ✅ **`/pdf/api/*` 认 Bearer token**:app.py `before_request` 的 `_bearer_user()` 已支持,
  扩展 token 无需浏览器 session 即可调受保护 API。
- ✅ **核心闭环实测**:用 Bearer token 调 `/pdf/api/dict-quick?word=vast&file=web:…` → 返回完整查词
  (音标 /vɑːst/、释义、词形、美音音频)。整条 token→接口→查词跑通,不用装扩展/iPad/Mac 就验证了。

## 配置服务器地址

`background.js` 的 `ORIGIN` + `manifest.json` 的 `host_permissions` 指向 **Pi**
(`https://bwicarus.taile44d0c.ts.net`,iPad 走 Tailscale,和现有 QA browser 一样)。
⚠ 不是暂停的 VPS `bwicarus.space`(代码停在 2026-05-28)。换服务器只改这两处。

## 准备 token

1. 浏览器登录 Pi 的 `/profile/`。
2. 创建标签为 `iPad Safari Extension` 的 Bearer token。
3. ⚠ 不要用 MCP token;不要写进源码/ZIP;只存扩展的 `storage.local`。
4. token 权限目前较大,个人 MVP 可用;正式发布前建议加作用域限制。

## 本地测试(Chromium,不需要 iPad/Mac)

```bash
# 静态检查
python3 -m json.tool manifest.json >/dev/null
node --check background.js && node --check content.js && node --check popup.js
```

桌面 Chromium(或系统 `apt install chromium`)→ `chrome://extensions` → 开开发者模式 →
「加载已解压的扩展程序」→ 指向本目录 → 打开任意网页 → 选中文字 → 弹窗存 token → 点查词。

也可用现有 Playwright Chromium(`~/.cache/ms-playwright/chromium-1223/chrome-linux/chrome`)
配 Xvfb 跑自动回归(headless 不支持扩展,要 headed）。

## 接口清单(核对过真实存在)

| 功能 | 接口 | 方法 |
|---|---|---|
| 连通性 | `/pdf/api/ping` | GET |
| 查词 | `/pdf/api/dict-quick` | GET(word/file/page/context)|
| 翻译 | `/pdf/api/translate` | POST |
| 解释 | `/pdf/api/explain` | POST |
| 单词制卡 | `/pdf/api/vocab-anki` | POST |
| 选段制卡 | `/pdf/api/snippets-to` | POST |
| 词组收藏 | `/pdf/api/phrases` | GET/POST/DELETE |
| 词组掌握 | `/pdf/api/phrase-mark` | GET/POST |

网页上下文统一按 `file: "web:<url>"` 传(与阅读器网页模式同构)。

## 打包到 iPad Safari

```bash
zip -r ../bw-reader-webext-0.1.0.zip . -x ".git/*" "*.md"
python3 -m zipfile -l ../bw-reader-webext-0.1.0.zip   # 确认根目录直接是 manifest.json,不多包一层
```

App Store Connect → 新建 App(iOS)→ Safari Web Extension Packager 上传 ZIP → Build → TestFlight
装到 iPad → 设置→App→Safari→扩展 启用并授权网站。

## 实施顺序(风险最低)

1. PING + token(已验证核心链路)
2. 选区 → 查词
3. **尽早打 TestFlight 包**在真 iPad 验证选区/触摸(headless 测不出)
4. 翻译/解释/制卡
5. 词组/掌握标记
6. AI 侧栏/语音上下文
7. 最后才做网页持久高亮

## 待单独设计(不照搬静态 HTML 逻辑)

- **登录态 SPA 正文**(Claude/ChatGPT):Pi 服务端自己抓不到 → 需"用户明确触发、短 TTL、按用户隔离"
  的网页快照接口(而非现在的服务端抓取)。
- **网页持久高亮**:现有 HTML 高亮只存字符偏移,在 React/无限滚动页会漂移 → 正式版要存
  `exact quote + prefix/suffix + DOM path` 多重锚点。
