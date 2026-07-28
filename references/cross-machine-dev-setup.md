# 跨机开发:Windows 编辑 + Pi 部署（2026-07-29 建立）

> 一句话:**代码在 Windows 上写,门禁与部署只在 Pi 上跑,两边靠 git 分支同步,不共享文件系统。**

## 为什么这么分

`deploy_reader.sh` 依赖 sudo / systemd / nginx / `/var/www/html` 原子安装,本质是 Pi 的东西;
真实数据(reader sidecars、KG、Anki、`webapp/data`)也只在 Pi。Windows 是 2TB 的开发机,
适合编辑与快测。所以不是"把整套搬去 Windows",而是**按能力分层**。

分层不是拍脑袋,是 2026-07-29 在 Windows 上把 86 个测试模块逐个跑出来的结果(见下)。

## 一次性设置

### Windows 侧

```powershell
git -C C:\claude fetch origin --prune
git -C C:\claude checkout -B learning-loop-review-fixes origin/learning-loop-review-fixes
git -C C:\claude config --local core.autocrlf false   # 换行归 .gitattributes 管,不归 autocrlf
git -C C:\claude config --local core.longpaths true   # 仓库最长路径 167 字符,加前缀仍 <260,保险
git -C C:\claude config --local core.quotepath false  # 中文文件名不转义显示
```

终端务必 `chcp 65001`,Python 加 `PYTHONUTF8=1` —— 否则 GBK 控制台读中文输出会
`UnicodeDecodeError`(建立本文档时实测撞到过)。

### 换行契约(`.gitattributes`,已入库)

| 类型 | 检出为 | 理由 |
|---|---|---|
| 默认 / `*.sh` / `*.service` / `*.timer` / `*.conf` | **LF** | Pi 是唯一部署源;带 CRLF 会让 shebang 与 unit 解析直接失败 |
| `*.ps1` / `*.cmd` / `*.bat` | **CRLF** | Windows 原生脚本 |
| `vendor/**`、`extensions/bw-reader-webext/vendor/**` | `-text` | 第三方,保持字节原样 |

⚠ **BOM 不归 git 管**。含中文的 PowerShell 5.1 脚本(`daily_anki_status.ps1` 等)必须保持
UTF-8 with BOM,编辑后自己补回。

### 不入库的东西

`webapp-data/`(本地实例运行时目录,含账号库 `app.db` 与日志)、
`_server_deploy/static/pdfjs/`(第三方 PDF.js,187 文件 4.5MB;生产上同样在 git 之外,
直接放 `/var/www/html/static/pdfjs`,**不在部署清单里**)。二者已进 `.gitignore`。
新机器要跑本地实例时,pdfjs 需要单独拷一份。

## 测试分层(2026-07-29 实测,86 个模块)

### ✅ Windows 上可跑:44 个

日常改代码的主力面。覆盖纯逻辑、服务层、卡片/助手/KG 算法、前端合同。

```
test_asr_ghost_sync                      test_assistant_fast_mode
test_assistant_review_mode               test_assistant_static_prompt_cache
test_card_candidate_service              test_card_improvement_assistant_api
test_card_improvement_commit             test_card_improvement_runtime
test_card_improvement_service            test_codex_appserver_client
…(其余见 `python3 -c` 推导:全部 test_*.py 减去下面两组)
```

### 📦 Windows 上跑不了:24 个(`fcntl`)

**结构性限制,不是缺包。** `fcntl` 是 POSIX 独有的文件锁,Windows 没有;
它被 `reader_sidecar_store` 那条链引用 —— 也就是**账户分区 sidecar 的并发锁是 POSIX-only 的**。

**2026-07-29 用户决定:不改。** 理由是这属于动生产并发锁,当前 Pi 上运行正常,
风险不对等。改到 sidecar 相关代码时,ssh 回 Pi 验证。

若将来要改,得单独立项(Windows 用 `msvcrt.locking` 或 `portalocker`),并带并发压测,
不能顺手改。

另有 2 个缺 `websockets`、若干缺 `spacy` / `pypinyin` —— 这些是**真·缺包**,pip 装得上,
想要可随时补。

### ❌ Windows 上失败是正确行为:17 个

打 systemd / `/var/www` / `sudo` / KG release 事务的门禁,本来就只在 Pi 成立,**不要去"修"**:

```
test_reader_deploy_transaction  test_reader_kg_release      test_kg_runtime_release
test_kg_runtime_launchers       test_kg_runtime_orchestrators test_reader_network_audit
test_reader_outgoing_context    test_reader_direct_commands  test_outgoing_fixture
test_context_sync_active        test_attention_ai_channel    test_pending_notes
test_task_tracker               test_unified_book_model      test_vbook_gate
test_vbook_resolver             test_codex_auth_lifecycle
```

（其中若干只是路径/环境假设,不是真 bug —— 判断标准:同一模块在 Pi 上是绿的。）

## 日常流程

1. **Windows 编辑** → 跑那 44 个模块 + node 合同测试快速自检
2. `git push` 到 `learning-loop-review-fixes`
3. **Pi 上** `git pull` → 跑全量门禁 → `bash scripts/deploy_reader.sh`
   （或从 Windows 用 `scripts/deploy_from_windows.ps1` 远程触发,门禁一条不少)
4. 真机验收 / E2E 只在 Pi

**绝不要**在 Windows 上直接改 Pi 的生产文件,也不要用共享文件系统绕开 git。

### 前置:Windows→Pi 的 SSH 别名

封装脚本走 Windows `~/.ssh/config` 里的 **`pi` 别名**(专用密钥
`C:/ssh/bwicarus-pi-ed25519-20260721` + `IdentitiesOnly yes`)。

⚠ **不要用裸 IP**。裸 IP 会退回默认密钥 `id_ed25519`,而 Pi 的 `authorized_keys` 里没有它 ——
表现是整条链**静默挂死在密码提示上**,Pi 的 sshd 日志里连一条连接记录都不会有
(2026-07-29 第一版封装就是这么卡了十几分钟才被发现)。

### 共享检出安全闸 `scripts/deploy_remote_guard.sh`

Pi 的工作树**天生是脏的** —— 每晚 daily 会重写 `anki/records/*.json` 与 `dashboard.json`,
再加上另一个 agent 的在制品。所以"脏就拒绝"这条规则不可用(封装会永远跑不起来)。

闸门只拦真正危险的那一种:**本次要拉的提交,恰好会改到别人正在改的文件**。

| 退出码 | 含义 | 处置 |
|---|---|---|
| 0 | 无来袭改动,或来袭与脏文件无交集 | 放行 |
| 2 | Pi 不在目标分支 | 停 —— **远程绝不切分支**,上机确认 |
| 3 | 来袭改动与脏文件有交集 | 停 —— 不自动 stash/reset,上机协调 |
| 4 | git 操作失败 | 停 |

闸门自身只做一次 `git fetch`,**不碰工作树**(隔离仓库三场景实测验证过)。
封装脚本远程只执行 `git merge --ff-only`,已彻底移除 `git checkout <branch>`。

## 多方协作(Pi 检出 + Windows 检出 + Codex)

Pi 的 `/home/bwicarus/claude` 现在是**共享检出**(我与 Codex 同用),Windows 是第三份工作副本。
规则:

- **Pi 检出仍是部署真源**;Windows 只经 git 往上游走,不直接改 Pi 工作树
- 同一时间**一个 agent 一个有界范围**,在 `references/reader-collaboration-status.md` 里认领
- 跨机改同一文件 = 冲突源。要并行就各开分支 + worktree,不要在同一分支上双写
- Pi 上未提交的 WIP 属于对方时,**不要顺手 commit 进你的快照**(2026-07-29 那次全树快照
  `4b3e84d` 就把 Codex 的在制品一并收了,已知悉)

## 已知历史

- Windows `C:\claude` 曾停在 `main`(7-12)带 28 个脏文件。逐字节比对确认**全部已被 Pi
  历史吸收、零独有改动**(是 5 月迁服务器前的旧快照),切换前 stash 为
  `pre-windows-migration-20260729` 留后路,可随时 `git stash pop` 取回。
- Pi 分支 `learning-loop-review-fixes` 领先 `origin/main` 497 个提交,2026-07-29 首次推送。
