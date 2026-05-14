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
| `_client/launcher/launcher.py` | PyInstaller 入口（onefile） |
| `_client/launcher/__main__.py` | launcher 主流程：选数据目录 → 拉 manifest → 下载 core zip → 解压 → load main |
| `_client/core/main.py` | 实际业务入口 + `CORE_VERSION = "0.9.32"` 写死 |
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
| `_client/build/build_core.py` | core 打包脚本（生成 core-<v>.zip + manifest.json）|
| `_client/build/build_launcher.py` | launcher.exe 打包脚本（PyInstaller --onefile） |
| `_client/build/bwicarus-client.spec` | PyInstaller spec 文件 |
| `_client/build/deploy_core.sh` | scp core zip + manifest 到服务器 |

## 改 core 业务逻辑（最常见）

```bash
# 1. 改 _client/core/*.py 任何文件
# 2. 升级版本号
sed -i 's/CORE_VERSION = "0.9.32"/CORE_VERSION = "0.9.33"/' _client/core/main.py

# 3. build core zip + manifest
cd _client
python build/build_core.py

# 4. 部署到服务器（scp）
bash build/deploy_core.sh
# 或手动：scp dist/client/* root@bwicarus.space:/var/www/html/static/client/

# 5. 客户端下次启动自动拉新版（用户不用操作）
```

## 改 launcher（罕见）

通常**不需要**改 launcher。只有改动数据目录指针逻辑 / 拉 core 流程 / 首次配置向导**外壳**时才动。改完后：

```bash
cd _client
# PyInstaller 打包
C:\Users\bwica\AppData\Local\Programs\Python\Python313\Scripts\pyinstaller.exe \
    --onefile --noconsole \
    --distpath dist \
    --workpath build \
    --specpath . \
    build/bwicarus-client.spec
# 部署到服务器（用户下次「下载客户端」时拿到新 .exe）
scp dist/bwicarus-client.exe root@bwicarus.space:/var/www/html/static/client/
```

旧 launcher 已下载的用户**不会自动更新 launcher 自身**——他们需要重新去 webapp `/profile/` 下载新版。所以**尽量在 core 里实现，不动 launcher**。

## 版本号管理

- `CORE_VERSION = "0.9.32"`（写死在 `_client/core/main.py:13`）
- `min_launcher = "0.1.0"` 在 manifest.json，限制最低 launcher 版本（防止旧 launcher 加载新 core）
- 每次 core 改动建议升级 patch 号（0.9.32 → 0.9.33），让用户客户端感知有新版

## 服务端文件

| 路径 | 内容 |
|---|---|
| `/var/www/html/static/client/core-<v>.zip` | core 业务逻辑（每版本独立文件） |
| `/var/www/html/static/client/manifest.json` | 当前版本指针 + sha256 + size + min_launcher |
| `/var/www/html/static/client/bwicarus-client.exe` | launcher exe（一次性下载） |

nginx 配置中 `/static/` 由 nginx 直接 serve（不经过 Flask）。

## 客户端运行时数据

| 路径 | 内容 |
|---|---|
| `%APPDATA%\bwicarus-client\datadir.txt` | 指针文件（指向真实数据目录）|
| `%LOCALAPPDATA%\bwicarus-client\core\<version>\` | 解压后的 core .py 文件 |
| `%LOCALAPPDATA%\bwicarus-client\config.json` | GUI 配置 |
| `%LOCALAPPDATA%\bwicarus-client\cmd_server_key.txt` | iPad API key |
| `%LOCALAPPDATA%\bwicarus-client\launcher.log` | 启动日志 |
| `%LOCALAPPDATA%\bwicarus-client\qa-history\` / `qa-temp\` | 截图问答临时图 |

## 客户端调主项目 scripts

客户端**不复制**主项目脚本，而是 subprocess 调用。**三个按钮职责清晰分开（0.9.32+）**：

| 按钮 / 触发 | 跑什么 | 含必复习计算？|
|---|---|---|
| 「立即运行登记新笔记」 | `register_notes.py` 单步 | 否 |
| 「刷新并上传网页」 | anki_status → review_priority → export_dashboard → upload dashboard → export_history → upload history | 否（只读 Anki） |
| 「立即跑完整定时任务」 / 凌晨定时 | ensure_alive → register → anki_status → review_priority → **build_review_deck** → cleanup_orphans → export_dashboard → (可选)upload → AnkiWeb sync | **是** |

完整 daily 流程由 `gui.py::_full_daily_pipeline` 实现，等价主项目 `daily_anki_status.py` 和 `daily_anki_status.ps1`。

## 跟服务器侧网页控制面板的关系

服务器侧 `https://bwicarus.space/control/` 是 EXE 客户端的网页版替代，**做的事一样**：
- 触发 register / daily / ankiweb-sync / 重启 Anki
- 切 AI 后端（claude / gpt）
- 各种开关（auto_restart / wake_anki / upload_after / qa_remote_daemon 等）
- daily 任务进度可视化

差异：
- 客户端 EXE 多了：vault watcher（实时检测笔记变化）、本机截图问答 ctrl+shift+q、悬浮窗任务监视、托盘
- 服务器侧 webapp 多了：网页可视化 / 多设备访问 / iPad 远程触发

迁移期建议两边都跑，但**凌晨 04:00 daily 任务只让一边跑**（避免 AnkiWeb sync 冲突）：
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
