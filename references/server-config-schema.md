# server-config.json 字段对照

`/home/bwicarus/claude/state/server-config.json`（VPS 是 `/root/claude/state/...`）。控制面板「设置」面板写入，所有 Windows EXE 客户端开关跟服务端共享同一份。

`qa-server.py::DEFAULT_CONFIG` 是默认值；用户改的部分通过 `_deep_merge` 覆盖到默认上。

## 顶层字段

### `qa_*` —— QA browser 配置

| 字段 | 默认 | 含义 |
|---|---|---|
| `qa_vault_path` | `/root/obsidian`（VPS）/ `/home/bwicarus/obsidian`（Pi） | Obsidian vault 根 |
| `qa_index_dir` | `<CLAUDE_DIR>/index` | 索引目录（关联知识用） |
| `qa_anki_records_dir` | `<CLAUDE_DIR>/anki/records` | Anki 卡片记录目录 |
| `qa_exercises_subdir` | `习题` | 「保存到 vault」时的习题子目录 |
| `qa_wrong_subdir` | `错题` | 「保存到 vault」时的错题子目录 |
| `qa_remote_access` | `true` | 父开关：允许局域网 / Tailscale 访问 daemon（监听 0.0.0.0）|
| `qa_remote_daemon` | `true` | 子开关：常驻 daemon :9091（关掉则只在本机 `ctrl+shift+q` 临时启）|

### `ai_backend` + `ai.{backend}.*` —— AI 后端

| 字段 | 默认 | 含义 |
|---|---|---|
| `ai_backend` | `claude_cli` | 当前激活的 backend 名 |
| `ai.claude_cli.command` | `/usr/bin/claude` | Claude CLI 路径 |
| `ai.claude_cli.model` | `opus` / `sonnet` | 模型（影响 max output / context）|
| `ai.claude_cli.effort` | `medium` / `high` / `max` | reasoning effort |
| `ai.codex_cli.command` | `/usr/bin/codex` | OpenAI CLI |
| `ai.codex_cli.model` | `""` | 模型名（gpt-5 / gpt-5.5 等） |
| `ai.claude_api.api_key` / `.model` | `""` | Anthropic API 直连参数 |
| `ai.openai_api.api_key` / `.model` | `""` | OpenAI API 直连参数 |
| `ai.ollama.{api_key,model,base_url}` | `""` | 本机 ollama HTTP |

切 backend 不重启：`_AdapterSession.send()` 每次读 cfg。

### `anki.*` —— Anki / AnkiConnect

| 字段 | 默认 | 含义 |
|---|---|---|
| `anki.exe_path` | `/opt/anki-venv/bin/anki` | Anki 可执行（Linux/服务端是 venv 内）|
| `anki.connect_url` | `http://127.0.0.1:8765` | AnkiConnect 监听 |
| `anki.auto_restart` | `false`（手动按钮）/ `true`（服务端 daily）| AnkiConnect 不可达时是否自动 force_restart Anki（杀进程 + 启动 + 等 ≤180s） |

### `scheduled_register.*` —— 凌晨定时任务

| 字段 | 默认 | 含义 |
|---|---|---|
| `scheduled_register.enabled` | `true` | 是否启用凌晨 daily（服务端是 systemd timer，所以这个开关给 Windows 客户端用）|
| `scheduled_register.time` | `04:00` (Windows) / `01:00` (Pi) | 触发时刻（Pi 的实际触发由 systemd timer 控制）|
| `scheduled_register.wake_anki` | `true` | 凌晨触发时是否 force_restart Anki |
| `scheduled_register.upload_after` | `false` (本机) / `true` (服务端) | 凌晨跑完后是否 upload dashboard/history |

### `auto_upload_after_register` —— 手动登记后

| 字段 | 默认 | 含义 |
|---|---|---|
| `auto_upload_after_register` | `false` | 客户端「立即运行登记新笔记」按钮跑完后是否自动接「刷新并上传网页」|

### `weak_card_refresh.*` —— 薄弱卡 AI 改写

`refresh_weak_cards.py --task weak`：找连续 lapses 多的卡，AI L1 改写问法 / L2 拆分。

| 字段 | 默认 | 含义 |
|---|---|---|
| `weak_card_refresh.enabled` | `true` | 凌晨是否跑这一步 |
| `weak_card_refresh.min_lapses` | `""`（用脚本默认 3） | 卡片至少 lapses 几次才入候选 |
| `weak_card_refresh.limit` | `""`（默认 20） | 单次最多处理多少张 |
| `weak_card_refresh.cooldown_days` | `""`（默认 30） | 改过的卡多少天内不再改 |
| `weak_card_refresh.escalate_lapses` | `""` | 升级 L2（拆删）的 lapses 阈值 |
| `weak_card_refresh.auto_escalate` | `true` | 多次 L1 仍 lapse 自动升 L2 |

### `card_antimodel.*` —— 已掌握卡换问法

`refresh_weak_cards.py --task antimodel`：稳定 ≥ N 天且复习足够多次的卡，AI 换角度重问（防"只记问法"）。

| 字段 | 默认 | 含义 |
|---|---|---|
| `card_antimodel.enabled` | `true` | 凌晨是否跑 |
| `card_antimodel.min_stability_days` | `60` | 卡稳定多少天后才换问法 |
| `card_antimodel.min_reps` | `5` | 至少复习几次 |
| `card_antimodel.limit` | `20` | 单次最多处理 |
| `card_antimodel.cooldown_days` | `60` | 改过的卡多少天内不再换 |

### `card_quality.*` —— 卡片质量体检

`refresh_weak_cards.py --task quality`：找答案太长 / 多知识点 / 指代不清 / again+hard 占比高的低质卡，AI 评分并建议原地优化或拆分。

| 字段 | 默认 | 含义 |
|---|---|---|
| `card_quality.enabled` | `false`（默认关，因为 AI 评分贵） | 凌晨是否跑 |
| `card_quality.max_back_len` | `200` | 答案最大字数（超过算"过长"启发式）|
| `card_quality.limit` | `20` | 单次最多评 |
| `card_quality.cooldown_days` | `45` | 已评卡多少天内不再评 |
| `card_quality.auto_split` | `true` | AI 判定"应拆"时是否自动拆 |
| `card_quality.relative_threshold` | `true` | 用同 type 的 P85 作动态阈值（而非绝对值）|
| `card_quality.hard_again_ratio` | `""`（默认 0.4） | again+hard 占比阈值 |
| `card_quality.min_reviews` | `""`（默认 3） | 至少复习几次才入候选 |
| `card_quality.sample_per_run` | `""`（默认 5） | 每晚随机采样几张全卡片库覆盖盲区 |

### `card_qa.*` —— QA 卡片改进

| 字段 | 默认 | 含义 |
|---|---|---|
| `card_qa.delete_original` | `true` | QA cardCtx 模式「修改 Anki」时，AI 生成新卡后是否删原卡（关掉则原 + 新都留）|

### `sidebar_links` —— 控制面板侧边栏自定义链接

`[{"label": "Obsidian", "url": "obsidian://..."}, ...]`，per-user 持久化（不在 server-config，在 `/api/nav-links` 用户单独存）。

## 字段位置流转

```
                          ┌──────────────────────────┐
   Windows EXE 客户端 ──→  │ server-config.json       │ ←── 控制面板「设置」面板
                          │ (state/, 服务器侧)        │
                          └─────────┬────────────────┘
                                    │
                  ┌─────────────────┼──────────────────┐
                  ▼                 ▼                  ▼
              qa-server         daily_anki_status   client GUI
              (读 ai_*,         (读 weak/antimodel  (Windows 端读
              qa_*, anki.*)     /quality 开关)       同步状态)
```

## 修改方法

1. **控制面板**（推荐）：`https://bwicarus.space/control/` →「设置」面板 → 修改 → 保存。后端 `/control/api/config` POST 写回文件。
2. **直接编辑**：`nano /home/bwicarus/claude/state/server-config.json`。qa-server 无需重启（每次读最新），但 daily 是 systemd timer，下次触发才生效。
3. **不要**手动编辑同时控制面板也在编辑：会有 race condition 覆盖。

## 客户端 EXE 字段对照

bwicarus-client（Windows EXE）的 `cfg` 跟 server-config 字段名一一对应。客户端的 `apply_cfg_to_server()` 把本地改动同步给服务端。详见 [`client-exe-development.md`](client-exe-development.md)。
