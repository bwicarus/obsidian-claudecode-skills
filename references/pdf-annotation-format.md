# PDF 标注格式

## 注释写入笔记的格式

PDF 原文内容以 HTML 注释写入笔记，**阅读模式不显示，源码模式可见**：

```
![[file.pdf#page=N&rect=x1,y1,x2,y2&color=yellow]]

<!-- 原文
提取的文字内容
-->

```

`annotate_note.py` 默认按此格式插入；已存在 `<!-- 原文` 块的链接自动跳过。

## 旧 callout 块迁移

如需将旧版 `> [!quote] 原文` callout 块转换为 HTML 注释格式：

```
python C:\obsidian\claude\scripts\annotate_note.py --note <笔记路径> --migrate
```

## 何时读取此文件
- 用户询问 PDF 标注/原文如何呈现
- 需要手动写入或修改 PDF 原文注释
- 处理旧 callout 块的迁移
