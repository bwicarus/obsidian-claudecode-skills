# bwicarus-client

跑在用户本机的 Windows 客户端，把本地 dashboard / history / private 数据推到 bwicarus.space，
后续会承担 AI 后端调用、Anki 同步、登记新笔记等任务。

## 目录结构

```
_client/
├─ launcher/main.py         # 启动器：检查更新 → 下载 core → 加载执行
├─ core/                    # 业务代码（热更新单元）
│   ├─ main.py              #   入口（CORE_VERSION 在这里）
│   ├─ gui.py               #   主窗口（customtkinter）
│   ├─ api_client.py        #   服务端 HTTP 通讯
│   └─ uploader.py          #   dashboard 上传任务
├─ build/
│   ├─ build_core.py        # 打包 core/ → core-<ver>.zip + manifest.json
│   ├─ build_launcher.py    # PyInstaller 打 launcher → bwicarus-client.exe
│   └─ deploy_core.sh       # scp dist/client/* 到服务端
└─ dist/
    └─ client/              # build_core.py 输出
```

## 运行时数据位置（用户机）

```
%LOCALAPPDATA%\bwicarus-client\
├─ config.json              # 服务端 URL / API token / 本地路径 / current_core_version
├─ launcher.log             # launcher 自身日志
└─ core\
    └─ <version>\           # 已下载的 core 包，每个版本一个目录
```

## 热更新机制

1. launcher.exe 启动 → `GET <server>/static/client/manifest.json`
2. 比对 `manifest.version` 与本地 `config.current_core_version`
3. 不一致就下载 `core_url` 指向的 zip → 解压到 `core/<version>/.partial` → 原子 rename 到 `core/<version>/`
4. `sys.path.insert(0, core_dir)` → `import main` → 调用 `main.main(config_path)`

加新功能 / 修 bug：改 core/ 里的 .py，把 `core/main.py:CORE_VERSION` 升一档，跑 `build/build_core.py` + `deploy_core.sh`。
**不需要重新分发 .exe**。

只在以下情况才重发 launcher.exe：
- launcher/main.py 自身改动（极少：网络 / 启动逻辑 / 配置 schema 大改）
- 业务代码引入新的运行时依赖（pip 包），launcher.exe 里没打进去

## 发布新版 core

```bash
# 1. 改代码（不要忘了升 core/main.py 的 CORE_VERSION）
# 2. 打包 + 部署
cd C:/claude/_client
python build/build_core.py
build/deploy_core.sh   # 或直接 scp dist/client/* root@31.220.31.30:/var/www/html/static/client/
```

服务端 nginx 通过 `try_files $uri $uri/ =404` 直接 serve `/var/www/html/static/client/*`，
不经过 webapp，不需要重启服务。

## 打 launcher.exe（首次或 launcher 自身改动时）

```bash
pip install pyinstaller customtkinter
python build/build_launcher.py
# 产物: dist/bwicarus-client.exe
```

## API token

在 https://bwicarus.space/profile/ 用账号登录后生成 Bearer token，填进客户端配置。
客户端推数据走：

```
POST /api/upload/<dataset>          # dataset ∈ {dashboard, history, private}
Authorization: Bearer <token>
X-Path: <相对路径>                  # 例如 index.html, dashboard.json
Content-Type: application/octet-stream
<body = 文件原始字节>
```

## 设计原则

- **launcher 极简稳定**：只做下载 + 加载，不带业务逻辑
- **core 可热更**：所有业务代码都在 core/，纯 .py
- **依赖打进 launcher.exe**：customtkinter / requests 等运行时由 PyInstaller 打包，core 包只含 .py 业务
- **失败兜底**：服务端不可达时用本地 cached core 启动；新版 core 启动失败可回滚（手工把 config 里 current_core_version 改回旧版）

## 当前进度

- [x] launcher 骨架 + 热更新链路（manifest → 下 core → 加载）
- [x] core 骨架 + customtkinter 主窗口
- [x] 配置 GUI Tab 化（基础 / 上传 / AI / Anki / 笔记登记）
- [x] dashboard 手动上传到服务端
- [x] AI 后端 adapter（claude_cli / codex_cli / claude_api / openai_api / ollama），含 ping 测试
- [x] Anki 配置 + AnkiConnect ping + 启动 Anki
- [x] 「运行登记新笔记」按钮（subprocess 流式日志 + 自动 upload）
- [x] 系统托盘（pystray）+ 关窗最小化拦截
- [x] vault 自动监听（watchdog 防抖 8s）+ 自动触发登记
- [x] PyInstaller 打 launcher.exe（dist/bwicarus-client.exe，~32MB）
- [ ] AI adapter 的 chat() 接入业务流程（目前 register_notes.py 仍走主项目的 ai_client.py）
- [ ] 客户端独立分发：把 register_notes 流程也搬进 core，不依赖外部 scripts
