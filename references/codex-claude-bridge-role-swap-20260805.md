# Windows 桥接器角色互换交接（2026-08-05）

## 用户决定

用户要求把桥相关任务完整转交 Claude 重新设计制作，Codex 接手 Claude 原先负责的
App / Swift 与 Safari 扩展工作。自本交接起：

- Claude 独占 `extensions/bw-reader-webext/windows/**`，包括
  `ComputerVoiceAudio/*.cs`、Windows 候选构建、安装和运行时诊断。
- Codex 独占 `ios/BWReader/**` 与 Safari 扩展前端；Claude 冻结这些文件。
- 两边不得同时修改或安装 Windows 候选。

## 简化目标

1. 保留 0.1.85 的已验收收益：全局热键不切前台；保留 Codex 按应用音频分流，
   以及音频路由自动切换与精确恢复。
2. 将 Codex F24 语音开关从自动 START、STOP 和失败收尾完全摘出。App / 扩展电脑按钮
   只建立音频路由、PCM 上下行与网络传输；Codex 没有开启语音时不算失败。
3. Codex 语音开关改为独立命令。若桥能读取真实 activity，就提供同步状态；
   若不能，则提供诚实的单次 toggle 按钮，不伪装成状态开关。
4. 连接断开 30 秒且再次确认没有任何活跃连接后，恢复原音频状态或默认路由。
5. GPT Classic 的现有 UIA 与预热语义不能误套到 Codex；按目标分别处理。

## 当前必须修的快照缺口

Pi 的合卷 active-reading 会带可选 `viewFile` / `viewPage`。0.1.85 的
`DirectContextSnapshot.ValidateActiveReading` 用精确七字段 `SetEquals`，会拒绝这两个
既有 canonical alias，随后客户端停止整个 context pump。请恢复既有设计：

- 显式允许并验证这两个字段成对出现；
- `viewFile` 只接受合法 `vbook:` 标识，`viewPage` 使用现有页码验证；
- 折叠、恢复和页面身份比较保留 canonical member 与 vbook 视图的映射；
- 不做字段透传，不让无效 active-reading 连带吞掉已接受的正文快照。

历史实现可参考提交 `112d488 fix(windows): preserve vbook snapshot identity`，但当前
0.1.85 文件已演进，不能整笔盲 cherry-pick。

## 当前现场

- 用户实测：网页端可以启动；App 端无反应；快照不更新。
- App 的旧缓存一字段消息兼容，以及网页快照通过 extension local storage 可靠接力，
  已由 Codex 修入 TestFlight `1.0.64 (1)`；CI、签名和上传均成功，正在 Apple 处理。
  不要为这两项改 Windows 语音 START。
- 当前安装 Windows 0.1.85 EXE SHA-256：
  `D825207CAC964D392D048E7A8C1012572EE729A6614857C8773DC63D75812DCA`。
- `C:\claude` 是脏共享检出，0.1.85 WIP 尚未完整提交。禁止 reset、clean、add -A。
- 共享检出的 `DirectContextSnapshot.cs` 当前未修改。Codex 曾启动一个 alias 子任务，
  但已停止；不要采用其未验证 worktree。
- 用户授权快速实现并直接安装；短暂停桥与中断进行中的服务无需再次确认，安装后说明即可。

## 交付要求

先回复已接受范围与准备修改的文件，然后直接实现、构建、安装和自检，不再等待用户确认。
回报改了什么、验证了什么、没做什么、下一步谁做。
