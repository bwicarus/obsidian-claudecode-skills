# bwicarus-client.exe 开发指南

Windows EXE 客户端的完整开发 / 构建 / 部署流程。**注意**：服务器侧迁移后 EXE 已经被网页控制面板替代，这个客户端目前是**双跑过渡期**的备份角色，长期会退役。但代码仍在维护，因为：
- 凌晨定时（仅当用户希望在自己 Windows 上跑而不是服务器 systemd timer）
- vault watcher（文件改动自动触发 register）
- 任务监视悬浮窗
- 本机 ctrl+shift+q 截图问答

## 架构两层

```
bwicarus-client.exe (PyInstaller --onefile, ~33MB)
        ↓ 启动时检查 manifest.json
        ↓ 自动下载 core-<version>.zip
        ↓ 解压到 %LOCALAPPDATA%\bwicarus-client\core\<version>\
        ↓ 加载 core/main.py 运行 GUI
```

**Launcher 与 core 解耦**的目的：
- launcher 不变（用户下载一次后不必更新），core 通过 git pull / 服务器替换实现热更
- 每次发新版只要重 build core zip 部署到服务器，下次客户端启动自动拉新版

## 源码位置

| 路径 | 内容 |
|---|---|
| `_client/launcher/main.py` | PyInstaller 入口 + 主流程（`LAUNCHER_VERSION = "0.2.0"`）：选数据目录 → 拉 manifest → 下载 core zip → 解压 → load core/main。含 `_pointer_file` / `_default_data_dir` / `fetch_manifest` / `download_core` / `load_core` / `main` |
| `_client/launcher/_runtime_imports.py` | 占位 import 清单，让 PyInstaller 静态分析时把 core 运行时依赖（customtkinter / watchdog / pystray / PIL / keyboard / stdlib 等）全打进 .exe（本身不被调用） |
| `_client/core/main.py` | 实际业务入口 + `CORE_VERSION = "0.9.32"` 写死（`main.py:13`） |
| `_client/core/gui.py` | customtkinter 主窗口（7 个 tab，约 1800 行） |
| `_client/core/wizard.py` | 首次启动 4 步配置向导 |
| `_client/core/anki.py` | AnkiClient（ping + sync + ensure_alive） |
| `_client/core/api_client.py` | 调 webapp `/api/upload/<dataset>` 等 |
| `_client/core/uploader.py` | upload_dataset（白名单 JSON+图片）|
| `_client/core/cmd_server_thread.py` | iPad 触发 HTTP server（:9090） |
| `_client/core/qa_browser.py` | 截图问答 web（:9091 + 本机 ctrl+shift+q）|
| `_client/core/runner.py` | run_script subprocess 跑主项目脚本 |
| `_client/core/paths.py` | 数据目录抽象（BWICARUS_APP_DIR / pointer / 默认） |
| `_client/core/watcher.py` | watchdog 监听 vault |
| `_client/core/scheduler.py` | 每日定时任务（默认 04:00） |
| `_client/core/floating_window.py` | 任务进度悬浮窗 |
| `_client/core/tray.py` | pystray 托盘 |
| `_client/core/hotkey.py` | 全局快捷键 |
| `_client/core/startup.py` | HKCU\\...\\Run 开机自启 |
| `_client/core/ai_backends.py` | 5 个 AI 后端 adapter |
| `_client/core/auth.py` | device-link OAuth |
| `_client/core/screenshot.py` | 全屏截图工具（PIL ImageGrab → PNG bytes，`capture_full_screen()`） |
| `_client/core/task_state.py` | 只读主项目 `state/active_tasks.json`（task_tracker 写），给悬浮窗当数据源 |
| `_client/core/card_improvement_service.py` | 卡片改进共享层（`CardReference` 解析 / prompt 构造 / 草稿解析），`qa_browser.py:16` 顶层硬 import |

> ⚠ **这两个文件已不只是客户端代码**：`card_improvement_service.py` 与 `ai_backends.py` 都在 `scripts/reader_deploy_manifest.py` 清单里（分别装到 webapp 和 kg_runtime），改完必须走 `scripts/deploy_reader.sh`，不能只当作随 core zip 发的客户端文件。

> ⚠️ **build/deploy 脚本不在仓库**：`_client/` 下当前只有 `core/` 和 `launcher/` 两个目录（外加 `README.md`），**没有 `_client/build/` 目录**。早期文档提到的 `build_core.py` / `build_launcher.py` / `deploy_core.sh` / `bwicarus-client.spec` 全仓库找不到。core zip / manifest.json 的生成与部署方式目前在仓库里无迹可寻（疑似手工或在另一台机器上做），下文「改 core / 改 launcher / 发布新版」的命令示例属于 **理想流程，并不能照搬执行**——动手前需先确认真实打包脚本所在。`_client/README.md` 也引用了同一批不存在的脚本。

## 改 core 业务逻辑（最常见）

> ⚠️ 下面第 3、4 步引用的 `build/build_core.py`、`build/deploy_core.sh` **当前不在仓库**（见上文源码位置表的警告）。打包/部署的真实手段需先确认；以下仅为目标流程示意。

```bash
# 1. 改 _client/core/*.py 任何文件
# 2. 升级版本号（写死在 _client/core/main.py:13）
sed -i 's/CORE_VERSION = "0.9.32"/CORE_VERSION = "0.9.33"/' _client/core/main.py

# 3. build core zip + manifest（脚本缺失，待补 / 手工打包）
cd _client
python build/build_core.py        # ← 该脚本仓库里不存在

# 4. 部署到服务器（脚本缺失）
bash build/deploy_core.sh         # ← 该脚本仓库里不存在
# 或手动：scp dist/client/* pi:/tmp/ && ssh pi 'sudo install -m 644 /tmp/core-*.zip /tmp/manifest.json /var/www/html/static/client/'   # Pi（ssh 别名 pi，登录用户 bwicarus，写 /var/www 需 sudo）；root@bwicarus.space 是 2026-06-10 起暂停的 VPS

# 5. 客户端下次启动自动拉新版（用户不用操作）
```

## 改 launcher（罕见）

通常**不需要**改 launcher。只有改动数据目录指针逻辑 / 拉 core 流程 / 首次配置向导**外壳**时才动（入口文件是 `_client/launcher/main.py`，依赖清单在 `_client/launcher/_runtime_imports.py`）。改完后用 PyInstaller `--onefile` 打包：

> ⚠️ 下面命令引用的 `build/bwicarus-client.spec` **当前不在仓库**（`_client/build/` 目录不存在）；真实打包入口应指向 `_client/launcher/main.py`。命令仅为示意，实际 spec / 打包脚本需先确认或补齐。

```bash
cd _client
# PyInstaller 打包（spec 文件缺失，需指向 launcher/main.py 自行准备）
C:\Users\bwica\AppData\Local\Programs\Python\Python313\Scripts\pyinstaller.exe \
    --onefile --noconsole \
    --distpath dist \
    --workpath build \
    launcher/main.py        # 或自备 .spec
# 部署到服务器（用户下次「下载客户端」时拿到新 .exe）
scp dist/bwicarus-client.exe pi:/tmp/ && ssh pi 'sudo install -m 644 /tmp/bwicarus-client.exe /var/www/html/static/client/'   # 目标是 Pi（ssh 别名 pi，登录用户 bwicarus，写 /var/www 需 sudo）；root@bwicarus.space 是 2026-06-10 起暂停的 VPS
```

旧 launcher 已下载的用户**不会自动更新 launcher 自身**——他们需要重新去 webapp `/profile/` 下载新版。所以**尽量在 core 里实现，不动 launcher**。

## 版本号管理

- `CORE_VERSION = "0.9.32"`（写死在 `_client/core/main.py:13`）
- `LAUNCHER_VERSION = "0.2.0"`（写死在 `_client/launcher/main.py:30`，仅用于日志/UA，不参与 core 选择）
- launcher 的版本比对逻辑只做 `manifest.version != cfg.current_core_version` 的字符串不等判断（`launcher/main.py` 的 `main()` 里），**没有任何最低 launcher 版本校验**，也从不读取 `min_launcher` 字段（代码里根本没有这个字段）。如需「防止旧 launcher 加载新 core」的下限保护，需先在 launcher 里实现。
- 每次 core 改动建议升级 patch 号（0.9.32 → 0.9.33），让用户客户端感知有新版

## 服务端文件

| 路径 | 内容 |
|---|---|
| `/var/www/html/static/client/core-<v>.zip` | core 业务逻辑（每版本独立文件） |
| `/var/www/html/static/client/manifest.json` | core 版本指针。launcher 实际只消费 `version` + `core_url` 两个键（`download_core()`）；**不校验 sha256，也不读 size**（代码里无任何 sha256/size/min_launcher 校验逻辑） |
| `/var/www/html/static/client/bwicarus-client.exe` | launcher exe（一次性下载） |

nginx 配置中 `/static/` 由 nginx 直接 serve（不经过 Flask）。

## 客户端运行时数据

只有 `datadir.txt` 路径写死在 `%APPDATA%`（launcher 唯一硬编码的位置，作指针）。其余文件全部落在**用户首次启动时选定的数据目录**（由 `datadir.txt` 指向）——`%LOCALAPPDATA%\bwicarus-client\` 只是 launcher `_default_data_dir()` 的默认兜底值，用户在首次启动弹窗里可改到 D 盘 / 网盘 / U 盘。下表用 `<数据目录>` 表示该可变根（默认即 `%LOCALAPPDATA%\bwicarus-client\`）。

| 路径 | 内容 |
|---|---|
| `%APPDATA%\bwicarus-client\datadir.txt` | 指针文件（写死位置，指向真实数据目录）|
| `<数据目录>\core\<version>\` | 解压后的 core .py 文件 |
| `<数据目录>\config.json` | GUI 配置 |
| `<数据目录>\cmd_server_key.txt` | iPad API key |
| `<数据目录>\launcher.log` | 启动日志 |
| `<数据目录>\qa-history\` / `qa-temp\` | 截图问答临时图 |
| `<数据目录>\client.lock` | 主 GUI 单例锁（`--qa` 模式跳过）|

## 客户端调主项目 scripts

客户端**不复制**主项目脚本，而是 subprocess 调用。**三个按钮职责清晰分开（0.9.32+）**：

| 按钮 / 触发 | 跑什么 | 含必复习计算？|
|---|---|---|
| 「立即运行登记新笔记」 | `register_notes.py` 单步 | 否 |
| 「刷新并上传网页」 | anki_status → review_priority → export_dashboard → upload dashboard → export_history → upload history | 否（只读 Anki） |
| 「立即跑完整定时任务」 / 凌晨定时 | ensure_alive → register → anki_status → review_priority → **build_review_deck** → cleanup_orphans → export_dashboard → (可选)upload → AnkiWeb sync | **是** |

完整 daily 流程由 `gui.py::_full_daily_pipeline` 实现，等价主项目 `daily_anki_status.py` 和 `daily_anki_status.ps1`。

## 跟服务器侧网页控制面板的关系

服务器侧 `https://bwicarus.taile44d0c.ts.net/control/`（Pi；公网 `bwicarus.space` 是 2026-06-10 起暂停的 VPS）是 EXE 客户端的网页版替代，**做的事一样**：
- 触发 register / daily / ankiweb-sync / 重启 Anki
- 切 AI 后端（claude / gpt）
- 各种开关（auto_restart / wake_anki / upload_after / qa_remote_daemon 等）
- daily 任务进度可视化

差异：
- 客户端 EXE 多了：vault watcher（实时检测笔记变化）、本机截图问答 ctrl+shift+q、悬浮窗任务监视、托盘
- 服务器侧 webapp 多了：网页可视化 / 多设备访问 / iPad 远程触发

迁移期建议两边都跑，但**凌晨 daily 任务只让一边跑**（避免 AnkiWeb sync 冲突；注意两边时刻不同：Windows 客户端默认 04:00，Pi 的 `bwicarus-daily.timer` 是 `OnCalendar=*-*-* 01:00:00`）：
- 推荐让 **服务器 systemd timer** 跑（更稳，机器不睡）
- Windows 客户端的 scheduled_register.enabled 设 False（在「笔记登记」tab）
- 或者反过来由 Windows 跑、服务器 timer disable

## 历史 EXE 启动器（已废弃）

`launchers/` 目录下还有几个独立 EXE 启动器，**0.9.20+ 已整合进 bwicarus-client.exe**：

- `任务监视.exe` → 整合进 `tray.py` + `floating_window.py`
- `登记新笔记.exe` → 由客户端「立即运行登记新笔记」按钮替代
- `上传网页.exe` → 由客户端「刷新并上传网页」按钮替代
- `cmd_server.exe` → 整合为 `cmd_server_thread.py` 线程
- `截图问答.exe` → 整合为 `qa_browser.py` 模块
- `relay.exe` → 已废弃（截图问答现走 Tailscale，不需要 SSH 反向隧道）

旧 launchers 源码保留在 `launchers/*.py` 仅作历史记录。**新功能不要加到这里**，全部加到 `_client/core/`。
