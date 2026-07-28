"""Cache-stable tool catalog primitives for the reader assistants.

This module is deliberately transport-neutral.  It does not import
``assistant.py`` or own handler functions; ``assistant.py`` binds this catalog
to the production executor while text, MCP and voice transports consume its
projections.  Its job is to keep these invariants executable:

* the catalog prefix is deterministic and independent of the current mode;
* textual progressive disclosure is append-only inside a logical thread;
* OpenAI tool-search definitions are declared once and use deferred loading;
* Realtime gets one stable, flattened tool list for the whole call;
* mode/host availability is enforced by the executor, never by deleting a
  definition that the model has already seen.

Keeping those rules in a small standalone module lets PDF, EPUB, web and voice
adapters share one catalog without coupling their handlers or duplicating
provider schemas.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from typing import Any, Callable, Iterable, Mapping, Sequence


_JSON_SCHEMA_ANY_OBJECT: Mapping[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": True,
}


class ToolRegistryError(ValueError):
    """Raised when a catalog or monotonic loading invariant is violated."""


@dataclass(frozen=True, slots=True)
class ToolNamespace:
    """A small, searchable family of related tools."""

    name: str
    description: str

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ToolRegistryError(f"invalid namespace name: {self.name!r}")
        if not self.description.strip():
            raise ToolRegistryError(f"namespace {self.name!r} needs a description")


@dataclass(frozen=True, slots=True)
class ToolSpec:
    """Provider-neutral definition plus runtime policy metadata."""

    name: str
    description: str
    namespace: str
    parameters: Mapping[str, Any] = field(
        default_factory=lambda: dict(_JSON_SCHEMA_ANY_OBJECT)
    )
    strict: bool = False
    core: bool = False
    surfaces: frozenset[str] = field(default_factory=frozenset)
    modes: frozenset[str] = field(default_factory=frozenset)
    hosts: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        if not self.name or not self.name.replace("_", "").isalnum():
            raise ToolRegistryError(f"invalid tool name: {self.name!r}")
        if not self.description.strip():
            raise ToolRegistryError(f"tool {self.name!r} needs a description")
        if not self.namespace:
            raise ToolRegistryError(f"tool {self.name!r} needs a namespace")
        if self.parameters.get("type") != "object":
            raise ToolRegistryError(
                f"tool {self.name!r} parameters must be an object schema"
            )

    def is_visible_on(self, surface: str) -> bool:
        return not self.surfaces or surface in self.surfaces

    def is_allowed(self, *, mode: str = "", host: str = "") -> bool:
        return (
            (not self.modes or mode in self.modes)
            and (not self.hosts or host in self.hosts)
        )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


class ToolRegistry:
    """Immutable, deterministically ordered catalog.

    ``mode`` is intentionally absent from all projection methods.  Switching
    from normal reading to review mode changes executor policy and appends a
    mode-state message; it must not reorder or remove tool definitions from
    the cached prefix.
    """

    def __init__(
        self,
        namespaces: Iterable[ToolNamespace],
        tools: Iterable[ToolSpec],
        *,
        schema_version: int = 1,
        max_tools_per_namespace: int = 9,
    ) -> None:
        ns_rows = sorted(tuple(namespaces), key=lambda item: item.name)
        tool_rows = sorted(tuple(tools), key=lambda item: (item.namespace, item.name))
        self.schema_version = int(schema_version)
        self.max_tools_per_namespace = int(max_tools_per_namespace)
        self._namespaces = {item.name: item for item in ns_rows}
        self._tools = {item.name: item for item in tool_rows}
        if len(self._namespaces) != len(ns_rows):
            raise ToolRegistryError("duplicate namespace")
        if len(self._tools) != len(tool_rows):
            raise ToolRegistryError("duplicate tool")
        for spec in tool_rows:
            if spec.namespace not in self._namespaces:
                raise ToolRegistryError(
                    f"tool {spec.name!r} references unknown namespace "
                    f"{spec.namespace!r}"
                )
        for namespace in self._namespaces:
            count = sum(1 for spec in tool_rows if spec.namespace == namespace)
            if count > self.max_tools_per_namespace:
                raise ToolRegistryError(
                    f"namespace {namespace!r} has {count} tools; "
                    f"limit is {self.max_tools_per_namespace}"
                )
        self._catalog_payload = self._make_catalog_payload()
        self.catalog_version = hashlib.sha256(
            _canonical_json(self._catalog_payload).encode("utf-8")
        ).hexdigest()[:16]

    @property
    def namespaces(self) -> tuple[ToolNamespace, ...]:
        return tuple(self._namespaces.values())

    @property
    def tools(self) -> tuple[ToolSpec, ...]:
        return tuple(self._tools.values())

    def get(self, name: str) -> ToolSpec:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise ToolRegistryError(f"unknown tool: {name}") from exc

    def tools_in(self, namespace: str, *, surface: str = "") -> tuple[ToolSpec, ...]:
        if namespace not in self._namespaces:
            raise ToolRegistryError(f"unknown namespace: {namespace}")
        return tuple(
            spec
            for spec in self._tools.values()
            if spec.namespace == namespace
            and (not surface or spec.is_visible_on(surface))
        )

    def visible_tools(self, surface: str = "") -> tuple[ToolSpec, ...]:
        """Return the deterministic projection for one production surface."""

        return tuple(
            spec
            for spec in self._tools.values()
            if not surface or spec.is_visible_on(surface)
        )

    def execution_allowed(
        self,
        name: str,
        *,
        mode: str = "",
        host: str = "",
        surface: str = "",
    ) -> bool:
        spec = self.get(name)
        return (
            (not surface or spec.is_visible_on(surface))
            and spec.is_allowed(mode=mode, host=host)
        )

    def cache_key(self, surface: str, *, shard: str = "") -> str:
        """Stable routing key; never include page, user, mode or loaded domains."""

        base = f"bw-reader:{surface}:tools-v{self.schema_version}:{self.catalog_version}"
        return base + (f":{shard}" if shard else "")

    def stable_text_prefix(self, surface: str) -> str:
        """Small fixed prefix for CLI/custom-JSON orchestrators."""

        core = [
            f"- {spec.name}: {spec.description}"
            for spec in self._tools.values()
            if spec.core and spec.is_visible_on(surface)
        ]
        groups = [
            f"- {ns.name}: {ns.description}"
            for ns in self._namespaces.values()
            if self.tools_in(ns.name, surface=surface)
        ]
        return (
            f"【工具目录版本】{self.catalog_version}\n"
            "【常驻工具】\n"
            + ("\n".join(core) if core else "(无)")
            + "\n【可渐进加载的工具域】\n"
            + "\n".join(groups)
            + "\n需要某个域的详细工具时调用 load_toolset(namespace)。"
            "域一旦加载，本线程只追加、不卸载。"
        )

    def openai_tool_search_tools(self, surface: str) -> list[dict[str, Any]]:
        """Stable Responses API projection for GPT-5.4+ tool search.

        All deferred schemas remain in the request's declared inventory, but
        the model initially sees only namespace summaries.  Runtime mode gates
        happen after a call is emitted and therefore do not mutate this list.
        """

        out: list[dict[str, Any]] = []
        for ns in self._namespaces.values():
            specs = self.tools_in(ns.name, surface=surface)
            if not specs:
                continue
            funcs: list[dict[str, Any]] = []
            for spec in specs:
                row: dict[str, Any] = {
                    "type": "function",
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": dict(spec.parameters),
                }
                if not spec.core:
                    row["defer_loading"] = True
                if spec.strict:
                    row["strict"] = True
                funcs.append(row)
            out.append(
                {
                    "type": "namespace",
                    "name": ns.name,
                    "description": ns.description,
                    "tools": funcs,
                }
            )
        out.append({"type": "tool_search"})
        return out

    def realtime_tools(
        self,
        surface: str = "realtime",
        *,
        description_resolver: Callable[[ToolSpec], str] | None = None,
    ) -> list[dict[str, Any]]:
        """Stable flattened list for providers without deferred tool loading."""

        out: list[dict[str, Any]] = []
        for spec in self.visible_tools(surface):
            description = (
                description_resolver(spec)
                if description_resolver is not None
                else spec.description
            )
            row: dict[str, Any] = {
                "type": "function",
                "name": spec.name,
                "description": description,
                "parameters": dict(spec.parameters),
            }
            if spec.strict:
                row["strict"] = True
            out.append(row)
        return out

    def _make_catalog_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "namespaces": [
                {"name": item.name, "description": item.description}
                for item in self._namespaces.values()
            ],
            "tools": [
                {
                    "name": item.name,
                    "description": item.description,
                    "namespace": item.namespace,
                    "parameters": item.parameters,
                    "strict": item.strict,
                    "core": item.core,
                    "surfaces": sorted(item.surfaces),
                    "modes": sorted(item.modes),
                    "hosts": sorted(item.hosts),
                }
                for item in self._tools.values()
            ],
        }


class MonotonicToolSession:
    """Append-only progressive disclosure state for textual orchestrators."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        surface: str,
        restored_order: Sequence[str] = (),
    ) -> None:
        self.registry = registry
        self.surface = surface
        self._load_order: list[str] = []
        for namespace in restored_order:
            self.load(namespace)

    @property
    def load_order(self) -> tuple[str, ...]:
        return tuple(self._load_order)

    @property
    def stable_prefix(self) -> str:
        return self.registry.stable_text_prefix(self.surface)

    def load(self, namespace: str) -> str:
        """Return one appendable tool-detail event; duplicates are no-ops."""

        specs = self.registry.tools_in(namespace, surface=self.surface)
        if namespace in self._load_order:
            return ""
        self._load_order.append(namespace)
        body = "\n".join(
            f"- {spec.name}: {spec.description}" for spec in specs
        ) or "(本端没有可用工具)"
        return (
            f"【已加载工具域:{namespace}@{self.registry.catalog_version}】\n"
            + body
        )

    def unload(self, namespace: str) -> None:
        raise ToolRegistryError(
            "loaded toolsets are append-only; start a new logical thread to unload"
        )


def openai_cache_observation(
    usage: Mapping[str, Any],
    *,
    registry: ToolRegistry,
    surface: str,
    loaded_namespaces: Sequence[str] = (),
) -> dict[str, Any]:
    """Normalize OpenAI Responses/Chat usage into a loggable cache record."""

    input_tokens = int(
        usage.get("input_tokens", usage.get("prompt_tokens", 0)) or 0
    )
    details = (
        usage.get("input_tokens_details")
        or usage.get("prompt_tokens_details")
        or {}
    )
    cached_tokens = int(details.get("cached_tokens", 0) or 0)
    cache_write_tokens = int(details.get("cache_write_tokens", 0) or 0)
    return {
        "surface": surface,
        "catalog_version": registry.catalog_version,
        "prompt_cache_key": registry.cache_key(surface),
        "input_tokens": input_tokens,
        "cached_tokens": cached_tokens,
        "cache_write_tokens": cache_write_tokens,
        "cache_read_ratio": (
            round(cached_tokens / input_tokens, 4) if input_tokens else 0.0
        ),
        "loaded_namespaces": list(loaded_namespaces),
    }
