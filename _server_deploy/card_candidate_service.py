"""Deterministic related-card candidate selection for the shared review UI.

The service owns only candidate discovery and ranking.  It deliberately does
not choose a card on behalf of the assistant and does not mutate Anki.  Runtime
specific I/O (AnkiConnect, the read-only Anki database and attention-profile
queries) is supplied by the caller so the same policy remains testable and
fail-soft.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import urllib.parse
from collections import defaultdict
from pathlib import Path
from typing import Callable, Iterable, Mapping


CONTRACT = "card-candidate-service/1"

_EVIDENCE = {
    "direct_source": (400, "当前内容来源"),
    "page_kg": (300, "页面知识点"),
    "focus_term": (180, "当前焦点"),
    "material_graph": (90, "相关材料"),
    "due": (0, "到期"),
}
_MATERIAL_PREFIXES = ("book:", "note:", "web:", "kg:", "anki:")
_TERM_RE = re.compile(
    r"[A-Za-z][A-Za-z0-9'_-]{1,}|[\u3040-\u30ff]{2,}|[\u3400-\u9fff]{2,}"
)


def _unique(values: Iterable) -> list:
    out = []
    seen = set()
    for value in values or ():
        if value in (None, "") or value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def _card_ids(values: Iterable) -> list[int]:
    out = []
    for value in values or ():
        try:
            card_id = int(value)
        except (TypeError, ValueError):
            continue
        if card_id > 0:
            out.append(card_id)
    return _unique(out)


def _clean_path(value: str, vault_root: Path | None = None) -> str:
    value = urllib.parse.unquote(str(value or "")).replace("\\", "/").strip()
    if not value:
        return ""
    value = re.sub(r"^\./+", "", value)
    path = Path(value)
    if path.is_absolute() and vault_root:
        try:
            value = path.resolve().relative_to(vault_root.resolve()).as_posix()
        except (OSError, ValueError):
            pass
    if "/obsidian/" in value:
        value = value.split("/obsidian/", 1)[1]
    return value.lstrip("/")


def normalize_material_ref(value: str, vault_root: Path | None = None) -> str:
    """Normalize a material ref without resolving or opening user input."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.lower().startswith(("http://", "https://")):
        return "web:" + raw
    if raw.startswith("web:"):
        return raw
    if raw.startswith("anki:"):
        tail = raw[5:].strip()
        return "anki:" + tail if tail.isdigit() else ""
    if raw.startswith("kg:"):
        book, marker, node = raw[3:].partition("#")
        return "kg:" + book.strip() + marker + node.strip() if book.strip() else ""
    if raw.startswith(("book:", "note:")):
        kind, tail = raw.split(":", 1)
        path, marker, page = tail.partition("#p")
        path = _clean_path(path, vault_root)
        if not path:
            return ""
        if kind == "book" and marker and page.isdigit():
            return "book:%s#p%d" % (path, int(page))
        return kind + ":" + path
    match = re.match(r"^(.+?)#(?:p|page=)(\d+)$", raw, re.I)
    if match:
        return "book:%s#p%d" % (
            _clean_path(match.group(1), vault_root),
            int(match.group(2)),
        )
    path = _clean_path(raw, vault_root)
    if path.lower().endswith((".pdf", ".epub", ".html", ".htm")):
        return "book:" + path
    if path.lower().endswith(".md"):
        return "note:" + path
    return raw[:2000]


def context_source_ref(
    context: Mapping | None, vault_root: Path | None = None
) -> str:
    context = context if isinstance(context, Mapping) else {}
    explicit = normalize_material_ref(context.get("source_ref") or "", vault_root)
    if explicit:
        return explicit
    file_value = str(context.get("file") or context.get("url") or "").strip()
    if not file_value:
        return ""
    ref = normalize_material_ref(file_value, vault_root)
    try:
        page = max(0, int(context.get("page") or 0))
    except (TypeError, ValueError):
        page = 0
    if ref.startswith("book:") and "#p" not in ref and page:
        return ref + "#p%d" % page
    return ref


def _context_key(context: Mapping, source_ref: str) -> str:
    raw = "\n".join(
        (
            source_ref,
            str(context.get("page") or ""),
            str(context.get("selection") or "")[:600],
            str(context.get("visible_text") or "")[:1800],
        )
    )
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()[:20]


class CardCandidateIndex:
    """Read-only index over card records and the existing knowledge graph."""

    def __init__(
        self,
        records_dir: Path | str,
        knowledge_graph_dir: Path | str,
        vault_root: Path | str | None = None,
    ):
        self.records_dir = Path(records_dir)
        self.knowledge_graph_dir = Path(knowledge_graph_dir)
        self.vault_root = Path(vault_root) if vault_root else None
        self._lock = threading.Lock()
        self._fingerprint = None
        self._snapshot = None

    def _files(self) -> list[Path]:
        records = (
            list(self.records_dir.glob("*.json"))
            if self.records_dir.is_dir()
            else []
        )
        graphs = (
            [
                path
                for path in self.knowledge_graph_dir.glob("*.json")
                if ".bak" not in path.name
            ]
            if self.knowledge_graph_dir.is_dir()
            else []
        )
        return sorted(records + graphs)

    def _file_fingerprint(self):
        out = []
        for path in self._files():
            try:
                stat = path.stat()
            except OSError:
                continue
            out.append((str(path), stat.st_mtime_ns, stat.st_size))
        return tuple(out)

    @staticmethod
    def _read_json(path: Path):
        try:
            value = json.loads(path.read_text("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _note_aliases(value: str) -> list[str]:
        value = str(value or "").replace("\\", "/").strip().lstrip("/")
        if value.startswith("note:"):
            value = value[5:]
        if not value:
            return []
        name = Path(value).name
        aliases = [value, name]
        if not value.lower().endswith(".md"):
            aliases.extend((value + ".md", name + ".md"))
        return _unique(alias.casefold() for alias in aliases)

    def _build(self):
        source_notes = defaultdict(set)
        alias_notes = defaultdict(set)
        note_text = {}
        kg_notes = defaultdict(set)
        page_nodes = []

        for path in (
            sorted(self.records_dir.glob("*.json"))
            if self.records_dir.is_dir()
            else ()
        ):
            data = self._read_json(path)
            source_note = _clean_path(data.get("source_note") or "", self.vault_root)
            source_link = str(data.get("source_link") or "")
            if not source_note and re.fullmatch(r"!?\[\[[^\]]+\]\]", source_link):
                source_note = source_link.lstrip("![").rstrip("]").split("|", 1)[0]
            source_ref = normalize_material_ref(
                ("note:" + source_note) if source_note else source_link,
                self.vault_root,
            )
            for card in data.get("cards") or ():
                if not isinstance(card, dict):
                    continue
                try:
                    note_id = int(card.get("anki_note_id") or 0)
                except (TypeError, ValueError):
                    continue
                if note_id <= 0:
                    continue
                if source_ref:
                    source_notes[source_ref].add(note_id)
                for alias in self._note_aliases(source_note or source_link):
                    alias_notes[alias].add(note_id)
                chunks = [
                    source_note,
                    source_link,
                    (
                        str(card.get("front") or ""),
                        str(card.get("back") or ""),
                        str(card.get("text") or ""),
                        str(card.get("reason") or ""),
                        " ".join(str(tag) for tag in (card.get("tags") or ())),
                    ),
                ]
                flat_chunks = chunks[:2] + list(chunks[2])
                note_text[note_id] = "\n".join(flat_chunks).casefold()

        for path in (
            sorted(self.knowledge_graph_dir.glob("*.json"))
            if self.knowledge_graph_dir.is_dir()
            else ()
        ):
            if ".bak" in path.name:
                continue
            data = self._read_json(path)
            book = str(data.get("book") or path.stem).strip()
            pdf_rel = _clean_path(data.get("pdf") or "", self.vault_root)
            for node in data.get("nodes") or ():
                if not isinstance(node, dict) or not node.get("id"):
                    continue
                node_ref = "kg:%s#%s" % (book, node["id"])
                note_ids = set()
                note_refs = [node.get("note_ref")]
                note_refs.extend(node.get("containing_notes") or ())
                for note_ref in note_refs:
                    for alias in self._note_aliases(note_ref or ""):
                        note_ids.update(alias_notes.get(alias, ()))
                if note_ids:
                    kg_notes[node_ref].update(note_ids)
                pages = node.get("pages") or ()
                if (
                    pdf_rel
                    and isinstance(pages, (list, tuple))
                    and len(pages) == 2
                ):
                    try:
                        start, end = int(pages[0]), int(pages[1])
                    except (TypeError, ValueError):
                        continue
                    if start > 0 and end >= start:
                        page_nodes.append((pdf_rel, start, end, node_ref))

        return {
            "source_notes": dict(source_notes),
            "alias_notes": dict(alias_notes),
            "note_text": note_text,
            "kg_notes": dict(kg_notes),
            "page_nodes": page_nodes,
        }

    def snapshot(self):
        fingerprint = self._file_fingerprint()
        if self._snapshot is not None and fingerprint == self._fingerprint:
            return self._snapshot
        with self._lock:
            fingerprint = self._file_fingerprint()
            if self._snapshot is None or fingerprint != self._fingerprint:
                self._snapshot = self._build()
                self._fingerprint = fingerprint
        return self._snapshot

    def page_kg_refs(self, file_value: str, page: int) -> list[str]:
        try:
            page = int(page)
        except (TypeError, ValueError):
            return []
        file_value = _clean_path(file_value, self.vault_root)
        if file_value.startswith("book:"):
            file_value = file_value[5:].split("#p", 1)[0]
        return _unique(
            node_ref
            for pdf_rel, start, end, node_ref in self.snapshot()["page_nodes"]
            if pdf_rel == file_value and start <= page <= end
        )

    def note_ids_for_ref(self, ref: str) -> list[int]:
        ref = normalize_material_ref(ref, self.vault_root)
        snapshot = self.snapshot()
        if ref.startswith("note:"):
            out = set(snapshot["source_notes"].get(ref, ()))
            for alias in self._note_aliases(ref):
                out.update(snapshot["alias_notes"].get(alias, ()))
            return sorted(out)
        if ref.startswith("kg:"):
            return sorted(snapshot["kg_notes"].get(ref, ()))
        if ref.startswith("book:") and "#p" in ref:
            path, page = ref[5:].rsplit("#p", 1)
            if page.isdigit():
                out = set()
                for node_ref in self.page_kg_refs(path, int(page)):
                    out.update(snapshot["kg_notes"].get(node_ref, ()))
                return sorted(out)
        return sorted(snapshot["source_notes"].get(ref, ()))

    def direct_note_ids_for_ref(self, ref: str) -> list[int]:
        """Exact record provenance only; do not promote page-KG matches."""
        ref = normalize_material_ref(ref, self.vault_root)
        snapshot = self.snapshot()
        if ref.startswith("note:"):
            out = set(snapshot["source_notes"].get(ref, ()))
            for alias in self._note_aliases(ref):
                out.update(snapshot["alias_notes"].get(alias, ()))
            return sorted(out)
        return sorted(snapshot["source_notes"].get(ref, ()))

    def focus_matches(self, terms: Iterable[str]) -> dict[str, list[int]]:
        text_by_note = self.snapshot()["note_text"]
        out = {}
        for term in _unique(str(value or "").strip() for value in terms or ()):
            folded = term.casefold()
            if len(folded) < 2:
                continue
            if " " in folded:
                matches = [
                    note_id
                    for note_id, text in text_by_note.items()
                    if folded in text
                ]
            else:
                matches = []
                word = re.compile(
                    r"(?<![A-Za-z0-9_])%s(?![A-Za-z0-9_])"
                    % re.escape(folded)
                )
                for note_id, text in text_by_note.items():
                    if (
                        word.search(text)
                        if re.search(r"[A-Za-z]", folded)
                        else folded in text
                    ):
                        matches.append(note_id)
            out[term] = sorted(matches)
        return out


class CardCandidateService:
    """Merge four evidence channels, then fill the batch with due cards."""

    def __init__(self, index: CardCandidateIndex):
        self.index = index

    @staticmethod
    def _safe_mapping(callback: Callable | None, *args) -> Mapping:
        if not callable(callback):
            return {}
        try:
            value = callback(*args)
        except Exception:
            return {}
        return value if isinstance(value, Mapping) else {}

    @staticmethod
    def _safe_value(callback: Callable | None, *args):
        if not callable(callback):
            return None
        try:
            return callback(*args)
        except Exception:
            return None

    def build(
        self,
        context: Mapping | None,
        due_card_ids: Iterable,
        *,
        resolve_note_cards: Callable[[list[int]], Mapping] | None = None,
        find_source_cards: Callable[[list[str]], Mapping] | None = None,
        search_term_cards: Callable[[list[str]], Mapping] | None = None,
        focus_terms: Callable[[str], object] | None = None,
        relate_material: Callable[[str], object] | None = None,
        material_graph: Callable[[str], object] | None = None,
        exclude_card_ids: Iterable = (),
        limit: int = 30,
    ) -> dict:
        context = dict(context) if isinstance(context, Mapping) else {}
        try:
            limit = min(60, max(1, int(limit)))
        except (TypeError, ValueError):
            limit = 30
        due_ids = _card_ids(due_card_ids)
        excluded_ids = set(_card_ids(exclude_card_ids))
        due_ids = [card_id for card_id in due_ids if card_id not in excluded_ids]
        source_ref = context_source_ref(context, self.index.vault_root)
        context_key = _context_key(context, source_ref)
        candidates = {}
        pending_notes = defaultdict(list)

        def evidence(kind: str, *, ref: str = "", term: str = "", rank=0):
            base, label = _EVIDENCE[kind]
            score = max(1, base - max(0, int(rank)) * 6) if base else 0
            value = {"kind": kind, "label": label, "score": score}
            if ref:
                value["ref"] = ref
            if term:
                value["term"] = term
                value["label"] = "%s：%s" % (label, term)
            return value

        def add_card(card_id, item):
            try:
                card_id = int(card_id)
            except (TypeError, ValueError):
                return
            if card_id <= 0:
                return
            if card_id in excluded_ids:
                return
            row = candidates.setdefault(
                card_id,
                {
                    "evidence": [],
                    "_evidence_keys": set(),
                    "_kind_scores": {},
                    "due_order": None,
                },
            )
            key = (item["kind"], item.get("ref", ""), item.get("term", ""))
            if key not in row["_evidence_keys"]:
                row["_evidence_keys"].add(key)
                row["evidence"].append(item)
            row["_kind_scores"][item["kind"]] = max(
                row["_kind_scores"].get(item["kind"], 0),
                item.get("score", 0),
            )

        def add_notes(note_ids, item):
            for note_id in _card_ids(note_ids):
                pending_notes[note_id].append(item)

        for position, card_id in enumerate(due_ids):
            item = evidence("due")
            add_card(card_id, item)
            candidates[card_id]["due_order"] = position

        direct_refs = [source_ref] if source_ref else []
        page_refs = self.index.page_kg_refs(
            context.get("file") or source_ref,
            context.get("page") or 0,
        )
        explicit_nodes = context.get("kg_nodes") or ()
        if isinstance(explicit_nodes, str):
            explicit_nodes = [explicit_nodes]
        for node in explicit_nodes:
            ref = normalize_material_ref(str(node or ""), self.index.vault_root)
            if ref.startswith("kg:"):
                page_refs.append(ref)
        page_refs = _unique(page_refs)

        source_map = self._safe_mapping(
            find_source_cards, _unique(direct_refs + page_refs)
        )
        for ref in direct_refs:
            item = evidence("direct_source", ref=ref)
            add_notes(self.index.direct_note_ids_for_ref(ref), item)
            for card_id in _card_ids(source_map.get(ref, ())):
                add_card(card_id, item)
        for rank, ref in enumerate(page_refs):
            item = evidence("page_kg", ref=ref, rank=rank)
            add_notes(self.index.note_ids_for_ref(ref), item)
            for card_id in _card_ids(source_map.get(ref, ())):
                add_card(card_id, item)

        focus_text = "\n".join(
            (
                str(context.get("selection") or "")[:600],
                str(context.get("visible_text") or "")[:1800],
            )
        ).strip()
        raw_terms = self._safe_value(focus_terms, focus_text) if focus_text else []
        if isinstance(raw_terms, Mapping):
            raw_terms = raw_terms.get("top") or raw_terms.get("terms") or ()
        terms = []
        for value in raw_terms or ():
            term = value.get("term") if isinstance(value, Mapping) else value
            term = str(term or "").strip()
            if len(term) >= 2:
                terms.append(term)
        if not terms and focus_text:
            terms = _TERM_RE.findall(focus_text)
        terms = _unique(terms)[:6]

        indexed_focus = self.index.focus_matches(terms)
        external_focus = self._safe_mapping(search_term_cards, terms)
        for rank, term in enumerate(terms):
            item = evidence("focus_term", term=term, rank=rank)
            add_notes(indexed_focus.get(term, ()), item)
            for card_id in _card_ids(external_focus.get(term, ())):
                add_card(card_id, item)

        material_refs = []
        structural_seeds = set(direct_refs + page_refs)
        graph_seeds = _unique(direct_refs + page_refs)
        for term in terms[:4]:
            related = self._safe_value(relate_material, term)
            if isinstance(related, Mapping):
                for material in related.get("materials") or ():
                    ref = (
                        material.get("ref")
                        if isinstance(material, Mapping)
                        else material
                    )
                    ref = normalize_material_ref(ref or "", self.index.vault_root)
                    if ref:
                        material_refs.append(ref)
                        graph_seeds.append(ref)
        for seed in _unique(graph_seeds)[:12]:
            graph = self._safe_value(material_graph, seed)
            if not isinstance(graph, Mapping):
                continue
            for layer in graph.get("layers") or ():
                for node in layer or ():
                    ref = node.get("ref") if isinstance(node, Mapping) else node
                    ref = normalize_material_ref(ref or "", self.index.vault_root)
                    if (
                        ref
                        and ref != source_ref
                        and ref not in structural_seeds
                    ):
                        material_refs.append(ref)
        material_refs = _unique(material_refs)[:40]
        material_source_map = self._safe_mapping(
            find_source_cards, material_refs
        )
        for rank, ref in enumerate(material_refs):
            item = evidence("material_graph", ref=ref, rank=min(rank, 12))
            if ref.startswith("anki:") and ref[5:].isdigit():
                add_card(int(ref[5:]), item)
            add_notes(self.index.note_ids_for_ref(ref), item)
            for card_id in _card_ids(material_source_map.get(ref, ())):
                add_card(card_id, item)

        note_map = self._safe_mapping(
            resolve_note_cards, sorted(pending_notes)
        )
        for note_id, items in pending_notes.items():
            card_ids = note_map.get(note_id, note_map.get(str(note_id), ()))
            for card_id in _card_ids(card_ids):
                for item in items:
                    add_card(card_id, item)

        for row in candidates.values():
            row["score"] = sum(
                score
                for kind, score in row["_kind_scores"].items()
                if kind != "due"
            )
            row["related"] = row["score"] > 0

        related_ids = sorted(
            (card_id for card_id, row in candidates.items() if row["related"]),
            key=lambda card_id: (
                -candidates[card_id]["score"],
                candidates[card_id]["due_order"]
                if candidates[card_id]["due_order"] is not None
                else 10**9,
                card_id,
            ),
        )
        selected = related_ids[:limit]
        if len(selected) < limit:
            selected_set = set(selected)
            selected.extend(
                card_id
                for card_id in due_ids
                if card_id not in selected_set
            )
            selected = selected[:limit]

        metadata = {}
        evidence_counts = defaultdict(int)
        for card_id in selected:
            row = candidates[card_id]
            ordered = sorted(
                row["evidence"],
                key=lambda item: (
                    -item.get("score", 0),
                    item["kind"],
                    item.get("ref", ""),
                ),
            )
            reasons = [
                item["label"]
                for item in ordered
                if item["kind"] != "due" or not row["related"]
            ]
            metadata[str(card_id)] = {
                "score": row["score"],
                "related": row["related"],
                "due": row["due_order"] is not None,
                "evidence": ordered,
                "reason_labels": _unique(reasons),
            }
            for kind in row["_kind_scores"]:
                evidence_counts[kind] += 1

        return {
            "contract": CONTRACT,
            "context_key": context_key,
            "source_ref": source_ref,
            "focus_terms": terms,
            "selected_card_ids": selected,
            "related_total": len(related_ids),
            "evidence_counts": dict(evidence_counts),
            "metadata": metadata,
        }
