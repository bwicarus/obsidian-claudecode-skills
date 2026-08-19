# GPT Classic legacy-inject 接管交接（2026-08-02 17:45 JST）

## 用户要求与当前边界

- 用户明确要求：把 GPT Classic 旧版文字注入的剩余故障交给 Claude 修改。
- Codex 目标的 legacy-inject 已可用，禁止为修 Classic 回退或改坏 Codex 路径。
- 用户负责实机验收；可直接安装可回退候选，安装导致桥接/WSS 短暂断开无需等待确认，只需事后告知。
- 当前 typist 已由 Codex 手工停止，避免继续刷新 Classic 输入框；不要在定位前自动启动或向真实会话注入测试文字。
- 工作区有他人及历史 WIP，禁止 reset/clean/add -A。

## 当前现场

- 已安装 Windows 桥接候选：`0.1.56`。
- 监听服务正常；本轮手工停止的是 typist，不是整个桥接服务。
- 回滚备份：`C:\Users\bwica\bw-computer-voice-bridge-backups\pre-0.1.56-20260802-173921`。
- typist 停止后状态：`running=false`，队列 1 条，因停止时正处于 UI 读取阶段而标记 `delivery_uncertain`：
  - session `session-v8rtSB10jwKZvc3Xe5-_6Q`
  - event `a0618c2cd837bb64`
  - seq `2237`
- 日志：`C:\Users\bwica\bw-computer-voice-bridge\typist-runtime\logs\voice-typist.jsonl`。
- 源码真值/当前未提交 diff：
  - `extensions/bw-reader-webext/windows/typist-runtime/voice_typist.py`
  - `extensions/bw-reader-webext/windows/typist-runtime/tests/test_voice_typist_direct_runtime.py`

## 已证实修通的部分

1. Classic 目标窗口筛选已修：
   - process name：`ChatGPT Classic`
   - executable suffix：`ChatGPT Classic.exe`
   - package prefix：`OpenAI.ChatGPT-Desktop_`
   - 现场曾准确解析到 PID 20304、HWND 592848、标题 `ChatGPT Classic`。
2. Classic composer 控件已实机只读确认：
   - AutomationId `prompt-textarea`
   - ControlType Edit
   - Name `与 ChatGPT 聊天`
3. 连续选区洪水已接上原本缺失的 coalesce：同 session 的 focus 使用固定 key，1.5 秒 settle 后只提交最新状态；不可替换 `delivery_started`/未提交项，并清除其余可替换旧项。
4. 当前 focused unittest 34 项全绿；0.1.56 packaged self-test 通过。

## 当前首个必败点（请从这里接手）

Classic 输入框能被写入，但从未到达 `ClassicComposerAutomation.invoke_send()`。每次都死在粘贴回读校验：

```text
payload_chars=78  -> paste mismatch (80 chars); composer cleared
payload_chars=111 -> paste mismatch (118 chars); composer cleared
```

所以这不是单纯 `LF -> CRLF`，也不是永远只多一个尾部 CRLF。0.1.54/0.1.56 中尝试的换行归一与“Classic 可额外一个尾部换行”仍不足；最新 111→118 的差值已证明 Classic/Chromium contenteditable 还会做别的纯文本序列化转换。日志没有出现 `classic_send_not_invokable` 或 `classic_composer_ambiguous`，因为发送按钮根本没被调用。

用户看到“选中一句后输入框内容每次不一样”，原因有两层：

- 0.1.53/0.1.54 之前每个增量 focus 都独立入队，已由 0.1.56 coalesce 修正。
- 当前 paste verification 每项会重试并清空，所以即便 coalesce 后仍会看到一次或少量刷新；主阻塞仍是回读比较。

最新日志证据还显示 coalesce 已实际生效（连续 `coalesced`，队列维持 1–2），不是测试假绿。

## 建议的最小下一步

1. 不要继续猜换行。先在 Classic 专属路径增加**不记录正文**的差异诊断：expected/actual 长度、CR/LF/Unicode separator 数量、首个不同位置及附近 code point 数值；用户触发一次即可确定真实转换。
2. 更优的架构候选：Classic composer 已有 UIA ValuePattern 与 TextPattern，可考虑用 UIA 读取/写入或用其读回做验证，绕开 `Ctrl+A/C` 对 contenteditable 的剪贴板序列化；但空输入时 UIA 曾返回占位文本 `问问 ChatGPT`，必须区分占位符与真实 Value。
3. 校验通过后再观察动态发送态。空 composer 时没有发送按钮，现场只读无法证明按钮；缓存 bundle 含 `composer-submit-button`，但不能当运行时证据。若到达发送阶段后失败，再枚举非空状态的唯一可见可用按钮并按真实 AutomationId/InvokePattern 修。
4. 保持 Codex 原有严格比较与 rollout 验证不变；所有兼容仅限定 `target_app_kind == chatgpt-classic`。
5. 下一候选建议 `0.1.57`；构建/校验入口：
   - `python extensions\bw-reader-webext\windows\package_computer_voice_direct.py --build 0.1.57`
   - `python extensions\bw-reader-webext\windows\package_computer_voice_direct.py --self-test <zip>`

## 本轮明确没做

- 没有把正文或选区写进诊断日志。
- 没有自行点击发送、启动语音或做真实对话验收。
- 没有提交、push、部署 Reader/Pi 或改 Swift。
- 没有改 Codex legacy-inject 行为。

## 回报格式

请完成定位/修改/候选安装后，通过 BWAB 回报 Codex：改了什么 / 验证了什么 / 没做什么 / 下一步谁做。若需要用户触发一次诊断，先明确告诉 Codex要观察的动作；不要重复让用户进行没有新增证据的盲测。
