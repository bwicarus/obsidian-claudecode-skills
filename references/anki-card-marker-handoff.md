# Anki 制卡落错牌组(QA 恒 0)—— 已修复(2026-07-14)

## 结论(受控实验坐实)

**根因不是 AnkiWeb sync,是 AnkiConnect 的 `addNote` 忽略了 `deckName`。**

早前的交接结论「制卡后 fire-and-forget sync 把新卡冲掉」是**错的**。重做受控实验:

```
addNote(deckName=QA)          → note id 正常返回
findNotes tag:<测试标签>       → 1     卡在
findCards → cardsInfo.deckName → 系统默认   ★ 卡从一开始就不在 QA
sync → 等 36s → 再查           → 仍然 1,牌组仍是 系统默认
```

卡**从未**被 sync 删过;它只是**从来就没进过 QA**。

## 机制

AnkiConnect(addon 2055492159)`createNote()` 用旧写法指定牌组:

```python
ankiNote.model()['did'] = deck['id']     # 改的是 models.get() 返回的**缓存 dict**
...
self.startEditing()                      # → mw.requireReset() → mw.reset()  ← 缓存被清
collection.addNote(ankiNote)             # → add_note(note, note.note_type()["did"])
                                         #   note_type() 重新取 → did 已退回 notetype 自带默认牌组
```

Anki 25 的 `Collection.addNote`(legacy shim)读的是 `note.note_type()["did"]`,而 `models.get()`
的注释明说返回的是**缓存引用**——`startEditing()` 里的 `mw.reset()` 把这份缓存刷掉,那次赋值就白做了。
结果:卡落 notetype 的默认牌组(**系统默认**)。

## 影响

- 「系统默认」牌组 2026-07-09 之前是空的,之后 6 天堆了 **39 张**——全是 `tag:pdf-snippets` 的阅读制卡。
- **Vocab 牌组一直正常**:`scripts/vocab/anki_from_word.py` 早就有「显式归位」兜底(注释还写着
  "Anki 25 偶有 createDeck 静默没建立 → addNote 落默认 deck"——当时归错因了,但补丁是对的)。

## 修复

**所有 `addNote` 之后显式 `changeDeck` 归位**(7 个调用点):

| 文件 | 说明 |
|---|---|
| `_server_deploy/pdf_reader.py` `_run_snippets_to` | 阅读助手制卡 → QA |
| `_server_deploy/epub_assistant.py` `_anki_fix_deck` | 撤销后重建卡(addNotes) |
| `scripts/anki_from_note.py` | 笔记登记制卡(deck = 科目::分支) |
| `scripts/refresh_weak_cards.py` | 薄弱卡 L2 拆分出的子卡 |
| `_client/core/qa_browser.py` | 截图问答 AI 改进卡 |
| `scripts/sentence_card.py` | (早已有) |
| `scripts/vocab/anki_from_word.py` | (早已有) |

**打捞**:`deck:系统默认 tag:pdf-snippets` 的 39 张卡已 `changeDeck` → QA;「系统默认」清零。

## 验证(端到端,走真实制卡链路)

```
制卡前 QA = 39
_run_snippets_to(make_anki=True) → ok=True added=2 deck=QA
  steps = ['正在整理要做卡的内容', 'AI 正在生成卡片', '正在写入 Anki（2 张）']
★ 新卡落在: ['QA']
QA(制卡后) = 41
QA(sync 后) = 41      ← sync 不会冲掉(它是无辜的)
```

制卡后的即时 AnkiWeb sync **保留**(已证明无害),卡能及时推到手机。
