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

## 关键文件
- `index/knowledge-index.md` — 分层知识索引主文件（各科目条目数汇总）
- `index/{科目}.md` — 科目索引（各分支条目）
- `index/{科目}/{分支}.md` — 分支索引（条目超过 30 条时自动拆分）
- `references/index-format.md` — 索引条目格式规范
- `references/vault-structure.md` — 学科分类体系与 vault 文件夹说明
- `references/obsidian-syntax.md` — Obsidian 链接语法参考
- `scripts/pdf_extract.py` — PDF 区域提取脚本（需要 Python + PyMuPDF）
- `scripts/annotate_note.py` — 批量 PDF 标注脚本（扫描笔记中所有 PDF 链接并插入原文注释）
- `scripts/summarize_note.py` — 摘要生成与分层索引管理脚本

## 环境
- Python：`C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe`
- 运行脚本时使用完整路径（`python` 命令未加入 PATH）

## 操作规范
- 所有 skill 只对用户**明确指定**的文件操作，不自动扫描整个 vault
- 写入索引前检查是否已有同名条目，有则更新而非重复插入
- 所有 Obsidian 链接使用 `[[文件名]]` 格式，不带扩展名

## PDF 标注格式
PDF 原文内容以 HTML 注释写入笔记，**阅读模式不显示，源码模式可见**：
```
![[file.pdf#page=N&rect=x1,y1,x2,y2&color=yellow]]
<!-- 原文
提取的文字内容
-->
```
如需将旧版 `> [!quote] 原文` callout 块转换为此格式，使用：
```
python scripts/annotate_note.py --note <笔记路径> --migrate
```
