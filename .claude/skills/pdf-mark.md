# Skill: pdf-mark

从 Obsidian vault 的 PDF 中提取指定区域内容。
自动判断是否有文字层；无文字层时依次尝试 OCR 和 Claude Haiku 视觉分析。

## 触发方式
用户输入 `/pdf-mark`，可以：
- 提供**单个链接**进行提取
- 提供**笔记路径**，批量处理笔记中所有 PDF 链接

## 输入格式

**方式一：单个链接（直接从 Obsidian 复制粘贴）**
```
![[000-LADR4eChinese.pdf#page=15&rect=23,98,478,192&color=yellow]]
```
- `page` — 页码（从 1 开始）
- `rect` — `x1,y1,x2,y2`（PDF 原生坐标，左下角原点，脚本自动转换为 fitz 坐标）
- `color` — 高亮颜色（不影响提取）

**方式二：批量处理整篇笔记**
提供笔记路径，脚本扫描所有 PDF 链接，在每个链接下方插入 `> [!quote] 原文` 块。
已处理过的链接（下方已有 callout）会自动跳过。

## 执行步骤

### 单链接模式
1. 解析链接，调用 `scripts/pdf_extract.py --link "..."`
2. 展示提取内容，询问是否写入某篇笔记

### 批量模式
1. 先用 `--dry-run` 预览，确认后再写入：
   ```
   python C:\obsidian\claude\scripts\annotate_note.py --note "<笔记路径>" --dry-run
   python C:\obsidian\claude\scripts\annotate_note.py --note "<笔记路径>"
   ```
2. 脚本输出每个链接的提取状态和字符数
3. 写入后告知用户处理了几个链接

## 脚本输出前缀
- `TEXT:` — 文字层提取（最准确）
- `OCR:` — Tesseract OCR
- `VISION:` — Claude Haiku 视觉分析

## 依赖
- `pip install pymupdf`（必须）
- `pip install pytesseract pillow` + 安装 Tesseract-OCR（OCR 可选）
- Anthropic API key（视觉分析可选，设置环境变量 `ANTHROPIC_API_KEY`）
