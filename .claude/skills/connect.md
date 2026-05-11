# Skill: connect

为单篇笔记查找相关笔记，写入「相关笔记」节，并把反向链接传播到目标笔记。

## 触发方式
用户输入 `/connect <笔记路径>`。

## 执行
直接调用编排脚本的单步模式：
```
python C:\claude\scripts\register_notes.py --note "<笔记路径>" --only connect
```

前提：笔记已通过 `/summarize` 写入索引，frontmatter 里有 `keywords` 和 `category`。

脚本内部：
1. 从笔记 frontmatter 读取 keywords 和 category
2. 读取该 category 的科目索引（含已拆分的分支文件）
3. 用 `references/prompts/find_related.md` 模板调 AI 找 2-6 个相关条目
4. 写入笔记的「相关笔记」节（合并模式：保留已有条目）
5. 反向传播：对每个目标笔记，追加一条指向当前笔记的反向链接（幂等）

## 单向 + 反向传播策略
- 笔记的正向链接由 AI **仅在首次处理时**生成（避免每次重处理都让 AI 重新判断导致链接抖动）
- 反向链接由脚本添加到目标笔记，无需 AI 介入
- 由于 `_file_hash` 会剥掉「相关笔记」节，反向链接的添加不会触发目标笔记的重处理

## 输出
```
<文件名> | ... | 链接:N 反向:M | ... | <耗时>s
```
