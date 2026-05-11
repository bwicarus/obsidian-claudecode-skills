# Skill: img-mark

为单篇笔记中的图片生成内容描述标注。

## 触发方式
用户输入 `/img-mark <笔记路径>`。

## 执行
直接调用编排脚本的单步模式：
```
python C:\claude\scripts\register_notes.py --note "<笔记路径>" --only img
```

脚本内部：
1. `annotate_images.py scan` 找出所有未标注的图片，按 `##` 段落分组
2. 用 `references/prompts/image_describe.md` 模板调 AI（让 Claude/Codex 用 Read 工具
   读取图片 → 判断关联性 → 分组 → 写描述）
3. 解析返回的 JSON
4. `annotate_images.py apply` 把分组描述写回笔记的 `<!-- 图片描述 -->` 注释块

## 严格关联标准
仅"同章节"或"都涉及某术语"不构成关联——必须有直接的内容关联（同一推导的连续步骤、
同一概念的不同视角等）才合并为一组。

## 输出
```
<文件名> | PDF:0 图片:N | ... | <耗时>s
```
