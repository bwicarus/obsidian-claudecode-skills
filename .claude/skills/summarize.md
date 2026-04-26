# Skill: summarize

对用户指定的笔记生成关键词和摘要，写入知识索引。
**脚本负责文件 I/O 和格式，AI 只负责输出摘要内容。**

## 触发方式
用户输入 `/summarize` 并提供笔记路径或文件名。

## 执行步骤

### 第一步：读取清理后的笔记内容
```
python C:\obsidian\claude\scripts\summarize_note.py --read "<笔记完整路径>"
```
脚本输出去噪后的纯文本（已去除 frontmatter、PDF 嵌入、callout 标记等）。

### 第二步：分析内容，仅输出以下三行
```
KEYWORDS: 关键词1, 关键词2, 关键词3
SUMMARY: 一句话摘要（不超过60字）
CATEGORY: 一级科目/二级分支
```
- KEYWORDS：3-8 个核心概念词，逗号分隔
- SUMMARY：说明核心知识点，不超过 60 字
- CATEGORY：从 `references/vault-structure.md` 中选择，格式如 `数学/线性代数`
- **不要输出其他任何内容**

### 第三步：写入知识索引
```
python C:\obsidian\claude\scripts\summarize_note.py --write "<笔记完整路径>" --keywords "关键词1, 关键词2" --summary "摘要内容" --category "数学/线性代数"
```
脚本自动处理：插入新条目 / 更新已有条目 / 新建不存在的分支。

### 第四步：汇报结果
告知用户写入结果（脚本会输出 `OK: 新增/更新 [[文件名]] → 科目/分支`）。

## 有效分类
见 `references/vault-structure.md`
