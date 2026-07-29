# 部署流程（唯一权威，2026-07-29 收敛）

> 本文档取代此前散落在三处的三代做法。凡与
> `.claude/skills/website.md`、`references/webapp-development.md`、
> `references/reader-extension-handoff.md` 旧文字冲突的，**以本文为准**。

## 先决事实

- **唯一部署机是 Pi**（`/home/bwicarus/claude` 检出、`/home/bwicarus/webapp` 生产）。
  VPS 自 2026-06-10 暂停，代码停在 5-28；除非明确要恢复 VPS，否则不要往它部署。
- **Windows 不直接写 Pi 生产**。源码只经 git 上游流动（见
  [`cross-machine-dev-setup.md`](cross-machine-dev-setup.md)）。
- 改动分两类，**判据是部署清单，不是文件所在目录**。

## 第一步：判断你的改动属于哪一类

```bash
python3 scripts/reader_deploy_manifest.py | cut -f1 | grep -F '<你改的文件路径>'
```

`scripts/reader_deploy_manifest.py` 是生产文件清单的**唯一事实源**（当前 150 项）。
命中 = A 类，没命中 = B 类。旧文档里任何手写的文件列表都不能代替它。

| | A 类（清单内） | B 类（清单外） |
|---|---|---|
| 典型 | `app.py`、`control.py`、`skilltree.py`、`pdf_reader.py`、`assistant.py`、`voice.py`、KG runtime、systemd unit、7 个阅读器模板、`nav.js` 与 cache-bust 静态资产 | `insights.py`、`fitness*.py`、`youtube_*.py`、`qa_server.py`、`mcp_server.py`、`templates/control.html`、`templates/fitness/*.html`、`static/fitness-plan.json` |
| 部署方式 | `scripts/deploy_reader.sh`（唯一写入口） | `cp` + `systemctl restart webapp` |
| 事务/回滚 | 脚本内建 | **没有，自己先备份** |

⚠ `control.py` 在清单内、`control.html` 在清单外——同一个功能的两个文件走不同链路，
改控制面板时两条都要走。

## A 类：走原子部署脚本

### 从 Windows（推荐，日常路径）

```powershell
powershell -File scripts\deploy_from_windows.ps1 -PreflightOnly
```

薄封装，不含门禁：查本地干净且已推送 → `deploy_remote_guard.sh` 安全闸 →
Pi 上 `git merge --ff-only` → Pi 上跑 `deploy_reader.sh`。任一段失败即停。
预检通过、**且人工验收通过**后，去掉 `-PreflightOnly` 再跑一次。

⚠ 这条链**必须在 Windows 本机终端跑**。经 `ssh → cmd → powershell` 嵌套远程驱动会因引号解析
失败并留下孤儿进程（2026-07-29 实测）。

### 在 Pi 上

```bash
bash scripts/deploy_reader.sh --preflight-only   # 无副作用，不建 backup/release/current
bash scripts/deploy_reader.sh                    # 正式
```

参数：`--no-e2e` 跳过 E2E 冒烟（不建议）；**`--pc` 不要用**——它会把候选 tar 直接解到
Windows `C:/claude`，绕过 git 覆盖工作树，与跨机边界冲突。

### 脚本已经保证的事（不要手工重做）

调用方**不需要**再手算 SHA、逐字节比对源文件、手列回滚目录。脚本内建：

- 四层摘要校验：`verify_candidate_digest` / `verify_deploy_payload_digest` /
  `verify_validation_digest` / `verify_checkout_inputs_match_candidate`（部署前后各验一轮）
- 原子安装 + 时间戳备份 + 失败自动 `rollback_deploy`，取证目录
  `/home/bwicarus/deploy-backups/reader/<DEPLOY_ID>/`
- KG 作为不可变 release 发布，并断言部署过程未意外写入 KG 状态
- 冻结写者 timer、记录/恢复 active units
- 健康检查：`webapp.service`(5000) / `voice-rt.service`(8767) HTTP+TCP 探针与运行时稳定性断言
- E2E 冒烟 `scripts/reader_e2e.py`

**失败即回滚并报错**。所以证据是"脚本退出 0 + 事务目录路径"，不是一段手工核对散文。

## B 类：清单外文件

没有事务保护，自己负责：

```bash
# Pi 上
cp /home/bwicarus/webapp/insights.py ~/deploy-backups/manual/insights.py.$(date +%s)
cp _server_deploy/insights.py /home/bwicarus/webapp/insights.py
sudo systemctl restart webapp && sleep 2 && systemctl is-active webapp
journalctl -u webapp -n 20 --no-pager
```

静态前端有个独立陷阱：**nginx(443) 从 `/var/www/html/static/` 服务，Flask 的 `/static/`
是陈旧副本**。改前端必须部署到 nginx 静态目录，直连 `127.0.0.1:5000` 看到的是旧的。

nginx 配置：**Pi 的 `/etc/nginx/sites-available/bwicarus` 与 git 里的 VPS 版结构完全不同，
绝不可 cp 覆盖**（会冲掉 Tailscale 证书配置，全站挂）。只能手工 patch 对应 server 块，
`nginx -t` 通过后再 reload。

## 阅读器 / PWA / 扩展改动的额外门禁

来自 `CLAUDE.md` 顶部交接入口，A/B 分类之外**另加**：

```bash
python3 extensions/bw-reader-webext/handoff_check.py            # 改动前
python3 extensions/bw-reader-webext/handoff_check.py --full     # 改动后
python3 extensions/bw-reader-webext/handoff_check.py --production  # 发布前，比对 Pi 生产与测试渠道
```

共享源码变化后还要跑：`build.py` → `node --test tests/reader_contract/*.test.mjs` →
`python3 -m unittest discover -s tests -p 'test_*.py'` → `test_release_pipeline.py`。
Windows 上只有 44 个 Python 模块可跑，另 24 个因 `fcntl` 结构性跑不了、17 个 Pi-only
失败是正确行为——清单见 [`cross-machine-dev-setup.md`](cross-machine-dev-setup.md)，不要去"修"。

## 验收与登记

- **验收按改动类型分级**（2026-07-29 用户拍板）：

  | 改动类型 | 部署前 |
  |---|---|
  | 新功能、UI/视觉变更、交互变更、数据迁移/schema、不可逆操作、扩展正式渠道发布 | **必须**先交付完整人工验收清单，用户做视觉与交互，agent 只监控后台请求/数据流/日志/持久化 |
  | 修 bug、重构、性能优化、纯文档 | **预检通过即可部署**，不必等用户验收 |

  免验收前提（缺一条就退回必须验收）：不改变任何用户可见界面或交互 / 被修行为有合同测试覆盖 /
  `deploy_reader.sh` 全程通过。部署后必须主动报健康检查结果。拿不准就按"必须验收"。
- 浏览器自动化算工程回归，**不能冒充**需验收档的用户人工验收。
- Windows 侧浏览器测试只用 `BW Codex Chrome Test` + `%LOCALAPPDATA%\BWReaderExtensionTest\browser-profile-v2`，
  或 Claude Code 内置 Browser pane（独立 profile、无扩展、无 cookie，做不了扩展链测试）。
  **不动日常 Chrome、账号和已装扩展。**
- 部署后往 [`reader-collaboration-status.md`](reader-collaboration-status.md) 追加一段，
  控制在 6 行内：改了什么 / 怎么验的（命令 + 结论，不复述计数）/ 明确没做什么 / 下一位做什么。

## 已废弃（保留以免有人照着旧文档跑）

| 旧做法 | 出处 | 为什么废 |
|---|---|---|
| `scp _server_deploy/x.py root@bwicarus.space:/root/webapp/` | 旧 `webapp-development.md` §部署流程、`website.md` | 目标是已暂停的 VPS；且 A 类文件现由 `deploy_reader.sh` 原子部署 |
| 手工制作时间戳回滚副本、逐文件核对部署清单 | 旧 `reader-extension-handoff.md` §11 | `deploy_reader.sh` + `reader_deploy_manifest.py` 已内建，手工重做只是重复 |
| 从 Windows `scp`/`tar` 直推 Pi 生产或工作树（含 `deploy_reader.sh --pc`） | 迁移前习惯 | 绕过 git，与跨机边界冲突，可能覆盖未提交改动 |
