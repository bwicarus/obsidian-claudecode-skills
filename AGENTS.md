# Obsidian 笔记管理项目

## Reader / PWA / 浏览器扩展：Codex 快速入口

Reader 主线先读 [Codex Reader Context](references/codex-reader-context.md)，再读
[当前协作状态](references/reader-collaboration-status.md)。这两个短入口不足以裁决时，才按
context 中的索引回看原始长文档。聊天记录、旧版本注释和历史测试不能覆盖当前代码、合同测试与
已登记的发布事实。

### 产品边界

| 页面 | 无扩展 | 有扩展 |
|---|---|---|
| 普通 HTTP/HTTPS 网页 | 无 BW 功能 | 扩展提供完整网页阅读功能 |
| PWA 真书（PDF/EPUB/导入 HTML/Markdown/收藏书） | PWA 完整 fallback | 扩展接管共享 UI、网络与通用数据；PWA 保留 renderer、私有 anchor 和书籍数据 |

不得恢复 PWA 任意网页解析器，也不得把扩展退回只为 PWA 提供数据的 provider。功能有差异或
矛盾时先登记到 [运行时冲突](references/reader-runtime-conflicts.md) 或
[视觉冲突](references/reader-ui-conflicts.md)，不能擅自删掉一边。

### 源码入口

- 共享视觉与组件：`_server_deploy/static/pdf/rc-ui.js`、`rc-*.js`
- 共享运行时合同：`_server_deploy/static/reader-runtime/*.js`
- 扩展宿主与 adapter：`extensions/bw-reader-webext/src/*.js`
- PWA 真书宿主/接管桥：`_server_deploy/static/reader-runtime/book-host.js`、
  `_server_deploy/static/pdf/pwa-extension-bridge.js`
- 服务端业务与桥接：`_server_deploy/pdf_reader.py`、`assistant.py`、`voice.py`、
  `tool_registry.py`
- 扩展 `vendor/` 是生成物，只能由 `extensions/bw-reader-webext/build.py` 更新，禁止手改。
- PDF `_server_deploy/static/pdf/reader.js` 也是生成物；唯一源码在 `reader.src/*.js`。

卡片职责保持单一：`rc-voicecall` 管视觉外壳、拖放、收藏和长按；`rc-flashcard` 管 Anki
正反面与学习状态；`rc-review` 管复习队列、提交和失败恢复；`rc-stickynote`/`web-pins`
管 placement。学习卡语义身份保持 `id === cid === gid`，placement ID 另算。

### 安全检查与常用命令

开始前：

```bash
git status --short
tail -n 160 references/reader-collaboration-status.md
python3 extensions/bw-reader-webext/handoff_check.py
```

共享源码变化后：

```bash
python3 extensions/bw-reader-webext/build.py
node --test --test-reporter=spec tests/reader_contract/*.test.mjs
python3 -m unittest discover -s tests -p 'test_*.py'
python3 extensions/bw-reader-webext/test_release_pipeline.py
python3 extensions/bw-reader-webext/release_preflight.py \
  --artifact extensions/bw-reader-webext-X.Y.Z-windows-test.zip \
  --skip-browser
```

浏览器行为按改动范围选择 `extensions/bw-reader-webext/test_*.py`，完整矩阵见
[扩展交接](references/reader-extension-handoff.md#9-测试)。Windows 只使用既有
`BW Codex Chrome Test` 独立环境及 `%LOCALAPPDATA%\BWReaderExtensionTest\browser-profile-v2`；
不得修改日常 Chrome/profile，也不得用在线 channel launcher 覆盖待测本地候选。
上面的 `--skip-browser` 只用于开发期检查候选元数据，不能作为发布通过证据；运行前把
`X.Y.Z` 换成实际不可变候选版本。

### 硬边界

- 工作区长期包含用户和历史改动；禁止 `git reset --hard`、`git clean`、宽范围 checkout，
  也不得把无关差异带入当前工作。
- 协作时先读共享状态并认领有界 scope；同一 checkout 不允许两个 agent 同时改同一组件。
- PWA 私有 anchor、PDF 几何、EPUB reflow 等只由对应 `DocumentHost` 解释；扩展只调用白名单。
- token/namespace/owner token 不得进入页面、日志或文档；内容脚本不得获得明文凭据。
- 同步、评分、Anki 添加等未知 mutation 结果一律 fail closed；不能为了“可用”冒险重复写。
- 发布必须使用项目原子部署/通道脚本和可复现候选，禁止手工 `cp` 覆盖生产文件。
- 生产文件清单以 `scripts/reader_deploy_manifest.py` 为唯一事实源，实际写入只走
  `scripts/deploy_reader.sh`；旧文档里的手工文件列表不能代替 manifest。
- 未来每次实际准备部署前，先一次性交付完整人工浏览器验收清单；用户负责全部视觉与交互操作，
  agent 只监控后台请求、数据流、日志和持久化。用户视觉验收与后台证据都通过后才可部署。
- 独立浏览器自动化仍可作为工程回归，但不能替代或冒充上述用户人工部署验收。
- 物理手写笔、双真实设备同步、registry 跨代迁移等未自动覆盖场景不得误报通过。

### 必须回看原始资料的情况

- 产品归属、接管、存储或同步：`reader-runtime-architecture.md`、
  `reader-extension-ownership.md`
- 当前版本、Windows 环境、发布/回滚：`reader-extension-handoff.md`、
  `reader-collaboration-status.md`
- 卡片/复习语义：`card-review-integration.md`、`anki-card-format.md`
- Codex app-server、CLI、MCP：`codex-integration.md`、`mcp-server.md`
- Obsidian/Anki 自动化：对应 `.claude/skills/*.md` 与下文列出的笔记规范

## Vault 位置
- Vault 根目录：`C:\obsidian\`
- Windows 项目目录：`C:\claude\`（管理脚本和配置，不含笔记）
- Pi 项目目录：`/home/bwicarus/claude`

## 学习方向
自学：英语、日语、计算机科学（当前重心：大学数学、大学物理）

## 技能 (Skills)
| 指令 | 功能 |
|------|------|
| `/登记新笔记` | 自动扫描新增/已修改笔记，执行完整流程：PDF 标注 → 摘要入索引 → 关联链接 → Anki 制卡 |
| `/summarize` | 对指定笔记生成关键词+摘要，写入知识索引 |
| `/connect` | 在知识索引中查找关联笔记，在原文末尾插入 Obsidian 链接 |
| `/pdf-mark` | 提取 PDF 指定页面和像素区域的内容（文字层或 OCR） |
| `/anki` | 对指定笔记或目录生成 Anki 卡片：agent 判断制卡，脚本同步到 Anki |
| `/website` | 管理个人网站 bwicarus.space：部署页面、更新仪表板、nginx 配置 |
| `/openai-cli-chat` | Codex CLI 多轮对话实现模板：`codex_call()` + `--image` 附图 + 应用层历史拼接 |

## 关键文件

**索引**
- `index/knowledge-index.md` — 主索引（各科目条目数汇总）
- `index/{科目}.md` — 科目索引（各分支条目）
- `index/{科目}/{分支}.md` — 分支索引（条目超 30 条时自动拆分）

**参考规范**（各 skill 按需加载）
- `references/index-format.md` / `vault-structure.md` / `obsidian-syntax.md`
- `references/anki-selection-rules.md` / `anki-card-format.md`
- `references/pdf-annotation-format.md`

**脚本**
- `scripts/summarize_note.py` / `connect_note.py` / `annotate_note.py` / `pdf_extract.py`
- `scripts/pending_notes.py` — 扫描待登记笔记（新增/已修改，命名规则 `[0-9A-Fa-f]{3}-*.md`）
- `scripts/anki_from_note.py` / `anki_status.py` / `daily_anki_status.ps1`
- `scripts/review_priority.py` — 知识图谱复习优先级（激活扩散 + Anki 薄弱度）
- `scripts/note_state.py` — 各脚本共享的笔记内容哈希状态模块

**EXE 启动器**（双击运行，源码在 `launchers/`，编译产物在 `launchers/dist/`）
- `登记新笔记.exe` — 在 `C:\claude` 下运行 `Codex --dangerously-skip-permissions -p "/登记新笔记"`
- `上传网页.exe` — 依次执行仪表板同步四步：Anki 状态更新 → 复习优先级 → 生成 dashboard.json → SCP 推送到服务器

重新编译方法：
```
C:\Users\bwica\AppData\Local\Programs\Python\Python313\Scripts\pyinstaller.exe --onefile --console --distpath launchers/dist --workpath launchers/build --specpath launchers launchers\<脚本名>.py
```

**状态文件（仅脚本读写，不传给 AI）**
- `anki/records/*.json` — Anki 制卡记录（含 `section_hashes`：各节内容哈希，用于增量制卡）
- `state/note-states.json` — 各 skill 最后处理的内容哈希 + 时间戳

## 环境
- Python：`C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe`
- 运行脚本时使用完整路径（`python` 命令未加入 PATH）

## 操作规范
- `/登记新笔记` 自动扫描 vault 根目录符合命名规则的待处理笔记；其余 skill 只对用户**明确指定**的文件操作
- 写入索引前检查是否已有同名条目，有则更新而非重复插入
- 所有 Obsidian 链接使用 `[[文件名]]` 格式，不带扩展名
- `anki/records/` 和 `state/note-states.json` 是脚本状态文件，只由脚本读写，**不要传给 AI**
- Anki 制卡按节（`#` 标题）追踪哈希；只有内容变动的节才送给 AI 判断，新卡追加到已有 record，旧卡不删除
- Anki 制卡默认不修改原 md；同步结果写入 `anki/records/`

## 自动化任务
- 任务名「Obsidian Anki 每日状态更新」，每天 04:00 运行
- 执行顺序：启动 Obsidian（等 5 分钟同步）→ 启动 Anki → 轮询 AnkiConnect（最多 2 分钟）→ 查询所有笔记掌握情况并写回 frontmatter 和 record → 计算复习优先级并写回 frontmatter 和 record
