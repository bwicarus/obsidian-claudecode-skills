# Obsidian 笔记管理项目

## Vault 位置
- Vault 根目录：`C:\obsidian\`
- 本项目目录：`C:\obsidian\claude\`（管理脚本和配置，不含笔记）

## 学习方向
自学：英语、日语、计算机科学（当前重心：大学数学、大学物理）

## 技能 (Skills)
| 指令 | 功能 |
|------|------|
| `/登记新笔记` | 完整登记流程：PDF 标注 → 摘要入索引 → 追加关联链接（三步合一） |
| `/summarize` | 对指定笔记生成关键词+摘要，写入知识索引 |
| `/connect` | 在知识索引中查找关联笔记，在原文末尾插入 Obsidian 链接 |
| `/pdf-mark` | 提取 PDF 指定页面和像素区域的内容（文字层或 OCR） |
| `/anki` | 对指定笔记或目录生成 Anki 卡片：agent 判断制卡，脚本同步到 Anki |
| `/perf` | 游戏性能分析：启动 Afterburner → 采集 → 相关性分析 → 低帧诊断 |

## 关键文件
- `index/knowledge-index.md` — 分层知识索引主文件（各科目条目数汇总）
- `index/{科目}.md` — 科目索引（各分支条目）
- `index/{科目}/{分支}.md` — 分支索引（条目超过 30 条时自动拆分）
- `references/index-format.md` — 索引条目格式规范
- `references/vault-structure.md` — 学科分类体系与 vault 文件夹说明
- `references/obsidian-syntax.md` — Obsidian 链接语法参考
- `references/anki-selection-rules.md` — Anki 制卡判断标准
- `references/anki-card-format.md` — Anki 卡片制作与 JSON 输出标准
- `references/pdf-annotation-format.md` — PDF 原文 HTML 注释格式与旧 callout 迁移命令
- `scripts/pdf_extract.py` — PDF 区域提取脚本（需要 Python + PyMuPDF）
- `scripts/annotate_note.py` — 批量 PDF 标注脚本（扫描笔记中所有 PDF 链接并插入原文注释）
- `scripts/summarize_note.py` — 摘要生成与分层索引管理脚本
- `scripts/connect_note.py` — 相关笔记写入脚本（由 agent 判断关联，脚本负责更新目标笔记）
- `scripts/anki_from_note.py` — Anki 同步脚本（agent 生成卡片 JSON，脚本负责 AnkiConnect 与 records）
- `anki/records/*.json` — Anki 制卡记录；用于跳过已处理笔记，不传给 AI

## 环境
- Python：`C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe`
- 运行脚本时使用完整路径（`python` 命令未加入 PATH）

## 操作规范
- 所有 skill 只对用户**明确指定**的文件操作，不自动扫描整个 vault
- 写入索引前检查是否已有同名条目，有则更新而非重复插入
- 所有 Obsidian 链接使用 `[[文件名]]` 格式，不带扩展名
- Anki records JSON 是脚本状态文件，只由脚本读取判断，**不要传给 AI**
- Anki 制卡默认不修改原 md；同步结果写入 `anki/records/`


