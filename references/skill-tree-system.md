# 技能树 / 知识图谱（KG）系统

学习"路线图"可视化 + 笔记↔节点关联管理。后端 KG JSON + Flask 路由 + 前端单页 SVG。

## KG 文件结构

`knowledge_graph/<book>.json`，顶层字段：

| 字段 | 含义 |
|---|---|
| `book` | 书名 |
| `note_prefix` | 笔记命名前缀（如 LADR=`000-`），用于 `register_notes.py::_find_book_for_note` 反查 |
| `pdf` | 教材 PDF 路径 |
| `scan_config` | rescan_rolling 参数（pages_per_night / model / stable_threshold / max_stable_days）|
| `nodes` | L0 章 / L1 节 / L2 知识点节点列表 |
| `edges` | 节点间依赖（`kind="prereq"`）|
| `_note_to_covered_l2` | 持久化字典 `{note_rel_path: [node_id, ...]}` |
| `_rejected_links` | 关联校验拒绝记录（见下文）|
| `_archive_suggestions` | 回收站候选节点 id 列表（auto_archive 写入）|

### 节点字段（L2 关键）
`id` / `name` / `numeric_label` / `level` (0/1/2) / `summary` / `pages` / `containing_notes` /
`mastery` (0-1) / `mastery_level` (0-5) / `mastery_inferred` (反向传递) / `state` (mastered/unlockable/locked) /
`note_ref` / `note_ref_ai_verified` / `card_refs` / `has_cards`

## 关联建立流程

笔记 ↔ KG 节点关联由 `scripts/kg/link_with_ai.py` 用 AI 判定 + 规则过滤。

```
笔记 mtime 改 / pending_kg_sync.json 列出
        ↓
link_with_ai.py
  └─ AI 判定每篇笔记覆盖哪些 L2 节点（严格 prompt，宁可空也不错关联）
  └─ 解锁规则过滤（见下）
  └─ 写 _note_to_covered_l2 / _rejected_links
        ↓
link_and_mastery.py
  └─ 重算每个节点 mastery / state / level
  └─ 反向传递（下游 mastered → 推断上游 inferred）
        ↓
KG 写回 → 技能树前端读 data.json 渲染
```

### 解锁规则过滤（2026-05-26 加）

AI 判定后对每个 (note, node) 关联强制校验，避免笔记跳着关联到深层 locked 节点：

| 节点状态 | 关联行为 |
|---|---|
| `state ∈ {mastered, unlockable}` | ✅ 通过；旧 `_rejected_links` 记录清除 |
| `state == locked` 且**前置 ≥ 50% mastered**（level≥2 占比） | ✅ 通过 |
| `state == locked` 且前置 < 50% mastered | ❌ 拒绝，写入 `_rejected_links` |
| `state == locked` 且**无前置数据** | ❌ 拒绝（保守，避免乱关联） |

`_rejected_links` 结构：
```json
{
  "<note_rel_path>": {
    "<node_id>": {
      "note_hash": "<sha1[:16] of cleaned content>",
      "reason": "node locked + 前置解锁 1/4=25%",
      "rejected_at": "<iso>"
    }
  }
}
```

**幂等**：每次 link_with_ai 跑都**重新评估规则**（节点 state / 前置 mastery 可能已变）：
- 规则通过 → 清除该 (note, node) 的拒绝记录
- 规则不通过且笔记 hash 没变 → 仅刷时间戳（不计入新拒绝数）
- 规则不通过且笔记 hash 变了 → 重新记录

效果：笔记没改但前置学完了 → 下次 daily 自动放行；前置没学完 → 永远拒绝，下次不浪费 AI 评估同一关联。

### register 同步链路（2026-05-26 修复）

`register_notes.py` 跑完后通过 `update_kg_for_processed()` 调用 link_with_ai/link_and_mastery 同步 KG。历史 bug + 修复：

- **根因**：`process_note` 返回 result 字典缺 `"note"` 字段，main() 用 `r.get("note")` 过滤 → processed 永远空 → update_kg 静默跳过
- **修复**：result 加 `"note": str(note_path)` + main 兼容 fallback
- **可观测性**：subprocess 用 `python -u` 强制 unbuffered；所有 print 加 `flush=True`
- **自动补救**：跑完校验 `_note_to_covered_l2` 是否含 processed 笔记，缺漏写 `state/pending_kg_sync.json`
- **daily 兜底**：`daily_anki_status.py::run_kg_link_mastery` 启动时读 pending 文件 → `touch` 笔记 mtime 强制纳入本次 `link_with_ai --since-days 7` → 跑完清空 pending

## UI 架构（_server_deploy/templates/skilltree.html）

### 三层叠加面板（2026-05-26 重构）

```
canvas-wrap (home SVG, 永远渲染、永远在底层)
    │
    ├── #focus-panel  ← 左侧拉出，焦点链路紧凑视图
    │   └── 独立 SVG，只画 chain 节点 + 它们所在的章带
    │       （整章/整行无 chain 节点 → 消失，跟原 focus 视图同款紧凑布局）
    │
    └── #detail       ← 右侧拉出，节点详情
```

**好处**：home 永远不重排，detail 打开/关闭不触发全图 render；focus 切换只重画面板内 SVG。

### 状态管理

| 字段 | 含义 |
|---|---|
| `current` | `{kind: "home" | "chapter" | "section", id?}`（focus 不再走 view kind） |
| `focusPanelId` | null = 面板关；非空 = 面板显示该节点的 chain 视图 |
| `detailNodeId` | null / 当前 detail 显示的节点 id |
| `_renderAnim` | true 时 render 用 d3 transition（视图切换平滑），false 瞬间渲染（resize / 普通 render） |

### 关键函数

| 函数 | 职责 |
|---|---|
| `render()` | 渲染 home SVG（带 chain class 应用） |
| `renderAnim()` | `_renderAnim=true; render()`，视图切换处用 |
| `openFocusPanel(id)` | 显示 focus 面板 + 应用 home chain 高亮 + 打开 detail；写 localStorage（最近学习节点） |
| `closeFocusPanel()` | 收回两个面板 + 清 home chain 高亮 |
| `renderFocusPanel(focusId)` | 面板内独立 SVG，紧凑章带 + 仅 chain 节点 |
| `applyChainHighlightOnHome(focusId)` | home tile 切换 chain class（不重排） |
| `findRecentLearningNode()` | 进页面定位：localStorage → mastery 1-4 最浅 depth → unlockable 最浅 |

### 性能优化

- d3 transition 仅在 `_renderAnim=true` 时启用（视图切换专用，380ms）
- resize 监听 debounce 200ms
- chain map 章节带 / drop-shadow 已简化（chain-anc/desc/self 不再带 filter，只 stroke + fill）
- focus 面板内 SVG 紧凑（只画 chain 节点，不复刻全图）

## 服务端 API（_server_deploy/skilltree.py）

| 路由 | 用途 |
|---|---|
| `/skilltree/<book>/` | HTML 页面 |
| `/skilltree/<book>/data.json` | KG JSON 数据 |
| `/skilltree/<book>/api/edit` | 编辑：add_prereq / delete_edge / delete_node / merge / update_summary |
| `/skilltree/<book>/api/archive-node` | 把节点归档到回收站笔记 |
| `/skilltree/<book>/api/restore-node` | 从回收站恢复（删 trash section + 创建独立笔记）|
| `/skilltree/<book>/api/build-note` | 从 PDF 创建笔记（含 AI 验证：ok/merge/delete） |
| `/skilltree/<book>/api/prune-notes` | 清理悬空笔记关联 |
| `/skilltree/<book>/api/books-meta` (POST) | 改 KG 元数据：title / note_prefix / scan_config |
| `/skilltree/<book>/api/delete` | 删除整本 KG |

控制面板技能树 tab：`https://bwicarus.space/control/` → "技能树" panel
- 书本列表 / 编辑 (title, note_prefix, scan_config) / 删除
- 额度消耗日志按钮 + modal（读 `state/quota_log.json`）

## 回收站机制

"过于简单的定义和知识点"（如 V/W 这种记号）的归档：

1. `auto_archive.py` 用启发式（contextual + legacy 两种）扫 KG，把 trivial 节点 id 写到 `KG._archive_suggestions`
2. 前端节点的 🗑 按钮高亮（仅提示）
3. 用户**手动**点击 🗑 → 调 `/api/archive-node` 真正归档：
   - 把节点 PDF 区域写到 `<prefix>回收站-<chap_num>.md`
   - KG 节点 `containing_notes` 设为回收站笔记 → `level=2/5` mastered（不阻塞下游解锁）
4. 取消归档：`/api/restore-node` 删 trash 节段 + 创建独立笔记

## 调试 / 维护

- 手动跑 link_with_ai（需先 `source .env` 拿到 `CLAUDE_PROJECT`）：
  ```bash
  cd /home/bwicarus/claude
  set -a; source .env; set +a
  /usr/bin/python3 -u scripts/kg/link_with_ai.py --kg knowledge_graph/LADR.json --since-days 2 --in-place
  /usr/bin/python3 -u scripts/kg/link_and_mastery.py --kg knowledge_graph/LADR.json --in-place
  ```
- 查 `_rejected_links`：
  ```bash
  python3 -c "import json; kg=json.load(open('knowledge_graph/LADR.json')); print(json.dumps(kg.get('_rejected_links',{}), ensure_ascii=False, indent=2))"
  ```
- daily 自动跑：`bwicarus-daily.timer` (01:00) → `daily_anki_status.py::run_kg_link_mastery` (link_with_ai → link_and_mastery → audit_kg → rescan_rolling)
- audit_kg 预算上限 `--budget-target-7d 60`（2026-05-26 从 88 调低）防一晚消耗过大
