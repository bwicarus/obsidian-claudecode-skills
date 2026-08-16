#!/usr/bin/env python3
"""Single, validated production manifest for the PWA reader deployment.

Every deployable file is expressed as one source plus a production target
(`target_group` + `target_rel`).  Both the deploy script and the read-only
handoff audit consume these exact entries.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]

TARGET_ROOTS = {
    "webapp": Path("/home/bwicarus/webapp"),
    "static": Path("/var/www/html/static"),
    # Immutable releases live beside this symlink.  The deploy transaction
    # stages all ``kg_runtime`` rows into one release directory and switches
    # ``current`` atomically only after its own runtime manifest is durable.
    "kg_runtime": Path("/home/bwicarus/reader-runtime/kg/current"),
    "systemd": Path("/etc/systemd/system"),
}
POLICY_EXACT = "exact"
POLICY_READER_GIT_STAMP = "reader_git_stamp"
POLICIES = {POLICY_EXACT, POLICY_READER_GIT_STAMP}

_READER_GIT_VALUE = re.compile(
    rb"(?:dev|[0-9a-f]{7,40})\+(?:clean|dirty)"
    rb"\xc2\xb7"
    rb"(?:0[1-9]|1[0-2])(?:0[1-9]|[12][0-9]|3[01])"
    rb"-(?:[01][0-9]|2[0-3])[0-5][0-9]"
)


@dataclass(frozen=True)
class DeployEntry:
    source_rel: str
    target_group: str
    target_rel: str
    policy: str = POLICY_EXACT

    def source_path(self, root: Path = ROOT) -> Path:
        return root / self.source_rel

    def target_path(
        self,
        target_roots: dict[str, Path] = TARGET_ROOTS,
    ) -> Path:
        return target_roots[self.target_group] / self.target_rel

    def deployed_content_matches(
        self,
        source_bytes: bytes,
        target_bytes: bytes,
    ) -> bool:
        return deployed_content_matches(self, source_bytes, target_bytes)


# Reader-owned Python modules that are independently edited/deployed.  Shared
# server infrastructure such as mcp_server.py is intentionally outside this
# reader release boundary.
WEBAPP_SOURCE_FILES = (
    "app.py",
    # 网页翻译路由通过这里解析共享 web_translate action，并调用无工具文本边界；
    # 必须与 html_reader.py 同批原子部署，不能让路由先引用生产中尚不存在的 helper。
    "assistant.py",
    # assistant/voice now consume one deterministic production catalog and
    # executor gate; deploy the registry and both callers atomically.
    "tool_registry.py",
    "voice.py",
    # voice-rt.service executes the installed webapp copy, never the mutable
    # checkout.  It is therefore part of the same atomic reader release.
    "voice_realtime_relay.py",
    "task_runtime.py",
    # Production routes import these modules directly.  Keeping them out of
    # the manifest would make a successful deploy depend on stale files left
    # behind by an older release.
    "kg_runtime.py",
    "control.py",
    "skilltree.py",
    # 复习卡改进的 app-server 多轮 runner + 签名草稿存储。领域 prompt/
    # 校验本体从下方唯一的 _client/core 源映射为同名生产模块。
    "card_improvement_runtime.py",
    # pdf_reader 的 review-queue POST 路径会懒加载这一候选索引。
    # 生产旧树没有该新文件，必须和路由同批安装，不能让 /login
    # 健康检查掩盖首次调用时的 ModuleNotFoundError。
    "card_candidate_service.py",
    "pdf_reader.py",
    # pdf_reader imports the authenticated Pi book catalog/download/upload
    # service at startup; deploy it atomically with the routes.
    "reader_book_library.py",
    # Manual Pi OCR coordinator and detached page-bounded worker are imported/
    # launched by pdf_reader; deploy them atomically with the authenticated API.
    "reader_book_ocr.py",
    "reader_book_ocr_worker.py",
    "reader_book_user_state.py",
    "html_reader.py",
    "favorites_reader.py",
    "reader_sidecar_store.py",
    # Account-isolated shared Markdown note; app.py imports it at startup.
    "shared_note.py",
    # Opt-in Windows computer-client pairing, one-shot start command, and
    # bounded audio-only WebRTC signalling; app.py imports the route adapter.
    "computer_voice_bridge.py",
    "computer_voice_pairing.py",
    "computer_voice_routes.py",
    "reader_sync_relay.py",
    "reader_events.py",
    "error_reports.py",         # 报错一键上传(pdf_reader 顶层 import 它)
    "book_toc.py",
    "grammar_reader.py",
    "epub_assistant.py",
    "vbook_route_policy.py",
    "reader_card_contract.py",   # 工具卡/part 契约(assistant.py 与桥接都 import 它;漏登记 → 依赖方上线而模块不上线)
    "reader_direct_commands.py",  # 无 AI 直接命令:协议/执行器/失败事件总线
    "reader_direct_wire.py",      # 直接命令接线层(pdf_reader 在前半段 import 它)
    "reader_outgoing_context.py", # 出向上下文:绘图版本 + 焦点状态机
    "kg_export.py",               # KG 只读导出端点(app.py 顶层 import 它)
    "kg_page_index.py",           # 当前页 → KG 节点(reader_outgoing_context import 它)
    "reader_pwa_retirement.py",   # PWA 页面退役拦截(pdf_reader import 它)
    "reader_sw_auth.py",
    "web_proxy_cap.py",
    "web_cookie_store.py",
    "web_cache_store.py",
    # Kept as explicit legacy/recovery runtime assets; this manifest does not
    # authorize deleting their production data or files.
    "rbi_access.py",
    "rbi_server.py",
)

# Every Python file under scripts/kg is deliberately named here.  Do not turn
# this into a directory glob: deployment/review must show the exact executable
# surface, and a newly added module must fail the inventory contract until it
# is consciously included.
KG_RUNTIME_KG_SOURCES = (
    "scripts/kg/__init__.py",
    "scripts/kg/audit_edges.py",
    "scripts/kg/audit_kg.py",
    "scripts/kg/auto_archive.py",
    "scripts/kg/build_grammar_nodes.py",
    "scripts/kg/build_nodes.py",
    "scripts/kg/build_unified_graph.py",
    "scripts/kg/concept_node_service.py",
    "scripts/kg/extract_edges.py",
    "scripts/kg/gen_page_brief.py",
    "scripts/kg/link_and_mastery.py",
    "scripts/kg/link_with_ai.py",
    "scripts/kg/mastery_overrides.py",
    "scripts/kg/merge_nodes.py",
    "scripts/kg/node_evidence.py",
    "scripts/kg/promote_concepts.py",
    "scripts/kg/propose_concept_notes.py",
    "scripts/kg/rescan_rolling.py",
)

# Frozen code dependencies imported by the KG modules.  Data still lives below
# CLAUDE_PROJECT; these rows only freeze executable code into the release.
KG_RUNTIME_DEPENDENCY_SOURCES = (
    "scripts/config.py",
    "scripts/attention_profile.py",
    # Production concept-graph jobs run this release-local, zero-data
    # invariant probe.  They must never import mutable checkout tests.
    "scripts/kg_lifecycle_gate.py",
    "scripts/lib/__init__.py",
    "scripts/lib/book_groups.py",
    "scripts/lib/claude_quota.py",
    "_client/core/ai_backends.py",
    "_server_deploy/reader_sidecar_store.py",
    # auto_archive --apply imports the archive primitive from this module.
    # Its webapp copy is a separate target; the runtime copy prevents that KG
    # command from reaching across to whichever webapp release happens to be
    # active.
    "_server_deploy/skilltree.py",
    "_server_deploy/web_cache_store.py",
)

# Stable systemd entry points.  They are copied into the immutable release and
# call the sibling scripts/kg tree rather than the mutable checkout.
KG_RUNTIME_LAUNCHER_SOURCES = (
    "scripts/quick_sync.py",
    "scripts/concept_graph_daily.py",
)

KG_RUNTIME_SOURCE_FILES = (
    *KG_RUNTIME_KG_SOURCES,
    *KG_RUNTIME_DEPENDENCY_SOURCES,
    *KG_RUNTIME_LAUNCHER_SOURCES,
)

SYSTEMD_UNIT_SOURCES = (
    "references/systemd/voice-rt.service",
    "references/systemd/bwicarus-quick-sync.service",
    "references/systemd/bwicarus-quick-sync.timer",
    "references/systemd/bwicarus-daily.service",
    "references/systemd/bwicarus-daily.timer",
    "references/systemd/concept-graph.service",
    "references/systemd/concept-graph.timer",
)

# 允许跨出 _server_deploy 的少量“唯一源码 → 生产 import 名”映射。
# 网页批翻协议和复习卡改进领域服务都不在 _server_deploy 复制第二份源码；
# 部署时与引用它们的路由/运行时原子安装。
EXTERNAL_DEPLOY_ENTRIES = (
    DeployEntry(
        source_rel="_client/core/card_improvement_service.py",
        target_group="webapp",
        target_rel="card_improvement_service.py",
    ),
    DeployEntry(
        source_rel="scripts/vocab/translate.py",
        target_group="webapp",
        target_rel="web_translate_protocol.py",
    ),
)
_EXTERNAL_DEPLOY_IDENTITIES = frozenset(
    (
        entry.source_rel,
        entry.target_group,
        entry.target_rel,
        entry.policy,
    )
    for entry in EXTERNAL_DEPLOY_ENTRIES
)

KG_RUNTIME_DEPLOY_ENTRIES = tuple(
    DeployEntry(
        source_rel=source_rel,
        target_group="kg_runtime",
        target_rel=source_rel,
    )
    for source_rel in KG_RUNTIME_SOURCE_FILES
)
_KG_RUNTIME_DEPLOY_IDENTITIES = frozenset(
    (
        entry.source_rel,
        entry.target_group,
        entry.target_rel,
        entry.policy,
    )
    for entry in KG_RUNTIME_DEPLOY_ENTRIES
)

SYSTEMD_DEPLOY_ENTRIES = tuple(
    DeployEntry(
        source_rel=source_rel,
        target_group="systemd",
        target_rel=PurePosixPath(source_rel).name,
    )
    for source_rel in SYSTEMD_UNIT_SOURCES
)
_SYSTEMD_DEPLOY_IDENTITIES = frozenset(
    (
        entry.source_rel,
        entry.target_group,
        entry.target_rel,
        entry.policy,
    )
    for entry in SYSTEMD_DEPLOY_ENTRIES
)

_ALLOWED_EXTERNAL_SOURCE_IDENTITIES = frozenset(
    (
        *_EXTERNAL_DEPLOY_IDENTITIES,
        *_KG_RUNTIME_DEPLOY_IDENTITIES,
        *_SYSTEMD_DEPLOY_IDENTITIES,
    )
)

# A caller may validate a prospective manifest (the deploy script does this
# before writing).  These entries are release invariants, not optional rows.
_REQUIRED_EXACT_IDENTITIES = frozenset(
    (
        *_KG_RUNTIME_DEPLOY_IDENTITIES,
        *_SYSTEMD_DEPLOY_IDENTITIES,
        (
            "_server_deploy/kg_runtime.py",
            "webapp",
            "kg_runtime.py",
            POLICY_EXACT,
        ),
        (
            "_server_deploy/control.py",
            "webapp",
            "control.py",
            POLICY_EXACT,
        ),
        (
            "_server_deploy/skilltree.py",
            "webapp",
            "skilltree.py",
            POLICY_EXACT,
        ),
        (
            "_server_deploy/voice_realtime_relay.py",
            "webapp",
            "voice_realtime_relay.py",
            POLICY_EXACT,
        ),
        (
            "_server_deploy/card_candidate_service.py",
            "webapp",
            "card_candidate_service.py",
            POLICY_EXACT,
        ),
        (
            "_server_deploy/reader_book_ocr.py",
            "webapp",
            "reader_book_ocr.py",
            POLICY_EXACT,
        ),
        (
            "_server_deploy/reader_book_ocr_worker.py",
            "webapp",
            "reader_book_ocr_worker.py",
            POLICY_EXACT,
        ),
        (
            "_server_deploy/reader_book_user_state.py",
            "webapp",
            "reader_book_user_state.py",
            POLICY_EXACT,
        ),
        (
            "_server_deploy/shared_note.py",
            "webapp",
            "shared_note.py",
            POLICY_EXACT,
        ),
    )
)

TEMPLATE_FILES = (
    "pdf_index.html",
    "pdf_reader.html",
    "epub_html_reader.html",
    "html_reader.html",
    "rbi_live.html",
    "web_live.html",
    "shared_note.html",
)

STATIC_EXPLICIT = (
    "nav.js",
    "pdf/web-adapter.js",
    "pdf/web-immersive.js",
    "reader-runtime/pwa-cache-identity.js",
)

STATIC_CONSTANTS = {
    "_server_deploy/pdf_reader.py": (
        "_PDF_READER_CACHE_ASSETS",
        "_PDF_SHARED_CACHE_ASSETS",
        "_EPUB_CACHE_ASSETS",
    ),
    "_server_deploy/html_reader.py": (
        "_HTML_CACHE_ASSETS",
        "_WEB_ADAPTER_CACHE_ASSETS",
        "_RBI_LIVE_CACHE_ASSETS",
    ),
}


def _literal_string_sequences(path: Path) -> dict[str, tuple[str, ...]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: dict[str, tuple[str, ...]] = {}
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if not isinstance(value, (ast.Tuple, ast.List, ast.Set)):
            continue
        strings: list[str] = []
        valid = True
        for item in value.elts:
            if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
                valid = False
                break
            strings.append(item.value)
        if not valid:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                found[target.id] = tuple(strings)
    return found


def _safe_relative(value: str, *, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or any(char in value for char in ("\x00", "\t", "\r", "\n", "\\"))
    ):
        raise ValueError(f"unsafe {field}: {value!r}")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in ("", ".", "..") for part in path.parts)
        or path.as_posix() != value
    ):
        raise ValueError(f"unsafe {field}: {value!r}")
    return value


def _static_files() -> tuple[str, ...]:
    assets = {
        _safe_relative(value, field="static target")
        for value in STATIC_EXPLICIT
    }
    for relative_source, names in STATIC_CONSTANTS.items():
        constants = _literal_string_sequences(ROOT / relative_source)
        for name in names:
            if name not in constants:
                raise RuntimeError(
                    f"{relative_source} does not define literal sequence {name}"
                )
            assets.update(
                _safe_relative(value, field="static target")
                for value in constants[name]
            )
    return tuple(sorted(assets))


def _raw_entries() -> tuple[DeployEntry, ...]:
    entries = [
        DeployEntry(
            source_rel=f"_server_deploy/{relative}",
            target_group="webapp",
            target_rel=relative,
        )
        for relative in WEBAPP_SOURCE_FILES
    ]
    entries.extend(EXTERNAL_DEPLOY_ENTRIES)
    entries.extend(KG_RUNTIME_DEPLOY_ENTRIES)
    entries.extend(SYSTEMD_DEPLOY_ENTRIES)
    entries.extend(
        DeployEntry(
            source_rel=f"_server_deploy/templates/{relative}",
            target_group="webapp",
            target_rel=f"templates/{relative}",
        )
        for relative in TEMPLATE_FILES
    )
    entries.extend(
        DeployEntry(
            source_rel=f"_server_deploy/static/{relative}",
            target_group="static",
            target_rel=relative,
            policy=(
                POLICY_READER_GIT_STAMP
                if relative == "pdf/reader.js"
                else POLICY_EXACT
            ),
        )
        for relative in _static_files()
    )
    return tuple(entries)


def validate_entries(
    entries: Iterable[DeployEntry],
    *,
    root: Path = ROOT,
    require_sources: bool = True,
) -> tuple[DeployEntry, ...]:
    checked = tuple(entries)
    seen_targets: set[tuple[str, str]] = set()
    stamped_targets: list[tuple[str, str]] = []
    identities: set[tuple[str, str, str, str]] = set()
    resolved_root = root.resolve()

    for entry in checked:
        source_rel = _safe_relative(entry.source_rel, field="source path")
        target_rel = _safe_relative(entry.target_rel, field="target path")
        external_identity = (
            source_rel,
            entry.target_group,
            target_rel,
            entry.policy,
        )
        if (
            not source_rel.startswith("_server_deploy/")
            and external_identity not in _ALLOWED_EXTERNAL_SOURCE_IDENTITIES
        ):
            raise ValueError(
                f"source is outside _server_deploy: {entry.source_rel!r}"
            )
        if entry.target_group not in TARGET_ROOTS:
            raise ValueError(f"unknown target group: {entry.target_group!r}")
        if entry.policy not in POLICIES:
            raise ValueError(f"unknown deploy policy: {entry.policy!r}")
        if (
            entry.target_group == "kg_runtime"
            and external_identity not in _KG_RUNTIME_DEPLOY_IDENTITIES
        ):
            raise ValueError(
                "unauthorized kg_runtime entry: "
                f"{entry.source_rel!r} -> {entry.target_rel!r}"
            )
        if (
            entry.target_group == "systemd"
            and external_identity not in _SYSTEMD_DEPLOY_IDENTITIES
        ):
            raise ValueError(
                "unauthorized systemd entry: "
                f"{entry.source_rel!r} -> {entry.target_rel!r}"
            )
        identities.add(external_identity)

        target_key = (entry.target_group, target_rel)
        if target_key in seen_targets:
            raise ValueError(
                "duplicate production target: "
                f"{entry.target_group}:{entry.target_rel}"
            )
        seen_targets.add(target_key)

        source = (root / source_rel).resolve()
        try:
            source.relative_to(resolved_root)
        except ValueError as exc:
            raise ValueError(
                f"source escapes repository: {entry.source_rel!r}"
            ) from exc
        if require_sources and not source.is_file():
            raise FileNotFoundError(f"deploy source does not exist: {source}")

        target_root = TARGET_ROOTS[entry.target_group].resolve()
        target = (target_root / target_rel).resolve()
        try:
            target.relative_to(target_root)
        except ValueError as exc:
            raise ValueError(
                f"target escapes {entry.target_group}: {entry.target_rel!r}"
            ) from exc

        if entry.policy == POLICY_READER_GIT_STAMP:
            stamped_targets.append(target_key)
            if target_key != ("static", "pdf/reader.js"):
                raise ValueError(
                    "reader_git_stamp policy is only valid for "
                    "static:pdf/reader.js"
                )

    if stamped_targets != [("static", "pdf/reader.js")]:
        raise ValueError(
            "manifest must contain exactly one stamped static:pdf/reader.js"
        )
    missing = _REQUIRED_EXACT_IDENTITIES - identities
    if missing:
        rendered = ", ".join(
            f"{source}->{group}:{target} ({policy})"
            for source, group, target, policy in sorted(missing)
        )
        raise ValueError(f"manifest is missing required exact entries: {rendered}")
    return checked


def manifest_entries() -> tuple[DeployEntry, ...]:
    return validate_entries(_raw_entries())


def deployed_content_matches(
    entry: DeployEntry,
    source_bytes: bytes,
    target_bytes: bytes,
) -> bool:
    """Compare a production copy according to its declared deploy policy."""

    if entry.policy == POLICY_EXACT:
        return source_bytes == target_bytes
    if entry.policy != POLICY_READER_GIT_STAMP:
        return False
    prefix = source_bytes + b"\n;window.__READER_GIT='"
    if not target_bytes.startswith(prefix):
        return False
    suffix = target_bytes[len(prefix):]
    if not suffix.endswith(b"';\n"):
        return False
    return _READER_GIT_VALUE.fullmatch(suffix[:-3]) is not None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--format",
        choices=("tsv",),
        default="tsv",
        help="machine-readable source/group/target/policy rows",
    )
    parser.parse_args()
    for entry in manifest_entries():
        print(
            entry.source_rel,
            entry.target_group,
            entry.target_rel,
            entry.policy,
            sep="\t",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
