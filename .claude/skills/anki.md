# Skill: anki

对单篇笔记生成 Anki 卡片并同步。

## 触发方式
用户输入 `/anki <笔记路径>`。

## 执行
直接调用编排脚本的单步模式：
```
python C:\claude\scripts\register_notes.py --note "<笔记路径>" --only anki
```

脚本内部：
1. `anki_from_note.py --read` 输出制卡上下文（节级哈希、references、待处理节内容）
2. 若 `pending_sections: []` → 直接返回 `跳过`
3. 否则用 `references/prompts/anki_cards.md` 模板调 AI 生成卡片 JSON
4. `anki_from_note.py --sync` 写入 Anki（自动启动 Anki + 等 AnkiConnect 上线）
5. 清理临时文件

## 单独使用 anki_status.py
查询学习进度，与制卡分开：
```
python C:\claude\scripts\anki_status.py --note "<笔记路径>" --write-frontmatter --write-record
python C:\claude\scripts\anki_status.py --all --wait-seconds 300  # 批量
```

## 注意
- 节级哈希追踪：只对内容变动的节重新制卡，不会重复
- 跳过原因记录：若 AI 判断"不值得制卡"，会在 record 里写明 `should_create_cards: false`，下次同节内容不变就跳过
- 制卡规则见 `references/anki-selection-rules.md`，卡片格式见 `references/anki-card-format.md`
