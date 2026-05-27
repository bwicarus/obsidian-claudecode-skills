# 截图问答（QA Browser）功能详解

源码：`_client/core/qa_browser.py`（HTML/JS 内嵌字符串）。
入口：本机 `ctrl+shift+q`（临时进程）或服务器 systemd `qa-server.service`（常驻 :9091 + :9090）。
iPad 用法见 [`ipad-remote-qa.md`](ipad-remote-qa.md)。

## 两种模式

| 模式 | 触发 | 用途 |
|---|---|---|
| **普通模式** | 默认进入；URL 无 `?card=` | 临时问答，可选择把 AI 回答整理成新笔记 |
| **cardCtx 模式** | URL `?card=<local_id>`（Anki 卡片复习时点链接进入） | 用 AI 回答改进卡片或源笔记 |

## AI 回复 + 选中机制

每个 AI 回答（assistant 气泡）下方自动加：

1. **`＋ 选用整条回答`**（气泡外侧）—— 把整条回答作为有用内容
2. **`+` 圆按钮**（每个标题旁）—— 单段选中

### 标题识别（2026-05-26 扩展）

- **真标题**：`<h1>~<h6>` —— 级联选择（点大标题，旗下小标题一起选）
- **假标题**：`<p>` / `<li>` 里只含一个 `<strong>` 且 strong 文本占 ≥85% 段落内容 —— 单段选中
  - 原因：AI 经常用 `**1. xxx**` 粗体段落代替 `## xxx` 当节标题
  - 处理：addHeadingPickers 给假标题加 `fake-head` class + `+` 按钮

### 选中后的去向

- 整条 ✓ + 选中 → 取整条回答（`md.dataset.raw`）
- 只勾标题 + 选段 → 取 `collectSelectedSections(md)`，按选中标题/假标题段拼接

## 普通模式：创建新笔记（2026-05-26 加）

任何 AI 回答勾选后，header 区出现 **`📝 创建新笔记`** 按钮：

```
用户 prompt 输入笔记名（如 "F^S 向量空间的本质"）
        ↓
POST /api/create-note  body={name, pairs, image_b64}
        ↓
_create_note_from_qa（_client/core/qa_browser.py:_card_update_note 旁）
  ├─ 清理 name → vault/<name>.md（同名追加 -1/-2）
  ├─ AI 整理 pairs → 结构化 Markdown
  │   prompt 要求：去对话冗余、## 标题、$...$ 数学、不发挥
  ├─ 保存截图到 vault/attachments/<name>.png（如果有），笔记顶部加 ![[]]
  └─ 写文件 → 返回 obsidian://open?vault=...&file=...
        ↓
前端弹出："✓ 笔记已创建" + "📂 在 Obsidian 中打开" 按钮
```

**关键**：笔记**不带 `[0-9A-Fa-f]{3}-` 前缀** → 不被 `pending_notes.py` 扫到 → **不触发 register**。算"草稿型"笔记，用户可以后续重命名加前缀让其进入正式流程。

## cardCtx 模式：改进 Anki 卡 / 源笔记

参数：`/?card=<local_id>` → `loadCardContext()` 拉卡片两面 + 显示在截图位置。

勾选有用回答后，header 出现三个按钮：
- **更新到笔记**：`_card_update_note(local_id, pairs, mode='detailed'|'concise')` → AI 改写源笔记
- **根据此修改 Anki**：`_card_update_anki(local_id, pairs)` → AI 生成新卡替代原卡（删/留旧卡由 server-config 控制）
- **全部更新**：两个都跑

异步实现：后端立即返回 `job_id`，前端轮询 `/api/card-job/<job_id>` 拿结果（移动端连接断了不丢）。

### 笔记改写两种模式（2026-05-27）

「更新到笔记」前先弹小选择：

| 模式 | 行为 | 适用 |
|---|---|---|
| **详细**（默认） | 默认保留 ④ 全部原文 + 在合适位置追加 / 改写 QA 内容；prompt 强调"有判断力"——允许重组顺序、合并相似段、连贯化跨段衔接（应对用户选了**间断**段落）；不强制逐字照搬 | 原笔记内容多、QA 是详细解析 |
| **精炼** | 允许大幅删减，提炼核心；同样有判断力可改写但不损失关键信息 | 原笔记太啰嗦想压缩 |

两种模式共用 `_card_update_note`，传 `mode` 参数。返回里加"保留率"百分比（新内容 / 旧内容长度比）让用户验证 AI 没过度精炼。

**prompt 进化历史**（按时间）：
1. 初版：要求 AI 严格保留所有原文 → 用户反馈"过度精炼"
2. v2：默认保留全部 + 把 QA 信息追加到合适位置 → 仍偶尔大改
3. **v3（当前）**：「有判断力」prompt——不强制 verbatim，但**信息完整性是硬性要求**；明确允许：
   - 重组顺序（QA 选段可能不按原文流）
   - 合并相似/重复段
   - 连贯化跨段衔接（用户选的是间断段，AI 要补衔接句）
   - 详细模式下：不能删原文段，只能改写
   - 精炼模式：能删但要标注"已精简"位置

写回前哈希覆盖（`state/note-states.json` 的 hash 更新），防止下次 register 把新内容当"已修改"重新跑 summarize/connect 流程。

## AI 后端 + System Prompt

`_AdapterSession`（`ai_client.py` 内）每次 send 重新读 `ai_settings.json`，支持切 backend 不重启。

`_SESSION_PROMPT`（2026-05-26 强化）：
```
你是一个截图问答助手。根据随附截图和对话历史回答用户问题。
只回答问题本身，不要修改文件，不要运行命令，不要描述你的系统环境。
**数学公式严格用 Markdown 数学语法**：行内公式 $...$，行间公式 $$...$$；
**不要**用反引号 ` 包裹数学表达式（前端会被当 inline code 灰底显示而非渲染公式），
也不要用 \(...\) 或 \[...\]。
例如：要写 $F^S$ 而不是 `F^S`，要写 $a_1, \ldots, a_n$ 而不是 `a_1,...,a_n`。
```

## SSE 流式

`/api/chat` 当 `Accept: text/event-stream` 时启用 SSE：
- 后端 `ad.chat_stream(msgs)` 逐 chunk yield
- 前端节流渲染（每 120ms re-marked + MathJax 一次）
- 流结束后跑 `addHeadingPickers(mdEl)` 给标题补 `+` 按钮（无 cardCtx 检查；2026-05-26 修复，之前漏判会让创建笔记按钮拿不到 pairs）

## 数据存储

- `BWICARUS_APP_DIR/qa.sqlite` —— 历史对话（QA session）
- `BWICARUS_APP_DIR/screenshots/` —— 截图 PNG
- 服务器实例：`/home/bwicarus/claude/state/qa-server-data/`
- 历史导出：`WEBAPP_HISTORY_DIR=/home/bwicarus/webapp/data/users/bwicarus/history/`（用户 dashboard 能看历史）

## 关键路由（do_POST in qa_browser.py）

| 路径 | 用途 |
|---|---|
| `/api/chat` | 主对话（SSE 流式 + 旧 JSON 兼容） |
| `/api/inject-image` | iPad 远程注入截图 |
| `/api/card-context` | 拿卡片两面（cardCtx 模式初始化） |
| `/api/card-update` | 卡片改进（async job） |
| `/api/card-delete` | 删卡 |
| `/api/create-note` | **普通模式：创建新笔记**（2026-05-26 加） |
| `/api/save` / `/api/discard` | 保存对话到 vault / 弃稿 |
| `/api/reset` | 清空 session |
| `/api/history/*` | 历史侧边栏 |
| `/api/search-related` | 「关联知识」按钮：搜 KG 关联节点 |

## iPad 远程入口

```
浏览器看页面：     http://<Tailscale-IP>:9091/
POST 截图注入：    http://<Tailscale-IP>:9090/qa?key=<KEY>
触发 register / daily：  http://<Tailscale-IP>:9090/run/<cmd>?key=<KEY>
```

详见 [`ipad-remote-qa.md`](ipad-remote-qa.md)。

---

## 数据存储详解

### SQLite schema（`<APP_DIR>/qa.sqlite`）

```sql
CREATE TABLE history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  created_at TEXT NOT NULL,           -- ISO 时间
  title TEXT NOT NULL,                -- 用户问题的首句作摘要
  messages_json TEXT NOT NULL,        -- 整条对话 [{role,text,image?}, ...]
  image_filename TEXT,                -- 关联截图（在 HIST_IMG_DIR）
  note TEXT,                          -- 保存到 vault 时的笔记路径（相对 VAULT）
  card_id TEXT                        -- cardCtx 模式下的 anki local_id
);
```

### 截图文件

存 `<APP_DIR>/screenshots/<sha1>.png`，文件名是图片二进制 sha1（去重）。

### 历史侧边栏（删除级联）

GUI：`/history-btn` 按钮 → 滑出 sidebar 列出最近条目。每条带「打开 / 删除」。

**删除是级联的**：`POST /api/history/delete` 内部 `_delete_history(hid)`：
1. SQLite `DELETE FROM history WHERE id=?`
2. 截图文件 `HIST_IMG_DIR/<file>.png` unlink（如果 ref count=0）
3. **Obsidian 笔记**：如果 entry 有 `note` 字段（保存到 vault 的笔记），`vault/<note>` 也 unlink
4. 触发 `_export_history_to_webapp()` 同步到 webapp `WEBAPP_HISTORY_DIR`

**踩坑**：早期版本不级联 → 删历史后留下孤儿截图 + 孤儿笔记。现在 daemon 启动会扫一次。

---

## 快捷功能

### 粘贴图片

`document.addEventListener('paste', ...)`：捕获 clipboard 图片项 → 转 base64 → 缩略图显示在 `#paste-row`。下条消息发送时随 `image_b64` 一起 POST 到 `/api/chat`。

### 关联知识搜索（`🔍 关联知识`）

`/api/search-related` (POST)：
1. 从当前对话取最近 user message 作 query
2. 调 `find_related_cards(note_names, match=query)`：从 anki records 找最相近的卡
3. 返回 markdown 渲染的卡片列表 + obsidian:// 链接

### 快捷按钮（`quick-bar`）

底部一排自定义按钮（`<api/qbtns>` 增删改）。每个按钮带文本，点击后填到输入框（不自动发送）。常用问句模板（如"解题思路是什么？"、"详细解释"等）。

### 全局快捷键

| 键 | 行为 |
|---|---|
| `Enter` | 发送 |
| `Shift+Enter` | 换行 |
| `Ctrl+Shift+Q`（本机，hotkey.py） | 启临时 QA browser（截图问答） |

---

## 数学公式渲染节流

SSE 流式时每 chunk 到达都会更新 innerHTML，**MathJax 重新 typeset 比 markdown 慢得多**，所以加节流：

```js
const RENDER_MS = 120;   // 每 120ms 重渲染一次
function maybeRender() {
  const now = Date.now();
  if (now - lastRender >= RENDER_MS) { renderNow(); }
  else if (!renderQueued) {
    renderQueued = true;
    setTimeout(renderNow, RENDER_MS - (now - lastRender));
  }
}
```

`renderNow()` 流程：`mdEl.innerHTML = renderMd(accumulated)` → `stickBottom()` → `typeset(mdEl)`（调 `MathJax.typesetPromise([mdEl])`）。

**renderMd** 用 marked.js + 数学占位符技巧：先把 `$...$` / `$$...$$` / `\[\]` / `\(\)` 替换成 `\x02M<N>\x02` 占位符（避免 marked 把 `_` `*` 当 markdown），跑 marked，再 restore 占位符。

---

## AI 后端 adapter

`_AdapterSession` 包装 `make_backend(backend_name, settings)`：

| backend_name | 实现 | 特点 |
|---|---|---|
| `claude_cli` | 调 `/usr/bin/claude` CLI（subprocess） | 服务器侧默认；Linux 上 sed patch 移除 `--dangerously-skip-permissions` + 加 `--allowedTools Read`（防 AI 乱改文件）|
| `codex_cli` | 调 `/usr/bin/codex` CLI | OpenAI 的本地 CLI |
| `claude_api` | Anthropic API direct（不经 CLI） | 需 API key |
| `openai_api` | OpenAI API direct | 需 API key |
| `ollama` | 本机 ollama HTTP | 离线 |

切换 backend：改 `state/server-config.json::ai_backend` 字段（控制面板可改）。`_AdapterSession.send()` 每次都重新读 cfg，无需重启 qa-server。

---

## 控制面板交互

`https://bwicarus.space/control/` 里 QA 相关开关（写入 `server-config.json`）：

| 字段 | 含义 |
|---|---|
| `qa_remote_access` | 父开关：允许局域网/Tailscale 访问 daemon (0.0.0.0:9091) |
| `qa_remote_daemon` | 子开关：常驻 daemon（关掉则只在本机 `ctrl+shift+q` 临时启）|
| `qa_exercises_subdir` / `qa_wrong_subdir` | "保存到 vault" 时的目标子目录（习题 / 错题）|
| `ai_backend` + `ai.{backend}.*` | AI 后端选择 + 参数 |

完整字段参考 [`references/server-config-schema.md`](server-config-schema.md)。
