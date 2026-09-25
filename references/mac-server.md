# Mac mini 服务器与开发机（2026-09-25 从 Windows 迁移）

> **一句话**：服务器与开发都在 Mac mini 上。App 连的 `bwicarus-2.taile44d0c.ts.net`
> 现在指向 Mac（Tailscale 设备改名实现，App 没改一行）。Windows 改名 `windows-pc`，
> 服务器进程全停，只剩备份价值。

## 1. 机器与访问

| 项 | 值 |
|---|---|
| 机器 | Mac mini，Apple M6，16 GB，macOS 27，内置盘 460 GB（迁移后剩 ~330 GB） |
| 用户 | `xuehaoyan`（管理员组） |
| Tailscale 名 | `bwicarus-2.taile44d0c.ts.net`，IP `100.66.124.26`（原名 mac-mini） |
| 远程 | `ssh xuehaoyan@100.66.124.26`（Windows 的 `~/.ssh/id_ed25519` 已授权） |
| 旧服务器 | Windows，Tailscale 名改为 `windows-pc`（100.99.9.124），服务已停 |

⚠ **远程 SSH 会话不能代码签名**（读不到登录钥匙串里的私钥，报 `errSecInternalComponent`）。
编译、模拟器、测试可以远程做；装真机、发 TestFlight 要在 Mac 桌面上开的会话里做
（Mac 上装有 Claude 桌面版），或先在 Mac 上 `security unlock-keychain`。

## 2. 目录布局（在用数据在内置盘，代码/缓存/归档/备份在外接盘 BWDev）

```
~/BW/
  src/claude -> /Volumes/BWDev/src/claude   代码仓库（符号链接；实体在外接盘，2026-09-25 迁）
  runtime/releases/<时间>-<提交>/  部署出来的代码副本（服务从这里跑）
  runtime/current -> releases/…  当前版本
  data/
    state/                     学习状态（原 Windows C:\tmp\reader-card-anchor-release\state，15 GB，kj 13 GB）
    webapp-data/               网页后端数据（app.db 等）
    obsidian/                  Obsidian 库（2758 文件，与 Windows 逐字节核对一致）
    BWReader/                  原 %LOCALAPPDATA%\BWReader（书、模型、卡片资源、语音设置、复制账本…）
    bridge/                    桥的安装根：native-host/（配置）+ runtime/（运行数据）
    project/{anki,index,dashboard,history,temp}   仓库里会被写入的小目录
    （legacy/、win-bridge-install/ 已逐字节校验后迁到 /Volumes/BWDev/archive/）
  config/server.env            服务环境变量（含 SECRET_KEY，权限 600，不进 git）
  config/codex-mcp-sections.toml  追加到 ~/.codex/config.toml 的两段 MCP（已追加）
  config/jev-api.txt           语音路由用的密钥文件（权限 600）
  venv/server/                 服务 Python 3.13（依赖与 Windows 那份 Python 逐包对齐）
  logs/<服务>.log              各服务日志
  xcode-derived -> /Volumes/BWDev/xcode-derived   命令行编译缓存（符号链接）
```

**必须存在的链接**（`deploy_mac.py` 会自动建前两个）：

| 链接 | 指向 | 为什么 |
|---|---|---|
| `~/Library/Application Support/BWReader` | `~/BW/data/BWReader` | .NET 8 在 macOS 上把 LocalApplicationData 映射到这里（**不是** ~/.local/share）。缺了它桥读空目录 → App 报「语音核心没在跑」 |
| `~/.local/share/BWReader` | 同上 | 保险 |
| `~/bw-computer-voice-bridge` | `~/BW/data/bridge` | 多处 Python 写死 `Path.home()/"bw-computer-voice-bridge"/"runtime"` |

Python 侧的「本地应用数据」靠环境变量 `LOCALAPPDATA=~/BW/data`（在 server.env 里）。

## 3. 服务（launchd，用户级 LaunchAgents）

标签前缀 `space.bwicarus.`，配置在 `~/Library/LaunchAgents/`，由 `deploy_mac.py` 生成。
常驻服务 KeepAlive（崩了 10 秒后自动拉起），日志 `~/BW/logs/<名>.log`。

| 服务 | 端口 | 说明 |
|---|---|---|
| webapp | 5000 | Flask 网页后端（`_server_deploy/app.py`） |
| mcp | 8766 | MCP 门面（要 `~/.config/mcp-http-token`） |
| voice-rt | 8767 | 实时语音中继 |
| rbi | 8769 | 远程浏览器（Playwright Chromium） |
| bridge | 43128 | 阅读器桥：`mac/ReaderBridge` 跨平台编译的 ComputerVoiceAudio |
| voice-core | 43131 | CLI 语音运行器（`voice_cli_runner.py`，Codex app-server） |
| supervisor | 43132 | 后台守护：ReaderPC 网页界面 + 复制账本应用 + 展示板卡片渲染 + Anki 保活 |
| obsidian-sync | — | `ob sync --continuous`（obsidian-headless 0.0.8，设备名 mac-headless） |
| anki | 8765 | 官方 Anki 26.9.3 隐藏启动（`open -g -j`）；AnkiConnect 只听 127.0.0.1 |
| kj-anki-sync | — | 每 15 分钟 `scripts/kj/cli.py anki-sync` |

**对外**：`tailscale serve` 47 条路由（与 Windows 原配置逐条相同），
443 → 5000/8766/8767/8769/43128 各路径，8443 → 43132（服务器网页界面）。
⚠ serve 配置绑在**主机名**上：Tailscale 改名后必须 `serve reset` 再重配（Windows 改名后就是因此全断）。

## 4. 部署

```bash
cd ~/BW/src/claude
/usr/bin/python3 extensions/bw-reader-webext/mac/deploy_mac.py            # 全部
/usr/bin/python3 extensions/bw-reader-webext/mac/deploy_mac.py --only bridge,webapp
/usr/bin/python3 extensions/bw-reader-webext/mac/deploy_mac.py --rollback  # current 指回上一版
```

做的事：仓库 → 新版本目录（数据目录链到 ~/BW/data）→ `dotnet publish` 桥 → 切 current →
重写 launchd 配置并重载。保留最近 5 个版本。**不要**在 `~/BW/runtime/current` 里直接改代码。

**Git 远端（2026-09-26）**：Mac 仓库 `origin` = `git@github.com:bwicarus/obsidian-claudecode-skills.git`
（Mac 的 `~/.ssh/id_ed25519` 已加到 GitHub 账号 bwicarus）。仓库在外接盘上，**每完成一块工作就推**，
别让提交只存在一块会掉线的盘上（2026-09-26 00:32 外接盘掉线过一次）。用户已授权推送。

## 5. 各子系统要点

- **桥**：同一份 `windows/ComputerVoiceAudio` 源码，`mac/ReaderBridge/ReaderBridge.csproj`
  以 `net8.0` + `BW_PORTABLE` 编译，不编 ChatGPT 窗口自动化。Windows 专属音频件在 Mac 上用不到：
  CLI 语音的 App 音频走 UDP 直连（`voice-audio-pipe.json` 在 = 直连）。修过的三处：
  ① `ComMtaLease.Enter` 非 Windows 返回空租约；② 提交边界的虚拟声卡校验补 `audioPipe is null`；
  ③ `BwHostPaths` 统一 Python/Codex/浏览器路径（Windows 路径逐字不变）。
  Mac 版失败时 stderr 会打 `[failure] 码 类型@[方法…]`（只有类型名与方法名，不含 message）。
- **CLI 语音**：设置在 `~/BW/data/BWReader/voice-cli/settings.json`（Windows 原件 `.windows-backup`）。
  Mac 上改过：codexExe=Mac 原生 codex、音频设备名留空（默认=LG TV / DJI MIC）、
  `mcpDisable=["node_repl"]`（写不存在的 bwab 会让 Codex 起不来）。
- **Codex**：`~/.codex/config.toml` 是用户原有配置 + 末尾追加的 reader_snapshot / voice_core 两段
  （原件 `config.toml.before-mcp-append`）。Codex 桌面版与 CLI 语音共用这份。
- **Codex 全局配置（2026-09-25 查出迁移时漏了）**：Mac 的 `~/.codex/AGENTS.md` 原是空文件，已从 Windows 恢复
  （现行 3.8 KB 精简入口；阅读器/语音规则经 `reader_capability_guide` 按话题取）。能力指南与语音入口指令里的
  命令改为运行时渲染本机路径（不再写死 `C:\…`）；`~/BWReader` 也链到数据根。
  **自建 skill 已迁（2026-09-25，13 个）**：路径改成 `~/BW/venv/server/bin/python ~/BWReader/…`，cmd 续行 `^`→`\`；
  `obsidian-project-notes` 的 PowerShell 脚本等价改写为 `scripts/obsidian_notes.py`（常驻同步 = launchd
  `space.bwicarus.obsidian-sync`）；`reader-image-cards` runner 的文件锁 msvcrt→fcntl、大载荷走 `/tmp`；
  excalidraw 三个脚本库路径同 `OBSIDIAN_NOTES_VAULT` 规则；`video-transcode` 加 Homebrew 路径与 VideoToolbox。
  原件归档 `/Volumes/BWDev/archive/windows-codex-20260925/`。`~/.codex` 已纳入每日备份（排除插件/缓存）。
  ⚠ 未迁 `rules/default.rules`：是历次「总是允许」攒下的 Windows 一次性命令，另含一条「pwsh 任意命令放行」——
  换成 Mac 等价物等于替用户放开全部审批，留给用户决定。
  ⚠ 仍缺：**ffmpeg**（Mac 未装，影响 video-transcode 与服务端 YouTube 字幕/转写）。**摄像头**：用户实际只用 Pi 上的 C920 —— Mac→Pi SSH
  已打通（`ssh pi`，Mac 公钥经 Windows 一跳装到 Pi），`camera_capture.py snap pi` 实测成功；另三台 local 是 Windows 的硬件。`~/.config` 的密钥已从 Windows 补齐（用户授权）。
- **Obsidian**：库 `~/BW/data/obsidian`；`资源/vocab` 被同步配置忽略（服务器本机生成），
  >200 MB 的文件云同步不收（`File too large` 提示正常）。
- **Anki**：数据从 AnkiWeb 同步（10 牌组 / 501 卡）。只装 AnkiConnect；Windows 的 AnkiTrayPro 不带。
  Windows 上「KJ Anki Sync」其实长期 `anki_unavailable`，Mac 上第一次真正跑通。
- **用户现况查询（MCP `user_situation`，2026-09-25）**：地点 / 各设备在场 / 正在读什么 / 复习 / 醒着与空闲。
  判断复用 `situation_signals`；桥另按设备存 `presence-signal-<设备>.json`。
  ⚠ 在终端里手跑 `situation_signals.py` 要带 `LOCALAPPDATA=~/BW/data`，否则找错根目录、全报「不知道」。
- **路径**：服务代码里写死的 `/home/bwicarus/...` 改由 `_server_deploy/bw_paths.py` 给出
  （Mac 的 /home 是系统自动挂载点，不可写）。

## 6. Xcode 本机开发

- Xcode 27.0（Swift 6.4）；云端是 Xcode 26.6。
- 准备：`ios/BWReader/prepare_local_xcode.sh`（与 CI 编译前三步相同：Safari 扩展包 → ReaderBundle → XcodeGen 2.46.0）。
  `--dev` = 开发签名（自动签名 + Apple Development），`project.yml` 不动。
  改了阅读器前端 / 扩展要重跑；只改 Swift 不用（新增删除文件要重跑）。
- 团队 XUE HAOYAN（`7MDVSLPV8F`），已有 Apple Development 证书与 4 个开发描述文件。
- Xcode 27 编译修过 4 处（行为不变）：两处事务闭包里的嵌套三元（NSNull/字典）改 if/else、
  一个长事务闭包挪成方法、书库界面长修饰链拆成三段。**写新代码时避免**：
  多分支类型不同的嵌套三元、超长 SwiftUI 修饰链、多段字符串 `+` 夹插值。
- 已打开 Xcode 偏好「遇到错误继续编译」（`IDEBuildingContinueBuildingAfterErrors`）。
- **命令行装真机**（2026-09-25 跑通，要在 Mac 桌面会话里；iPad `bwpad` UDID `00008132-000405AE36F0801C`，
  4 个描述文件都已含它，不需要 `-allowProvisioningUpdates`）：

  ⚠ **改过 `_server_deploy/static/pdf/*.js` 必须先重生成 ReaderBundle**：xcodebuild 只是把
  `Generated/ReaderBundle` 原样拷进包，**不会**自己重新打包。2026-09-26 连装两版，JS 改动都没进 App，
  而编译全绿、看不出任何异常 —— 判据是客户端日志里静态路径的哈希（`/static/<hash>/pdf/…`）没变。

  ```bash
  cd ~/BW/src/claude/ios/BWReader
  ~/BW/venv/server/bin/python package_local_reader.py --output Generated/ReaderBundle   # 改过 JS 时
  xcodebuild -project BWReader.xcodeproj -scheme BWReader -configuration Debug \
    -destination 'id=00008132-000405AE36F0801C' -derivedDataPath ~/BW/xcode-derived/device build
  xcrun devicectl device install app --device 00008132-000405AE36F0801C \
    ~/BW/xcode-derived/device/Build/Products/Debug-iphoneos/bwicarus-test.app
  xcrun devicectl device process launch --device 00008132-000405AE36F0801C space.bwicarus.bwreader2
  ```

  产物名是 `bwicarus-test.app`（不是 BWReader.app）。与 TestFlight 版同 bundle id，覆盖安装保留数据；
  要断点调试就在 Xcode 里选 bwpad 按 Run。
- **模拟器**（2026-09-26 起日常测试用它，用户在模拟器里自己操作、含语音；登录用 Apple 账户，由用户本人登）：
  iPad Pro 11 (M5) `7E409756-50E0-4090-BC46-376FEF34DFC4`。必须 `ARCHS=arm64 ONLY_ACTIVE_ARCH=YES`，
  否则 xcodebuild 选 x86_64 报「Unable to resolve module dependency」。

  ```bash
  xcodebuild -project BWReader.xcodeproj -scheme BWReader -configuration Debug \
    -destination 'id=7E409756-50E0-4090-BC46-376FEF34DFC4' -derivedDataPath ~/BW/xcode-derived/sim \
    ARCHS=arm64 ONLY_ACTIVE_ARCH=YES build
  xcrun simctl install 7E409756-50E0-4090-BC46-376FEF34DFC4 \
    ~/BW/xcode-derived/sim/Build/Products/Debug-iphonesimulator/bwicarus-test.app
  xcrun simctl launch --terminate-running-process 7E409756-50E0-4090-BC46-376FEF34DFC4 space.bwicarus.bwreader2
  ```

  模拟器 App 的本机数据可直接读：`xcrun simctl get_app_container <UDID> space.bwicarus.bwreader2 data`
  下的 `Library/Application Support/`（`conversation-cache/normal.json` 是侧栏对话的原样投影，
  查「显示成双/缺消息」先看它）。客户端日志里模拟器的设备号结尾是 `dc6d7c`，iPad 是 `3cd63a`。

## 7. Windows 侧现状（已退役）

- 停掉的：ReaderPC 整棵进程树、桥、网页后端与三个边车、CLI 语音运行器、Obsidian 同步。
- 禁用的计划任务：`BW ReaderPC Watchdog`、`KJ Anki Sync`、`Obsidian Headless Sync`。
- 2026-09-26 复查：`BW ReaderPC Watchdog` 又是 Ready（会把 Windows 服务器拉起），已与 `BW Computer Voice Setup`
  一起禁用（经 `ssh windows-pc` 的管理员会话即可，不必上桌面）。Windows 上 5000/8766/8767/8769/43128/43131/43132
  均无监听；只剩用户开着的 Codex 桌面版及其只读 MCP 子进程。
- Windows 现在的用途只剩：Mac 取旧文件的来源（`ssh windows-pc`）。不再承担开发、服务器、备份任何一项。
- 回退：把 Mac 的 Tailscale 名改回、Windows 改回 `bwicarus-2` 并 `serve` 重配、启用上述任务。
  注意 Mac 上切换后产生的数据要先搬回去。

## 8. 外接盘 BWDev（Buffalo ESD-S1C 1 TB，APFS，2026-09-25 用户抹盘）

用户原话：当作备份与项目工程文件用，「尽量不把东西放在这台 mac 里，但会影响运行和服务速度的就不需要妥协」。

| 在外接盘 | 留内置盘（影响运行） |
|---|---|
| 代码仓库 `src/claude`、`xcode-derived`、`archive/`（旧副本）、`backups/`（每日快照） | `~/BW/data`（服务在用）、`runtime`、`venv`、`config`、`logs`、launchd |

- **外接盘断开时**：所有服务照常（launchd 只用 runtime/venv/data/logs，运行副本不引用仓库）；
  只是不能开发/部署，备份当天跳过（不会退回写内置盘）。
- **每日备份**：launchd `space.bwicarus.backup` 03:30 跑 `mac/backup_mac.py`，
  SQLite 在线备份 API + rsync 硬链接去重，保留 7 天 + 4 周；状态看 `~/BW/logs/backup.status.json`。
  首份 27 GB / 82 s，之后每天增量约 1 GB 量级（大库 kj-public.db 13.6 GB 不变时硬链接复用）。
- 卷上 `Owners: Disabled`（外接盘默认忽略属主），对仓库和备份无影响。

## 9. 还没做的

- 系统设置由用户开：自动登录（服务是用户级，登录后才起）、断电后自动开机。睡眠已是永不。
- 没迁的：PC 端 OCR 预处理（Windows 的 reader-pc-ocr-venv / models）、spacy 语法分析 venv
  （server.env 里 `SPACY_PYTHON=~/BW/venv/spacy` 还不存在）、DocLayout-YOLO。
- 这批 Mac/Xcode 27 修复在 `claude/mac-migration-20260925`，Codex 的分支里没有，需要合并。
- Windows 上的旧数据：核对无误后再删。
