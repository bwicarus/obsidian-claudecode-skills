# 技能树 / 知识图谱（KG）系统

学习"路线图"可视化 + 笔记↔节点关联管理。后端 KG JSON + Flask 路由 + 前端单页 SVG。

## KG 文件结构

`knowledge_graph/<book>.json`，顶层字段：

| 字段 | 含义 |
|---|---|
| `book` | 书名 |
| `note_prefix` | 笔记命名前缀（如 LADR=`000-`），用于 `register_notes.py::_find_book_for_note` 反查 |
| `pdf` | 教材 PDF 路径 |
| `scan_config` | rescan_rolling 参数（enabled / pages_per_night / stable_threshold / max_stable_days / deep_model / deep_effort）|
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
| `/skilltree/` (GET) | 书本索引页（`skilltree_index`，render `skilltree_index.html`）|
| `/skilltree/api/books-meta` (GET) | 列**所有书**元数据（控制面板技能树 tab 用，不带 `<book>`）|
| `/skilltree/<book>/` | HTML 页面 |
| `/skilltree/<book>/data.json` | KG JSON 数据 |
| `/skilltree/<book>/pdf` (GET) | 取该书的教材 PDF（`skilltree_pdf`）|
| `/skilltree/<book>/api/edit` (POST) | 编辑，op：`add_edge` / `delete_edge` / `delete_node` / `merge` / `update_summary` / `set_meta`（`set_meta` 改 KG 顶层元数据，白名单 key = note_prefix / title / scan_config）|
| `/skilltree/<book>/api/archive-node` | 把节点归档到回收站笔记 |
| `/skilltree/<book>/api/restore-node` | 从回收站恢复（删 trash section + 创建独立笔记）|
| `/skilltree/<book>/api/build-note` | 从 PDF 创建笔记（含 AI 验证：ok/merge/delete）。**job 化**：秒回 `{ok, job_id}`，重活进后台线程（见下「build-note 流程」）|
| `/skilltree/<book>/api/build-note-status/<job_id>` (GET) | build-note job 轮询：`running` / `done`（带 `result`）/ `unknown`（webapp 重启 job 丢失，前端兜底提示）|
| `/skilltree/<book>/api/prune-notes` | 清理悬空笔记关联 |
| `/skilltree/<book>/api/toggle-tracked` (POST) | 切换**语法 KG L2 节点**的 `tracked`（仅 `kg.kind=='grammar'` + level 2 可切，body `{node_id}`；语法分析系统用，见 `grammar-analysis-system.md`）|
| `/skilltree/<book>/api/delete` | 删除整本 KG |

> 元数据编辑（title / note_prefix / scan_config）走 `POST /skilltree/<book>/api/edit` body `op="set_meta" key=... value=...`（control.html 第 877-884 行就是这么调的），**没有** `/skilltree/<book>/api/books-meta` 这个 per-book POST 路由。

控制面板技能树 tab：`https://bwicarus.taile44d0c.ts.net/control/`（Pi，当前唯一活跃实例；`bwicarus.space` 的 VPS 自 2026-06-10 暂停、代码停在 2026-05-28，本文后面记的 kg_audit 每本书开关等 2026-06-17 之后的改动只在 Pi 上有）→ "技能树" panel
- 书本列表 / 编辑 (title, note_prefix, scan_config) / 删除
- 额度消耗日志按钮 + modal（读 `state/quota_log.json`）
- **新建书本**按钮 → 弹 modal（见下）

### 新建书本 web UI（2026-05-25）

控制面板「技能树」panel 顶部「＋ 新建书本」按钮 → 弹 modal `openNewBookDialog()`：

```
┌─ 新建书本 ──────────────────────────────┐
│ Book ID (英文/数字)：    [000-LADR]      │
│ Title (中文显示)：       [LADR 4e 中译]  │
│ Note 前缀：             [资源/books/000-LADR/]
│ PDF 路径（vault 内）：   [资源/books/000-LADR/000-LADR4eChinese.pdf]
│ TOC 页范围：             [3-9]            │
│ 内容页范围：             [10-580]         │
│ → [开始构建] [取消]                       │
└──────────────────────────────────────────┘
```

后端 `POST /control/api/kg-build`（`control.py::control_kg_build`，后台线程串行）：
1. spawn `scripts/kg/build_nodes.py --pdf --book --model --effort [--pages]` → **直接写 `knowledge_graph/<book>.json`**（没有任何 yml / `nodes/<book>/` 目录）
2. 把 `note_prefix` / `title` 写进该 json
3. spawn `scripts/kg/extract_edges.py --kg <json> --in-place` → 推导 prereq 关系
4. 返回 `{job_id}`；前端轮询 `/control/api/kg-build-log?job=<job_id>` 滚动显示日志（stdout + stderr）。注意查询参数名是 `job`（响应体里字段才叫 `job_id`）

job 状态在内存 dict 里（webapp restart 丢失，但 KG 文件已落盘所以不影响最终结果）。

`build_nodes.py` 关键改动：**文字层 + 图像双输入**（2026-05-21 `df4e525`）：
- 旧版只给 AI 喂 PDF 文字层 → 数学符号、公式、上下标常被识别错（如 V₁ → V1，定理编号错位）
- 新版同时喂 **PDF 渲染图像 + 文字层**：AI 拿图当 ground truth，文字层只做候选辅助
- 修了大量"编号识别错位"bug（如本来是定理 5.2.3 被识别成 5.23）

### build-note 流程：AI 验证 + job 化（2026-06-10）

`POST /api/build-note` body `{node_id}` **秒回 `{ok, job_id}`**，重活全在后台线程
`_build_note_job(book, node_id)`（fitz 提页文本 + AI 验证可达 2min + 生成笔记 + `_edit_lock` 内更新 KG——
旧同步路由会撞 nginx 超时）。前端（skilltree.html L1409 起）2s 轮询
`GET /api/build-note-status/<job_id>`，`result` 与旧同步响应体一致，deleted / merged / ok 三分支复用。
- 同 (book, node_id) 已有 running job → 直接复用 `job_id`（响应带 `dedup:true`，防重复点击起双 job 烧双份 AI）
- job 存内存 dict `_build_note_jobs`（webapp 重启丢失 → status 返回 `unknown`）；每次新建顺手清 1h 前已完成的旧 job

**AI 验证**（仅 L2 且 `note_ref_ai_verified` 为假时跑；L1 整节笔记不验证）：`_ai_verify_node` 把节点元数据 +
PDF pages 实际文本 + 同节兄弟节点喂给 AI（claude_cli sonnet/medium），判定：
- `ok` → 正常建笔记
- `merge`（与某兄弟同概念）→ `_apply_edit(kg, "merge", {canonical: target_id, drop: [node_id]})` 自动合并
- `delete`（PDF 里该编号是例子等 KG 误识别）→ `_apply_edit delete_node` 自动删除
- AI 调用失败兜底返回 ok 但带 `_fallback: True` 标记（不阻断建笔记）

**验证结论落盘缓存** `state/skilltree-verify-cache.json`（`_verify_cache_key/hit/put`，skilltree.py L235 起）：
- 键 = `sha1(node.id|name|summary|pages|PDF mtime)`——节点元数据或 PDF 任一变化即失效重验
- **只缓存真实 `action==ok`**：delete/merge 执行后节点随之消失，缓存重放会误删/误并，绝不缓存；
  `_fallback` 的假 ok 同样不缓存（下次还有机会真验证）
- 命中 → 跳过重复验证直接建笔记；超 2000 条按插入序截断；写失败只损失缓存不阻断

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
- audit_kg 预算上限 `--budget-target-7d 60`（2026-05-26 从 88 调低）+ **`--budget-5h-cap 70`（2026-06-17 加）**防一晚消耗过大
- **按书开关 + 增量审查**（2026-06-17，commit dc720c7）：daily 每本书查 `server-config["kg_audit"]`：`enabled`（全局总开关）/ `books[<book>]`（每本，未列出用 `default`，均默认 True）→ 关了跳过 audit（关联+掌握度照常）；`incremental`（默认 True）→ 传 `--incremental` 只审「新增/改过」的节点（按 `node_audit_hash`=编号/名称/摘要/类型/页码 指纹比对 `audit_progress.audited_hashes`，全局表跨书合并不轮转）。控制面板「设置」面板有每本书开关（`/control/api/kg-audit` 列书 + `data-cfg-path` 复用 saveConfig）。config_schema 加 `kg_audit.{enabled,incremental,default}` + `books.*` 通配（validate_partial 支持末段 `*`）
- ⚠ **audit_kg 预算闸的「周低点暴涨」坑**（2026-06-16 实测：KG 步从常态 ~440s/Δ5h~15% 暴涨到 **3660s/Δ5h+87%(13→100%)/Δ7d-sonnet+24%**）：`audit_kg --deep --budget-loop` 对**每个 L2 节点**发 1 次 sonnet 深审（最多 `--budget-max-batches 30 × --ai-sample-size 20 = 600` 节点/本），预算闸 `can_run_aggressive`（`scripts/lib/claude_quota.py`）**只看 7d 利用率(<60%)+时间(<约 04:00 cutoff)、刻意不看 5h 窗口**。
  - **现象解释**：常态夜 7d-sonnet 起始 ≥60% → 闸第一批就 STOP，audit 几乎不跑(那 ~440s 是被闸住的样子)；某夜 7d-sonnet 正好滚到周低点(实测 0%)→ 闸整晚失效 → audit **跑满 600 节点上限**，叠加 LADR 触发「全图轮转、清空 `ai_audited_node_ids` 重审」放大到全 355 节点，把 5h 烧到 100%。
  - **诊断**：`state/quota_log.json` 看哪步 `delta` 非零(每步前后额度快照)；KG 步的 AI 调用**直接 spawn `/usr/bin/claude`、不写 `ai_calls.log`**，所以那个 log 在 KG 时段=0 行≠没消耗。
  - **修**：`--budget-5h-cap`（脚本默认 100=不限；daily 传 70）在 budget-loop 里补一道 `util_5h()` 天花板，5h 达 70% 即停，审查仍持续维护 KG、但单夜不独占整个 5h 窗口。`ed576b6`。**注**：argparse help 串里别写字面 `%`/`<`，会让 `format_help` 崩。

---

## 踩坑笔记（gotchas）

**重要：这些坑会反复回来咬你，必读。**

### `_note_to_covered_l2` 是覆盖不是 union

`link_with_ai.py` 的「增量合并」块（约第 336-344 行）：每次 AI 跑完，对处理的笔记 `persistent[rel] = sorted(set(covered))` **直接覆盖**，不是 union。后果：

- 手动编辑 KG 给某节点加 `containing_notes`，下次 link_with_ai 重新处理该笔记会**清空**手动添加的关联
- 防御：用解锁规则过滤（已加 `_rejected_links`）+ 必要时改节点本身的 name/summary 让 AI 不再漏判（而不是手动加 containing_notes）

### 手动跑 KG 脚本必须先 source .env

`scripts/config.py:20` 默认 fallback：
```python
PROJECT_DIR = Path(os.environ.get("CLAUDE_PROJECT", r"C:\claude"))
```

服务器侧手动跑 `link_with_ai.py` / `link_and_mastery.py` / `audit_kg.py` 等，shell 必须先：
```bash
cd /home/bwicarus/claude
set -a; source .env; set +a
```

否则 `config.PROJECT_DIR = C:\claude`（Windows fallback），脚本会报 `ModuleNotFoundError: No module named 'ai_backends'`（因为它从 `config.PROJECT_DIR / "_client" / "core"` 加 sys.path）。

systemd 服务（qa-server / bwicarus-daily）有 `EnvironmentFile=...env`，跑这些不用手动 source。

### subprocess 输出 buffer 陷阱

`subprocess.Popen(cmd, stdout=log_file)` 时，Python 子进程的 stdout 是 **block-buffered**（不是 line-buffered）。如果子进程 print 但不 flush 就退出，最后 4KB 输出可能丢。

**症状**：log 戛然而止（比如 register log "Anki:不制卡 | 20s" 之后什么都没有），但下一步函数明明被调用了。

**修法**：
- 子进程命令前加 `-u`：`subprocess.run([py, "-u", "scripts/..."])` 强制 unbuffered
- 关键 print 加 `flush=True`：`print(..., flush=True)`

这就是 2026-05-26 修复 register 同步 bug 的核心可观测性增强。

### 服务器有两套 nginx 配置

- **VPS**（⏸ 2026-06-10 起暂停、代码停在 2026-05-28，日常不要再往这台部署）`/etc/nginx/sites-enabled/default`：跟 git 里 `_server_deploy/nginx/bwicarus.conf` 一致，历史上可 cp 部署
- **Pi** `/etc/nginx/sites-available/bwicarus`：Tailscale HTTPS Cert + 80/443 两 server 块，**与 git 版结构完全不同**。**绝不可 cp 覆盖**（会冲掉 Tailscale 证书配置 → 全站挂）。Pi 改 nginx 只能手 patch

### `_render_anim` 标志位的生命周期

`_renderAnim` 默认 false，只有 `renderAnim()` 函数会先置 true 然后 `render()` 内开头**立刻读取并 reset**。所以：
- 永远不会"连续两次 render 都带动画"——一次性消耗
- 手动调 `render()`（不经过 `renderAnim`）必定瞬间渲染
- `_renderAnim=true` 会影响 d3 的 `.exit()`/`enter`/`merge` 是否套 `.transition()`

---

## renderAnim 调用点对照

| 触发 | 文件:行 | 用途 |
|---|---|---|
| popstate（浏览器后退/前进） | skilltree.html `addEventListener("popstate", ...)` | 视图切换平滑 |
| pushView 新视图 | `function pushView(v)` | 进章节/小节视图 |
| goBack 到 home | `function goBack()` | 退回整体视图 |
| 折叠条点击 | locked-bar `.on("click", ... renderAnim())` | 章节折叠/展开 |
| 设置面板保存 | `closeSettings(); applyZoomState(); renderAnim()` | fitToWidth toggle 等触发重排 |
| 编辑数据后（删节点关详情） | `closeDetail(); renderAnim()` | 数据变更后视觉过渡 |

**不**用 renderAnim 的：
- 初始 load（首屏不带 fade-in，免眼花）
- 普通 render（fitToWidth padding 已变但视图未切换）
- resize debounce 后的 render（性能优先）
- 数据 reload 后（buildAggregatedEdges 等）的 render

---

## focus 面板内部布局算法

`renderFocusPanel(focusId)` 在 `_server_deploy/templates/skilltree.html` 内：

1. **章节筛选**：用 `computeChainSets(focusId)` 拿 ancestors/descendants，并集为 `chainSet`；遍历所有 L2 节点找 `ancestorAtLevel(nid, 0)`（章 id）属于 `chainSet`
2. **章节排序**：用全局 `depthByLevel[0]` 按 globalDepth 升序排，再 reverse（基础在底，跟 home 一致）
3. **章内分行**：每章里 chain 的 L2 节点按 `depthByLevel[2][n.id]` 分组，行按 depth 倒序排（远祖先在上）
4. **行内排序**：同一行节点按 `numeric_label` 字典序，再按 `name`
5. **行内列布局**：`cols = max(2, floor((availW + gapX) / (blockW + gapX)))`，行内节点居中
6. **章带**：左右各 4px 色线 + 左侧章名标签
7. **边**：只画两端都在 positions 里的 chain 边（chain class 自动从 `tileClass(d, focusId)` 算）

**布局参数**（小号紧凑）：`blockW=130 blockH=54 gapX=14 gapY=22 labelH=26 bandPadY=10`

**与 home 的区别**：
- home 显示所有 L2 + 全部章带；focus 面板只显示 chain 节点 + 它们所在的章带
- 整章无 chain → 不出现；某章某行无 chain → 该行不占空间（紧凑下移）

---

## CSS class 速查（skilltree.html）

| class | 触发 | 视觉效果 |
|---|---|---|
| `.tile.mastered` | mastery_level ≥ 2 | 绿框 `#34d399` + 深绿底 |
| `.tile.unlockable` | level ≥ 1 或前置全 mastered | 蓝框 `#60a5fa` + 深蓝底 |
| `.tile.locked` | 前置未满足 | 灰虚线框 |
| `.tile.previewable` (旧) | (兼容字段) | 等同 locked |
| `.tile.focus` | tileClass(d, focusedId) 时 d.id===focusedId | 黄边框 + drop-shadow（保留一处发光）|
| `.tile.chain-anc/desc/desc-locked/self/fade` | focus 模式应用 chain 角色 | 链路高亮（蓝/紫/虚线/黄/淡化）|
| `.tile.inferred` | mastery_inferred=true | 边框 stroke-dasharray + ↓ 角标 |
| `.tile.search-hit` | applySearchHighlight 匹配 | 粉框 |
| `g.locked-bar` | home 视图未开放章节 | 折叠条，点击展开/折叠 |
| `path.edge.mast/unl/prev/lock` | edgeClass(e) 输出 | 边颜色按目标节点 state |
| `path.edge.chain-edge-anc/desc/desc-locked/fade` | focus 模式 | 链路边高亮 |
| `#detail.theme-{mastered,unlockable,locked,previewable}` | showDetail 应用 stateOf(n) | 面板配色 + 顶部色带 |

---

## 全局变量速查

| 变量 | 类型 | 作用 |
|---|---|---|
| `current` | `{kind: "home"\|"chapter"\|"section", id?}` | 当前视图状态 |
| `focusPanelId` | string \| null | focus 面板是否打开 + 显示哪个节点 |
| `detailNodeId` | string \| null | detail 面板当前节点 id |
| `_renderAnim` | bool | render 是否带 transition（一次性，render 内自动 reset） |
| `chainAncestors / chainDescendants` | Set | computeChainSets 的结果（focus 链路）|
| `_chainCacheFocusId` | string | chain cache，避免同 focus 重复计算 |
| `wrap.__positions` | object | render 末尾暴露的 `{node_id: {x, y}}`，供 closeDetail 等外部用 |
| `positions`（render 内局部） | object | render 内当前布局的节点坐标 |
| `depthByLevel` | `{0: {}, 1: {}, 2: {}}` | 每层节点 depth map（computeGlobalDepth 写入）|
| `chapterColor` | `{chap_id: hex}` | computeChapterColors 算出 |
| `id2` | `{node_id: node}` | 节点 id → 对象的 lookup |
| `nodesByLevel[0/1/2]` | `[node, ...]` | 按 level 分组的节点列表 |
| `edgesByLevel[0/1/2]` | `[edge, ...]` | 聚合后的 edges |

---

## 架构决策记录（ADR）

### ADR-1：home 永远在底层，focus 改叠加面板

**问题**：原 focus 视图是切 `current.kind="focus"` 然后 `render()` 整图重排，节点位置都变，detail 打开/关闭也重排 → 用户操作时视觉混乱。

**决策**（2026-05-26）：home 整体视图始终渲染、永远不重排；focus 用左侧叠加面板（自己的 SVG）显示紧凑 chain map；detail 右侧叠加。

**代价**：focus 面板的 SVG 是独立一份，焦点节点切换时面板内全部重渲染（但只有 chain 节点 ~< 50 个，开销远小于全图）。

**好处**：home 永久缓存位置；detail 开关零代价；视觉稳定。

### ADR-2.5：关闭详情/退出 focus 时 viewport 跟随节点（不跳）

**问题**：原行为关闭 detail 面板后整图重排，用户视野的"当前节点"位置可能跳变，迷失。

**决策**（2026-05-26 `64cfcc1` / `77002ee`）：关闭详情 / 退出 focus 时：
1. 先算关闭后目标节点在新布局里的坐标（render 末尾暴露 `wrap.__positions`）
2. 跟 d3 transition 同步平移 viewport，让目标节点屏幕坐标**不动**
3. transition 时长 380ms（跟 renderAnim 一致）

实现：`closeDetail()` 末尾读旧 `screenPos`，render 完后 `d3.zoomTransform` 调整让 `screenPos` 不变。

### ADR-2.7：三处 UX 微调（2026-05-26 `f30de63`）

| 微调 | 做了什么 | 为啥 |
|---|---|---|
| 关闭详情滚到底 | closeDetail 后让页面滚到最下（chain 末端常在底部）| 用户改完节点想看下一步关联在哪 |
| 折叠条放最上 | `g.locked-bar` 永远 render 在最后（z-order 最高，盖住节点边）| 之前被某些 edge 盖住点不到 |
| Tooltip 设置开关 | 设置面板加"显示 tooltip" 复选框 + localStorage 持久化 | 老用户不需要 tooltip 想关掉 |

### ADR-2.8：A71-减法与除法 历史孤儿清理（2026-05-26 `f64910d`）

**问题**：vault 出现一个 `A71-减法与除法.md`（不符合 000- 命名规则），是早期 `_rand_hex3()` 在 `scripts/anki_from_note.py` 里给笔记起名的遗留，已经 dead code。

**决策**：手动重命名为 `000-减法与除法.md`；删 `_rand_hex3` 函数；该笔记已重新 register 进 KG。

**经验**：vault 内不符合 `[0-9A-Fa-f]{3}-*.md` 模式的文件 → pending_notes 直接跳过，长期积累成孤儿。register / KG 关联都不会主动发现。

### ADR-2.9：build_nodes 文字层 + 图像双输入（2026-05-21 `df4e525`）

**问题**：PDF 文字层 OCR 偶尔识别错位（如定理 5.2.3 → 5.23），AI 直接拿错的文字会生成错号节点。

**决策**：`build_nodes.py` 现在喂给 AI **PDF 渲染图像（rasterize 后的 PNG）+ 文字层** 双输入：
- 图像作 ground truth，让 AI "看见" 真实排版（上下标、公式、定理编号视觉对齐）
- 文字层作辅助候选（避免 OCR 错误）

实现：`page.get_pixmap(matrix=fitz.Matrix(DPI/72, DPI/72))`（`DPI=144`，约 2x）渲染高清 PNG → base64 → 传给 vision-capable AI 后端（claude / gpt-4o vision）。

**坑**：vision token 是文本 token ~10x 单价；只在首次构建跑（不进 daily 流程）。

### ADR-3：解锁规则过滤放在 link_with_ai

**问题**：AI 容易把笔记关联到尚未学到的深层节点（笔记内容偶尔提及但用户实际没学），导致技能树前进度虚高。

**决策**（2026-05-26）：在 link_with_ai 跑完 AI 后，用**当前**（即上一轮 link_and_mastery 算的）节点 state 做过滤；拒绝写入 `_rejected_links`。

**为什么不放在 link_and_mastery**：link_and_mastery 是无 AI 的纯计算，需要从 `_note_to_covered_l2` 读已确认的关联；如果它再 filter 一次会导致语义混乱（哪个是真的关联）。

**为什么用上一轮 state**：daily 内 state_map 冻结，一致性比实时性重要。如果允许动态更新 state，本轮内笔记 A → 节点 B 通过让 B 变 unlockable，再判定笔记 C → 节点 C（依赖 B），形成隐式依赖序，难以调试。**下次 daily 跑会自然 propagate。**

### ADR-3：pending_kg_sync.json 用 touch mtime 兜底

**问题**：register 同步 KG bug 偶尔触发（subprocess buffer / AI 失败），如何让 daily 兜底重做。

**备选方案**：
- A. 给 link_with_ai 加 `--include-notes <path1,path2>` 参数：精准但需要改 link_with_ai 命令行接口
- B. 把 pending 笔记的 mtime touch 到当前时间：复用现有 `--since-days N` 路径，零侵入

**决策**：选 B。代价是 touch 文件可能让 `pending_notes.py` 误以为是新笔记（但 pending_notes 用 content hash 判断不是 mtime，所以没影响）。

**实现**：`daily_anki_status.py::run_kg_link_mastery` 开头读 `state/pending_kg_sync.json`，对每个 path `Path(p).touch(exist_ok=True)`；跑完 link_with_ai 后清空 pending。

### ADR-4：QA 创建笔记不带 `[0-9A-Fa-f]{3}-` 前缀

**问题**：用户从 QA 截图问答中"创建新笔记"，是否要触发完整 register 流程（PDF 标注 / 摘要 / 制卡 / KG 同步）？

**决策**（2026-05-26）：不触发。笔记名直接用用户输入（清理非法字符），**不加 `[0-9A-Fa-f]{3}-` 前缀**，自动避开 `pending_notes.py` 的扫描规则。这是"草稿型"笔记，用户后续可以手动重命名（加前缀）让其进入正式流程。

**代价**：草稿笔记可能堆积。用户自负责清理/合并/重命名。

---

## 反向链接 propagate_back_links 与 register 的关系

`scripts/register_notes.py::process_note` 流程：

```
PDF 标注 → 图片标注 → AI 分析 → 写入索引 → 查找关联 →
写入正向链接 → propagate_back_links（反向传播）→ Anki 制卡
```

- **查找关联**：`ai_find_related` 用 AI 找该笔记关联的其它笔记（基于 index 内容）
- **写入正向链接**：在当前笔记末尾追加 `[[相关笔记]]`
- **反向传播**：`propagate_back_links(source_path)` 遍历刚写入的 link 列表，在每个目标笔记的"相关笔记"节追加 `[[source]]`（互相可见）
- **跟 KG 的关系**：完全独立。KG 关联（`_note_to_covered_l2`）跟笔记内的"相关笔记"链接是两套机制：
  - 笔记内链接：用户语义层的"这两个概念有关"
  - KG 关联：技能解锁层的"这篇笔记覆盖了哪个原子知识点"

`backfill_back_links.py` 是一次性脚本，给存量笔记补全反向链接（不在 daily 流程内）。
