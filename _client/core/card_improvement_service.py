"""Shared card-improvement domain service.

This module deliberately contains no HTTP, Flask, AnkiConnect, filesystem-write,
or reader UI code.  The old ``qa_browser`` page and the reader review workspace
can therefore use the same prompts, output validation, and card-reference
resolution while keeping their own presentation and commit policies.

The runner is injected.  A legacy caller may pass a one-shot ``ask(prompt)``
callable; a newer caller may pass an object whose ``ask`` method keeps one
native CLI/app-server thread alive across the calls in ``prepare_bundle``.
Nothing here claims that a runner is stateful when it is not.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Iterable, Protocol


_ENTITY_ID_RE = re.compile(r"^card_[a-f0-9]{4,12}$")
_ENTITY_WITH_INDEX_RE = re.compile(r"^(card_[a-f0-9]{4,12})(?::|/|#)(\d{1,3})$")
_CARD_TYPES = frozenset(("basic", "reverse", "cloze"))


class CardImprovementError(ValueError):
    """A request or model result is not safe to apply."""


class CardResolver(Protocol):
    """Resolve one stable card reference to a normalized card context."""

    def resolve(self, reference: "CardReference") -> dict[str, Any] | None:
        ...


@dataclass(frozen=True)
class CardReference:
    """A stable card group id plus its index inside that group."""

    card_id: str
    index: int | None = None

    @classmethod
    def parse(cls, card_id: str, index: Any = None) -> "CardReference":
        raw = str(card_id or "").strip()
        inline = _ENTITY_WITH_INDEX_RE.fullmatch(raw)
        if inline:
            raw = inline.group(1)
            if index in (None, ""):
                index = inline.group(2)
        parsed_index: int | None
        if index in (None, ""):
            parsed_index = None
        else:
            try:
                parsed_index = int(index)
            except (TypeError, ValueError) as exc:
                raise CardImprovementError("卡片 index 不是整数") from exc
            if parsed_index < 0 or parsed_index > 999:
                raise CardImprovementError("卡片 index 超出范围")
        return cls(raw, parsed_index)

    @property
    def is_entity(self) -> bool:
        return bool(_ENTITY_ID_RE.fullmatch(self.card_id))


class CompositeCardResolver:
    """Try resolvers in order without coupling the service to one storage host."""

    def __init__(self, resolvers: Iterable[CardResolver | Callable[[CardReference], Any]]):
        self._resolvers = tuple(resolvers)

    def resolve(self, reference: CardReference) -> dict[str, Any] | None:
        for resolver in self._resolvers:
            try:
                fn = resolver.resolve if hasattr(resolver, "resolve") else resolver
                result = fn(reference)  # type: ignore[misc]
            except (FileNotFoundError, PermissionError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(result, dict) and result:
                return result
        return None


class JsonEntityRegistryResolver:
    """Read ``card_xxxxxx + index`` from one reader entity registry.

    Account-scoped readers should inject the already-authorized account registry
    path.  The legacy QA daemon can inject ``state/assets/registry.json`` as a
    compatibility fallback.  This class never scans other account directories.
    """

    def __init__(self, registry_path: str | Path):
        self.registry_path = Path(registry_path)

    def resolve(self, reference: CardReference) -> dict[str, Any] | None:
        registry = json.loads(self.registry_path.read_text(encoding="utf-8"))
        return MappingEntityRegistryResolver(registry).resolve(reference)


class MappingEntityRegistryResolver:
    """Resolve an entity from an already-authorized registry mapping."""

    def __init__(self, registry: dict[str, Any]):
        self.registry = registry

    def resolve(self, reference: CardReference) -> dict[str, Any] | None:
        if not reference.is_entity:
            return None
        registry = self.registry
        if not isinstance(registry, dict):
            raise ValueError("invalid entity registry")
        entity = registry.get(reference.card_id)
        if not isinstance(entity, dict) or entity.get("kind") != "cards":
            return None
        cards = entity.get("data")
        if not isinstance(cards, list) or not cards:
            return None
        idx = 0 if reference.index is None else reference.index
        if idx < 0 or idx >= len(cards) or not isinstance(cards[idx], dict):
            return None
        raw = cards[idx]
        meta = entity.get("meta") if isinstance(entity.get("meta"), dict) else {}
        states = entity.get("states") if isinstance(entity.get("states"), dict) else {}
        state = states.get(str(idx)) if isinstance(states.get(str(idx)), dict) else {}
        source_ref = str(
            entity.get("src")
            or entity.get("source")
            or meta.get("src")
            or meta.get("source")
            or ""
        )
        ctype = str(raw.get("type") or ("cloze" if raw.get("cloze") else "basic"))
        return {
            "local_id": reference.card_id,
            "entity_id": reference.card_id,
            "entity_index": idx,
            "type": ctype if ctype in _CARD_TYPES else "basic",
            "front": str(raw.get("front") or ""),
            "back": str(raw.get("back") or ""),
            "text": str(raw.get("cloze") or raw.get("text") or ""),
            "cloze": str(raw.get("cloze") or raw.get("text") or ""),
            "anki_note_id": state.get("_nid"),
            "source_ref": source_ref,
            "source_note": str(entity.get("source_note") or meta.get("source_note") or ""),
            "source_link": str(entity.get("source_link") or meta.get("source_link") or ""),
            "source_url": str(entity.get("source_url") or meta.get("source_url") or ""),
        }


def normalize_pairs(pairs: Iterable[dict[str, Any]] | None) -> list[dict[str, str]]:
    """Normalize selected QA fragments once for every UI and workflow."""

    out: list[dict[str, str]] = []
    for pair in pairs or ():
        if not isinstance(pair, dict):
            continue
        answer = str(pair.get("answer") or "").strip()
        if not answer:
            continue
        out.append({
            "question": str(pair.get("question") or "").strip(),
            "answer": answer,
        })
    if not out:
        raise CardImprovementError("没有标记为有用的回答")
    return out


def pairs_text(pairs: Iterable[dict[str, Any]] | None) -> str:
    return "\n\n".join(
        f"问：{p['question']}\n答：{p['answer']}"
        for p in normalize_pairs(pairs)
    )


_NOTE_PROMPT_HEAD = (
    "背景逻辑：我在复习一张由这篇笔记生成的 Anki 卡片(②)、对照笔记(①)时，"
    "有个地方没看懂，于是问了问题(③)；AI 回答里我**主动勾选为有用**的部分(④)"
    "就是我认为应该补到笔记里的内容。\n\n"
    "任务：把 ④ 的实质内容补到笔记 ① 的对应位置（与 ③ 困惑最相关处）。\n\n"
    "**处理方式（两种模式共同遵循）**\n"
    "1. **不必照搬每句话**：④ 是参考素材，允许改写措辞、调整顺序、合并相似表达。"
    "目标是让笔记自然顺畅、信息密度合适——不是文字级 paste。\n"
    "2. **改写原段 或 追加新段** 都可以，选让笔记更连贯的方式：\n"
    "   - 笔记 ① 已经简略提到 ④ 的概念 → 通常**改写原段**更顺\n"
    "   - ④ 是新主题/新角度 → 追加新段（位置合适即可，可用 `### 子标题`）\n"
    "3. **连贯化（关键）**：④ 可能是**间断选中**的（跨标题、跳过中间、多轮问答）。"
    "写入时必须把片段重新组织成前后通顺的文字：\n"
    "   - 必要时补**过渡词**（『此外』『另一方面』『相应地』），但不引入 ④ 没有的新事实\n"
    "   - 同一概念的不同侧面可以用 `### 子标题` 分组\n"
    "   - 间断处不要留 markdown 空标题、孤立 list 项、半句话\n"
    "4. **不得添加 ④ 没有的事实**：自创类比、自创例子、自创引申、自创对比——一律不要。\n"
    "5. ②③ 仅用于定位（判断 ④ 补到哪一节、怎么衔接），**不作为内容来源**。\n"
    "6. 保留 frontmatter（开头 --- 之间）和「相关笔记」节原样不动；"
    "保持 Obsidian Markdown，数学公式用 $...$ 或 $$...$$。\n"
    "7. 直接输出修改后的完整笔记内容，**第一行就是笔记原本的开头（--- 或正文）**，"
    "不要写任何说明、前言或代码围栏。\n\n"
    "**两种模式的差异（详细/精炼）见下**：\n"
)
_NOTE_PROMPT_VERBOSE = _NOTE_PROMPT_HEAD + (
    "**当前模式：详细（verbose）**\n"
    "- 保留 ④ 里的**关键例子、关键推导步骤、对照表的主要行**（不必每个都搬，但要让读者能看清\n"
    "  概念的『展开层次』，而不只是结论）\n"
    "- 公式 / 定义 / 结论：保留原貌\n"
    "- 措辞可改写、相似句可合并，但**不要为了短而砍掉独立的知识点或例子**\n"
    "- 一般情况下，新增长度应接近或略小于 ④ 的长度（不应剧烈压缩）\n\n"
)
_NOTE_PROMPT_CONCISE = _NOTE_PROMPT_HEAD + (
    "**当前模式：精炼（concise）**\n"
    "- 在**核心信息不丢**的前提下大胆浓缩：多个相似例子合到 1 个最有代表性的；推导提炼为关键\n"
    "  几步；对照表压成行内表述\n"
    "- 公式 / 定义 / 结论：保留原貌\n"
    "- 适合 ④ 内容较长但你只想抓住要点时\n"
    "- 新增长度通常会明显短于 ④（这是预期）\n\n"
)


def _card_description(card: dict[str, Any], *, include_type: bool = False) -> str:
    if str(card.get("type") or "") == "cloze":
        desc = f"挖空文本：{card.get('text') or card.get('cloze') or ''}\n补充：{card.get('back') or ''}"
        return ("类型：cloze\n" + desc) if include_type else desc
    desc = (
        f"正面（问）：{card.get('front') or ''}\n"
        f"背面（答）：{card.get('back') or ''}"
    )
    return (f"类型：{card.get('type') or 'basic'}\n" + desc) if include_type else desc


def build_anki_prompt(card: dict[str, Any], pairs: Iterable[dict[str, Any]]) -> str:
    return (
        "背景逻辑：我在复习这张 Anki 卡片时有个地方没看懂，于是问了问题；"
        "下面【有效问答】里的回答是我**勾选为有用**的，它恰好讲清了我的困惑——"
        "也就是说原卡片在这一点上不够清楚或不够到位，导致我卡住了。\n\n"
        "任务：在原卡基础上生成一张或多张改进后的新卡来代替它，"
        "重点是把『让我卡住、而有效回答讲清了的那个点』在新卡里讲明白（更准确/清晰/易记）。\n"
        "约束：改进所依据的新信息只来自【有效问答】，不要自行添加问答里没有的"
        "类比/例子/引申；保持卡片简洁聚焦，别堆砌。数学公式用 LaTeX（行内 \\( \\)，行间 \\[ \\]）。\n\n"
        f"=== 原卡片（我卡住的那张）===\n{_card_description(card, include_type=True)}\n\n"
        f"=== 有效问答（我的困惑 + 讲清它的回答，改进依据）===\n{pairs_text(pairs)}\n\n"
        "只输出 JSON 数组，不要其它文字，每个元素：\n"
        '[{"type":"basic|cloze|reverse","front":"非cloze的问题","back":"答案",'
        '"text":"cloze专用，含{{c1::...}}","reason":"制卡理由"}]'
    )


def build_note_prompt(
    card: dict[str, Any],
    original_note: str,
    pairs: Iterable[dict[str, Any]],
    verbosity: str = "verbose",
) -> str:
    mode = "concise" if verbosity == "concise" else "verbose"
    head = _NOTE_PROMPT_CONCISE if mode == "concise" else _NOTE_PROMPT_VERBOSE
    pairs_label = "可适度提炼合并，核心信息不丢" if mode == "concise" else "应完整保留"
    return (
        head
        + f"=== ① 当前笔记 ===\n{original_note}\n\n"
        + "=== ② 正在复习的 Anki 卡片（仅用于定位，不作内容来源）===\n"
        + _card_description(card)
        + "\n\n"
        + f"=== ③ 我问的问题 + ④ 我勾选的有用回答（**{pairs_label}**）===\n"
        + pairs_text(pairs)
    )


def build_note_followup_prompt(
    original_note: str,
    verbosity: str = "verbose",
) -> str:
    """Note-edit turn used only after the same native thread saw card + QA."""

    mode = "concise" if verbosity == "concise" else "verbose"
    head = _NOTE_PROMPT_CONCISE if mode == "concise" else _NOTE_PROMPT_VERBOSE
    return (
        "继续使用上一轮里给出的【原卡片】和【有效问答】。"
        "上一轮生成的新卡片不是新的事实来源；仍然只允许把原来的【有效问答】作为新增内容来源。\n\n"
        + head
        + f"=== ① 当前笔记 ===\n{original_note}\n\n"
        + "=== ②③④ 上下文 ===\n"
        + "原卡片、用户问题和勾选的有用回答已经在同一 thread 的上一轮中给出，"
        + "不要要求用户重发，也不要把上一轮生成的新卡片当作事实来源。"
    )


def parse_anki_drafts(raw: str) -> list[dict[str, str]]:
    text = str(raw or "")
    start, end = text.find("["), text.rfind("]")
    if start < 0 or end <= start:
        raise CardImprovementError("AI 未返回 JSON 卡片数组")
    try:
        rows = json.loads(text[start : end + 1])
    except json.JSONDecodeError as exc:
        raise CardImprovementError("AI 返回的卡片 JSON 无法解析") from exc
    if not isinstance(rows, list):
        raise CardImprovementError("AI 返回的卡片不是数组")
    out: list[dict[str, str]] = []
    for row in rows[:24]:
        if not isinstance(row, dict):
            continue
        ctype = str(row.get("type") or "basic").strip().lower()
        if ctype not in _CARD_TYPES:
            ctype = "basic"
        front = str(row.get("front") or "").strip()
        back = str(row.get("back") or "").strip()
        cloze = str(row.get("text") or row.get("cloze") or "").strip()
        reason = str(row.get("reason") or "QA 改进").strip()[:500]
        if ctype == "cloze":
            if not cloze or "{{c" not in cloze:
                continue
        elif not front or not back:
            continue
        out.append({
            "type": ctype,
            "front": front,
            "back": back,
            "text": cloze,
            "cloze": cloze,
            "reason": reason or "QA 改进",
        })
    if not out:
        raise CardImprovementError("AI 未生成有效卡片")
    return out


def clean_note_draft(original_note: str, raw: str) -> str:
    content = str(raw or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\n", "", content)
        content = re.sub(r"\n```\s*$", "", content)
    if str(original_note or "").lstrip().startswith("---"):
        index = content.find("---")
        if index > 0:
            content = content[index:]
    if not content or len(content) < len(original_note) * 0.5:
        raise CardImprovementError("AI 返回内容异常（过短），已放弃写入")
    return content


class CardImprovementService:
    """The one prompt/validation implementation shared by all card UIs."""

    def __init__(
        self,
        runner: Callable[[str], str] | Any,
        *,
        runner_label: str | None = None,
    ):
        self.runner = runner
        self.runner_label = (
            runner_label
            or getattr(runner, "label", None)
            or getattr(runner, "__name__", None)
            or runner.__class__.__name__
        )
        self.trace: dict[str, Any] = {
            "workflow": "card_improvement",
            "runner": str(self.runner_label),
            "steps": [],
            # This is recipe-compatible metadata only.  Saving/running a recipe
            # remains the task runtime's responsibility.
            "recipe_candidate": {
                "kind": "intent",
                "intent": "improve_review_card",
                "version": 1,
            },
        }

    def _ask(
        self,
        prompt: str,
        step: str,
        *,
        fallback_prompt: str | None = None,
    ) -> str:
        if hasattr(self.runner, "ask_with_fallback"):
            call = lambda value: self.runner.ask_with_fallback(  # noqa: E731
                value, fallback_prompt=fallback_prompt
            )
        else:
            call = self.runner.ask if hasattr(self.runner, "ask") else self.runner
        try:
            raw = call(prompt)
        except Exception as exc:
            self.trace["steps"].append({"name": step, "status": "error"})
            raise CardImprovementError(f"AI 生成失败：{exc}") from exc
        text = str(raw or "")
        self.trace["steps"].append({
            "name": step,
            "status": "done",
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16],
            "output_chars": len(text),
        })
        return text

    def improve_cards(
        self,
        card: dict[str, Any],
        pairs: Iterable[dict[str, Any]],
    ) -> dict[str, Any]:
        selected = normalize_pairs(pairs)
        cards = parse_anki_drafts(self._ask(build_anki_prompt(card, selected), "improve_cards"))
        return {"cards": cards, "trace": self.trace}

    def improve_note(
        self,
        card: dict[str, Any],
        original_note: str,
        pairs: Iterable[dict[str, Any]],
        verbosity: str = "verbose",
        *,
        reuse_thread_context: bool = False,
    ) -> dict[str, Any]:
        selected = normalize_pairs(pairs)
        mode = "concise" if verbosity == "concise" else "verbose"
        full_prompt = build_note_prompt(card, original_note, selected, mode)
        prompt = (
            build_note_followup_prompt(original_note, mode)
            if reuse_thread_context
            else full_prompt
        )
        raw = self._ask(
            prompt,
            "improve_note",
            fallback_prompt=full_prompt,
        )
        return {
            "content": clean_note_draft(original_note, raw),
            "verbosity": mode,
            "trace": self.trace,
        }

    def prepare_bundle(
        self,
        card: dict[str, Any],
        pairs: Iterable[dict[str, Any]],
        *,
        original_note: str | None = None,
        verbosity: str = "verbose",
    ) -> dict[str, Any]:
        """Prepare one or both drafts through the same injected runner.

        If the runner owns a native thread, these calls remain turns in that
        thread.  With a legacy one-shot callable they remain honest independent
        calls; the trace identifies the runner but does not pretend otherwise.
        """

        result: dict[str, Any] = {}
        # Put the card task first: its prompt establishes the shared card + QA
        # context.  A native app-server runner can then append only the note and
        # note-editing policy instead of retransmitting those inputs.
        result["anki"] = self.improve_cards(card, pairs)
        if original_note is not None:
            reuse = bool(getattr(self.runner, "can_reuse_context", False))
            result["note"] = self.improve_note(
                card,
                original_note,
                pairs,
                verbosity,
                reuse_thread_context=reuse,
            )
        result["trace"] = self.trace
        return result
