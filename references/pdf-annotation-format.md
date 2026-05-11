# 标注格式（PDF + 图片）

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
python C:\claude\scripts\annotate_note.py --note <笔记路径> --migrate
```

---

## 图片标注格式

图片内容描述以相同的 HTML 注释格式写入笔记，阅读模式不显示：

### 单张图片或独立图片

```
![[diagram.png]]

<!-- 图片描述
对图片内容的文字描述
-->
```

### 关联图片组（同一标题下多张相关图片）

前 N-1 张图片标记占位符，最后一张图片下写入完整描述：

```
![[step1.png]]

<!-- 已标注 -->

![[step2.png]]

<!-- 已标注 -->

![[step3.png]]

<!-- 图片描述
[step1.png, step2.png, step3.png]
三张图展示了傅里叶变换的推导过程：第一张定义时域信号，
第二张进行积分变换，第三张得出频域表示。
-->
```

### 混合情况（同一节内既有关联图片又有独立图片）

```
![[overview.png]]

<!-- 图片描述
系统架构总览图，展示三层结构。
-->

![[layer1.png]]

<!-- 已标注 -->

![[layer2.png]]

<!-- 图片描述
[layer1.png, layer2.png]
两张图分别展示输入层和隐藏层的详细结构。
-->
```

### 分组规则

1. 以标题（`#` ~ `######`）为边界划分段落，同一段内的图片作为一个候选组
2. 脚本将同段图片一起发送给 Claude Code（多模态读图）由 AI 判断关联性
3. AI 返回分组结果（字典），关联的图片合并描述，无关的单独描述
4. 已存在 `<!-- 图片描述` 或 `<!-- 已标注 -->` 的图片链接自动跳过

### 工作流（Claude Code 执行）

```
# 第一步：脚本扫描，输出待处理图片 JSON
python C:\claude\scripts\annotate_images.py scan --note <笔记路径>

# 第二步：Claude Code 用 Read 工具读取 JSON 中 path 字段指向的图片文件
#         对每个 section 的图片：分析内容，判断关联性，生成分组描述

# 第三步：Claude Code 将结果写入临时 JSON 文件，格式如下：
# {
#   "sections": [
#     {
#       "images": [  <-- 从 scan 输出原样复制
#         {"index": 0, "filename": "a.png", "path": "...", "abs_end": 105},
#         {"index": 1, "filename": "b.png", "path": "...", "abs_end": 143}
#       ],
#       "groups": [
#         {"indices": [0, 1], "description": "两张图展示了..."},
#         {"indices": [2], "description": "该图展示了..."}
#       ]
#     }
#   ]
# }

# 第四步：脚本写回标注
python C:\claude\scripts\annotate_images.py apply --note <笔记路径> --result <结果JSON路径>
```

支持的图片格式：`.png`, `.jpg`, `.jpeg`, `.gif`, `.bmp`, `.webp`, `.svg`

---

## 何时读取此文件
- 用户询问 PDF 标注/原文如何呈现
- 用户询问图片标注/描述如何呈现
- 需要手动写入或修改标注注释
- 处理旧 callout 块的迁移
