# Windows 服务器三守护合一（方案，2026-09-03 用户要求「记得早点合并」）

## 现状（为什么要合）

Windows 上现在同时有三个常驻守护，外加一个计划任务盯着后两个：

| 进程 | 管什么 | 看护它的是谁 |
|---|---|---|
| **ReaderPC 服务器**（`ReaderPC-Server.exe`，PyInstaller，自带托盘） | C# Direct 桥（43128/43129）、PC 预处理 worker、复制账本、语音链 | 自己的看门狗 `start-readerpc.vbs`（每 5 分钟） |
| Flask 托盘守护（`_server_deploy/local_supervisor.pyw`） | `app.py` 127.0.0.1:5000（App 的服务器 API） | 计划任务 `BwicarusServer` → `scripts/windows_server_watchdog.ps1` |
| sidecar 守护（`scripts/windows_sidecar_services.py`） | voice-rt 8767 / watch-voice 8768 / rbi 8769 / mcp 8766 | 同上 |

2026-09-03 一天里后两个死了三次（重启后 Run 项没执行、守护自己卡死、再次重启），
每次都靠事后补看护。ReaderPC 的服务托管框架（`PcOcrServiceController`：status 文件 +
start/stop + 进程身份校验 + 日志尾）已经是成熟的一套，Flask 与 sidecar 没有理由另起两套。

## 目标形态

**只剩 ReaderPC 一个常驻进程、一个托盘图标、一个看门狗。**

```
ReaderPC-Server.exe
├── DirectBridge (C#)            43128/43129   ← 现有
├── PcOcr worker                 (按需)        ← 现有 PcOcrServiceController
├── WebApp service   ★新          127.0.0.1:5000  python app.py
└── Sidecar services ★新
    ├── voice-rt   8767
    ├── watch-voice 8768
    ├── rbi        8769
    └── mcp        8766
```

## 施工步骤

1. **抽象一个通用 `ManagedProcessController`**（从 `PcOcrServiceController` 泛化）：
   命令行、cwd、env、health 探针（TCP 端口 / HTTP GET / status 文件三选一）、
   崩溃退避拉起、连续快失败熔断、日志文件。status 写 `readerpc-server.status.json` 的
   `services` 子树，控制面板（`control_plane.py`）与托盘菜单据此显示每个服务的状态并可单独重启。
2. **WebApp 服务**：命令 = `python app.py`，cwd `_server_deploy`，env 从工作树 `.env.local` 读
   （沿用 `windows_sidecar_services.load_env`），health = HTTP `127.0.0.1:5000/login` 任何响应。
   把 `local_supervisor.pyw` 的两项副作用搬过来：代码变更自动重启（`_server_deploy/*.py` mtime）、
   Obsidian Headless Sync 计划任务看护。托盘菜单里的「打开 dashboard」也搬过来。
3. **四个 sidecar**：各自一个 `ManagedProcessController`，health = TCP 端口可连。
4. **看门狗只留一个**：`start-readerpc.vbs` 已经每 5 分钟检查 ReaderPC 在不在；
   ReaderPC 内部再对每个服务做 2 秒一轮的存活检查 + 心跳（`readerpc-server.status.json` 带
   `heartbeatEpochMs`）。删除计划任务 `BwicarusServer`、HKCU Run `BwicarusLocal` / `BwicarusSidecars`、
   `local_supervisor.pyw`、`windows_sidecar_services.py`、`windows_server_watchdog.ps1`。
5. **发布**：`package_readerpc_server.py` 的 `RUNTIME_SOURCES` 加 `_server_deploy/app.py` 及其
   全部依赖模块（这是最大的工作量：Flask 应用依赖 `_server_deploy/*.py` 几十个文件 + `scripts/`
   若干 —— 用 `reader_deploy_manifest.py` 的 150 项清单作为打包源列表，避免手工漏文件），
   或者 ReaderPC 只托管**工作树里的** `app.py`（不打进包），代价是服务器代码随工作树而变、
   没有版本原子性。**建议先走后者**（今天的实际运行方式就是工作树），把原子发布留到第二阶段。
6. **迁移顺序**：先在 ReaderPC 里以「影子模式」拉起 WebApp/sidecar 控制器但不启动（只显示状态）→
   确认无冲突 → 关掉旧守护改由 ReaderPC 启动 → 观察一天 → 删旧守护与计划任务。

## 边界

- `_server_deploy/app.py` 的启动参数与 `.env.local` 不变，App 与 tailscale serve 映射不动。
- ReaderPC 退出即全停（2026-08-17 用户定案）继续成立：退出 ReaderPC = 服务器全停，符合「一个开关」。
- 契约：`test_desktop_launcher.py` / `test_bridge_core.py` 需要为新控制器补用例（沿 PcOcr 的夹具）。

## 进度

- **2026-09-03 步骤 1 + 步骤 6 前半（影子模式）已上线**：ReaderPC `0.1.119` 原子安装并完成启动接管。
  `readerpc_services.py` 新增 `ManagedProcessController`（TCP 探针 / 退避拉起 / 进程身份校验 /
  只停自己拉起的），`default_server_services()` 从工作树发现 `app.py` + `.env.local`，给出
  webapp 5000 与 voice-rt 8767 / watch-voice 8768 / rbi 8769 / mcp 8766。启动器多一行「服务器服务」
  与偏好 `manageServerServices`（默认 False：只观测端口，端口由旧守护占着就算可达，不拉起）；
  状态文件 `readerpc-server.status.json` 带 `services` 子树。
- 下一步：观察一天影子行无误报 → 停计划任务 `BwicarusServer` 与两个旧守护 → 打开托管开关 →
  观察一天 → 删旧守护脚本、计划任务与 HKCU Run 项（步骤 4）。步骤 2 的两项副作用（代码变更重启、
  Obsidian Sync 计划任务看护）尚未搬，切换前必须补上。
