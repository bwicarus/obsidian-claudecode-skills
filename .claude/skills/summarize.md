# Skill: summarize

对单篇笔记生成关键词+摘要+分类，写入知识索引。

## 触发方式
用户输入 `/summarize <笔记路径>`。

## 执行
直接调用编排脚本的单步模式：
```
python C:\claude\scripts\register_notes.py --note "<笔记路径>" --only summarize
```

脚本内部：
1. `summarize_note.clean_note()` 去除 frontmatter / PDF 嵌入 / callout 等噪声
2. 用 `references/prompts/analyze.md` 模板调 `ai_client.ask()` 做语义分析
3. 解析 KEYWORDS / SUMMARY / CATEGORY 三行
4. `summarize_note.write_to_index()` 写入对应科目/分支索引（自动新建或更新）
5. Excalidraw 笔记会改用 `references/prompts/analyze_excalidraw.md`，让 AI 直接读 PNG

## 输出
```
<文件名> | PDF:0 图片:0 | 索引:新增/更新 | 链接:0 反向:0 | Anki: | <耗时>s
```
