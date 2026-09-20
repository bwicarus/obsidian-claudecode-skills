"""Versioned App-owned instructions; independent of customer conversation history."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ASSISTANT_ROOT = Path(__file__).with_name("assistant")


def contract(base_instructions):
    agent = (ASSISTANT_ROOT / "AGENTS.md").read_text(encoding="utf-8")
    skill = (ASSISTANT_ROOT / ".agents/skills/stocks-monitoring/SKILL.md").read_text(encoding="utf-8")
    text = (base_instructions + "\n\n[股票 App 当前能力契约]\n" + agent +
            "\n上述能力契约更新旧版功能说明，保留已有对话事实与用户选择。不要重新执行历史用户请求；等待本轮明确意图。")
    digest = hashlib.sha256((text + "\n" + skill).encode()).hexdigest()
    return text, digest


async def sync_contract(rpc, thread_id, marker, text, digest):
    """A resume config override alone does not replace old developer history in 0.155.1."""
    marker = Path(marker)
    expected = {"threadId": thread_id, "digest": digest}
    try:
        if json.loads(marker.read_text()) == expected:
            return False
    except (OSError, ValueError):
        pass
    await rpc("thread/inject_items", {"threadId": thread_id, "items": [{
        "type": "message", "role": "developer", "content": [{"type": "input_text", "text": text}]}]})
    temporary = marker.with_suffix(marker.suffix + ".tmp")
    temporary.write_text(json.dumps(expected), encoding="utf-8")
    temporary.replace(marker)
    return True
