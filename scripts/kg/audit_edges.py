#!/usr/bin/env python3
"""audit_edges.py — v3-D 边审计(代替人工逐条确认;规格 references/emergent-edge-algorithm.md §4)。

抽查 emergent-graph 里的边(优先:status=auto 从未审过 / tier2 散文边 / rel_detail=unconfirmed),
重读证据句 ±上下文,AI 批量判 keep|demote|remove:
  keep   → status=audited
  demote → kind=related + status=audited(不再参与解锁门控)
  remove → 移入 removed_edges 墓碑(不物理删,可查可恢复)
用户改过的边(emergent-confirmations.json edges 里有记录)审计**不动**。
每轮上限 MAX_PER_RUN 条、一次批量调用;全程写 state/attention/edge-audit-log.jsonl。
CLI:默认 dry-run;--run 应用。
"""
import json
import hashlib
import os
import re
import sys
import time
import sqlite3
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config  # noqa: E402
import attention_profile as AP  # noqa: E402
_CODE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
import promote_concepts as PC  # noqa: E402
from concept_node_service import (  # noqa: E402
    ConceptNodeError,
    ConceptNodeService,
    _exclusive_file_lock,
)

STATE = config.PROJECT_DIR / "state"
EMERGENT = STATE / "attention" / "emergent-graph.json"
CONF = STATE / "attention" / "emergent-confirmations.json"
LOG = STATE / "attention" / "edge-audit-log.jsonl"
AUDIT_OUTBOX = STATE / "attention" / "edge-audit-outbox"
SEARCH_DB = STATE / "pdf-search.db"
MAX_PER_RUN = 20


def _audit_service():
    return ConceptNodeService(
        graph_path=EMERGENT,
        journal_path=EMERGENT.parent / "kg-node-mutations.jsonl",
        aliases_path=PC.ALIASES_FILE,
        confirmations_path=CONF,
        kg_dir=PC.KG_DIR,
        concept_root=Path(AP.VAULT_ROOT) / "资源" / "概念",
    )


def _fsync_directory(path):
    """Durably publish/remove outbox directory entries on POSIX."""
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(str(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_durable_directory(path):
    path = Path(path)
    missing = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    for directory in reversed(missing):
        directory.mkdir()
        _fsync_directory(directory.parent)


def _write_json_atomic(path, value):
    _ensure_durable_directory(path.parent)
    temporary = path.with_name(
        "." + path.name + "." + uuid.uuid4().hex + ".tmp"
    )
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _audit_outbox_path(mutation_id):
    name = hashlib.sha256(str(mutation_id).encode("utf-8")).hexdigest()
    return AUDIT_OUTBOX / (name + ".json")


def _audit_outbox_semantic_payload(payload):
    value = json.loads(json.dumps(payload, ensure_ascii=False))
    value.pop("payloadDigest", None)
    for entry in value.get("entries") or []:
        if isinstance(entry, dict):
            entry.pop("ts", None)
    return value


def _audit_outbox_digest(payload):
    value = {
        key: item
        for key, item in payload.items()
        if key != "payloadDigest"
    }
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_audit_outbox(path, payload):
    if (
        not isinstance(payload, dict)
        or payload.get("contract") != "kg-edge-audit-outbox/1"
        or not isinstance(payload.get("entries"), list)
    ):
        raise RuntimeError("edge audit outbox 结构无效")
    mutation_id = str(payload.get("mutationId") or "")
    if (
        not mutation_id
        or len(mutation_id) > 300
        or Path(path).name != _audit_outbox_path(mutation_id).name
        or payload.get("payloadDigest") != _audit_outbox_digest(payload)
    ):
        raise RuntimeError("edge audit outbox 身份或摘要无效")
    entry_ids = set()
    for entry in payload["entries"]:
        if not isinstance(entry, dict):
            raise RuntimeError("edge audit outbox entry 无效")
        edge_identity = str(entry.get("edge_id") or entry.get("edge") or "")
        expected_id = mutation_id + ":" + edge_identity
        entry_id = str(entry.get("entry_id") or "")
        if (
            not edge_identity
            or str(entry.get("mutation_id") or "") != mutation_id
            or entry_id != expected_id
            or entry_id in entry_ids
        ):
            raise RuntimeError("edge audit outbox entry 身份无效或重复")
        entry_ids.add(entry_id)
    return mutation_id


def _stage_audit_logs(mutation_id, logs):
    mutation_id = str(mutation_id or "")
    if not mutation_id or len(mutation_id) > 300:
        raise RuntimeError("edge audit mutationId 无效")
    entries = []
    for log in logs:
        entry = dict(log)
        entry["mutation_id"] = mutation_id
        entry["entry_id"] = mutation_id + ":" + str(
            entry.get("edge_id") or entry.get("edge") or ""
        )
        entries.append(entry)
    payload = {
        "contract": "kg-edge-audit-outbox/1",
        "mutationId": mutation_id,
        "entries": entries,
    }
    payload["payloadDigest"] = _audit_outbox_digest(payload)
    path = _audit_outbox_path(mutation_id)
    _validate_audit_outbox(path, payload)
    with _exclusive_file_lock(
        AUDIT_OUTBOX.with_suffix(".lock")
    ):
        if path.exists():
            try:
                existing = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError, TypeError) as exc:
                raise RuntimeError("edge audit outbox 损坏") from exc
            _validate_audit_outbox(path, existing)
            if (
                _audit_outbox_semantic_payload(existing)
                != _audit_outbox_semantic_payload(payload)
            ):
                raise RuntimeError("edge audit mutationId 对应不同日志 payload")
            return path
        _write_json_atomic(path, payload)
    return path


def _append_audit_logs(entries):
    _ensure_durable_directory(LOG.parent)
    with _exclusive_file_lock(LOG.with_suffix(LOG.suffix + ".lock")):
        existing_entries = {}
        if LOG.exists():
            # 与 KG journal 一样只按物理 ASCII LF 切 JSONL；AI reason
            # 可能合法包含 U+0085/U+2028/U+2029。
            for raw_line in LOG.read_text("utf-8").split("\n"):
                if raw_line.endswith("\r"):
                    raw_line = raw_line[:-1]
                if not raw_line.strip():
                    continue
                try:
                    old_log = json.loads(raw_line)
                except (TypeError, ValueError) as exc:
                    raise RuntimeError(
                        "edge audit log 损坏，拒绝继续追加"
                    ) from exc
                if not isinstance(old_log, dict):
                    raise RuntimeError(
                        "edge audit log 结构无效，拒绝继续追加"
                    )
                if old_log.get("entry_id"):
                    entry_id = str(old_log["entry_id"])
                    existing = existing_entries.get(entry_id)
                    if existing is not None and existing != old_log:
                        raise RuntimeError(
                            "edge audit log entry_id 对应冲突内容"
                        )
                    existing_entries[entry_id] = old_log
        with LOG.open("a", encoding="utf-8") as handle:
            for entry in entries:
                entry_id = str((entry or {}).get("entry_id") or "")
                if not entry_id:
                    raise RuntimeError("edge audit log entry_id 无效")
                existing = existing_entries.get(entry_id)
                if existing is not None:
                    if existing != entry:
                        raise RuntimeError(
                            "edge audit log entry_id 对应冲突内容"
                        )
                    continue
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                existing_entries[entry_id] = entry
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(LOG.parent)


def _flush_audit_outbox(service):
    if not AUDIT_OUTBOX.exists():
        return
    with _exclusive_file_lock(
        AUDIT_OUTBOX.with_suffix(".lock")
    ):
        for path in sorted(AUDIT_OUTBOX.glob("*.json")):
            try:
                payload = json.loads(path.read_text("utf-8"))
            except (OSError, ValueError, TypeError) as exc:
                raise RuntimeError("edge audit outbox 损坏") from exc
            mutation_id = _validate_audit_outbox(path, payload)
            status = service.mutation_status(mutation_id).get("status")
            if status == "ambiguous":
                raise RuntimeError("edge audit mutation 状态不明确")
            if status != "applied":
                continue
            _append_audit_logs(payload["entries"])
            path.unlink()
            _fsync_directory(path.parent)


def _ek(e):
    return "%s|%s|%s" % (e["from"], e["to"], e.get("kind", "prereq"))


def _context_for(e):
    """取证据句的容器上下文(±~200字):note → 笔记可扫描文本;book → 该页 body。"""
    src = e.get("quote_src", "")
    quote = e.get("quote", "")
    text = ""
    if src.startswith("note:"):
        f = PC._find_note_file(src[5:])
        if f:
            try:
                text = "\n".join(seg for seg, _t in PC._note_scannable_text(f.read_text("utf-8")))
            except Exception:
                pass
    elif src.startswith("book:") and SEARCH_DB.exists():
        m = re.match(r"book:(.+)#p(\d+)", src)
        if m:
            con = sqlite3.connect("file:%s?mode=ro" % SEARCH_DB, uri=True)
            r = con.execute("SELECT body FROM pages_data WHERE file=? AND page=?",
                            (m.group(1), int(m.group(2)))).fetchone()
            con.close()
            text = (r[0] if r else "") or ""
    if not text or not quote:
        return quote
    norm = lambda x: re.sub(r"\s+", "", x)
    i = norm(text).find(norm(quote)[:40])
    if i < 0:
        return quote
    # 粗略按压缩后位置换算原文位置(足够取邻域)
    ratio = len(text) / max(1, len(norm(text)))
    j = int(i * ratio)
    return text[max(0, j - 200): j + len(quote) + 200]


def pick_audit_set(g, *, confirmation_edges=None):
    """审计集:未审过的 auto 边 + 散文边 + unconfirmed,跳过用户改过的。"""
    conf_edges = (
        PC._load_conf_edges()
        if confirmation_edges is None
        else dict(confirmation_edges)
    )
    out = []
    for e in g.get("edges", []):
        k2 = "%s|%s" % (e["from"], e["to"])
        if _ek(e) in conf_edges or k2 in conf_edges:
            continue                      # 用户签过字的不动
        st = e.get("status", "auto")
        if st in ("auto", "shadow"):      # R4:未审计的(含 shadow prereq)全是审计对象
            out.append(e)
    return out[:MAX_PER_RUN]


def audit(run=False, model="sonnet", effort="low"):
    service = _audit_service() if run else None
    if service is not None:
        _flush_audit_outbox(service)
    g = json.loads(EMERGENT.read_text("utf-8"))
    confirmation_edges = PC._load_conf_edges(strict=True)
    todo = pick_audit_set(g, confirmation_edges=confirmation_edges)
    if not todo:
        print("无待审计边")
        return {"audited": 0}
    lines = []
    for i, e in enumerate(todo):
        ctx = _context_for(e)
        lines.append("%d. %s %s %s(%s)\n   证据:「%s」\n   上下文:「%s」"
                     % (i, e["from"], "→" if e["kind"] == "prereq" else "—", e["to"],
                        e.get("rel_detail", ""), e.get("quote", "")[:150], ctx[:300]))
    prompt = ("审计下列自动生成的概念关系边。对每条,依据证据句+上下文判断:\n"
              "- keep: 关系与类型(→=前置,—=相关)都站得住\n"
              "- demote: 存在关系但**不是前置**(例子/推广/顺带提及)→ 应降为相关\n"
              "- remove: 证据撑不住这条关系\n\n" + "\n\n".join(lines)
              + '\n\n只输出严格 JSON 数组:[{"i":0,"verdict":"keep|demote|remove","reason":"≤15字"},...]')
    sys.path.insert(0, str(_CODE_ROOT / "_client" / "core"))
    from ai_backends import make_backend
    be = make_backend("claude_cli", {"command": "/usr/bin/claude",
                                     "model": model, "effort": effort, "timeout": 240})
    try:
        raw = be.chat([{"role": "user", "content": prompt}]).strip()
        raw = re.sub(r"^```[a-zA-Z]*\n|\n```\s*$", "", raw)
        a, b = raw.find("["), raw.rfind("]")
        arr = json.loads(raw[a:b + 1]) if a != -1 and b > a else []
    except Exception as e2:
        print("⚠ AI 审计失败:%s(边保持原状,下轮再试)" % str(e2)[:60], file=sys.stderr)
        return {"audited": 0, "error": True}
    got = {int(x["i"]): x for x in arr if isinstance(x, dict) and "i" in x}
    stats = {"keep": 0, "demote": 0, "remove": 0, "noans": 0}
    logs = []
    audit_updates = {}
    audits = g.setdefault("edge_audits", {})   # R4:审计=持久 overlay(第 3 层),重建先应用,墓碑不复活
    for i, e in enumerate(todo):
        x = got.get(i) or {}
        v = x.get("verdict")
        if v not in ("keep", "demote", "remove"):
            stats["noans"] += 1
            continue
        stats[v] += 1
        logs.append({"ts": int(time.time()), "edge": _ek(e), "edge_id": e.get("id"), "verdict": v,
                     "reason": (x.get("reason") or "")[:40], "was": {"kind": e["kind"], "status": e.get("status")}})
        if not run:
            continue
        eid = e.get("id") or PC._edge_id(e["from"], e["to"])
        audit_updates[eid] = {
            "verdict": v,
            "reason": (x.get("reason") or "")[:40],
            "ts": int(time.time()),
        }
        audits[eid] = audit_updates[eid]
    if run:
        audit_identity = {
            edge_id: {
                "verdict": value.get("verdict"),
                "reason": value.get("reason"),
            }
            for edge_id, value in sorted(audit_updates.items())
        }
        operation_payload = {
            "updates": audit_identity,
            "confirmationEdges": confirmation_edges,
            "edgeClaimsSha256": PC._semantic_digest(
                g.get("edge_claims") or {}
            ),
            "edgeAuditsSha256": PC._semantic_digest(
                g.get("edge_audits") or {}
            ),
            "edgeVersion": PC.EDGE_VER,
        }
        basis = json.dumps(
            operation_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        mutation_id = "audit-edges:" + __import__("hashlib").sha256(
            basis.encode("utf-8")
        ).hexdigest()[:24]

        def apply_audits(current):
            if PC._load_conf_edges(strict=True) != confirmation_edges:
                raise ConceptNodeError(
                    "用户边确认在 KG 审计事务前发生变化",
                    "BW_KG_NODE_STALE_EXTERNAL",
                )
            if (
                PC._semantic_digest(current.get("edge_claims") or {})
                != operation_payload["edgeClaimsSha256"]
                or PC._semantic_digest(current.get("edge_audits") or {})
                != operation_payload["edgeAuditsSha256"]
            ):
                raise ConceptNodeError(
                    "KG 审计所依据的边投影已变化",
                    "BW_KG_NODE_STALE_GRAPH",
                )
            current.setdefault("edge_audits", {}).update(audit_updates)
            current["edges"] = PC.derive_edges(
                current,
                confirmation_edges=confirmation_edges,
            )
            return {
                "audited": len(audit_updates),
                "nEdges": len(current["edges"]),
            }

        _stage_audit_logs(mutation_id, logs)
        service.mutate_graph(
            mutation_id=mutation_id,
            source="audit-edges",
            mutator=apply_audits,
            operation_contract="kg-op/audit-edges/1",
            operation_payload=operation_payload,
        )
        _flush_audit_outbox(service)
    for L in logs:
        print("  %s %s — %s" % ({"keep": "✓", "demote": "↓", "remove": "✗"}[L["verdict"]], L["edge"], L["reason"]))
    print("审计 %d 条:keep %d / demote %d / remove %d%s"
          % (len(todo), stats["keep"], stats["demote"], stats["remove"], " [已应用]" if run else " [dry-run]"))
    return stats


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--effort", default="low")
    a = ap.parse_args()
    audit(run=a.run, model=a.model, effort=a.effort)
