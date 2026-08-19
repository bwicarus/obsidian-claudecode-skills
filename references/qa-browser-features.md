# 截图问答（QA Browser）功能详解

源码：`_client/core/qa_browser.py`（HTML/JS 内嵌字符串）。
入口：本机 `ctrl+shift+q`（临时进程）或服务器 systemd `qa-server.service`（常驻 :9091 + :9090）。systemd ExecStart 直接跑 `_server_deploy/qa_server.py`，它内部再起 qa_browser daemon(:9091) + cmd_server(:9090)。
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
        ↓ 立即返回 {ok, job_id}（AI 整理 10-30s，移动端连接易断 → 后台线程跑，复用 _card_jobs）
后台 _create_note_from_qa（_client/core/qa_browser.py:1404）
  ├─ 清理 name → vault/<name>.md（同名追加 -1/-2）
  ├─ AI 整理 pairs → 结构化 Markdown
  │   prompt 要求：去对话冗余、## 标题、$...$ 数学、不发挥
  ├─ 保存截图到 vault/attachments/<name>.png（如果有），笔记顶部加 ![[]]
  └─ 写文件 → 返回 obsidian://open?vault=...&file=...
        ↓
前端 pollCreateNoteJob 轮询 GET /api/card-update-status?job=<id>（复用端点零新端点，封顶 60 次）
        ↓
弹出："✓ 笔记已创建" + "📂 在 Obsidian 中打开" 按钮
```

**关键**：笔记**不带 `[0-9A-Fa-f]{3}-` 前缀** → 不被 `pending_notes.py` 扫到 → **不触发 register**。算"草稿型"笔记，用户可以后续重命名加前缀让其进入正式流程。

## cardCtx 模式：改进 Anki 卡 / 源笔记

参数：`/?card=<local_id>` → `loadCardContext()` 拉卡片两面 + 显示在截图位置。

勾选有用回答后，header 出现三个按钮：
- **更新到笔记**：POST `/api/card-update` `{target:'note', verbosity:'verbose'|'concise'}` → 后台 `_prepare_legacy_card_draft()`（qa_browser.py:1122）**只出草稿、一个字都不写盘** → 前端渲染「草稿预览（尚未写入）」→ 用户确认后 POST `/api/card-update-commit` → `_commit_legacy_card_draft()`（qa_browser.py:1343）才真正改写源笔记
- **根据此修改 Anki**：同样两段式（`/api/card-update` 出草稿 → `/api/card-update-commit` 写入）；**原卡永远保留**——`_commit_legacy_anki_draft`（qa_browser.py:1199）docstring 写死「The original card is never deleted.」、:1299 `deleted = False`，页面文案也是「卡片改进始终保留原卡，避免丢失 FSRS 复习历史」，早已不由 server-config 控制删/留
- **全部更新**：两个都跑

异步实现：提交 POST `/api/card-update`（立即返回 `{job_id}`），前端轮询 GET `/api/card-update-status?job=<job_id>` 拿结果（done 的 job 保留 30 分钟，弱网下轮询能重取，移动端连接断了不丢）。这套 `_card_jobs` + 轮询端点同时被 `/api/create-note` 和 server 模式 `/api/save` 复用，见「后台 job 化」节。

### 笔记改写两种模式（2026-05-27）

「更新到笔记」前先弹小选择：

| 模式 | 行为 | 适用 |
|---|---|---|
| **详细**（默认） | 默认保留 ④ 全部原文 + 在合适位置追加 / 改写 QA 内容；prompt 强调"有判断力"——允许重组顺序、合并相似段、连贯化跨段衔接（应对用户选了**间断**段落）；不强制逐字照搬 | 原笔记内容多、QA 是详细解析 |
| **精炼** | 允许大幅删减，提炼核心；同样有判断力可改写但不损失关键信息 | 原笔记太啰嗦想压缩 |

两种模式共用同一条 prepare/commit 链（`_prepare_legacy_card_draft` → `_commit_legacy_card_draft`），传 `verbosity` 参数（`verbose`=详细 / `concise`=精炼，`/api/card-update` body 也读 `verbosity`，非法值回落 `verbose`）。commit 返回的 summary 是「净变化 ±N 字」（qa_browser.py:1392），旧的"保留率"百分比已不存在。

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

## 后台 job 化（断连不丢结果，2026-06）

慢操作（AI 调用 / AnkiWeb sync）全部移出请求线程，移动端锁屏/弱网断连不丢结果。

### `_card_jobs` job 表 + 共用轮询端点

模块级 `_card_jobs: job_id → {"status": "running"|"done", "result", "_t"}`（qa_browser.py:296）。三个 POST 路由立即返回 `{ok, job_id}` 后台线程跑：

| 路由 | 后台跑什么 | 前端轮询函数 |
|---|---|---|
| `/api/card-update` → `/api/card-update-commit` | `_prepare_legacy_card_draft`（只出草稿不写盘）/ `_commit_legacy_card_draft`（用户确认后才写 Anki/笔记，另起一个 job）| prepare 用 `pollCardJob`（qa_browser.py:2459）、commit 用 `pollCardCommitJob`（:2395）——都是 running 每 2s 重拉、失败重试 3s 上限 40 次 |
| `/api/create-note` | `_create_note_from_qa` | `pollCreateNoteJob`（封顶 60 次）|
| `/api/save`（仅 server 模式） | `_do_full_save` 全链路 | `pollSaveJob`（120 次未完成 / 连续 40 次轮询失败才放弃，提示「任务仍在后台执行，稍后可在历史记录查看」）|

轮询端点统一 GET `/api/card-update-status?job=<id>`：**不 pop** —— done 的 job 保留 30 分钟（弱网下「done 响应丢了」的轮询能重取，避免 job 被删后一直 404），请求时懒清理过期项（qa_browser.py:2883-2893）。

### `/api/save`：先快照再异步（关键顺序）

`classify_conversation` 是同步 AI 调用可达数十秒。`/api/save` 入口**先**快照会话（`msgs_snap` = messages 深拷 + `img_snap` + `tmp_snap`，qa_browser.py:3090 起），后台 `_do_full_save`（classify → find_related_cards → do_save → archive_conversation → push_to_website 线程 → `_export_history_to_webapp`）**全链路只用快照**；server 模式拿到 job_id 响应前就 `session.reset()` + 清 state，daemon 立刻可服务下一轮。⚠ 坑：链路上任何一处仍读全局 `state` 都会在 reset 后拿到空会话。本地模式（ctrl+shift+q 临时进程）保持同步返回 —— `state['done'].set()` 的时机依赖响应已送出，不能 job 化。

### `/api/card-delete`：sync 后台化（非 job）

`_card_delete`（qa_browser.py:1473）：Anki `deleteNotes` + records 写盘**同步完成**后即返回 `{ok: true, synced: "pending"}`；AnkiWeb sync（最长 120s，不值得占住请求线程）改后台 fire-and-forget 线程，失败由 15min `anki-sync-refresh.timer` 兜住。前端 synced **三态**：`true`=已同步 / `'pending'`=后台同步中（显示「同步中」）/ falsy=未同步 —— `'pending'` 是 truthy，**不能直接 `j.synced ?`** 当布尔用。对比：cardCtx「修改 Anki」的 commit（`_commit_legacy_anki_draft`，qa_browser.py:1199）末尾的 sync 仍同步等待 —— 它本身已在 job 线程里跑，等得起。

## AI 后端 + System Prompt

`_AdapterSession`（定义在 `_client/core/qa_browser.py`，**不在** scripts/ai_client.py）每次 send 调 `_GET_CFG()` 读当前 `ai_backend` + `ai.{backend}` 设置（来自 server-config / 客户端 cfg），支持切 backend 不重启。QA browser 用 `from ai_backends import make_backend`，并用 shim `_AiClientShim` 把 `ai_client.xxx` 重定向到本地实现——与主项目 scripts/ai_client.py 是两套，**不读 `ai_settings.json`**。

`_SESSION_PROMPT`（2026-05-26 强化）：
```
你是一个截图问答助手。根据随附截图和对话历史回答用户问题。
只回答问题本身，不要修改文件，不要运行命令，不要描述你的系统环境。
**数学公式严格用 Markdown 数学语法**：行内公式 $...$，行间公式 $$...$$；
**不要**用反引号 ` 包裹数学表达式（这样在前端会被当成代码块灰底显示而非公式），
也不要用 \(...\) 或 \[...\]。
例如：要写 $F^S$ 而不是 `F^S`，要写 $a_1, \ldots, a_n$ 而不是 `a_1,...,a_n`。
```

## SSE 流式

`/api/chat` 当 `Accept: text/event-stream` 时启用 SSE：
- 后端 `ad.chat_stream(msgs)` 逐 chunk yield
- 前端节流渲染（每 120ms re-marked + MathJax 一次）
- 流结束后跑 `addHeadingPickers(mdEl)` 给标题补 `+` 按钮（无 cardCtx 检查；2026-05-26 修复，之前漏判会让创建笔记按钮拿不到 pairs）

## 数据存储

- `<STORE_DIR>/history.db` —— 历史对话（QA session，表名 `conversations`）
- `<STORE_DIR>/images/`（`HIST_IMG_DIR`）—— 截图 PNG
- 服务器实例：`/home/bwicarus/claude/state/qa-server-data/`
- 历史导出：`WEBAPP_HISTORY_DIR=/home/bwicarus/webapp/data/users/bwicarus/history/`（用户 dashboard 能看历史）

## 关键路由（do_POST in qa_browser.py）

| 路径 | 用途 |
|---|---|
| `/api/chat` | 主对话（SSE 流式 + 旧 JSON 兼容） |
| `/api/inject-image` | iPad 远程注入截图 |
| `/api/card-context` | 拿卡片两面（cardCtx 模式初始化） |
| `/api/card-update` | 卡片改进（async job） |
| `/api/card-update-status?job=` | GET 轮询 job 结果（card-update / create-note / server 模式 save 共用，done 保留 30min） |
| `/api/card-delete` | 删卡（删 Anki note + records 同步完成；AnkiWeb sync 后台 fire-and-forget，返回 `synced:'pending'`） |
| `/api/create-note` | **普通模式：创建新笔记**（2026-05-26 加；async job） |
| `/api/save` / `/api/discard` | 保存对话到 vault / 弃稿（save 在 server 模式下先快照再 job 化，见「后台 job 化」节） |
| `/api/reset` | 清空 session |
| `/api/history/*` | 历史侧边栏 |
| `/api/search-related` | 「关联知识」按钮：AI 基于知识索引选相关**笔记**（带 mastery%），非 KG/Anki 卡 |

## iPad 远程入口

```
浏览器看页面：     http://<Tailscale-IP>:9091/
POST 截图注入：    http://<Tailscale-IP>:9090/qa?key=<KEY>
触发 register / daily：  http://<Tailscale-IP>:9090/run/<cmd>?key=<KEY>
```

详见 [`ipad-remote-qa.md`](ipad-remote-qa.md)。

---

## 数据存储详解

### SQLite schema（`<STORE_DIR>/history.db`）

```sql
CREATE TABLE conversations (
  id          TEXT PRIMARY KEY,       -- 时间戳字符串 %Y%m%d-%H%M%S（非自增整数）
  timestamp   TEXT NOT NULL,          -- 保存时间
  img_fname   TEXT,                   -- 关联截图文件名（在 HIST_IMG_DIR=images/）
  note        TEXT,                   -- 保存信息文本（含 "→ /path/to/note.md"，删除时正则解析）
  messages    TEXT,                   -- 整条对话 JSON
  record_type TEXT DEFAULT 'normal',  -- normal | wrong（错题）
  related_cards TEXT DEFAULT '[]'     -- 错题关联卡片 JSON（do_save 时 find_related_cards 填）
);
```

（`record_type` / `related_cards` 由 `init_db()` 用 `ALTER TABLE` 补加，老库自动迁移。cardCtx 模式的卡片身份**不**存这张表，复习链接靠 URL `?card=<local_id>`。）

### 截图文件

存 `<STORE_DIR>/images/`（`HIST_IMG_DIR`）。文件名是时间戳：远程注入 `remote-<ts>.png`、本机 `screenshot-<ts>.png`（不是 sha1）。剪贴板图片去重用的 md5（`get_clipboard_hash`）只用于粘贴轮询，跟落盘文件名无关。

### 历史侧边栏（删除级联）

GUI：`/history-btn` 按钮 → 滑出 sidebar 列出最近条目。每条带「打开 / 删除」。

**删除是级联的**：`POST /api/history/delete` 的级联逻辑直接 inline 在 do_POST 分支里（无独立 `_delete_history` 函数）：
1. SQLite `DELETE FROM conversations WHERE id=?`
2. 截图文件 `HIST_IMG_DIR/<img_fname>` 无条件 unlink（`if img.exists(): img.unlink()`，无 ref-count 判断）
3. **Obsidian 笔记**（除非 body 传 `keep_note=true` 只删库+截图保留笔记）：从 `note` 字段正则解析出 `→ ...md` 路径 → 若该路径存在直接 unlink；跨平台 fallback：路径不可达时按文件名在 `EXERCISES_DIR` / `WRONG_DIR` 下查找
4. 触发 `_export_history_to_webapp()` 同步到 webapp `WEBAPP_HISTORY_DIR`

---

## 快捷功能

### 粘贴图片

`document.addEventListener('paste', ...)`：捕获 clipboard 图片项 → 转 base64 → 缩略图显示在 `#paste-row`。下条消息发送时随 `image_b64` 一起 POST 到 `/api/chat`。

### 关联知识搜索（`🔍 关联知识`）

`/api/search-related` (POST，do_POST 分支)：
1. 调 `load_index_notes()` 加载知识索引（笔记名 → keywords + summary），取最近 6 条对话 + 可选 `query` 作上下文
2. 把上下文 + 笔记摘要（前 80 篇）喂给 `ai_client.ask()`，让 AI 选 3-6 篇最相关**笔记**并给关联原因
3. 返回 markdown：每行 `- [[笔记名]] — 关联原因  掌握 X%`（mastery 来自 `get_mastery`），并把这次搜索追加进 session

**注意**：`find_related_cards`（从 anki records 找相近卡）是另一套机制，只在 `/api/save` 的 `_do_full_save`（错题保存）里调，与 search-related 无关。

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
| `claude_cli` | 调 `claude` CLI（走 PATH，`default_command="claude"`，subprocess） | 服务器侧默认；**脱壳**跑：`--setting-sources ""` + `cwd=_STRIP_CWD`（项目树外）→ 不加载 CLAUDE.md/插件（省 token）。`--allowedTools Read`（带图时模型需 Read 落盘图片）现已**硬编码**进 ai_backends.py 的 chat/chat_stream，源码里已无 `--dangerously-skip-permissions` 字面量。qa-server.service 那条 sed patch 现在是历史残留/幂等兜底，命中目标串已不存在。**Gemini 兜底**：claude 失败/限流/一个字都没吐 → `_gemini_chat(...)` 整段一次性返回（chat_stream 里也是流末补一段），省额度 + 防单边挂 |
| `codex_cli` | 调 `/usr/bin/codex` CLI | OpenAI 的本地 CLI |
| `claude_api` | Anthropic API direct（不经 CLI） | 需 API key |
| `openai_api` | OpenAI API direct | 需 API key |
| `ollama` | 本机 ollama HTTP | 离线 |

切换 backend：改 `state/server-config.json::ai_backend` 字段（控制面板可改）。`_AdapterSession.send()` 每次都重新读 cfg，无需重启 qa-server。

---

## 控制面板交互

Pi 控制面板 `https://bwicarus.taile44d0c.ts.net/control/` 里 QA 相关开关（写入 `server-config.json`；`bwicarus.space` 是 2026-06-10 起暂停的 VPS，别往那边改）：

| 字段 | 含义 |
|---|---|
| `qa_remote_access` | 父开关：允许局域网/Tailscale 访问 daemon (0.0.0.0:9091) |
| `qa_remote_daemon` | 子开关：常驻 daemon（关掉则只在本机 `ctrl+shift+q` 临时启）|
| `qa_exercises_subdir` / `qa_wrong_subdir` | "保存到 vault" 时的目标子目录（习题 / 错题）|
| `ai_backend` + `ai.{backend}.*` | AI 后端选择 + 参数 |

完整字段参考 [`references/server-config-schema.md`](server-config-schema.md)。
