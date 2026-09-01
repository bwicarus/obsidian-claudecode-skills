#!/usr/bin/env python3
"""Read-only handoff audit for the WebExtension/PWA shared reader work.

Default: validate documentation, manifests, syntax and generated-vendor parity.
--full: additionally run the current-product Chromium contract tests (via Xvfb when needed).
--production: additionally compare the current source/package metadata with Pi deploy targets.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
HANDOFF = ROOT / "references" / "reader-extension-handoff.md"
REQUIRED_DOCS = (
    HANDOFF,
    ROOT / "references" / "reader-runtime-architecture.md",
    ROOT / "references" / "reader-extension-ownership.md",
    ROOT / "references" / "reader-runtime-conflicts.md",
    ROOT / "references" / "reader-ui-conflicts.md",
    HERE / "README.md",
)
TESTS = (
    "test_smoke.py",
    "test_pwa_handoff.py",
    "test_pwa_native_contract.py",
    "test_pwa_takeover.py",
    "test_web_ink_input.py",
    "test_web_ink_assistant.py",
    "test_card_drag.py",
    "test_sidebar_layout.py",
    "test_web_notes_local.py",
    # 网页字符层与卡片锚定的真机回归。⚠ 必须在这张单子里 ——
    # 那三个文件当初只用最小 DOM 桩测过就发了出去，桩全绿而真浏览器里有
    # 五个 high（三击选段整类失效、对 AI 谎称页面为空、描边一个像素都不画…）。
    # 没人跑的测试等于不存在。
    "test_web_bind_local.py",
    "test_web_vocab_scheduler.py",
)
RUNTIME_TEST_GLOB = "tests/reader_contract/*.test.mjs"
READER_NETWORK_AUDIT = ROOT / "scripts" / "audit_reader_network.py"
REQUIRED_RUNTIME_TESTS = {
    "account-context.contract.test.mjs",
    "book-extension-handoff.contract.test.mjs",
    "data-store.contract.test.mjs",
    "document-note-repository.contract.test.mjs",
    "document-notes-facade.contract.test.mjs",
    "document-host.contract.test.mjs",
    "direct-sync-host.contract.test.mjs",
    "direct-sync-leader.contract.test.mjs",
    "direct-sync-protocol.contract.test.mjs",
    "direct-sync-signal-transport.contract.test.mjs",
    "extension-account-storage.contract.test.mjs",
    "extension-popup.contract.test.mjs",
    "extension-provider.contract.test.mjs",
    "extension-settings-sync.contract.test.mjs",
    "pwa-marker.contract.test.mjs",
    "pwa-runtime-lifecycle.contract.test.mjs",
    "pwa-service-bridge.contract.test.mjs",
    "reader-service-worker.contract.test.mjs",
    "reader-ui.contract.test.mjs",
    "runtime-modes.contract.test.mjs",
    "server-sync-transport.contract.test.mjs",
    "sync-owner-lease.contract.test.mjs",
    "sync-coordinator.contract.test.mjs",
    "sync-conflict-control.contract.test.mjs",
    "sync-gateway.contract.test.mjs",
    "sync-runtime.contract.test.mjs",
    "stickynote-repository-io.contract.test.mjs",
    "text-hit-testing.contract.test.mjs",
    "web-sandbox.contract.test.mjs",
}
ACTIVE_PWA_ORIGIN = "https://bwicarus.taile44d0c.ts.net"
TRUSTED_PWA_MATCHES = {
    f"{ACTIVE_PWA_ORIGIN}/pdf/view",
    f"{ACTIVE_PWA_ORIGIN}/pdf/view?*",
    f"{ACTIVE_PWA_ORIGIN}/pdf/epub/view",
    f"{ACTIVE_PWA_ORIGIN}/pdf/epub/view?*",
    f"{ACTIVE_PWA_ORIGIN}/pdf/html/view",
    f"{ACTIVE_PWA_ORIGIN}/pdf/html/view?*",
    f"{ACTIVE_PWA_ORIGIN}/pdf/fav/open",
    f"{ACTIVE_PWA_ORIGIN}/pdf/fav/open?*",
}
RUNTIME_FILES = (
    "account-context.js",
    "interaction-policy.js",
    "document-host.js",
    "data-store.js",
    "indexeddb-store.js",
    "data-registry.js",
    "sync-owner-lease.js",
    "sync-gateway.js",
    "server-sync-transport.js",
    "direct-sync-protocol.js",
    "direct-sync-signal-transport.js",
    "sync-coordinator.js",
    "sync-runtime.js",
    "sync-conflict-control.js",
    "direct-sync-host.js",
    "direct-sync-leader.js",
    "vocabulary-state.js",
    "storage-router.js",
    "runtime-selector.js",
    "legacy-rc-bridge.js",
    "pwa-service-bridge.js",
    "pwa-runtime.js",
)
EXTENSION_ACCOUNT_STORAGE_SOURCE = (
    ROOT / "_server_deploy" / "static" / "reader-runtime" /
    "extension-account-storage.js"
)
EXTENSION_ACCOUNT_STORAGE_VENDOR = (
    HERE / "vendor" / "reader-runtime-extension-account-storage.js"
)


class Audit:
    def __init__(self) -> None:
        self.errors: list[str] = []
        self.warnings: list[str] = []

    def ok(self, message: str) -> None:
        print(f"✓ {message}")

    def error(self, message: str) -> None:
        self.errors.append(message)
        print(f"✗ {message}")

    def warn(self, message: str) -> None:
        self.warnings.append(message)
        print(f"! {message}")


def run(cmd: list[str], *, cwd: Path = ROOT, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=capture, check=False)


def load_build_module():
    spec = importlib.util.spec_from_file_location("bw_webext_build", HERE / "build.py")
    if not spec or not spec.loader:
        raise RuntimeError("cannot load build.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_safari_package_module():
    spec = importlib.util.spec_from_file_location(
        "bw_webext_package_safari",
        HERE / "package_safari.py",
    )
    if not spec or not spec.loader:
        raise RuntimeError("cannot load package_safari.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_release_preflight_module():
    spec = importlib.util.spec_from_file_location(
        "bw_webext_release_preflight",
        HERE / "release_preflight.py",
    )
    if not spec or not spec.loader:
        raise RuntimeError("cannot load release_preflight.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_reader_deploy_manifest_module():
    path = ROOT / "scripts" / "reader_deploy_manifest.py"
    spec = importlib.util.spec_from_file_location(
        "bw_reader_deploy_manifest",
        path,
    )
    if not spec or not spec.loader:
        raise RuntimeError("cannot load reader_deploy_manifest.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def string_sequence_constants(path: Path) -> dict[str, set[str]]:
    """Resolve module-level string tuples/lists and their simple concatenations."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    nodes: dict[str, ast.AST] = {}
    for statement in tree.body:
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
            value = statement.value
            for target in targets:
                if isinstance(target, ast.Name) and value is not None:
                    nodes[target.id] = value

    resolved: dict[str, set[str]] = {}

    def resolve_node(node: ast.AST, stack: set[str]) -> set[str]:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return {node.value}
        if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
            values: set[str] = set()
            for item in node.elts:
                values.update(resolve_node(item, stack))
            return values
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return resolve_node(node.left, stack) | resolve_node(node.right, stack)
        if isinstance(node, ast.Name):
            if node.id in resolved:
                return resolved[node.id]
            if node.id in stack or node.id not in nodes:
                return set()
            value = resolve_node(nodes[node.id], stack | {node.id})
            resolved[node.id] = value
            return value
        return set()

    for name, node in nodes.items():
        resolved.setdefault(name, resolve_node(node, {name}))
    return resolved


def audit_docs(audit: Audit, version: str) -> None:
    missing = [str(path.relative_to(ROOT)) for path in REQUIRED_DOCS if not path.is_file()]
    if missing:
        audit.error("缺少交接文档: " + ", ".join(missing))
        return
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    if "references/reader-extension-handoff.md" not in claude:
        audit.error("CLAUDE.md 没有指向扩展交接入口")
    else:
        audit.ok("CLAUDE.md 已连接单一交接入口")
    text = HANDOFF.read_text(encoding="utf-8")
    if version not in text:
        audit.error(f"交接文档未登记 manifest 当前版本 {version}")
    else:
        audit.ok(f"交接文档版本与 manifest 一致: {version}")
    for token in (
        "conflict",
        "pending",
        "account-context/1",
        "book-host/1",
        "bw-reader-pwa/1",
        "document-host/1",
        "data-store/1",
        "sync-gateway/2",
        "test_pwa_native_contract.py",
        "HELLO",
        "TAKEOVER",
        "GOODBYE",
        "tests.test_pwa_web_reader_retirement",
        "book-extension-handoff.contract.test.mjs",
        "reader-service-worker.contract.test.mjs",
    ):
        if token not in text:
            audit.error(f"交接文档缺少门禁标记: {token}")


def audit_manifest(audit: Audit) -> str:
    try:
        manifest = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
        version = str(manifest["version"])
    except Exception as exc:
        audit.error(f"manifest 无法读取: {exc}")
        return "unknown"
    missing: list[str] = []
    for block in manifest.get("content_scripts", []):
        for rel in block.get("js", []):
            if not (HERE / rel).is_file():
                missing.append(rel)
    for rel in manifest.get("background", {}).get("scripts", []):
        if not (HERE / rel).is_file():
            missing.append(rel)
    for rel in ("background.js", "content.js", "popup.html", "popup.js"):
        if not (HERE / rel).is_file():
            missing.append(rel)
    if missing:
        audit.error("manifest 引用了不存在的文件: " + ", ".join(sorted(set(missing))))
    else:
        audit.ok(f"manifest {version} 的全部运行文件存在")

    policy_errors: list[str] = []
    if manifest.get("manifest_version") != 3:
        policy_errors.append("manifest_version 必须是 3")
    permissions = manifest.get("permissions") or []
    if permissions != [
        "storage",
        "alarms",
        "nativeMessaging",
        "offscreen",
    ]:
        policy_errors.append(
            "permissions 必须且只能是 storage + alarms + "
            "nativeMessaging + offscreen"
        )
    if manifest.get("optional_permissions"):
        policy_errors.append("不得声明 optional_permissions")
    if manifest.get("host_permissions") != [
        f"{ACTIVE_PWA_ORIGIN}/*",
        "https://api.openai.com/*",
    ]:
        policy_errors.append(
            "host_permissions 必须只覆盖当前 PWA 源与 OpenAI Realtime"
        )
    if manifest.get("optional_host_permissions"):
        policy_errors.append("不得声明 optional_host_permissions")

    blocks = manifest.get("content_scripts") or []
    if len(blocks) != 2:
        policy_errors.append("content_scripts 必须是 PWA marker + 全网页 runtime 两块")
    else:
        block, runtime = blocks
        allowed_block_keys = {"matches", "js", "run_at", "all_frames"}
        if set(block) != allowed_block_keys:
            policy_errors.append(
                "provider marker 块字段必须且只能是 matches/js/run_at/all_frames"
            )
        matches = block.get("matches") or []
        if len(matches) != 8 or set(matches) != TRUSTED_PWA_MATCHES:
            policy_errors.append(
                "PWA marker 必须只匹配四个精确书籍路由的无参数与带参数形式"
            )
        if block.get("js") != ["src/pwa-marker.js"]:
            policy_errors.append("正式模式必须只静态加载 src/pwa-marker.js")
        if block.get("run_at") != "document_start":
            policy_errors.append("pwa-marker 必须在 document_start 加载")
        if block.get("all_frames") is not False:
            policy_errors.append("pwa-marker 必须只在顶层 frame 加载")
        if set(runtime) != allowed_block_keys:
            policy_errors.append("全网页 runtime 块字段必须且只能是 matches/js/run_at/all_frames")
        if set(runtime.get("matches") or []) != {"http://*/*", "https://*/*"}:
            policy_errors.append("全网页 runtime 必须覆盖全部 http(s) 页面")
        runtime_js = runtime.get("js") or []
        try:
            release = load_release_preflight_module()
            expected_runtime_js = list(release.expected_runtime_js(HERE))
        except (Exception, SystemExit) as exc:
            expected_runtime_js = []
            policy_errors.append(f"无法生成 runtime 精确清单: {exc}")
        if runtime.get("run_at") != "document_idle":
            policy_errors.append("全网页 runtime 必须在 document_idle 加载")
        if runtime.get("all_frames") is not False:
            policy_errors.append("全网页 runtime 必须只在顶层 frame 加载")
        if expected_runtime_js and runtime_js != expected_runtime_js:
            policy_errors.append(
                "全网页 runtime JS 必须与 build.py/既定适配器清单逐项同序一致"
            )
    if manifest.get("background") != {
        "service_worker": "background.js",
        "scripts": ["background.js"],
        "persistent": False,
    }:
        policy_errors.append("默认后台必须是 background.js 的 MV3/兼容双声明")

    if policy_errors:
        audit.error("扩展/PWA manifest 门禁失败: " + "; ".join(policy_errors))
    else:
        audit.ok(
            "manifest 锁定：普通 http(s) 全功能注入；PWA marker "
            "仅四个精确书籍路由（兼容查询参数）"
        )
    return version


def audit_release_source_layout(audit: Audit) -> None:
    try:
        release = load_release_preflight_module()
        release.validate_source_layout(HERE)
        launcher_version = release.source_launcher_version(HERE)
    except (Exception, SystemExit) as exc:
        audit.error(f"Windows 发布源码白名单失败: {exc}")
        return
    audit.ok(
        "Windows 发布源码为精确白名单；src/vendor 仅 JS、icons 仅 PNG、"
        f"launcher v{launcher_version} 与真机清单均无符号链接或额外文件"
    )


def audit_safari_manifest(audit: Audit) -> None:
    try:
        package_safari = load_safari_package_module()
        manifest = package_safari.safari_manifest()
        package_safari.validate(manifest)
        compat_manifest = package_safari.safari_manifest(compat=True)
        package_safari.validate(compat_manifest, compat=True)
        compat_scripts = compat_manifest.get("background", {}).get("scripts") or []
        required_prefix = [
            "vendor/reader-runtime-account-context.js",
            "vendor/reader-runtime-extension-account-storage.js",
            "vendor/reader-runtime-data-store.js",
            "vendor/reader-runtime-indexeddb-store.js",
            "vendor/reader-runtime-data-registry.js",
            "vendor/reader-runtime-sync-owner-lease.js",
            "vendor/reader-runtime-sync-gateway.js",
            "vendor/reader-runtime-server-sync-transport.js",
            "vendor/reader-runtime-direct-sync-protocol.js",
            "vendor/reader-runtime-sync-coordinator.js",
            "vendor/reader-runtime-sync-runtime.js",
            "vendor/reader-runtime-sync-conflict-control.js",
            "vendor/reader-runtime-document-note-repository.js",
            "vendor/reader-runtime-interaction-policy.js",
            "vendor/reader-runtime-vocabulary-state.js",
        ]
        if compat_scripts[:len(required_prefix)] != required_prefix:
            raise RuntimeError(
                "Safari compat 后台依赖顺序必须是 "
                "account-context → extension-account-storage → data-store → "
                "indexeddb-store → data-registry → sync-owner-lease → sync-gateway → "
                "server-sync-transport → direct-sync-protocol → "
                "sync-coordinator → sync-runtime → sync-conflict-control → "
                "document-note-repository → "
                "interaction-policy → vocabulary-state"
            )
        mutations: list[tuple[str, dict]] = []
        missing_run_at = json.loads(json.dumps(manifest))
        missing_run_at["content_scripts"][0].pop("run_at", None)
        mutations.append(("缺少 run_at", missing_run_at))
        duplicate_match = json.loads(json.dumps(manifest))
        duplicate_match["content_scripts"][0]["matches"].append(
            duplicate_match["content_scripts"][0]["matches"][0]
        )
        mutations.append(("重复入口", duplicate_match))
        missing_icons = json.loads(json.dumps(manifest))
        missing_icons["icons"] = {}
        mutations.append(("缺少图标", missing_icons))
        accepted: list[str] = []
        for label, mutated in mutations:
            try:
                package_safari.validate(mutated)
            except SystemExit:
                continue
            accepted.append(label)
        if accepted:
            raise RuntimeError("校验器错误接受: " + ", ".join(accepted))
    except (Exception, SystemExit) as exc:
        audit.error(f"Safari 正式清单门禁失败: {exc}")
        return
    audit.ok(
        "Safari 正式清单保持 MV3、普通网页完整 runtime、"
        "四个精确书籍路由 marker 和完整不透明图标集"
    )


def audit_runtime_wiring(audit: Audit) -> None:
    runtime_dir = ROOT / "_server_deploy" / "static" / "reader-runtime"
    missing = [name for name in RUNTIME_FILES if not (runtime_dir / name).is_file()]
    if missing:
        audit.error("缺少统一 runtime 源码: " + ", ".join(missing))
        return
    extension_missing = [
        str(path.relative_to(ROOT))
        for path in (
            EXTENSION_ACCOUNT_STORAGE_SOURCE,
            EXTENSION_ACCOUNT_STORAGE_VENDOR,
        )
        if not path.is_file()
    ]
    if extension_missing:
        audit.error("缺少扩展账户存储唯一源码/生成物: " + ", ".join(extension_missing))
    else:
        background_text = (HERE / "background.js").read_text(encoding="utf-8")
        dependency_order = [
            background_text.find("vendor/reader-runtime-account-context.js"),
            background_text.find("vendor/reader-runtime-extension-account-storage.js"),
            background_text.find("vendor/reader-runtime-data-store.js"),
            background_text.find("vendor/reader-runtime-indexeddb-store.js"),
            background_text.find("vendor/reader-runtime-data-registry.js"),
            background_text.find("vendor/reader-runtime-sync-owner-lease.js"),
            background_text.find("vendor/reader-runtime-sync-gateway.js"),
            background_text.find("vendor/reader-runtime-server-sync-transport.js"),
            background_text.find("vendor/reader-runtime-direct-sync-protocol.js"),
            background_text.find("vendor/reader-runtime-sync-coordinator.js"),
            background_text.find("vendor/reader-runtime-sync-runtime.js"),
            background_text.find("vendor/reader-runtime-sync-conflict-control.js"),
            background_text.find("vendor/reader-runtime-document-note-repository.js"),
            background_text.find("vendor/reader-runtime-interaction-policy.js"),
            background_text.find("vendor/reader-runtime-vocabulary-state.js"),
        ]
        if any(position < 0 for position in dependency_order) or (
            dependency_order != sorted(dependency_order)
        ):
            audit.error(
                "扩展后台依赖顺序必须是 "
                "account-context → extension-account-storage → data-store → "
                "indexeddb-store → data-registry → sync-owner-lease → sync-gateway → "
                "server-sync-transport → direct-sync-protocol → "
                "sync-coordinator → sync-runtime → sync-conflict-control → "
                "document-note-repository → "
                "interaction-policy → vocabulary-state"
            )
        else:
            audit.ok("扩展账户存储唯一源码、vendor 与后台依赖顺序完整")
    templates = (
        ROOT / "_server_deploy" / "templates" / "pdf_reader.html",
        ROOT / "_server_deploy" / "templates" / "epub_html_reader.html",
        ROOT / "_server_deploy" / "templates" / "html_reader.html",
    )
    unwired: list[str] = []
    misordered: list[str] = []
    for template in templates:
        text = template.read_text(encoding="utf-8")
        positions: list[int] = []
        for name in RUNTIME_FILES:
            position = text.find(f"/static/reader-runtime/{name}")
            positions.append(position)
            if position < 0:
                unwired.append(f"{template.name}:{name}")
        if all(position >= 0 for position in positions) and positions != sorted(positions):
            misordered.append(template.name)
    if unwired:
        audit.error("PWA 模板未完整加载统一 runtime: " + ", ".join(unwired))
    elif misordered:
        audit.error("PWA runtime 加载顺序错误: " + ", ".join(misordered))
    else:
        audit.ok("PDF/EPUB/HTML 三个正式阅读器入口按统一顺序加载 runtime")

    def template_assets(path: Path, variable: str) -> set[str]:
        text = path.read_text(encoding="utf-8")
        found: set[str] = set()
        pattern = re.compile(
            r"""(?:src|href)=["']/static/([^"']+?)\?v=\{\{\s*"""
            + re.escape(variable)
            + r"""(?:\|[^}]*)?\s*\}\}"""
        )
        for match in pattern.finditer(text):
            found.add(match.group(1))
        return found

    pdf_source = ROOT / "_server_deploy" / "pdf_reader.py"
    html_source = ROOT / "_server_deploy" / "html_reader.py"
    pdf_constants = string_sequence_constants(pdf_source)
    html_constants = string_sequence_constants(html_source)
    cache_contracts = (
        (templates[0], "reader_js_v", pdf_constants.get("_PDF_READER_CACHE_ASSETS", set())),
        (templates[0], "shared_js_v", pdf_constants.get("_PDF_SHARED_CACHE_ASSETS", set())),
        (templates[0], "js_v", html_constants.get("_WEB_ADAPTER_CACHE_ASSETS", set())),
        (templates[1], "reader_js_v", pdf_constants.get("_EPUB_CACHE_ASSETS", set())),
        (templates[2], "reader_js_v", html_constants.get("_HTML_CACHE_ASSETS", set())),
    )
    cache_errors: list[str] = []
    for template, variable, declared in cache_contracts:
        loaded = template_assets(template, variable)
        missing = sorted(loaded - declared)
        stale = sorted(declared - loaded)
        if missing:
            cache_errors.append(
                f"{template.name}:{variable} 缺少 " + ",".join(missing)
            )
        if stale:
            cache_errors.append(
                f"{template.name}:{variable} 多登记 " + ",".join(stale)
            )
    pdf_text = pdf_source.read_text(encoding="utf-8")
    helper_start = pdf_text.find("def _static_asset_version(")
    helper_end = pdf_text.find("\ndef _reader_js_v(", helper_start)
    helper = pdf_text[helper_start:helper_end if helper_end >= 0 else None]
    for token in ("hashlib.sha256()", "found_path", "st_mtime_ns", "st_size"):
        if token not in helper:
            cache_errors.append(f"_static_asset_version 缺少可靠指纹字段 {token}")
    if cache_errors:
        audit.error("模板缓存指纹合同失败: " + "; ".join(cache_errors))
    else:
        audit.ok("PDF/EPUB/HTML 模板资源与 path+mtime_ns+size 指纹清单逐项一致")

    web_source = (ROOT / "_server_deploy" / "html_reader.py").read_text(encoding="utf-8")
    web_start = web_source.find("def pdf_web_live(")
    web_end = web_source.find("def pdf_web_portal(", web_start + 1)
    web_section = web_source[web_start:web_end if web_end >= 0 else None]
    required_web_tokens = (
        "_retired_web_redirect_target",
        "redirect(target, code=302)",
        'response.headers["Referrer-Policy"] = "no-referrer"',
    )
    missing_web_tokens = [token for token in required_web_tokens if token not in web_section]
    if missing_web_tokens:
        audit.error("/pdf/web/live 退役跳转合同缺失: " + ", ".join(missing_web_tokens))
    elif "render_template" in web_section:
        audit.error("/pdf/web/live 不得再渲染 PWA 网页阅读器模板")
    else:
        audit.ok("/pdf/web/live 只做无凭据 http(s) 原站跳转，不再渲染网页阅读器")

    route_defaults = (
        (templates[0], "pdf"),
        (templates[1], "epub"),
        (templates[2], "html"),
    )
    route_errors: list[str] = []
    for template, default_kind in route_defaults:
        text = template.read_text(encoding="utf-8")
        app_token = (
            '<meta name="bw-reader-app" '
            f'content="{{{{ reader_app|default(\'{default_kind}\', true) }}}}">'
        )
        route_token = (
            '<meta name="bw-reader-route" '
            f'content="{{{{ reader_route|default(\'{default_kind}\', true) }}}}">'
        )
        if app_token not in text:
            route_errors.append(f"{template.name}:bw-reader-app")
        if route_token not in text:
            route_errors.append(f"{template.name}:bw-reader-route")
    favorites_text = (ROOT / "_server_deploy" / "favorites_reader.py").read_text(encoding="utf-8")
    favorite_start = favorites_text.find("def _fav_serve_reader(")
    favorite_end = favorites_text.find("\n\n# 等待页:", favorite_start)
    favorite_section = favorites_text[favorite_start:favorite_end if favorite_end >= 0 else None]
    for token in ('"epub_html_reader.html"', 'reader_app="epub"', 'reader_route="favorite"'):
        if token not in favorite_section:
            route_errors.append(f"favorites_reader.py:{token}")
    if route_errors:
        audit.error("阅读器路由类型合同失败: " + ", ".join(route_errors))
    else:
        audit.ok("PDF/EPUB/HTML 模板支持路由类型；收藏入口保留 EPUB 适配器并标记 favorite")


def audit_vendor(audit: Audit) -> None:
    try:
        build = load_build_module()
    except Exception as exc:
        audit.error(f"无法加载 build.py: {exc}")
        return
    drift: list[str] = []
    unavailable: list[str] = []
    preserved_pi_assets: list[str] = []

    def is_tracked_unchanged(path: Path) -> bool:
        try:
            relative = path.relative_to(ROOT).as_posix()
        except ValueError:
            return False
        tracked = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", relative],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        unchanged = subprocess.run(
            ["git", "diff", "--quiet", "HEAD", "--", relative],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        return (
            tracked.returncode == 0
            and unchanged.returncode == 0
            and path.is_file()
            and not path.is_symlink()
        )

    for rel, name in build.LIBS.items():
        pi_absolute = rel.startswith("/")
        source = (
            Path(rel)
            if Path(rel).is_absolute() or pi_absolute
            else build.SRC / rel
        )
        target = build.DST / name
        if not source.is_file():
            if pi_absolute and is_tracked_unchanged(target):
                preserved_pi_assets.append(name)
                continue
            unavailable.append(str(source))
            continue
        if not target.is_file() or target.read_bytes() != source.read_bytes():
            drift.append(name)
    for name in build.FILES:
        source = build.SRC / name
        target = build.DST / name
        if not source.is_file():
            unavailable.append(str(source))
            continue
        text = source.read_text(encoding="utf-8")
        header = f"/* AUTO-GENERATED by build.py — 源=_server_deploy/static/pdf/{name} 逐字包装,勿手改 */\n"
        expected = header + build.WRAP_TOP + text + build.WRAP_BOT
        if not target.is_file() or target.read_text(encoding="utf-8") != expected:
            drift.append(name)
    for rel, name in build.GUARDED_LIBS.items():
        source = build.SRC / rel
        target = build.DST / name
        if not source.is_file():
            unavailable.append(str(source))
            continue
        text = source.read_text(encoding="utf-8")
        header = f"/* AUTO-GENERATED by build.py — 源=_server_deploy/static/pdf/{name} 逐字包装,勿手改 */\n"
        expected = header + build.LIB_WRAP_TOP + text + build.LIB_WRAP_BOT
        if not target.is_file() or target.read_text(encoding="utf-8") != expected:
            drift.append(name)
    if unavailable:
        audit.error("缺少 vendor 唯一源码，禁止跳过比较: " + ", ".join(unavailable))
    if drift:
        audit.error("vendor 与唯一源码漂移；运行 build.py: " + ", ".join(drift))
    if not unavailable and not drift:
        suffix = (
            "；Windows 保留且确认未改的 Pi-only vendor: "
            + ", ".join(preserved_pi_assets)
            if preserved_pi_assets
            else ""
        )
        audit.ok("vendor 与共享源码逐字一致" + suffix)


def audit_reader_concat(audit: Audit) -> None:
    """reader.js 必须逐字等于 reader.src/*.js 的顺序拼接。

    2026-09-01 实锤：上一轮把六块修复直接改进拼合产物、没回写源，
    漂移 257 行躺了一整天而默认档全绿 —— 这条一致性当时只在
    --production 档（比 Pi 部署副本时）才查。产物是 build 脚本的输出，
    谁 build 谁覆盖；改产物不回写源 = 下一次 build 静默蒸发。
    放进默认档，开工前一跑就现形。
    """
    pdf_dir = ROOT / "_server_deploy" / "static" / "pdf"
    parts = sorted((pdf_dir / "reader.src").glob("*.js"))
    combined = b"".join(part.read_bytes() for part in parts)
    if not combined:
        audit.error("reader.src 为空 —— 拼合源目录丢失?")
        return
    if combined != (pdf_dir / "reader.js").read_bytes():
        audit.error(
            "reader.js 与 reader.src/*.js 拼合结果漂移(有人直接改了产物?)"
            " —— 把改动回写 reader.src 后跑 scripts/build_pdf_reader_js.sh 重建"
        )
    else:
        audit.ok(f"reader.js == cat(reader.src/*.js) 拼合一致 ({len(parts)} 源文件)")


def audit_syntax(audit: Audit) -> None:
    node = shutil.which("node")
    if not node:
        audit.error("找不到 node，无法做 JavaScript 语法检查")
        return
    files = [HERE / "background.js", HERE / "content.js", HERE / "popup.js"]
    files.extend(sorted((HERE / "src").glob("*.js")))
    files.append(ROOT / "_server_deploy" / "static" / "pdf" / "web-immersive.js")
    files.extend(sorted((ROOT / "_server_deploy" / "static" / "reader-runtime").glob("*.js")))
    failed: list[str] = []
    for path in files:
        result = run([node, "--check", str(path)], capture=True)
        if result.returncode:
            failed.append(str(path.relative_to(ROOT)))
            if result.stderr:
                print(result.stderr.rstrip())
    if failed:
        audit.error("JavaScript 语法失败: " + ", ".join(failed))
    else:
        audit.ok(f"JavaScript 语法通过 ({len(files)} files)")
    py_files = [
        HERE / "build.py",
        HERE / "publish_test_channel.py",
        HERE / "release_preflight.py",
        HERE / "package_safari.py",
        HERE / "handoff_check.py",
        HERE / "test_release_pipeline.py",
        ROOT / "_server_deploy" / "app.py",
        ROOT / "_server_deploy" / "reader_sw_auth.py",
        ROOT / "_server_deploy" / "reader_sync_relay.py",
        ROOT / "_server_deploy" / "pdf_reader.py",
        ROOT / "_server_deploy" / "html_reader.py",
        ROOT / "_server_deploy" / "favorites_reader.py",
        ROOT / "_server_deploy" / "web_proxy_cap.py",
        ROOT / "_server_deploy" / "web_cookie_store.py",
        ROOT / "_server_deploy" / "web_cache_store.py",
        ROOT / "_server_deploy" / "rbi_access.py",
        ROOT / "_server_deploy" / "rbi_server.py",
        ROOT / "scripts" / "rbi_render.py",
        ROOT / "scripts" / "build_search_index.py",
        ROOT / "scripts" / "attention_profile.py",
        ROOT / "scripts" / "kg" / "concept_node_service.py",
        ROOT / "scripts" / "kg" / "gen_page_brief.py",
        ROOT / "scripts" / "reader_deploy_manifest.py",
        READER_NETWORK_AUDIT,
    ]
    py_failed: list[str] = []
    for path in py_files:
        if not path.is_file():
            py_failed.append(f"{path.relative_to(ROOT)}: 文件不存在")
            continue
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except Exception as exc:
            py_failed.append(f"{path.relative_to(ROOT)}: {exc}")
    if py_failed:
        audit.error("Python 工具语法失败: " + "; ".join(py_failed))
    else:
        audit.ok("交接/构建/发布 Python 工具语法通过（不写 __pycache__）")


def run_runtime_contract_tests(audit: Audit) -> None:
    node = shutil.which("node")
    if not node:
        audit.error("找不到 node，无法运行 reader runtime 契约测试")
        return
    tests = sorted((ROOT / "tests" / "reader_contract").glob("*.test.mjs"))
    if not tests:
        audit.error(f"未找到 runtime 契约测试: {RUNTIME_TEST_GLOB}")
        return
    present = {path.name for path in tests}
    missing = sorted(REQUIRED_RUNTIME_TESTS - present)
    if missing:
        audit.error("缺少必跑 runtime 契约测试: " + ", ".join(missing))
        return
    result = run([node, "--test", "--test-reporter=spec", *map(str, tests)], capture=True)
    if result.returncode:
        if result.stdout:
            print(result.stdout.rstrip())
        if result.stderr:
            print(result.stderr.rstrip())
        audit.error("DocumentHost/DataStore/SyncGateway 契约测试失败")
    else:
        audit.ok(f"统一 runtime 契约测试通过 ({len(tests)} files)")


def run_reader_network_audit(audit: Audit) -> None:
    if not READER_NETWORK_AUDIT.is_file():
        audit.error("缺少读者交互网络依赖审计脚本")
        return
    result = run(
        [sys.executable, str(READER_NETWORK_AUDIT), "--check"],
        capture=True,
    )
    if result.returncode:
        if result.stdout:
            print(result.stdout.rstrip())
        if result.stderr:
            print(result.stderr.rstrip())
        audit.error("新增或扩大的交互网络依赖债务")
        return
    summary = (result.stdout or "").strip().splitlines()
    audit.ok(
        "交互网络依赖门禁通过"
        + (f"：{summary[0]}" if summary else "")
    )


def run_indexeddb_browser_contract(audit: Audit) -> None:
    runner = ROOT / "tests" / "reader_contract" / "indexeddb_store_browser.py"
    if not runner.is_file():
        audit.error("缺少 IndexedDB 浏览器契约测试")
        return
    result = run([sys.executable, str(runner)], capture=True)
    if result.returncode:
        if result.stdout:
            print(result.stdout.rstrip())
        if result.stderr:
            print(result.stderr.rstrip())
        audit.error("真实 Chromium IndexedDB 契约测试失败")
    else:
        audit.ok(result.stdout.strip() or "真实 Chromium IndexedDB 契约测试通过")


def run_text_hit_browser_contract(audit: Audit) -> None:
    runner = ROOT / "tests" / "reader_contract" / "text_hit_testing_browser.py"
    if not runner.is_file():
        audit.error("缺少真实文字 Range 命中浏览器契约测试")
        return
    result = run([sys.executable, str(runner)], capture=True)
    if result.returncode:
        if result.stdout:
            print(result.stdout.rstrip())
        if result.stderr:
            print(result.stderr.rstrip())
        audit.error("真实 Chromium 文字 Range 命中契约测试失败")
    else:
        audit.ok(result.stdout.strip() or "真实 Chromium 文字 Range 命中契约测试通过")


def run_identity_regression_tests(audit: Audit) -> None:
    result = run(
        [
            sys.executable,
            "-m",
            "unittest",
            "-v",
            "tests.test_voice_card_identity",
            "tests.test_reader_provider_identity",
            "tests.test_reader_private_storage",
            "tests.test_pwa_web_reader_retirement",
        ],
        capture=True,
    )
    if result.returncode:
        if result.stdout:
            print(result.stdout.rstrip())
        if result.stderr:
            print(result.stderr.rstrip())
        audit.error("卡片编号 / Provider 身份 / 私有存储 / PWA 网页阅读器退役回归测试失败")
    else:
        audit.ok("卡片 cid/gid、Provider 身份、网页私有存储与网页阅读器退役边界回归测试通过")


def run_stage7_regression_tests(audit: Audit) -> None:
    result = run(
        [
            sys.executable,
            "-m",
            "unittest",
            "-v",
            "tests.test_reader_sync_relay",
            "tests.test_concept_node_service",
            "tests.test_page_brief_kg_integration",
            "tests.test_reader_deploy_manifest",
        ],
        capture=True,
    )
    if result.returncode:
        if result.stdout:
            print(result.stdout.rstrip())
        if result.stderr:
            print(result.stderr.rstrip())
        audit.error("自动 KG / 同步 relay / 部署清单回归测试失败")
    else:
        audit.ok("自动 KG、同步 relay 与部署清单回归测试通过")


def run_release_pipeline_tests(audit: Audit) -> None:
    runner = HERE / "test_release_pipeline.py"
    if not runner.is_file():
        audit.error("缺少 Windows/Safari 发布管线回归测试")
        return
    result = run([sys.executable, str(runner)], capture=True)
    if result.returncode:
        if result.stdout:
            print(result.stdout.rstrip())
        if result.stderr:
            print(result.stderr.rstrip())
        audit.error("Windows/Safari 发布管线回归测试失败")
    else:
        # 该回归会故意构造一个坏 manifest 并确认门禁拒绝它；不要把这个
        # 预期负例的“✗”原样混进总审计成功输出，以免被误判为真实失败。
        audit.ok("Windows/Safari 发布管线回归测试通过（含预期拒绝负例）")


def audit_worktree(audit: Audit) -> None:
    result = run(["git", "status", "--porcelain"], capture=True)
    if result.returncode:
        audit.warn("无法读取 git status")
        return
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    modified = sum(1 for line in lines if not line.startswith("??"))
    untracked = sum(1 for line in lines if line.startswith("??"))
    if lines:
        audit.warn(f"工作区非干净：{modified} 个已跟踪变更，{untracked} 个未跟踪项；禁止批量 reset/clean")
    else:
        audit.ok("工作区干净")


def audit_live_runtime_parity(audit: Audit) -> bool:
    """Do not mistake a local-new-extension/live-old-PWA mix for a code failure."""

    items = production_manifest_items()
    # 部署面整个不在场（Windows 开发机：target 是 Pi 的绝对路径）≠ 混测
    # 风险 —— 没有"旧 PWA"可混。2026-09-01 实锤：166 项「部署缺失」把
    # 浏览器测试整步拦死，而 PWA 已 410、Pi 已退纯备份（产品边界
    # 2026-08-18/30）。这个门禁只在部署面真实存在时把关；部分存在才是
    # 真漂移，仍走下面的逐项比对。
    if items and not any(target.is_file() for _, _, target in items):
        audit.ok("部署面不在本机（无旧 PWA 可混测），浏览器门禁直接放行")
        return True
    drift: list[str] = []
    for entry, source, target in items:
        if not source.is_file():
            drift.append(f"源码缺失 {source.relative_to(ROOT)}")
        elif not target.is_file():
            drift.append(f"部署缺失 {target}")
        elif not production_copy_matches(source, target, entry=entry):
            drift.append(str(target))
    if not drift:
        audit.ok("真实浏览器目标与当前 PWA/服务器源码一致")
        return True
    preview = "、".join(drift[:8])
    remainder = len(drift) - min(len(drift), 8)
    if remainder:
        preview += f" 等，另有 {remainder} 项"
    audit.error(
        "真实浏览器门禁未运行：本地新扩展不能与尚未部署的旧 PWA 混测。"
        f"不一致目标：{preview}"
    )
    return False


def run_browser_tests(audit: Audit) -> None:
    if not audit_live_runtime_parity(audit):
        return
    prefix: list[str] = []
    # Windows 桌面会话自带显示，DISPLAY/xvfb 是 X11 世界观 —— 不豁免的话
    # 这一步在主力开发机上永远 error（2026-09-01 实锤）。
    if sys.platform != "win32" and not os.environ.get("DISPLAY"):
        xvfb = shutil.which("xvfb-run")
        if not xvfb:
            audit.error("无 DISPLAY 且找不到 xvfb-run，无法运行 headed Chromium 回归")
            return
        prefix = [xvfb, "-a"]
    for name in TESTS:
        print(f"\n== {name} ==")
        result = run([*prefix, sys.executable, str(HERE / name)], cwd=HERE)
        if result.returncode:
            audit.error(f"浏览器回归失败: {name}")
            return
    audit.ok("普通网页扩展 + 书籍 PWA 接管/无扩展 fallback 浏览器合同全部通过")


def production_manifest_items() -> list[tuple[object, Path, Path]]:
    manifest = load_reader_deploy_manifest_module()
    return [
        (
            entry,
            entry.source_path(ROOT),
            entry.target_path(),
        )
        for entry in manifest.manifest_entries()
    ]


def production_source_pairs() -> list[tuple[Path, Path]]:
    """Compatibility view; the manifest entries remain the source of truth."""

    return [
        (source, target)
        for _entry, source, target in production_manifest_items()
    ]


def production_copy_matches(
    source: Path,
    target: Path,
    *,
    entry: object | None = None,
) -> bool:
    if entry is None:
        manifest = load_reader_deploy_manifest_module()
        entry = next(
            (
                candidate
                for candidate in manifest.manifest_entries()
                if candidate.target_path() == target
            ),
            None,
        )
    if entry is None:
        return source.read_bytes() == target.read_bytes()
    source_bytes = source.read_bytes()
    if (
        getattr(entry, "policy", None) == "reader_git_stamp"
        and source.name == "reader.js"
    ):
        parts = sorted((source.parent / "reader.src").glob("*.js"))
        if not parts:
            return False
        source_bytes = b"".join(part.read_bytes() for part in parts)
    return entry.deployed_content_matches(
        source_bytes,
        target.read_bytes(),
    )


def audit_production(audit: Audit, version: str) -> None:
    for entry, source, target in production_manifest_items():
        if not source.is_file():
            audit.error(f"生产清单中的源码不存在: {source.relative_to(ROOT)}")
            continue
        if not target.is_file():
            audit.error(f"部署目标不存在: {target}")
        elif not production_copy_matches(source, target, entry=entry):
            audit.error(f"部署副本与源码不同: {target}")
        else:
            audit.ok(f"部署副本一致: {target}")
    try:
        release = load_release_preflight_module()
        channel_path = release.DEFAULT_DEPLOYED_CHANNEL
        channel = release.read_json(channel_path)
        deployed_version = str(channel.get("version", ""))
        if deployed_version != version:
            raise RuntimeError(
                f"已部署 channel 版本 {deployed_version} != 当前源码 {version}"
            )
        launcher_version = channel.get("launcherVersion")
        if type(launcher_version) is not int:
            raise RuntimeError("已部署 channel launcherVersion 不是整数")
        deploy_root = channel_path.parent
        package_path = deploy_root / release.package_name(version)
        launcher_script = deploy_root / release.launcher_script_name(
            launcher_version
        )
        launcher_archive = deploy_root / release.launcher_archive_name(
            launcher_version
        )
        release.audit_artifact(
            package_path=package_path,
            channel_path=channel_path,
            launcher_script_path=launcher_script,
            launcher_archive_path=launcher_archive,
            version=version,
            source_root=HERE,
        )
    except (Exception, SystemExit) as exc:
        audit.error(f"实际部署的 Windows 测试 channel/ZIP/launcher 核验失败: {exc}")
        return
    audit.ok(
        f"实际部署 channel、{package_path.name}、版本化 launcher 脚本/ZIP "
        "与当前源码逐字一致"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="run all current-product Chromium contract tests")
    parser.add_argument("--production", action="store_true", help="also compare Pi deployment and test channel artifacts")
    args = parser.parse_args()

    audit = Audit()
    version = audit_manifest(audit)
    audit_release_source_layout(audit)
    audit_safari_manifest(audit)
    audit_runtime_wiring(audit)
    audit_docs(audit, version)
    audit_vendor(audit)
    audit_reader_concat(audit)
    audit_syntax(audit)
    run_runtime_contract_tests(audit)
    run_reader_network_audit(audit)
    run_indexeddb_browser_contract(audit)
    run_text_hit_browser_contract(audit)
    run_identity_regression_tests(audit)
    run_stage7_regression_tests(audit)
    run_release_pipeline_tests(audit)
    audit_worktree(audit)
    if args.full:
        run_browser_tests(audit)
    if args.production:
        audit_production(audit, version)

    print("\n=== handoff result ===")
    print(f"version={version} errors={len(audit.errors)} warnings={len(audit.warnings)}")
    if audit.errors:
        print("BLOCKED: 先修复以上错误，再继续扩展/PWA 主线。")
        return 1
    print("READY: 新会话可按 reader-extension-handoff.md 继续。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
