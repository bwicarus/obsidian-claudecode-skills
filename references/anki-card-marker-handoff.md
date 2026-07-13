
## 七、【追加·最高优先】制卡后 AnkiWeb sync 把本地新卡冲掉(独立后端 bug)

用户复查发现 QA 牌组始终 0 张。受控实验(直连 AnkiConnect 127.0.0.1:8765)锁定根因:

```
addNote 到 QA        → {"result":<note_id>,"error":null}   建卡成功
findNotes deck:QA    → 1                                   sync 前有卡
sync (action:sync)   → {"result":null,"error":null}        触发同步
findNotes deck:QA    → 0                                   ★ sync 后卡没了
```

**结论**:`_run_snippets_to`(pdf_reader.py ~8078)制卡后 `if added>0` 的 **fire-and-forget AnkiWeb sync**
把刚 addNote 的本地新卡冲掉了——sync 方向解析成「以 AnkiWeb 为准/下载覆盖」,还没上传的本地新卡被删。
`_task_anki`(voice.py)也有同款 sync。用户每次制卡→立即 sync→卡被冲没 = 「QA 恒 0」真相。
注意「今日新增(全牌组)3」仍在(早先 auto-anki 生词卡),只有**刚建还没上传**的 QA 卡被冲,像是时序/冲突问题
(sync 在卡 upload 前就以 download 方向 resolve)。

### 待查/待修方向(下个 session,先修这个再谈 UI)
1. 为什么 sync 是 download-wins:headless anki 与 AnkiWeb collection 是否 **schema 分叉/full-sync required**
   → 冲突时 aqt 默认可能选下载。查 `journalctl -u anki-headless` sync 时的 full/conflict 提示。
2. **制卡后不要 fire-and-forget 立即 sync**,或改为:先确保 addNote 落盘(collection save)+ sync 用
   **upload/merge 方向**(AnkiConnect `sync` 不带方向;可能要 `ensure` collection saved,或换成
   定时 anki-sync-refresh.timer 的既有 sync 路径——那个 15min timer 是否也在冲卡?一并查)。
3. 临时缓解:把 `_run_snippets_to` / `_task_anki` 里的即时 sync **去掉**(制卡只 addNote,同步交给
   dailytimer / anki-sync-refresh),先让卡能留在 QA;再单独解决 sync 冲突根因。
4. 验证修复:建卡→查 QA→(按修复方案)→再查 QA 应仍在;并确认能同步到用户 AnkiWeb 设备。

⚠ 这是**功能性数据丢失 bug,优先级高于 UI 批次**。UI 完成回报要有意义,前提是卡真的留下来了。

### 相关既有设施
- `scripts/anki_sync_refresh.py` + `anki-sync-refresh.timer`(15min 拉手机复习数据,只读卡状态**不改牌组**)——
  确认它的 sync 是否也可能参与冲卡。
- daily 流程 `daily_anki_status.py` 末尾 AnkiWeb sync。
- CLAUDE.md「Anki 25 headless 关键坑」章。
