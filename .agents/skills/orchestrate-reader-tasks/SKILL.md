---
name: orchestrate-reader-tasks
description: Orchestrate complex Reader work in Windows Codex voice through native Codex Skills, MCP tools, web tools, and subagents with minimum latency. Use for multi-step or cross-source Reader research, interactive practice papers, or compound structured output to the Reader App or extension. Ordinary current-page, image, scroll, highlight, and card requests should call their direct Reader tool without loading this skill.
---

# Orchestrate Reader Tasks

Use the current Codex session as the task owner. Call Reader MCP tools directly; do not start a
new `codex exec`, Claude CLI, or other nested CLI worker from this Windows-native path.

## Choose the shortest path

1. Answer directly when no live Reader fact or action is needed.
2. For a current page, selection, image, scroll, card, navigation, highlight, or tool status,
   call the one matching Reader MCP tool. Do not read a guide first.
3. For a multi-step task follow「多步研究任务」below. Call `reader_capability_guide` only when
   you need an interface detail (card shapes/bind: `cards`, command shell: `command-format`,
   practice paper: `interactive-paper`, tool ownership: `capability-matrix`). It holds
   interface facts only; there is no workflow topic to read.
4. Keep sequential work in the main agent. Spawn native subagents only when at least two
   independent evidence streams can run in parallel or a long synthesis benefits from isolation.
5. All Reader tools run inside one `exec` script (`tools.mcp__reader_snapshot__…`): chain the
   steps in one script instead of spending a turn per tool.

## Saving a flow as a skill is the user's call

Never decide, suggest, or judge whether something is "worth saving". When the user presses
「保存为工具」 in the sidebar (the notification carries `turn=<id>`) or says 把刚才那个存成工具,
load `$organize-into-skill` and follow it: real trace → `flow.json` → `bw_skill_build.py`
(lint + generated `run.js` + dry-run against the trace) → save. Silent by default; ask only
when a step is genuinely ambiguous. You write `flow.json`, never the runner. To run a saved
skill later, paste its `run.js` block into one `exec` call verbatim.

## 多步研究任务（原 `research-task` 指南，2026-09-13 逐字搬入）

本文件承接旧 `do_task` CLI worker 的任务合同，但执行者改为当前 Codex 会话及其原生子代理。

## 输入包

保留用户原始要求，不要把它改写成更窄的任务。只有与问题有关时才加入：当前书名和文件、
页码、当前选区、最近对话中的必要约束。不要传整段聊天记录或整本书。

## 执行

1. 先列出完成目标所需的事实与动作；一个工具能完成就退出复杂路径。
2. Reader 当前事实走本机 MCP，开放网络研究走 Codex 原生搜索/浏览工具；已配置的服务能力
   走对应 MCP。不要用 shell 读取快照，也不要猜服务工具名或参数。
3. 顺序依赖的步骤留在主 agent。只有独立证据流才并行派发子代理，并让它们默认只读。
4. 汇总时区分已证实、推论和无法取得的事实。不要把工具开始执行当成成功。
5. 用户要求向 Reader 写入时，由主 agent 在核对当前来源后执行；模糊写入结果不重试。

语音收尾应先说结论，再用一两句说明已完成的动作或确切缺口；不要朗读工具过程。

## Keep context small and current

- Read `reader_context_snapshot` once at the start only when the task depends on the current
  document. Require `contextStatus=ready`; never reuse stale page text.
- Pass subagents a compact task packet: original user request, current book/page/selection when
  relevant, constraints, and the exact read-only evidence question. Do not copy the whole chat.
- Let the primary agent own all Reader writes. Re-read the snapshot before a write if the task
  took long enough that the user may have changed pages.

## Preserve safety and compatibility

- Treat an ambiguous write result as unknown. Do not retry a paper, highlight, card, or saved
  task blindly.
- Use only tools actually exposed in the current session. Never guess an old service tool name
  or schema; read `capability-matrix` and use service capability discovery when available.
- Keep the existing Realtime invocation and legacy CLI implementation unchanged. They are
  compatibility paths, not an extra layer in the Windows-native fast path.
- If a required App/service capability is not exposed, report the exact missing channel instead
  of substituting a different product action.
