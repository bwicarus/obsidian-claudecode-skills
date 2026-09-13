---
name: __NAME__
description: __DESCRIPTION__
---

# __TITLE__

__SUMMARY__

## 触发

用户说：__TRIGGERS__

## 步骤（机器可读版在 flow.json，运行器按它跑，不回模型）

__STEPS__

## 运行

把下面这段**原样**作为 `exec` 的输入执行（不要改动、不要拆开）。运行器会一步步调工具，
每步 text 一行 `[bw-flow] … ok`；碰到 `needs_ai` 的步骤会停下并给出 `bwFlowHandoff`，
你按 handoff 里的 `resume` 说明把结果存进 store 后**原样重跑**同一段。

<!-- bw-flow:run.js:begin -->
```js
__RUN_JS__
```
<!-- bw-flow:run.js:end -->

## 来源

由 `organize-into-skill` 从 __SOURCE__ 整理（__DATE__）。改流程改 `flow.json`，然后
`python %LOCALAPPDATA%\BWReader\skill-kit\bw_skill_build.py <本目录>` 重新生成上面的 run.js。
