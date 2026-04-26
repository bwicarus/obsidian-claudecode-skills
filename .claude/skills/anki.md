# Skill: anki

对指定笔记生成 Anki 卡片并同步到 Anki。

## 触发方式
用户输入 `/anki` 并提供笔记路径或文件名。

## 执行步骤

### 第一步：读取制卡上下文
```
C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe C:\obsidian\claude\scripts\anki_from_note.py --read "<笔记完整路径>"
```
脚本输出笔记内容、制卡规则（selection-rules）、卡片格式（card-format）和 expected_json 结构。

### 第二步：判断并生成卡片 JSON
根据脚本输出中的 references 判断是否值得制卡，生成符合 expected_json 结构的 JSON。

**输出规则：**
- 只输出 JSON，不输出 Markdown 代码块或任何额外解释
- front/back/text 字段内不能使用中文弯引号（`"` `"`），改用书名号（`「」`）或直角引号
- 数学公式使用 LaTeX，不要只复制原句

### 第三步：同步到 Anki
将生成的 JSON 写入临时文件并调用脚本同步：

```
C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe C:\obsidian\claude\scripts\anki_from_note.py --sync "<笔记完整路径>" --cards-json "<tmp_cards.json路径>"
```

脚本会自动检测 AnkiConnect，未响应时自动启动 Anki 并等待上线（最多 60 秒）。

若不需要制卡，直接写入 no_cards 记录，跳过 Anki：
```
C:\Users\bwica\AppData\Local\Programs\Python\Python313\python.exe C:\obsidian\claude\scripts\anki_from_note.py --sync "<笔记完整路径>" --no-cards-reason "<原因>"
```

### 第四步：清理与汇报
- 删除临时 JSON 文件
- 告知用户：同步了几张卡（分 OK/FAIL）、record 写入路径

## 临时文件路径
`C:\obsidian\claude\anki\tmp_cards.json`（同步完成后立即删除）

## 注意
- 制卡判断规则见 `references/anki-selection-rules.md`
- 卡片格式要求见 `references/anki-card-format.md`
- 已有 record 的笔记会自动跳过，如需重新制卡加 `--force`
