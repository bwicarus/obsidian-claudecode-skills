---
name: operate-qa-browser
description: Inspect, explain, troubleshoot, modify, or run this project's QA Browser and remote screenshot-question workflow, including ordinary conversations, card-context review, image injection, note creation, card updates, SSE responses, asynchronous jobs, and iPad access. Use when the user mentions 截图问答, QA Browser, cardCtx, remote QA, /api/create-note, /api/card-update, or qa-server.
---

# Operate QA Browser

Work from the current QA implementation and references, not from old service patches or remembered line numbers.

## Load the right context

- Read `../../../references/qa-browser-features.md` for modes, state, routes, jobs, and data behavior.
- Also read `../../../references/ipad-remote-qa.md` when the task involves iPad access, remote image injection, or device workflow.
- Inspect `../../../_client/core/qa_browser.py` for the browser UI and handlers.
- Inspect `../../../_server_deploy/qa_server.py` and the current service definition when the task involves the Pi daemon or startup behavior.

## Diagnose by boundary

1. Identify ordinary mode versus card-context mode.
2. Trace the actual request through UI, route, asynchronous job, AI backend, persistence, and polling response.
3. Separate a dropped mobile connection from a failed background job; inspect the job status before retrying a mutation.
4. Check the current configured AI backend and adapter. Do not assume this subsystem uses the main `scripts/ai_client.py` settings.
5. Treat stale `ExecStartPre` patches and hard-coded line references as historical hints only; verify current source and unit configuration.

## Change and verify

- Preserve idempotency for note/card mutations and avoid replaying an unknown-result request.
- Keep ordinary note creation separate from card-context card or source-note updates.
- Verify the affected HTTP response or SSE stream, job transition, and resulting persisted object.
- Use an isolated browser environment for browser regression. Do not change the user's daily browser or account state.
- Physical iPad behavior remains unverified until the user performs the relevant device interaction.

## Deployment

Read `../../../references/deployment-workflow.md`, query the deployment manifest for each changed path, and follow the current Pi route. Do not use VPS commands or manual deployment snippets copied from `.claude/skills/截图问答.md`.

Do not restart or deploy the service unless the current request authorizes that external action.

