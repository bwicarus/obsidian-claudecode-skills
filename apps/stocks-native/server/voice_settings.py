"""Account-owned voice preferences and a bounded, model-free Codex capability query."""
from __future__ import annotations

import asyncio
from contextlib import closing
from copy import deepcopy
import json
import os
from pathlib import Path
import sqlite3
import time

from assistant_contract import ASSISTANT_ROOT


DEFAULTS = {"backendModel": "gpt-5.6-sol", "effort": "medium", "voice": "sol"}


class VoiceSettingsError(ValueError):
    def __init__(self, message, *, code="invalid_voice_settings", status=400):
        super().__init__(message)
        self.code, self.status, self.detail = code, status, {}


def normalize_catalog(models, voices):
    """The current v3 transport uses listVoices.v1, as in Reader's voice selector."""
    out_models = []
    for model in models:
        if not isinstance(model, dict) or model.get("hidden"):
            continue
        identifier = model.get("id")
        if not isinstance(identifier, str) or not identifier or len(identifier) > 160:
            continue
        efforts = list(dict.fromkeys(entry["reasoningEffort"] for entry in
            (model.get("supportedReasoningEfforts") or []) if isinstance(entry, dict)
            and isinstance(entry.get("reasoningEffort"), str) and entry["reasoningEffort"]))
        default_effort = model.get("defaultReasoningEffort")
        if default_effort not in efforts:
            default_effort = efforts[0] if efforts else ""
        out_models.append({"id": identifier, "displayName": model.get("displayName") or identifier,
                           "defaultEffort": default_effort, "efforts": efforts})
    voice_payload = voices.get("voices") if isinstance(voices, dict) else None
    available = voice_payload.get("v1") if isinstance(voice_payload, dict) else None
    out_voices = [{"id": value, "displayName": value} for value in dict.fromkeys(available or [])
                  if isinstance(value, str) and value and len(value) <= 80]
    if not out_models or not out_voices:
        raise VoiceSettingsError("暂时无法取得模型与声音列表，请重试", code="voice_catalog_unavailable", status=503)
    return {"models": out_models, "voices": out_voices, "defaults": dict(DEFAULTS)}


async def load_live_catalog():
    """Initialize a temporary app-server; never start a thread, turn or audio session."""
    env = {key: value for key, value in os.environ.items() if key not in ("OPENAI_API_KEY", "OPENAI_BASE_URL")}
    process = await asyncio.create_subprocess_exec(
        os.environ.get("STOCKS_CODEX", "/opt/codex/0.155.1/bin/codex"),
        "-c", 'forced_login_method="chatgpt"', "-c", "features.plugins=false",
        "-c", "features.memories=false", "app-server", "--listen", "stdio://",
        cwd=str(ASSISTANT_ROOT), env=env, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, limit=4 * 1024 * 1024)
    sequence = 0

    async def send(body):
        process.stdin.write((json.dumps(body) + "\n").encode())
        await process.stdin.drain()

    async def rpc(method, params):
        nonlocal sequence
        sequence += 1
        request_id = "catalog-" + str(sequence)
        await send({"id": request_id, "method": method, "params": params})
        while True:
            raw = await process.stdout.readline()
            if not raw:
                raise RuntimeError("Capability connection closed")
            response = json.loads(raw)
            if "method" in response:
                if "id" in response:
                    await send({"id": response["id"], "error": {"code": -32601, "message": "Capability query only"}})
                continue
            if response.get("id") == request_id:
                if "error" in response:
                    raise RuntimeError("Capability query failed")
                return response.get("result", {})

    async def query():
        await rpc("initialize", {"clientInfo": {"name": "stocks_voice_settings", "version": "1"},
                                 "capabilities": {"experimentalApi": True}})
        await send({"method": "initialized"})
        voices = await rpc("thread/realtime/listVoices", {})
        models, cursor = [], None
        for _ in range(8):
            response = await rpc("model/list", {"cursor": cursor} if cursor else {})
            models.extend(response.get("data") or [])
            cursor = response.get("nextCursor")
            if not cursor:
                return normalize_catalog(models, voices)
        raise RuntimeError("Capability catalog exceeds its bounded page limit")

    try:
        return await asyncio.wait_for(query(), timeout=35)
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=4)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


class VoiceSettingsService:
    def __init__(self, root, *, catalog_loader=None, clock=time.monotonic):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.root / "voice-settings.sqlite3"
        with closing(self._connect()) as connection, connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS voice_settings (
                owner TEXT PRIMARY KEY, backend_model TEXT NOT NULL,
                effort TEXT NOT NULL, voice TEXT NOT NULL)""")
        if os.name != "nt":
            self.path.chmod(0o600)
        self._loader = catalog_loader or load_live_catalog
        self._clock = clock
        self._cached, self._expires = None, 0
        self._lock = asyncio.Lock()

    def _connect(self):
        return sqlite3.connect(self.path, timeout=10)

    @staticmethod
    def _owner(owner):
        if not isinstance(owner, str) or not owner or len(owner) > 256:
            raise VoiceSettingsError("账户身份无效")
        return owner

    def load(self, owner):
        owner = self._owner(owner)
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT backend_model,effort,voice FROM voice_settings WHERE owner=?", (owner,)).fetchone()
        return dict(zip(("backendModel", "effort", "voice"), row)) if row else dict(DEFAULTS)

    async def catalog(self):
        if self._cached is not None and self._clock() < self._expires:
            return deepcopy(self._cached)
        async with self._lock:
            if self._cached is not None and self._clock() < self._expires:
                return deepcopy(self._cached)
            try:
                catalog = await self._loader()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise VoiceSettingsError("暂时无法取得模型与声音列表，请重试", code="voice_catalog_unavailable", status=503) from exc
            self._cached, self._expires = deepcopy(catalog), self._clock() + 900
            return deepcopy(catalog)

    async def save(self, owner, payload):
        owner = self._owner(owner)
        if not isinstance(payload, dict) or set(payload) != set(DEFAULTS):
            raise VoiceSettingsError("请提供后台模型、思考强度和声音三个设置")
        if any(not isinstance(value, str) or len(value) > 160 for value in payload.values()):
            raise VoiceSettingsError("语音设置格式无效")
        catalog = await self.catalog()
        model = next((entry for entry in catalog["models"] if entry["id"] == payload["backendModel"]), None)
        if model is None:
            raise VoiceSettingsError("该后台模型目前不可用，请刷新列表")
        if payload["effort"] not in (model["efforts"] or [""]):
            raise VoiceSettingsError("该模型不支持所选思考强度")
        if payload["voice"] not in {entry["id"] for entry in catalog["voices"]}:
            raise VoiceSettingsError("该声音目前不可用，请刷新列表")
        settings = {key: payload[key] for key in DEFAULTS}
        await asyncio.to_thread(self._write, owner, settings)
        return settings

    def _write(self, owner, settings):
        with closing(self._connect()) as connection, connection:
            connection.execute("""INSERT INTO voice_settings(owner,backend_model,effort,voice) VALUES(?,?,?,?)
                ON CONFLICT(owner) DO UPDATE SET backend_model=excluded.backend_model,
                effort=excluded.effort,voice=excluded.voice""",
                (owner, settings["backendModel"], settings["effort"], settings["voice"]))
