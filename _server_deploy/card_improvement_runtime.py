"""Runtime adapter for the shared card-improvement service.

The domain prompts and validation live in ``_client/core/card_improvement_service``.
This module only adds:

* an honest Codex app-server thread runner;
* an explicitly-labelled one-shot fallback;
* opaque, owner-bound draft handles used by prepare/confirm APIs;
* the single serialized commit coordinator shared by the retained QA page and
  the reader/extension review workspace.

Preparing a draft never writes an Anki note, changes a source note, or deletes
the original card.  Committing accepts only a signed frozen draft handle.
"""
from __future__ import annotations

import copy
import hashlib
import hmac
import inspect
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = PROJECT_ROOT / "_client" / "core"
if str(CORE_DIR) not in sys.path:
    sys.path.insert(0, str(CORE_DIR))

from card_improvement_service import (  # noqa: E402
    CardImprovementError,
    CardImprovementService,
    normalize_pairs,
)


class CardImprovementRuntimeError(RuntimeError):
    pass


class CardImprovementCommitConflict(CardImprovementRuntimeError):
    """The source changed after preview; applying the frozen draft is unsafe."""


def _err_text(error: BaseException | str) -> str:
    return str(error or "unknown")[:160]


def _normalize_service_tier(value: Any) -> str:
    tier = str(value or "").strip().lower()
    if tier not in ("", "priority"):
        raise CardImprovementRuntimeError(
            "service_tier 只支持空值或 priority"
        )
    return tier


def _accepts_keyword(call: Callable[..., Any], name: str) -> bool | None:
    """Return whether ``call`` advertises a keyword, or None if unknowable."""

    try:
        parameters = inspect.signature(call).parameters.values()
    except (TypeError, ValueError):
        return None
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        or parameter.name == name
        for parameter in parameters
    )


class OneShotCardImprovementRunner:
    """Fallback runner whose lack of a server-side conversation is explicit."""

    native_multiturn = False
    can_reuse_context = False

    def __init__(
        self,
        ask: Callable[[str], str | None],
        *,
        reason: str = "configured_one_shot",
        label: str = "one-shot",
        service_tier: str = "",
    ):
        self._ask = ask
        self.fallback_reason = reason
        self.label = label
        self.mode = "one_shot_fallback"
        self.one_shot_turns = 0
        self.service_tier_request = _normalize_service_tier(service_tier)
        self.service_tier_effective = ""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def ask(self, prompt: str) -> str:
        self.one_shot_turns += 1
        value = self._ask(prompt)
        if not str(value or "").strip():
            raise CardImprovementRuntimeError("一次性 AI runner 返回空")
        return str(value)

    def ask_with_fallback(
        self,
        prompt: str,
        *,
        fallback_prompt: str | None = None,
    ) -> str:
        return self.ask(fallback_prompt or prompt)

    def info(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "native_thread": False,
            "native_multiturn_used": False,
            "native_turns": 0,
            "one_shot_turns": self.one_shot_turns,
            "fallback_reason": self.fallback_reason,
            "service_tier_request": self.service_tier_request,
            "service_tier_effective": self.service_tier_effective,
        }


class CodexAppThreadCardImprovementRunner:
    """Use one app-server thread for every generation turn in one bundle."""

    label = "codex-app-server-thread"

    def __init__(
        self,
        codex_app: Any,
        *,
        fallback: Callable[[str], str | None] | None = None,
        model: str = "gpt-5.6-luna",
        effort: str = "low",
        timeout: int = 180,
        service_tier: str = "",
    ):
        self.codex_app = codex_app
        self.fallback = fallback
        self.model = model
        self.effort = effort
        self.timeout = timeout
        self.service_tier_request = _normalize_service_tier(service_tier)
        self.service_tier_effective = ""
        self.thread_id = ""
        self.native_turns = 0
        self.one_shot_turns = 0
        self.fallback_reason = ""
        self.mode = "codex_app_thread"

    @property
    def native_multiturn(self) -> bool:
        return bool(self.thread_id)

    @property
    def can_reuse_context(self) -> bool:
        return bool(self.thread_id and self.native_turns > 0)

    def __enter__(self):
        try:
            if self.service_tier_request:
                supports_tier = _accepts_keyword(
                    self.codex_app.thread_start,
                    "service_tier",
                )
                if supports_tier is False:
                    raise CardImprovementRuntimeError(
                        "app_server_service_tier_unsupported:thread_start"
                    )
                try:
                    thread_id = self.codex_app.thread_start(
                        self.model,
                        service_tier=self.service_tier_request,
                    )
                except TypeError as error:
                    if supports_tier is None:
                        raise CardImprovementRuntimeError(
                            "app_server_service_tier_unsupported:thread_start:"
                            + _err_text(error)
                        ) from error
                    raise
            else:
                # Preserve compatibility with adapters that only implement the
                # original ``thread_start(model)`` contract.
                thread_id = self.codex_app.thread_start(self.model)
            self.thread_id = str(thread_id or "")
            if not self.thread_id:
                raise RuntimeError("thread/start 未返回 id")
        except Exception as error:
            self.thread_id = ""
            self.mode = "one_shot_fallback"
            message = _err_text(error)
            if message.startswith("app_server_service_tier_unsupported:"):
                self.fallback_reason = message
            else:
                self.fallback_reason = "app_server_start_failed:" + message
            if self.fallback is None:
                raise CardImprovementRuntimeError(self.fallback_reason) from error
        return self

    def close(self):
        thread_id, self.thread_id = self.thread_id, ""
        if thread_id:
            try:
                self.codex_app.thread_close(thread_id)
            except Exception:
                pass

    def __exit__(self, *_):
        self.close()
        return False

    def _fallback_ask(self, prompt: str) -> str:
        if self.fallback is None:
            raise CardImprovementRuntimeError(
                self.fallback_reason or "Codex app-server 不可用且没有一次性兜底"
            )
        self.one_shot_turns += 1
        value = self.fallback(prompt)
        if not str(value or "").strip():
            raise CardImprovementRuntimeError("一次性兜底返回空")
        return str(value)

    def ask(self, prompt: str) -> str:
        return self.ask_with_fallback(prompt, fallback_prompt=prompt)

    def ask_with_fallback(
        self,
        prompt: str,
        *,
        fallback_prompt: str | None = None,
    ) -> str:
        if self.thread_id:
            try:
                if self.service_tier_request:
                    supports_tier = _accepts_keyword(
                        self.codex_app.turn_stream,
                        "service_tier",
                    )
                    if supports_tier is False:
                        raise CardImprovementRuntimeError(
                            "app_server_service_tier_unsupported:turn_stream"
                        )
                    try:
                        stream = self.codex_app.turn_stream(
                            self.thread_id,
                            prompt,
                            self.effort,
                            timeout=self.timeout,
                            service_tier=self.service_tier_request,
                        )
                    except TypeError as error:
                        if supports_tier is None:
                            raise CardImprovementRuntimeError(
                                "app_server_service_tier_unsupported:turn_stream:"
                                + _err_text(error)
                            ) from error
                        raise
                else:
                    # Preserve the original adapter signature when Fast was
                    # not requested.
                    stream = self.codex_app.turn_stream(
                        self.thread_id,
                        prompt,
                        self.effort,
                        timeout=self.timeout,
                    )
                value = "".join(stream).strip()
                if not value:
                    raise RuntimeError("app-server turn 返回空")
                self.native_turns += 1
                if self.service_tier_request:
                    self.service_tier_effective = self.service_tier_request
                return value
            except Exception as error:
                message = _err_text(error)
                if message.startswith("app_server_service_tier_unsupported:"):
                    self.fallback_reason = message
                else:
                    self.fallback_reason = "app_server_turn_failed:" + message
                self.mode = (
                    "hybrid_fallback" if self.native_turns else "one_shot_fallback"
                )
                self.close()
        return self._fallback_ask(fallback_prompt or prompt)

    def info(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "native_thread": self.native_turns > 0,
            "native_multiturn_used": self.native_turns >= 2,
            "native_turns": self.native_turns,
            "one_shot_turns": self.one_shot_turns,
            "fallback_reason": self.fallback_reason,
            "service_tier_request": self.service_tier_request,
            "service_tier_effective": self.service_tier_effective,
        }


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _normalize_targets(value: Any) -> tuple[str, ...]:
    if isinstance(value, str):
        aliases = {
            "anki": ("anki",),
            "cards": ("anki",),
            "note": ("note",),
            "all": ("anki", "note"),
        }
        targets = aliases.get(value.strip().lower(), ())
    elif isinstance(value, (list, tuple, set)):
        targets = tuple(
            "anki" if str(item).lower() == "cards" else str(item).lower()
            for item in value
        )
    else:
        targets = ()
    selected = set(x for x in targets if x in ("anki", "note"))
    # A bundle always runs cards first and note second.  Keeping this order
    # canonical is important: the note turn can reuse the card + QA context
    # already present in the same native app-server thread.
    targets = tuple(x for x in ("anki", "note") if x in selected)
    if not targets:
        raise CardImprovementRuntimeError("target 必须是 anki、note 或 all")
    return targets


def _normalize_card(card: Any) -> dict[str, Any]:
    if not isinstance(card, dict):
        raise CardImprovementRuntimeError("缺少卡片上下文")
    ctype = str(card.get("type") or ("cloze" if card.get("cloze") else "basic"))
    normalized = {
        "type": ctype if ctype in ("basic", "reverse", "cloze") else "basic",
        "front": str(card.get("front") or "")[:20000],
        "back": str(card.get("back") or "")[:20000],
        "text": str(card.get("text") or card.get("cloze") or "")[:20000],
        "cloze": str(card.get("cloze") or card.get("text") or "")[:20000],
        "local_id": str(card.get("local_id") or "")[:160],
        "entity_id": str(card.get("entity_id") or "")[:160],
        "entity_index": card.get("entity_index"),
        "anki_note_id": card.get("anki_note_id"),
        "source_note": str(card.get("source_note") or "")[:1000],
        "source_ref": str(card.get("source_ref") or "")[:1000],
        "source_link": str(card.get("source_link") or "")[:2000],
        "source_url": str(card.get("source_url") or "")[:4000],
        "deck": str(card.get("deck") or "")[:500],
    }
    if not (normalized["front"] or normalized["back"] or normalized["text"]):
        raise CardImprovementRuntimeError("卡片内容为空")
    return normalized


class CardImprovementDraftStore:
    """Opaque owner-bound handles for explicit prepare → commit."""

    def __init__(self, *, ttl_seconds: int = 4 * 3600, secret: bytes | None = None):
        self.ttl_seconds = max(60, int(ttl_seconds))
        self._secret = secret or secrets.token_bytes(32)
        self._items: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def _signature(self, nonce: str, owner: str) -> str:
        return hmac.new(
            self._secret,
            f"{nonce}\0{owner}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()[:32]

    def _parse(self, draft_id: str, owner: str) -> str:
        match = str(draft_id or "").split(".", 1)
        if len(match) != 2 or not match[0].startswith("d_"):
            raise CardImprovementRuntimeError("无效 draft id")
        nonce, signature = match
        expected = self._signature(nonce, owner)
        if not hmac.compare_digest(signature, expected):
            raise CardImprovementRuntimeError("draft id 校验失败")
        return nonce

    def _gc_locked(self, now: float):
        for nonce, item in list(self._items.items()):
            if float(item.get("expires_at") or 0) <= now:
                self._items.pop(nonce, None)

    def create(self, owner: str, payload: dict[str, Any]) -> str:
        owner = str(owner or "").strip()
        if not owner:
            raise CardImprovementRuntimeError("draft owner 不能为空")
        nonce = "d_" + secrets.token_urlsafe(24)
        now = time.time()
        item = copy.deepcopy(payload)
        item.update({
            "owner": owner,
            "created_at": int(now),
            "expires_at": int(now + self.ttl_seconds),
            "committed": [],
        })
        with self._lock:
            self._gc_locked(now)
            self._items[nonce] = item
        return nonce + "." + self._signature(nonce, owner)

    def get(self, draft_id: str, owner: str) -> dict[str, Any]:
        owner = str(owner or "").strip()
        nonce = self._parse(draft_id, owner)
        now = time.time()
        with self._lock:
            self._gc_locked(now)
            item = self._items.get(nonce)
            if not item:
                raise CardImprovementRuntimeError("草稿不存在或已过期")
            if item.get("owner") != owner:
                raise CardImprovementRuntimeError("草稿不属于当前用户")
            return copy.deepcopy(item)

    def mark_committed(self, draft_id: str, owner: str, target: str):
        owner = str(owner or "").strip()
        nonce = self._parse(draft_id, owner)
        with self._lock:
            item = self._items.get(nonce)
            if not item:
                raise CardImprovementRuntimeError("草稿不存在或已过期")
            if item.get("owner") != owner:
                raise CardImprovementRuntimeError("草稿不属于当前用户")
            committed = item.setdefault("committed", [])
            if target not in committed:
                committed.append(target)


DEFAULT_DRAFT_STORE = CardImprovementDraftStore()
_DEFAULT_COMMIT_LOCK = threading.Lock()


def atomic_replace_text(path: Path, content: str) -> None:
    """Durably replace one text file without exposing partial contents."""

    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(
        prefix="." + path.name + ".card-improvement-",
        suffix=".tmp",
        dir=str(path.parent),
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, path.stat().st_mode & 0o777)
        except OSError:
            pass
        os.replace(tmp, path)
        try:
            directory_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def commit_card_improvement_draft(
    *,
    draft_id: str,
    target: str,
    owner: str,
    commit_anki: Callable[[str, dict[str, Any], list[dict[str, Any]]], dict[str, Any]]
    | None = None,
    resolve_note_path: Callable[[dict[str, Any]], Path] | None = None,
    after_note_commit: Callable[
        [dict[str, Any], Path, str, str, dict[str, Any]], None
    ]
    | None = None,
    store: CardImprovementDraftStore | None = None,
    commit_lock: threading.Lock | threading.RLock | None = None,
) -> dict[str, Any]:
    """Commit one target from an owner-bound frozen draft.

    Both the reader assistant and the retained legacy QA page call this exact
    coordinator.  Surface-specific code may only provide the Anki transport,
    the already-authorized source-note resolver, and optional bookkeeping
    after a successful note replacement.  Validation, idempotency,
    serialization, baseline conflict handling, durable replacement, and the
    committed marker therefore cannot drift between the two entry points.
    """

    target = str(target or "").strip().lower()
    if target not in ("anki", "note"):
        raise CardImprovementRuntimeError("target 只能是 anki 或 note")
    store = store or DEFAULT_DRAFT_STORE
    lock = commit_lock or _DEFAULT_COMMIT_LOCK
    with lock:
        item = store.get(draft_id, owner)
        if target not in (item.get("targets") or []):
            raise CardImprovementRuntimeError("该草稿不包含这个提交目标")
        if target in (item.get("committed") or []):
            return {
                "ok": True,
                "dedup": True,
                "target": target,
                "summary": "这个草稿目标已经提交过，没有重复写入。",
            }
        drafts = (
            item.get("drafts")
            if isinstance(item.get("drafts"), dict)
            else {}
        )
        identity = (
            item.get("identity")
            if isinstance(item.get("identity"), dict)
            else {}
        )

        if target == "anki":
            cards = drafts.get("cards")
            if not isinstance(cards, list) or not cards:
                raise CardImprovementRuntimeError(
                    "草稿里没有可提交的 Anki 卡片"
                )
            if commit_anki is None:
                raise CardImprovementRuntimeError("当前入口没有 Anki 提交适配器")
            result = commit_anki(
                str(draft_id),
                copy.deepcopy(identity),
                copy.deepcopy(cards),
            )
            if not isinstance(result, dict):
                raise CardImprovementRuntimeError("Anki 提交响应无效")
            if not result.get("ok"):
                return result
            store.mark_committed(draft_id, owner, target)
            result = dict(result)
            result["target"] = target
            return result

        note = (
            drafts.get("note")
            if isinstance(drafts.get("note"), dict)
            else {}
        )
        content = note.get("content")
        base_sha = str(note.get("base_sha256") or "")
        if not isinstance(content, str) or not content.strip():
            raise CardImprovementRuntimeError(
                "草稿里没有可提交的笔记内容"
            )
        if len(content) > 400_000:
            raise CardImprovementRuntimeError("笔记草稿过长")
        if not re.fullmatch(r"[a-f0-9]{64}", base_sha):
            raise CardImprovementRuntimeError("笔记草稿缺少有效的基线摘要")
        if resolve_note_path is None:
            raise CardImprovementRuntimeError("当前入口没有源笔记解析器")
        path = Path(resolve_note_path(copy.deepcopy(identity)))
        current = path.read_text("utf-8")
        current_sha = hashlib.sha256(current.encode("utf-8")).hexdigest()
        draft_sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
        if current_sha == draft_sha:
            store.mark_committed(draft_id, owner, target)
            return {
                "ok": True,
                "dedup": True,
                "target": target,
                "result": {"path": str(path), "sha256": draft_sha},
                "summary": "原笔记已经是这份草稿，没有重复覆盖。",
            }
        if current_sha != base_sha:
            raise CardImprovementCommitConflict(
                "原笔记在预览后已发生变化；为避免覆盖新内容，本次提交已停止。"
            )

        atomic_replace_text(path, content)
        warning = ""
        if after_note_commit is not None:
            try:
                after_note_commit(
                    copy.deepcopy(identity),
                    path,
                    current,
                    content,
                    copy.deepcopy(note),
                )
            except Exception as error:
                warning = "笔记已写入，但后处理失败：" + _err_text(error)
        store.mark_committed(draft_id, owner, target)
        result = {
            "ok": True,
            "target": target,
            "result": {
                "path": str(path),
                "sha256": draft_sha,
                "before_chars": len(current),
                "after_chars": len(content),
                "verbosity": (
                    "concise"
                    if note.get("verbosity") == "concise"
                    else "verbose"
                ),
            },
            "summary": "已用确认过的草稿更新原笔记。",
        }
        if warning:
            result["warning"] = warning
        return result


def prepare_card_improvement_draft(
    *,
    owner: str,
    card: dict[str, Any],
    pairs: Any,
    target: Any,
    original_note: str | None = None,
    verbosity: str = "verbose",
    codex_app: Any = None,
    one_shot: Callable[[str], str | None] | None = None,
    model: str = "gpt-5.6-luna",
    effort: str = "low",
    timeout: int = 180,
    service_tier: str = "",
    store: CardImprovementDraftStore = DEFAULT_DRAFT_STORE,
) -> dict[str, Any]:
    """Prepare immutable draft output; never commit it."""

    targets = _normalize_targets(target)
    normalized_card = _normalize_card(card)
    selected = normalize_pairs(pairs)
    selected_chars = sum(
        len(pair["question"]) + len(pair["answer"]) for pair in selected
    )
    if selected_chars > 120_000:
        raise CardImprovementRuntimeError("选中问答过长")
    if "note" in targets:
        if original_note is None:
            raise CardImprovementRuntimeError("生成笔记草稿需要原笔记")
        if len(original_note) > 400_000:
            raise CardImprovementRuntimeError("原笔记过长，不能安全生成完整替换草稿")

    if codex_app is not None:
        runner: Any = CodexAppThreadCardImprovementRunner(
            codex_app,
            fallback=one_shot,
            model=model,
            effort=effort,
            timeout=timeout,
            service_tier=service_tier,
        )
    elif one_shot is not None:
        runner = OneShotCardImprovementRunner(
            one_shot,
            reason="app_server_not_available",
            service_tier=service_tier,
        )
    else:
        raise CardImprovementRuntimeError("没有可用的 card-improvement runner")

    with runner:
        service = CardImprovementService(runner)
        generated: dict[str, Any] = {}
        if targets == ("anki", "note"):
            generated = service.prepare_bundle(
                normalized_card,
                selected,
                original_note=original_note,
                verbosity=verbosity,
            )
        else:
            if "anki" in targets:
                generated["anki"] = service.improve_cards(
                    normalized_card, selected
                )
            if "note" in targets:
                generated["note"] = service.improve_note(
                    normalized_card,
                    str(original_note),
                    selected,
                    verbosity,
                )
        runner_info = runner.info()

    drafts: dict[str, Any] = {}
    if "anki" in generated:
        drafts["cards"] = generated["anki"]["cards"]
    if "note" in generated:
        drafts["note"] = {
            "content": generated["note"]["content"],
            "verbosity": generated["note"]["verbosity"],
            "base_sha256": hashlib.sha256(
                str(original_note).encode("utf-8")
            ).hexdigest(),
            "base_chars": len(str(original_note)),
        }
    identity = {
        "local_id": normalized_card.get("local_id") or "",
        "entity_id": normalized_card.get("entity_id") or "",
        "entity_index": normalized_card.get("entity_index"),
        "anki_note_id": normalized_card.get("anki_note_id"),
        "source_note": normalized_card.get("source_note") or "",
        "source_ref": normalized_card.get("source_ref") or "",
        "source_link": normalized_card.get("source_link") or "",
        "source_url": normalized_card.get("source_url") or "",
        "deck": normalized_card.get("deck") or "",
        "card_base_sha256": _canonical_hash({
            key: normalized_card.get(key)
            for key in ("type", "front", "back", "text", "cloze")
        }),
    }
    stored = {
        "targets": list(targets),
        "identity": identity,
        "drafts": drafts,
        "trace": service.trace,
        "runner": runner_info,
    }
    draft_id = store.create(str(owner), stored)
    return {
        "ok": True,
        "draft_id": draft_id,
        "targets": list(targets),
        "identity": identity,
        "drafts": drafts,
        "runner": runner_info,
        "trace": service.trace,
    }
