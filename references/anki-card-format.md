# Anki 卡片制作标准

本文件用于规定通过制卡判断后的内容如何转换为 Anki 卡片。Claude Code / Codex 等外层 agent 负责生成本文件规定的 JSON；`scripts/anki_from_note.py` 负责把 JSON 同步到 Anki 并写入 records。

## 基本原则

- 一张卡只测试一个知识点。
- 问题必须明确，不能依赖隐含上下文。
- 答案尽量短，但保留必要条件、限制条件和符号含义。
- 优先测试理解、辨析和应用入口，不只复制原句。
- 数学、物理公式可使用 LaTeX，必要时说明符号含义。
- 语言卡片应保留自然例句，避免只给孤立翻译。
- 不要生成过宽的问题，例如“解释某章内容”。
- 不要生成答案过长、需要整段背诵的卡片。

## Deck 命名

优先使用：

```text
科目::分支
```

示例：

```text
数学::线性代数
物理::力学
计算机科学::数据结构与算法
英语::词汇
日语::语法
```

无法判断时使用：

```text
Obsidian::未分类
```

## 卡片类型

### basic

适合定义、概念、定理、判定条件、简短问答。

```yaml
type: basic
front: 子空间需要满足哪两个封闭性条件？
back: 加法封闭与数乘封闭。
```

### cloze

适合公式、条件列表、语言例句、需要挖空回忆的固定表达。

```yaml
type: cloze
text: 子空间必须对 {{c1::加法}} 和 {{c2::数乘}} 封闭。
```

### reverse

适合术语互译、外语词汇、符号与名称互相回忆。

```yaml
type: reverse
front: linear transformation
back: 线性变换
```

### problem

适合典型题型、方法入口、算法步骤。

```yaml
type: problem
front: 判断一个集合是否为向量空间子空间的一般步骤是什么？
back: 检查非空、加法封闭、数乘封闭。
```

## 完整 JSON 输出

agent 只输出 JSON，不输出 Markdown 代码块或额外解释：

```json
{
  "should_create_cards": true,
  "reason": "这篇笔记值得或不值得制卡的总体原因",
  "cards": [
    {
      "type": "basic",
      "deck": "数学::线性代数",
      "front": "问题；cloze 卡可留空",
      "back": "答案；cloze 卡可作为 Back Extra",
      "text": "仅 cloze 卡使用；非 cloze 卡留空",
      "reason": "为什么这张卡值得制作",
      "tags": ["数学", "线性代数"]
    }
  ]
}
```

不需要制卡时：

```json
{
  "should_create_cards": false,
  "reason": "主要是学习安排，不包含需要长期记忆的知识点",
  "cards": []
}
```

## 单张卡字段要求

agent 输出给脚本的 JSON 中，每张卡都必须包含以下字段：

```json
{
  "type": "basic",
  "deck": "数学::线性代数",
  "front": "问题；cloze 卡可留空",
  "back": "答案；cloze 卡可作为 Back Extra",
  "text": "仅 cloze 卡使用；非 cloze 卡留空",
  "reason": "为什么这张卡值得制作",
  "tags": ["数学", "线性代数"]
}
```

## 类型字段规则

- `basic`、`reverse`、`problem` 必须填写 `front` 和 `back`，`text` 留空。
- `cloze` 必须填写 `text`，其中至少包含一个 `{{c1::...}}` 格式的挖空。
- `cloze` 的 `back` 可填写补充解释；没有补充时留空。
- `reason` 必须具体说明制卡理由，如“核心定义”“易混概念”“常用判定条件”。
- `tags` 使用 2-6 个短标签，不要包含空格。

## 质量底线

- 不要把一个长列表硬塞进一张卡。
- 不要生成答案不确定或过度依赖上下文的卡。
- 不要为笔记标题、目录、来源信息单独制卡。
- 如果无法写出明确 `reason`，通常不应制卡。
