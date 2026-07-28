#!/usr/bin/env python3
"""Safe, repeatable Codex app-server protocol probe.

The probe deliberately:

* uses the existing Codex login and never reads auth.json;
* removes API-key environment variables before launching child processes;
* runs in an empty temporary directory with an ephemeral thread whose write
  policy is read-only (ordinary filesystem reads remain possible, so this only
  reduces accidental project discovery);
* suppresses app-server stderr and never prints raw JSON-RPC payloads;
* sends no audio and stops immediately if Realtime unexpectedly starts.

The Codex child still uses the existing local Codex config and login. Run this
probe only with a trusted local config and a trusted ``--codex-command``; it is
not an isolation boundary for hooks, MCP servers, apps, or filesystem reads.

Examples:

    python3 scripts/codex_appserver_probe.py
    python3 scripts/codex_appserver_probe.py \
      --codex-command "npx -y @openai/codex@0.145.0"

The JSON report contains only allow-listed capability and outcome fields. The
report itself is safe to attach to an issue or paste into a handoff document.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import shlex
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable


PROBE_MARKER = "CODEX_PROBE_OK"
API_KEY_ENV_NAMES = {
    "OPENAI_API_KEY",
    "AZURE_OPENAI_API_KEY",
    "CODEX_API_KEY",
}
REALTIME_VERSION_PRIORITY = ("v3", "v2", "v1")
# WebSocket is the only transport that this no-audio probe can start without
# manufacturing a browser SDP offer.
REALTIME_TRANSPORT_PRIORITY = ("websocket",)
REALTIME_VOICE_PRIORITY = {
    "v3": (
        "juniper",
        "maple",
        "spruce",
        "ember",
        "vale",
        "breeze",
        "arbor",
        "sol",
        "cove",
    ),
    "v2": ("marin", "alloy", "cedar", "coral"),
    "v1": ("marin", "alloy", "cedar", "coral"),
}
TOOL_ITEM_TYPES = {
    "collabagenttoolcall",
    "collabtoolcall",
    "commandexecution",
    "dynamictoolcall",
    "filechange",
    "imageview",
    "mcptoolcall",
    "websearch",
}
TOOL_EVENT_PREFIXES = (
    "item/commandExecution/",
    "item/fileChange/",
    "item/mcpToolCall/",
)


class ProbeFailure(RuntimeError):
    """A local probe failure whose message is safe to print."""


class RpcError(RuntimeError):
    """A JSON-RPC error. Raw server text is retained only for classification."""

    def __init__(self, error: Any):
        self.error = error if isinstance(error, dict) else {}
        super().__init__("JSON-RPC request failed")


def child_env() -> dict[str, str]:
    """Return the current environment without API-key variables.

    Codex may still use its normal ChatGPT login file. The probe never opens,
    copies, or prints that file.
    """

    env = dict(os.environ)
    for name in list(env):
        upper = name.upper()
        looks_like_openai_secret = (
            ("OPENAI" in upper or "CODEX" in upper)
            and any(
                marker in upper
                for marker in ("API_KEY", "ACCESS_TOKEN", "BEARER_TOKEN")
            )
        )
        if (
            upper in API_KEY_ENV_NAMES
            or upper.endswith("_OPENAI_API_KEY")
            or looks_like_openai_secret
        ):
            env.pop(name, None)
    return env


def run_capture(command: list[str], timeout: float = 30.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        timeout=timeout,
        env=child_env(),
    )


def codex_version(command: list[str]) -> str:
    try:
        proc = run_capture(command + ["--version"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeFailure("Codex version check failed") from exc
    match = re.search(r"\bcodex-cli\s+([A-Za-z0-9._+-]+)", proc.stdout)
    return match.group(1) if proc.returncode == 0 and match else "unknown"


def login_kind(command: list[str]) -> str:
    """Return only a normalized auth kind, never the raw login response."""

    try:
        proc = run_capture(command + ["login", "status"])
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    text = (proc.stdout + "\n" + proc.stderr).lower()
    if proc.returncode != 0:
        return "not_logged_in"
    if "logged in using chatgpt" in text:
        return "chatgpt"
    if "api key" in text:
        return "api_key"
    return "other"


def exec_capabilities(command: list[str]) -> dict[str, bool]:
    try:
        proc = run_capture(command + ["exec", "--help"])
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProbeFailure("Codex exec help check failed") from exc
    if proc.returncode != 0:
        raise ProbeFailure("Codex exec help returned a non-zero status")
    text = proc.stdout
    return {
        "jsonl_events": "--json" in text,
        "image_input": "--image" in text,
        "output_schema": "--output-schema" in text,
        "output_last_message": "--output-last-message" in text,
        "ephemeral": "--ephemeral" in text,
        "resume_subcommand": bool(re.search(r"(?m)^\s+resume\s+", text)),
        "audio_or_realtime_flag": bool(
            re.search(r"(?m)^\s+--(?:audio|microphone|realtime)\b", text)
        ),
    }


def _recursive_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _recursive_strings(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _recursive_strings(item)


def _enum(definitions: dict[str, Any], name: str) -> list[str]:
    value = definitions.get(name) or {}
    result = value.get("enum") if isinstance(value, dict) else None
    return [str(item) for item in result] if isinstance(result, list) else []


def schema_capabilities(command: list[str]) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="codex-appserver-schema-") as tmp:
        out = Path(tmp)
        try:
            proc = run_capture(
                command
                + [
                    "app-server",
                    "generate-json-schema",
                    "--experimental",
                    "--out",
                    str(out),
                ],
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProbeFailure("app-server schema generation failed") from exc
        if proc.returncode != 0:
            raise ProbeFailure("app-server schema generation returned a non-zero status")

        try:
            client = json.loads((out / "ClientRequest.json").read_text("utf-8"))
            server = json.loads((out / "ServerNotification.json").read_text("utf-8"))
        except (OSError, ValueError) as exc:
            raise ProbeFailure("generated app-server schema could not be parsed") from exc

        client_strings = set(_recursive_strings(client))
        server_strings = set(_recursive_strings(server))
        realtime_methods = sorted(
            item for item in client_strings if item.startswith("thread/realtime/")
        )
        realtime_events = sorted(
            item for item in server_strings if item.startswith("thread/realtime/")
        )

        interrupt_required: list[str] = []
        interrupt_file = out / "v2" / "TurnInterruptParams.json"
        if interrupt_file.exists():
            try:
                interrupt_required = [
                    str(item)
                    for item in json.loads(interrupt_file.read_text("utf-8")).get(
                        "required", []
                    )
                ]
            except (OSError, ValueError):
                interrupt_required = []

        realtime: dict[str, Any] = {}
        realtime_file = out / "v2" / "ThreadRealtimeStartParams.json"
        if realtime_file.exists():
            try:
                realtime_doc = json.loads(realtime_file.read_text("utf-8"))
                definitions = realtime_doc.get("definitions") or {}
                properties = realtime_doc.get("properties") or {}
                transport_def = definitions.get("ThreadRealtimeStartTransport") or {}
                transports = sorted(
                    {
                        item
                        for item in _recursive_strings(transport_def)
                        if item in {"websocket", "webrtc"}
                    }
                )
                realtime = {
                    "versions": _enum(definitions, "RealtimeConversationVersion"),
                    "voices": _enum(definitions, "RealtimeVoice"),
                    "transports": transports,
                    "supports_initial_items": "initialItems" in properties,
                    "supports_codex_handoff_mode": (
                        "codexResponseHandoffMode" in properties
                    ),
                    "supports_client_managed_handoffs": (
                        "clientManagedHandoffs" in properties
                    ),
                }
            except (OSError, ValueError):
                realtime = {}

        return {
            "realtime_methods": realtime_methods,
            "realtime_events": realtime_events,
            "turn_interrupt_required": interrupt_required,
            "realtime": realtime,
        }


def first_supported(
    preferred: tuple[str, ...], available: list[str] | tuple[str, ...]
) -> str | None:
    available_set = {str(item) for item in available}
    return next((item for item in preferred if item in available_set), None)


def stop_realtime_safely(
    rpc: "RpcProcess", thread_id: str, timeout: float
) -> dict[str, bool]:
    """Request Realtime shutdown and distinguish acknowledgement from closure."""

    stop_request_accepted = False
    closed_observed = False
    try:
        rpc.request(
            "thread/realtime/stop",
            {"threadId": thread_id},
            timeout=min(timeout, 15),
        )
        stop_request_accepted = True
    except (RpcError, ProbeFailure):
        pass

    if stop_request_accepted:
        try:
            rpc.wait_event(
                lambda event: (
                    event.get("method") == "thread/realtime/closed"
                    and (event.get("params") or {}).get("threadId") == thread_id
                ),
                timeout=min(timeout, 15),
                label="thread/realtime/closed",
            )
            closed_observed = True
        except ProbeFailure:
            pass

    return {
        "stop_request_accepted": stop_request_accepted,
        "closed_observed": closed_observed,
        "safe_stop": stop_request_accepted and closed_observed,
    }


def classify_rpc_error(error: Any) -> dict[str, Any]:
    error = error if isinstance(error, dict) else {}
    code = error.get("code")
    text = str(error.get("message") or "").lower()
    if (
        "api" in text
        and "key" in text
        and ("realtime" in text or "conversation" in text)
        and ("require" in text or "auth" in text)
    ):
        kind = "api_key_required"
    elif "not supported" in text and "voice" in text:
        kind = "unsupported_voice"
    elif "not supported" in text and ("modality" in text or "output" in text):
        kind = "unsupported_output_modality"
    elif "not supported" in text and ("version" in text or "realtime" in text):
        kind = "unsupported_realtime_version"
    elif "authentication failed" in text or "authentication required" in text:
        kind = "authentication_error"
    elif "method not found" in text or code == -32601:
        kind = "method_not_found"
    elif (
        "invalid params" in text
        or "missing field" in text
        or "missing required" in text
        or code == -32602
    ):
        kind = "invalid_params"
    elif "not found" in text:
        kind = "not_found"
    else:
        kind = "other"
    result: dict[str, Any] = {"kind": kind}
    if isinstance(code, int):
        result["code"] = code
    return result


class RpcProcess:
    def __init__(self, command: list[str], cwd: str):
        self._command = command
        self._cwd = cwd
        self._proc: subprocess.Popen[str] | None = None
        self._incoming: queue.Queue[Any] = queue.Queue()
        self._events: list[dict[str, Any]] = []
        self._request_id = 100
        self.stderr_lines_suppressed = 0
        self.tool_event_count = 0
        self.tool_event_kinds: set[str] = set()

    def start(self) -> None:
        try:
            self._proc = subprocess.Popen(
                self._command
                + ["app-server", "--enable", "realtime_conversation"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                bufsize=1,
                cwd=self._cwd,
                env=child_env(),
            )
        except OSError as exc:
            raise ProbeFailure("app-server could not be started") from exc
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            for line in self._proc.stdout:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict):
                    self._record_tool_event(item)
                    self._incoming.put(item)
        finally:
            self._incoming.put(None)

    def _record_tool_event(self, event: dict[str, Any]) -> None:
        """Count actual model/tool execution events, not MCP startup notices."""

        method = str(event.get("method") or "")
        kind = ""
        if method.startswith(TOOL_EVENT_PREFIXES):
            kind = method
        elif method in {"item/started", "item/completed"}:
            params = event.get("params") or {}
            item = params.get("item") or {}
            item_type = str(item.get("type") or "").lower()
            if item_type in TOOL_ITEM_TYPES:
                kind = item_type
        if kind:
            self.tool_event_count += 1
            self.tool_event_kinds.add(kind)

    def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        for _line in self._proc.stderr:
            self.stderr_lines_suppressed += 1

    def _write(self, payload: dict[str, Any]) -> None:
        if (
            self._proc is None
            or self._proc.poll() is not None
            or self._proc.stdin is None
        ):
            raise ProbeFailure("app-server exited unexpectedly")
        try:
            self._proc.stdin.write(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
            )
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise ProbeFailure("app-server input pipe closed") from exc

    def notify(self, method: str, params: dict[str, Any]) -> None:
        self._write({"method": method, "params": params})

    def request(
        self, method: str, params: dict[str, Any], timeout: float = 30.0
    ) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        self._write(
            {
                "method": method,
                "id": request_id,
                "params": params,
            }
        )
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeFailure(f"JSON-RPC request timed out: {method}")
            try:
                item = self._incoming.get(timeout=remaining)
            except queue.Empty as exc:
                raise ProbeFailure(f"JSON-RPC request timed out: {method}") from exc
            if item is None:
                raise ProbeFailure("app-server exited unexpectedly")
            if item.get("id") == request_id:
                if item.get("error"):
                    raise RpcError(item["error"])
                result = item.get("result")
                return result if isinstance(result, dict) else {}
            self._events.append(item)

    def wait_event(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        timeout: float = 30.0,
        label: str = "JSON-RPC event",
    ) -> dict[str, Any]:
        for index, item in enumerate(self._events):
            if predicate(item):
                return self._events.pop(index)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeFailure(f"{label} timed out")
            try:
                item = self._incoming.get(timeout=remaining)
            except queue.Empty as exc:
                raise ProbeFailure(f"{label} timed out") from exc
            if item is None:
                raise ProbeFailure("app-server exited unexpectedly")
            if predicate(item):
                return item
            self._events.append(item)

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except OSError:
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=3)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self._proc.kill()
            except OSError:
                pass


def _event_turn_id(event: dict[str, Any]) -> str:
    params = event.get("params") or {}
    if isinstance(params.get("turnId"), str):
        return params["turnId"]
    turn = params.get("turn") or {}
    return str(turn.get("id") or "") if isinstance(turn, dict) else ""


def wait_turn(
    rpc: RpcProcess,
    thread_id: str,
    turn_id: str,
    timeout: float,
    label: str,
) -> dict[str, Any]:
    deltas: list[str] = []
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProbeFailure("turn completion timed out")

        def relevant(event: dict[str, Any]) -> bool:
            method = event.get("method")
            params = event.get("params") or {}
            if params.get("threadId") != thread_id:
                return False
            if method not in {"item/agentMessage/delta", "turn/completed", "error"}:
                return False
            event_tid = _event_turn_id(event)
            return not event_tid or event_tid == turn_id

        event = rpc.wait_event(
            relevant,
            timeout=remaining,
            label=f"{label} turn/completed",
        )
        method = event.get("method")
        params = event.get("params") or {}
        if method == "item/agentMessage/delta":
            delta = params.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
            continue
        if method == "error":
            return {
                "status": "error",
                "delta_events": len(deltas),
                "text": "".join(deltas),
            }
        turn = params.get("turn") or {}
        return {
            "status": str(turn.get("status") or "unknown"),
            "delta_events": len(deltas),
            "text": "".join(deltas),
        }


def _turn_id(result: dict[str, Any]) -> str:
    turn = result.get("turn") or {}
    turn_id = turn.get("id") if isinstance(turn, dict) else None
    if not isinstance(turn_id, str) or not turn_id:
        raise ProbeFailure("turn/start response did not contain a turn id")
    return turn_id


def run_live_probe(
    command: list[str],
    schema: dict[str, Any],
    timeout: float,
    model: str | None,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="codex-appserver-probe-") as tmp:
        rpc = RpcProcess(command, tmp)
        rpc.start()
        try:
            rpc.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "bw-reader-codex-protocol-probe",
                        "title": "protocol-probe",
                        "version": "1",
                    },
                    "capabilities": {"experimentalApi": True},
                },
                timeout=min(timeout, 30),
            )
            rpc.notify("initialized", {})
            result["initialize"] = "ok"

            thread_params: dict[str, Any] = {
                "cwd": tmp,
                "approvalPolicy": "never",
                "sandbox": "read-only",
                "ephemeral": True,
                "environments": [],
                "developerInstructions": (
                    "This is an isolated protocol probe. Never call tools. "
                    "Answer only as the user explicitly requests."
                ),
            }
            if model:
                thread_params["model"] = model
            thread_response = rpc.request(
                "thread/start", thread_params, timeout=min(timeout, 45)
            )
            thread = thread_response.get("thread") or {}
            thread_id = thread.get("id") if isinstance(thread, dict) else None
            if not isinstance(thread_id, str) or not thread_id:
                raise ProbeFailure("thread/start response did not contain a thread id")
            result["thread_start"] = {
                "status": "ok",
                "model": str(thread_response.get("model") or "unknown"),
                "ephemeral": bool(thread.get("ephemeral")),
            }

            text_start = rpc.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [
                        {
                            "type": "text",
                            "text": (
                                "Protocol probe. Reply with exactly "
                                f"{PROBE_MARKER} and nothing else. Do not use tools."
                            ),
                        }
                    ],
                    "effort": "low",
                    "environments": [],
                },
                timeout=min(timeout, 45),
            )
            text_turn_id = _turn_id(text_start)
            text_done = wait_turn(
                rpc, thread_id, text_turn_id, timeout, "minimal text"
            )
            text = text_done.pop("text")
            result["text_turn"] = {
                **text_done,
                "characters": len(text),
                "marker_seen": PROBE_MARKER in text,
                "marker_exact": text.strip() == PROBE_MARKER,
            }

            cancel_start = rpc.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [
                        {
                            "type": "text",
                            "text": (
                                "Do not use tools. Write PROBE_STREAM, then list "
                                "the integers 1 through 2000, one per line."
                            ),
                        }
                    ],
                    "effort": "low",
                    "environments": [],
                },
                timeout=min(timeout, 45),
            )
            cancel_turn_id = _turn_id(cancel_start)

            def cancel_started_or_done(event: dict[str, Any]) -> bool:
                method = event.get("method")
                params = event.get("params") or {}
                return bool(
                    params.get("threadId") == thread_id
                    and _event_turn_id(event) == cancel_turn_id
                    and method in {"item/agentMessage/delta", "turn/completed"}
                )

            cancel_first_event = rpc.wait_event(
                cancel_started_or_done,
                timeout=min(timeout, 45),
                label="cancellation turn first delta",
            )
            cancel_was_running = (
                cancel_first_event.get("method") == "item/agentMessage/delta"
            )

            try:
                rpc.request(
                    "turn/interrupt",
                    {"threadId": thread_id},
                    timeout=min(timeout, 15),
                )
                result["invalid_interrupt"] = {"kind": "unexpected_success"}
            except RpcError as exc:
                result["invalid_interrupt"] = classify_rpc_error(exc.error)

            try:
                rpc.request(
                    "turn/interrupt",
                    {"threadId": thread_id, "turnId": cancel_turn_id},
                    timeout=min(timeout, 15),
                )
                interrupt_rpc: dict[str, Any] = {"rpc_status": "ok"}
            except RpcError as exc:
                interrupt_rpc = {
                    "rpc_status": "error",
                    **classify_rpc_error(exc.error),
                }
            if cancel_was_running:
                cancelled = wait_turn(
                    rpc,
                    thread_id,
                    cancel_turn_id,
                    min(timeout, 45),
                    "interrupted",
                )
                cancelled.pop("text")
                result["interrupt"] = {
                    "running_state_observed": True,
                    **interrupt_rpc,
                    **cancelled,
                }
            else:
                completed_turn = (
                    (cancel_first_event.get("params") or {}).get("turn") or {}
                )
                result["interrupt"] = {
                    "running_state_observed": False,
                    **interrupt_rpc,
                    "status": str(completed_turn.get("status") or "completed"),
                }

            try:
                rpc.request(
                    "probe/not-a-method",
                    {},
                    timeout=min(timeout, 15),
                )
                result["unknown_method"] = {"kind": "unexpected_success"}
            except RpcError as exc:
                rejected = classify_rpc_error(exc.error)
                # Some app-server versions use JSON-RPC -32600 rather than
                # -32601 for a typed-but-unsupported request. The method name
                # is fixed by this probe, so classify the observed rejection
                # without exposing the raw server message.
                if rejected.get("kind") == "other":
                    rejected["kind"] = "request_rejected"
                result["unknown_method"] = rejected

            methods = schema.get("realtime_methods") or []
            rt_schema = schema.get("realtime") or {}
            if "thread/realtime/start" not in methods:
                result["realtime"] = {"status": "unsupported_by_schema"}
            else:
                versions = rt_schema.get("versions") or []
                version = first_supported(REALTIME_VERSION_PRIORITY, versions)
                transports = rt_schema.get("transports") or []
                transport = first_supported(
                    REALTIME_TRANSPORT_PRIORITY, transports
                )
                voices = rt_schema.get("voices") or []
                voice = first_supported(
                    REALTIME_VOICE_PRIORITY.get(version or "", ()), voices
                )
                if not version or not transport or not voice:
                    result["realtime"] = {
                        "status": "no_compatible_safe_selection",
                        "version_selected": version,
                        "transport_selected": transport,
                        "voice_selected": voice,
                        "selection_from_schema": True,
                        "audio_sent": False,
                    }
                else:
                    rt_params: dict[str, Any] = {
                        "threadId": thread_id,
                        "outputModality": "audio",
                        "prompt": (
                            "Authentication-only protocol probe. Do not respond "
                            "until input arrives."
                        ),
                        "transport": {"type": transport},
                        "includeStartupContext": False,
                        "version": version,
                        "voice": voice,
                    }
                    base_rt_result = {
                        "version": version,
                        "transport": transport,
                        "voice": voice,
                        "output_modality": "audio",
                        "selection_from_schema": True,
                        "audio_sent": False,
                    }
                    try:
                        rpc.request(
                            "thread/realtime/start",
                            rt_params,
                            timeout=min(timeout, 30),
                        )

                        def realtime_terminal(event: dict[str, Any]) -> bool:
                            return event.get("method") in {
                                "thread/realtime/error",
                                "thread/realtime/started",
                            } and (event.get("params") or {}).get(
                                "threadId"
                            ) == thread_id

                        rt_event = rpc.wait_event(
                            realtime_terminal,
                            timeout=min(timeout, 30),
                            label="thread/realtime terminal event",
                        )
                        rt_method = rt_event.get("method")
                        if rt_method == "thread/realtime/error":
                            rt_message = str(
                                (rt_event.get("params") or {}).get("message")
                                or ""
                            )
                            rt_classified = classify_rpc_error(
                                {"message": rt_message}
                            )
                            result["realtime"] = {
                                "status": (
                                    "authentication_required"
                                    if rt_classified["kind"]
                                    == "api_key_required"
                                    else "error"
                                ),
                                "error_class": rt_classified["kind"],
                                **base_rt_result,
                            }
                        else:
                            stop_result = stop_realtime_safely(
                                rpc, thread_id, timeout
                            )
                            result["realtime"] = {
                                "status": "capability_available",
                                **stop_result,
                                **base_rt_result,
                            }
                    except RpcError as exc:
                        classified = classify_rpc_error(exc.error)
                        result["realtime"] = {
                            "status": (
                                "authentication_required"
                                if classified["kind"] == "api_key_required"
                                else "error"
                            ),
                            "error_class": classified["kind"],
                            **base_rt_result,
                        }
            result["tool_events"] = {
                "count": rpc.tool_event_count,
                "kinds": sorted(rpc.tool_event_kinds),
                "asserted_zero": rpc.tool_event_count == 0,
            }
            result["stderr_lines_suppressed"] = rpc.stderr_lines_suppressed
        finally:
            rpc.close()
    return result


def evaluate(report: dict[str, Any], live_requested: bool) -> bool:
    if report.get("auth") != "chatgpt":
        return False
    security = report.get("security") or {}
    security_ok = bool(
        security.get("api_key_environment_forwarded") is False
        and security.get("auth_file_read_by_probe") is False
        and security.get("audio_sent") is False
        and security.get("raw_rpc_or_stderr_printed") is False
        and security.get("filesystem_reads_allowed") is True
        and security.get("thread_ephemeral_requested") is True
        and security.get("sandbox_write_policy") == "read-only"
        and security.get("working_directory") == "empty_temporary_directory"
        and security.get("trusted_local_config_required") is True
        and security.get("user_config_isolated") is False
    )
    schema = report.get("schema") or {}
    exec_caps = report.get("exec") or {}
    if (
        not security_ok
        or not schema.get("realtime_methods")
        or not exec_caps.get("jsonl_events")
    ):
        return False
    if not live_requested:
        return True
    live = report.get("live") or {}
    text_turn = live.get("text_turn") or {}
    interrupt = live.get("interrupt") or {}
    thread_start = live.get("thread_start") or {}
    tool_events = live.get("tool_events") or {}
    realtime = live.get("realtime") or {}
    realtime_ok = bool(
        (
            realtime.get("status") == "authentication_required"
            and realtime.get("error_class") == "api_key_required"
        )
        or (
            realtime.get("status") == "capability_available"
            and realtime.get("stop_request_accepted") is True
            and realtime.get("closed_observed") is True
            and realtime.get("safe_stop") is True
        )
    )
    return bool(
        live.get("initialize") == "ok"
        and thread_start.get("status") == "ok"
        and thread_start.get("ephemeral") is True
        and text_turn.get("status") == "completed"
        and text_turn.get("marker_exact") is True
        and (live.get("invalid_interrupt") or {}).get("kind") == "invalid_params"
        and interrupt.get("rpc_status") == "ok"
        and interrupt.get("status") == "interrupted"
        and (live.get("unknown_method") or {}).get("kind")
        in {"method_not_found", "request_rejected"}
        and tool_events.get("count") == 0
        and tool_events.get("asserted_zero") is True
        and realtime_ok
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Probe Codex app-server using existing ChatGPT auth without exposing "
            "or forwarding API-key environment variables."
        )
    )
    parser.add_argument(
        "--codex-command",
        default="codex",
        help=(
            "Codex command prefix, parsed without a shell. "
            'Example: "npx -y @openai/codex@0.145.0".'
        ),
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Optional text-turn model override. The value is not included in prompts.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=90.0,
        help="Per-turn timeout in seconds (default: 90).",
    )
    parser.add_argument(
        "--schema-only",
        action="store_true",
        help="Inspect schema/help/login only; do not start app-server turns.",
    )
    args = parser.parse_args()

    command = shlex.split(args.codex_command)
    if not command:
        parser.error("--codex-command must not be empty")
    if args.timeout < 10:
        parser.error("--timeout must be at least 10 seconds")

    started = time.monotonic()
    try:
        version = codex_version(command)
        auth = login_kind(command)
        schema = schema_capabilities(command)
        report: dict[str, Any] = {
            "probe": "codex-appserver-protocol",
            "codex_version": version,
            "auth": auth,
            "security": {
                "api_key_environment_forwarded": False,
                "auth_file_read_by_probe": False,
                "audio_sent": False,
                "raw_rpc_or_stderr_printed": False,
                "thread_ephemeral_requested": True,
                "sandbox_write_policy": "read-only",
                "filesystem_reads_allowed": True,
                "working_directory": "empty_temporary_directory",
                "trusted_local_config_required": True,
                "user_config_isolated": False,
            },
            "exec": exec_capabilities(command),
            "schema": schema,
        }
        if args.schema_only:
            report["live"] = {"status": "skipped_by_option"}
        elif auth != "chatgpt":
            report["live"] = {
                "status": "skipped",
                "reason": "existing_login_is_not_chatgpt",
            }
        else:
            report["live"] = run_live_probe(
                command, schema, args.timeout, args.model
            )
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        report["passed"] = evaluate(report, not args.schema_only)
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if report["passed"] else 1
    except ProbeFailure as exc:
        safe_report = {
            "probe": "codex-appserver-protocol",
            "status": "probe_failure",
            "reason": str(exc),
            "security": {
                "api_key_environment_forwarded": False,
                "auth_file_read_by_probe": False,
                "audio_sent": False,
                "raw_rpc_or_stderr_printed": False,
                "thread_ephemeral_requested": True,
                "sandbox_write_policy": "read-only",
                "filesystem_reads_allowed": True,
                "working_directory": "empty_temporary_directory",
                "trusted_local_config_required": True,
                "user_config_isolated": False,
            },
            "passed": False,
        }
        print(json.dumps(safe_report, ensure_ascii=False, indent=2, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
