---
name: orchestrate-reader-tasks
description: Orchestrate complex Reader work in Windows Codex voice through native Codex Skills, MCP tools, web tools, and subagents with minimum latency. Use for multi-step or cross-source Reader research, interactive practice papers, check-report verification, saved generative tasks, or compound structured output to the Reader App or extension. Ordinary current-page, image, scroll, highlight, and card requests should call their direct Reader tool without loading this skill. Preserve the existing Realtime and legacy CLI paths without using a nested CLI worker for the Windows-native route.
---

# Orchestrate Reader Tasks

Use the current Codex session as the task owner. Call Reader MCP tools directly; do not start a
new `codex exec`, Claude CLI, or other nested CLI worker from this Windows-native path.

## Choose the shortest path

1. Answer directly when no live Reader fact or action is needed.
2. For a current page, selection, image, scroll, card, navigation, highlight, or tool status,
   call the one matching Reader MCP tool. Do not read a guide first.
3. For a complex Reader task, call `reader_capability_guide` once with the exact topic below.
   Use `topic=index` only when the topic is genuinely unknown.
   For a compound request, choose the topic that owns the requested artifact and carry secondary
   requirements inside that workflow; do not load several guides merely because several labels match.
4. Keep sequential work in the main agent. Spawn native subagents only when at least two
   independent evidence streams can run in parallel or a long synthesis benefits from isolation.

## Route complex tasks

- General multi-step or cross-source research: `topic=research-task`
- Interactive handwritten practice paper: `topic=interactive-paper`
- Practice-paper check report or source verification: `topic=check-report`
- Rerun a saved generative task: `topic=saved-task`
- Tool availability, ownership, or fallback decision: `topic=capability-matrix`
- Latency, mutation, retry, and subagent rules: `topic=task-routing`

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
