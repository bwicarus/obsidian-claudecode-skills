#!/usr/bin/env python3
"""Build the deterministic, runtime-offline ReaderBundle for the native app.

The app is the product and lifecycle owner.  This bundle only supplies the
existing PDF/EPUB rendering surface to its loopback HTTP server; it deliberately
contains no web-app manifest, service worker, or extension-takeover script.

Third-party archives are fetched only while building, from pinned URLs, and are
accepted only after an exact SHA-256 match.  The built app never downloads code,
PDF.js support data, or MathJax fonts at runtime.
"""
from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.request

from jinja2 import Environment, StrictUndefined, select_autoescape


HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
STATIC = ROOT / "_server_deploy" / "static"
TEMPLATES = ROOT / "_server_deploy" / "templates"
DEFAULT_OUTPUT = HERE / "Generated" / "ReaderBundle"
DEFAULT_CACHE = Path(
    os.environ.get(
        "BW_LOCAL_READER_SOURCE_CACHE",
        Path.home() / ".cache" / "bwreader-local-reader",
    )
)

BUNDLE_CONTRACT = "bw-local-reader-bundle/1"
PDF_SHELL = "shells/pdf.html"
EPUB_SHELL = "shells/epub.html"
CSP_NONCE_PLACEHOLDER = "__BW_LOCAL_CSP_NONCE__"

PDF_PLACEHOLDERS = (
    "__BW_LOCAL_PDF_URL_JSON__",
    "__BW_LOCAL_FILE_REL_JSON__",
    "__BW_LOCAL_FILE_NAME_HTML__",
    "__BW_LOCAL_PDF_SIZE__",
    "__BW_LOCAL_INITIAL_PAGE__",
    "__BW_LOCAL_INITIAL_PAGE_TS__",
    CSP_NONCE_PLACEHOLDER,
)
EPUB_PLACEHOLDERS = (
    "__BW_LOCAL_FILE_REL_JSON__",
    "__BW_LOCAL_FILE_NAME_JSON__",
    "__BW_LOCAL_FILE_NAME_HTML__",
    "__BW_LOCAL_EPUB_SHA_JSON__",
    "__BW_LOCAL_INITIAL_EPUB_POS__",
    "__BW_LOCAL_INITIAL_EPUB_POS_TS__",
    CSP_NONCE_PLACEHOLDER,
)


@dataclass(frozen=True)
class ExternalPackage:
    name: str
    version: str
    url: str
    sha256: str

    @property
    def cache_name(self) -> str:
        return f"{self.name}-{self.version}-{self.sha256[:16]}.tgz"


# Keep these aligned with production.  PDF.js 4.7.76 is the deployed renderer
# API generation; upgrading it independently of reader.src is not safe.
PDFJS = ExternalPackage(
    name="pdfjs-dist",
    version="4.7.76",
    url="https://registry.npmjs.org/pdfjs-dist/-/pdfjs-dist-4.7.76.tgz",
    sha256="5198d6e4a1d3df30ed5005aa82a830ed2ef3c99c1c1d32017e4d9376899ef4bd",
)
MARKED = ExternalPackage(
    name="marked",
    version="9.1.6",
    url="https://registry.npmjs.org/marked/-/marked-9.1.6.tgz",
    sha256="6cee05496e2e90947f90f431fdeb66f3093716f81d3c2b65caccd6bf0542bd2b",
)
MATHJAX = ExternalPackage(
    name="mathjax-full",
    version="3.2.2",
    url="https://registry.npmjs.org/mathjax-full/-/mathjax-full-3.2.2.tgz",
    sha256="d8f080d2e4bdfb75284aac34d5eff155002eca47798b1d4e4bde31453e653f87",
)
HTML2CANVAS = ExternalPackage(
    name="html2canvas",
    version="1.4.1",
    url="https://registry.npmjs.org/html2canvas/-/html2canvas-1.4.1.tgz",
    sha256="542536a762933dadf7605b275ae096a31734ab3cf488069b7d75523c309f5b12",
)
JSZIP = ExternalPackage(
    name="jszip",
    version="3.10.1",
    url="https://registry.npmjs.org/jszip/-/jszip-3.10.1.tgz",
    sha256="5117f4a2a645aeb307bf3b829c575ad58135cc97e75291e594532ab5b5b21b23",
)
DOMPURIFY = ExternalPackage(
    name="dompurify",
    version="3.4.7",
    url="https://registry.npmjs.org/dompurify/-/dompurify-3.4.7.tgz",
    sha256="a5096949288f85f5201eee7908adb755305e9ddb29bd66a4e9d703f12126d22f",
)
EXTERNAL_PACKAGES = (PDFJS, MARKED, MATHJAX, HTML2CANVAS, JSZIP, DOMPURIFY)
EXTERNAL_LICENSES = {
    PDFJS.name: "licenses/pdfjs-dist-LICENSE",
    MARKED.name: "licenses/marked-LICENSE.md",
    MATHJAX.name: "licenses/mathjax-full-LICENSE",
    HTML2CANVAS.name: "licenses/html2canvas-LICENSE",
    JSZIP.name: "licenses/jszip-LICENSE.markdown",
    DOMPURIFY.name: "licenses/dompurify-LICENSE",
}

EXPECTED_PDFJS_FILES = {
    "static/pdfjs/pdf.mjs": "a61b937e4b39edb9d10adeace5011361ee877e6aaab53d0c964c4b586178b589",
    "static/pdfjs/pdf.worker.mjs": "eb69f338a90aef4a8d6b21608c60c9b9f2169a2bb6d56d423a479a127df1bc12",
}
EXPECTED_PDFJS_CMAP_COUNT = 169
EXPECTED_PDFJS_STANDARD_FONT_COUNT = 16
EXPECTED_MARKED_SHA256 = (
    "6002af63485b043fa60ddaba1b34363b98d2a8b2c63b607004f3a2405a8a053a"
)
EXPECTED_HTML2CANVAS_SHA256 = (
    "e87e550794322e574a1fda0c1549a3c70dae5a93d9113417a429016838eab8cb"
)
EXPECTED_JSZIP_SHA256 = (
    "acc7e41455a80765b5fd9c7ee1b8078a6d160bbbca455aeae854de65c947d59e"
)
EXPECTED_DOMPURIFY_SHA256 = (
    "f84e522876a6cfadecb89c173356409acec39f580c69018559c9a50e96299b0c"
)
EXPECTED_MATHJAX_FONTS = {
    "MathJax_AMS-Regular.woff",
    "MathJax_Calligraphic-Bold.woff",
    "MathJax_Calligraphic-Regular.woff",
    "MathJax_Fraktur-Bold.woff",
    "MathJax_Fraktur-Regular.woff",
    "MathJax_Main-Bold.woff",
    "MathJax_Main-Italic.woff",
    "MathJax_Main-Regular.woff",
    "MathJax_Math-BoldItalic.woff",
    "MathJax_Math-Italic.woff",
    "MathJax_Math-Regular.woff",
    "MathJax_SansSerif-Bold.woff",
    "MathJax_SansSerif-Italic.woff",
    "MathJax_SansSerif-Regular.woff",
    "MathJax_Script-Regular.woff",
    "MathJax_Size1-Regular.woff",
    "MathJax_Size2-Regular.woff",
    "MathJax_Size3-Regular.woff",
    "MathJax_Size4-Regular.woff",
    "MathJax_Typewriter-Regular.woff",
    "MathJax_Vector-Bold.woff",
    "MathJax_Vector-Regular.woff",
    "MathJax_Zero.woff",
}

PWA_EXCLUDED_RESOURCES = {
    "static/pdf/pwa-extension-bridge.js",
    "static/reader-runtime/pwa-cache-identity.js",
    "static/reader-runtime/pwa-runtime.js",
    "static/reader-runtime/pwa-service-bridge.js",
}
MANIFEST_NAME = "bundle-manifest.json"
NATIVE_INTERFACE_CONTRACT = "reader-native-interface-manifest/2"
NATIVE_INTERFACE_SOURCE = HERE / "native_reader_interface_manifest.json"
NATIVE_FORMULA_RECOGNITION_SOURCE = HERE / "App" / "NativeFormulaRecognition.swift"
NATIVE_INTERFACE_NAME = "native_reader_interface_manifest.json"
NATIVE_INTERFACE_GLOBAL = "__BW_NATIVE_INTERFACE_MANIFEST__"
NATIVE_INTERFACE_OWNERS = {"local", "pi", "native"}
NATIVE_INTERFACE_MATCHES = {"exact", "segment"}
NATIVE_INTERFACE_STATUSES = {"supported", "degraded", "pending"}
NATIVE_INTERFACE_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
NATIVE_INTERFACE_SURFACES = {"pdf", "epub"}
NATIVE_REMOTE_BOOK_MODES = {"required", "conditional"}
NATIVE_REMOTE_BOOK_SCOPES = {"current", "catalog"}
NATIVE_REMOTE_BOOK_LOCATIONS = {"query", "json"}
NATIVE_REMOTE_BOOK_TRANSFORMS = {"exact", "prefix-before-delimiter"}
NATIVE_REMOTE_BOOK_POINTERS = {
    "/file", "/context/file", "/context/file_rel", "/ctx/file_rel",
    "/item/file", "/remove_item/file",
}

# Only first-party consumers participate in the interface-coverage audit.
# These resources are loaded by the shell but either implement the router itself
# or are pinned third-party code and therefore cannot declare Reader routes.
NATIVE_INTERFACE_SCAN_EXCLUDED_RESOURCES = {
    "static/pdf/native-local-runtime.js",
    "static/pdf/vendor/jszip.min.js",
    "static/pdf/vendor/purify.min.js",
    "static/qa/marked.js",
    "static/qa/mathjax-full.js",
}
NATIVE_INTERFACE_ROUTE_LITERAL = re.compile(
    r'''(?P<quote>['"`])(?P<path>/(?:pdf/api|api/assistant)/'''
    r'''(?:[A-Za-z0-9._~:@%+*-]+/)*[A-Za-z0-9._~:@%+*-]*)'''
)
NATIVE_INTERFACE_SWIFT_CONSUMERS = (
    (
        "pdf",
        "ios/BWReader/App/NativeFormulaRecognition.swift",
        NATIVE_FORMULA_RECOGNITION_SOURCE,
    ),
)

# The native fetch bridge is part of the compatibility surface, not an opaque
# implementation detail.  Every route it handles locally (including bounded
# hybrid handlers in front of Pi) is pinned here with the manifest semantics
# that make that dispatch reachable.  validate_native_runtime_dispatch() also
# proves that the named handler still contains the route literal, so neither a
# manifest-only declaration nor a dead table row can pass packaging.
NATIVE_RUNTIME_INTERFACE_ENTRIES = {
    "/api/assistant/chat": (
        "pi", ("POST",), ("epub", "pdf"), "localFetch"
    ),
    "/api/assistant/voice-tool": (
        "pi", ("POST",), ("epub", "pdf"), "localFetch"
    ),
    "/api/assistant/voice-page-text": (
        "local", ("GET",), ("epub", "pdf"), "localFetch"
    ),
    "/pdf/api/active-reading": (
        "pi", ("GET", "POST"), ("epub", "pdf"), "localFetch"
    ),
    "/pdf/api/book-langs": (
        "local", ("GET", "POST"), ("epub", "pdf"), "handleLocalState"
    ),
    "/pdf/api/book-crop": (
        "local", ("GET", "POST"), ("pdf",), "localFetch"
    ),
    "/pdf/api/book-meta": (
        "local", ("GET",), ("pdf",), "localFetch"
    ),
    "/pdf/api/context-sync": (
        "pi", ("GET", "POST"), ("epub", "pdf"), "localFetch"
    ),
    "/pdf/api/device-location-pref": (
        "local", ("GET", "POST"), ("epub", "pdf"), "handleLocalState"
    ),
    "/pdf/api/epub-action": (
        "pi", ("POST",), ("epub",), "localFetch"
    ),
    "/pdf/api/epub-assistant": (
        "pi", ("POST",), ("epub",), "localFetch"
    ),
    "/pdf/api/epub-css": (
        "local", ("GET",), ("epub",), "handleEPUB"
    ),
    "/pdf/api/epub-highlights": (
        "local", ("GET", "POST", "PATCH", "DELETE"),
        ("epub",), "handleLocalState"
    ),
    "/pdf/api/card-asset": (
        "native", ("GET",), ("epub", "pdf"), "__native_owner__"
    ),
    "/pdf/api/card-asset-ensure": (
        "native", ("GET",), ("epub", "pdf"), "__native_owner__"
    ),
    "/pdf/api/img-proxy": (
        "native", ("GET",), ("epub", "pdf"), "__native_owner__"
    ),
    "/pdf/api/epub-ink": (
        "local", ("GET", "POST"), ("epub",), "handleLocalState"
    ),
    "/pdf/api/epub-manifest": (
        "local", ("GET",), ("epub",), "handleEPUB"
    ),
    "/pdf/api/epub-resource": (
        "local", ("GET",), ("epub",), "handleEPUB"
    ),
    "/pdf/api/epub-search": (
        "local", ("GET",), ("epub",), "handleEPUB"
    ),
    "/pdf/api/epub-section": (
        "local", ("GET",), ("epub",), "handleEPUB"
    ),
    "/pdf/api/highlights": (
        "local", ("GET", "POST", "PATCH", "DELETE"),
        ("epub", "pdf"), "handleLocalState"
    ),
    "/pdf/api/ink": (
        "local", ("GET", "POST"), ("epub", "pdf"), "handleLocalState"
    ),
    "/pdf/api/job-status": (
        "pi", ("GET",), ("epub", "pdf"), "localFetch"
    ),
    "/pdf/api/note-composite": (
        "local", ("POST",), ("epub", "pdf"), "handleLocalState"
    ),
    "/pdf/api/notes": (
        "local", ("GET", "POST", "PATCH", "DELETE"),
        ("epub", "pdf"), "handleLocalState"
    ),
    "/pdf/api/notification-action": (
        "local", ("POST",), ("epub", "pdf"), "handleLocalState"
    ),
    "/pdf/api/ocr-selection": (
        "local", ("POST",), ("pdf",), "handleLocalState"
    ),
    "/pdf/api/outgoing/drawing": (
        "local", ("GET",), ("epub", "pdf"), "handleLocalState"
    ),
    "/pdf/api/outgoing/focus": (
        "local", ("POST",), ("epub", "pdf"), "handleLocalState"
    ),
    "/pdf/api/outgoing/journal": (
        "local", ("GET",), ("epub", "pdf"), "handleLocalState"
    ),
    "/pdf/api/outgoing/state": (
        "local", ("GET",), ("epub", "pdf"), "handleLocalState"
    ),
    "/pdf/api/page-chars": (
        "local", ("GET",), ("pdf",), "handleLocalState"
    ),
    "/pdf/api/page-image": (
        "local", ("GET",), ("pdf",), "localFetch"
    ),
    "/pdf/api/page-overlay": (
        "pi", ("GET",), ("pdf",), "handleLocalState"
    ),
    "/pdf/api/page-translate": (
        "local", ("GET",), ("pdf",), "localFetch"
    ),
    "/pdf/api/page-text-status": (
        "local", ("GET",), ("pdf",), "handleLocalState"
    ),
    "/pdf/api/pdf-insert-page": (
        "local", ("POST", "PATCH", "DELETE"),
        ("pdf",), "handleLocalState"
    ),
    "/pdf/api/prefs": (
        "local", ("GET", "POST"), ("epub", "pdf"), "handleLocalState"
    ),
    "/pdf/api/word-card-index": (
        "local", ("GET", "POST"), ("epub", "pdf"), "handleLocalState"
    ),
    "/pdf/api/video-player-prefs": (
        "local", ("GET", "POST"), ("epub", "pdf"), "handleLocalState"
    ),
    "/pdf/api/prewarm-async": (
        "local", ("POST",), ("pdf",), "handleLocalState"
    ),
    "/pdf/api/prewarm-status": (
        "local", ("GET",), ("pdf",), "handleLocalState"
    ),
    "/pdf/api/reading-pos": (
        "local", ("GET", "POST"), ("epub", "pdf"), "handleLocalState"
    ),
    "/pdf/api/reocr-page": (
        "local", ("POST",), ("pdf",), "handleLocalState"
    ),
    "/pdf/api/reocr-page/clear": (
        "local", ("POST",), ("pdf",), "handleLocalState"
    ),
    "/pdf/api/search": (
        "local", ("GET",), ("pdf",), "handleLocalState"
    ),
    "/pdf/api/sync-batch": (
        "pi", ("POST",), ("epub", "pdf"), "localFetch"
    ),
    "/pdf/api/translate-direct": (
        # 翻译直连(2026-09-02 A 方案):Swift 持桥下发的 Google key 直连 v2,
        # runtime 在桥缓存 miss 后调它,失败再退 Pi。
        "native", ("POST",), ("epub", "pdf"), "__native_owner__"
    ),
    "/pdf/api/translate-sentence": (
        # 翻译二期保守层(2026-09-02):桥缓存前置的有界混合处理器 ——
        # 命中回缓存,miss 照旧打 Pi 并异步推桥留底。owner 仍是 pi。
        "pi", ("POST",), ("epub", "pdf"), "localFetch"
    ),
    "/pdf/api/to-note": (
        "native", ("POST",), ("epub", "pdf"), "__native_owner__"
    ),
    "/pdf/api/toc": (
        "native", ("GET",), ("pdf",), "__native_owner__"
    ),
    "/pdf/api/ui-version": (
        "local", ("GET",), ("epub", "pdf"), "handleLocalState"
    ),
    "/pdf/api/userpages": (
        "local", ("GET", "POST", "PATCH", "DELETE"),
        ("epub", "pdf"), "handleLocalState"
    ),
}

NATIVE_INTERFACE_SERVER_SOURCES = (
    (ROOT / "_server_deploy" / "assistant.py", "/api/assistant"),
    (ROOT / "_server_deploy" / "pdf_reader.py", "/pdf"),
    (ROOT / "_server_deploy" / "epub_assistant.py", "/pdf"),
    (ROOT / "_server_deploy" / "grammar_reader.py", "/pdf"),
    (ROOT / "_server_deploy" / "book_toc.py", "/pdf"),
    (ROOT / "_server_deploy" / "favorites_reader.py", "/pdf"),
    (ROOT / "_server_deploy" / "html_reader.py", "/pdf"),
    (ROOT / "_server_deploy" / "reader_events.py", "/pdf"),
)


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def obtain_archive(package: ExternalPackage, cache_dir: Path, *, offline: bool) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    destination = cache_dir / package.cache_name
    if destination.is_file() and sha256_file(destination) == package.sha256:
        return destination
    if destination.exists():
        destination.unlink()
    if offline:
        raise SystemExit(
            f"pinned source is not cached for offline build: {package.cache_name}"
        )

    request = urllib.request.Request(
        package.url,
        headers={"User-Agent": "BWReader deterministic local bundle builder/1"},
    )
    temporary = destination.with_name(destination.name + ".part")
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as out:
            shutil.copyfileobj(response, out, length=1024 * 1024)
        actual = sha256_file(temporary)
        if actual != package.sha256:
            raise SystemExit(
                f"{package.name} archive digest mismatch: {actual} != {package.sha256}"
            )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _archive_file(archive: tarfile.TarFile, name: str) -> bytes:
    try:
        member = archive.getmember(name)
    except KeyError as exc:
        raise SystemExit(f"pinned archive is missing {name}") from exc
    if not member.isfile() or PurePosixPath(member.name).is_absolute() or ".." in PurePosixPath(member.name).parts:
        raise SystemExit(f"unsafe or non-file archive member: {name}")
    handle = archive.extractfile(member)
    if handle is None:
        raise SystemExit(f"cannot read archive member: {name}")
    return handle.read()


def write_bytes(root: Path, relative: str, payload: bytes) -> None:
    target = root / PurePosixPath(relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)


def _require_exact_keys(
    value: dict[str, object], expected: set[str], *, label: str
) -> None:
    actual = set(value)
    if actual != expected:
        raise SystemExit(
            f"{label} keys mismatch: missing={sorted(expected - actual)!r}, "
            f"extra={sorted(actual - expected)!r}"
        )


def _validate_native_remote_book_policy(
    value: object,
    *,
    owner: object,
    route_methods: list[str],
    label: str,
) -> None:
    if value is None:
        return
    if owner != "pi" or not isinstance(value, dict):
        raise SystemExit(f"{label} must be null or a Pi-owned policy object")
    _require_exact_keys(
        value,
        {"mode", "scope", "requiredMethods", "identities", "continuation"},
        label=label,
    )
    mode = value.get("mode")
    scope = value.get("scope")
    required_methods = value.get("requiredMethods")
    identities = value.get("identities")
    continuation = value.get("continuation")
    if mode not in NATIVE_REMOTE_BOOK_MODES:
        raise SystemExit(f"{label} mode is invalid")
    if scope not in NATIVE_REMOTE_BOOK_SCOPES:
        raise SystemExit(f"{label} scope is invalid")
    if (
        not isinstance(required_methods, list)
        or any(method not in route_methods for method in required_methods)
        or len(required_methods) != len(set(required_methods))
        or required_methods
        != [method for method in route_methods if method in required_methods]
    ):
        raise SystemExit(f"{label} requiredMethods are invalid")
    if mode == "required" and required_methods != route_methods:
        raise SystemExit(f"{label} required mode must cover every route method")
    if mode == "conditional" and required_methods == route_methods:
        raise SystemExit(f"{label} conditional mode must leave a conditional method")
    if not isinstance(identities, list) or not identities:
        raise SystemExit(f"{label} identities must be a non-empty array")
    identity_methods: set[str] = set()
    for index, identity in enumerate(identities):
        identity_label = f"{label} identities[{index}]"
        if not isinstance(identity, dict):
            raise SystemExit(f"{identity_label} must be an object")
        _require_exact_keys(
            identity,
            {"methods", "location", "pointer", "transform"},
            label=identity_label,
        )
        methods = identity.get("methods")
        location = identity.get("location")
        pointer = identity.get("pointer")
        transform = identity.get("transform")
        if (
            not isinstance(methods, list)
            or not methods
            or any(method not in route_methods for method in methods)
            or len(methods) != len(set(methods))
            or methods != [method for method in route_methods if method in methods]
        ):
            raise SystemExit(f"{identity_label} methods are invalid")
        if location not in NATIVE_REMOTE_BOOK_LOCATIONS:
            raise SystemExit(f"{identity_label} location is invalid")
        if pointer not in NATIVE_REMOTE_BOOK_POINTERS:
            raise SystemExit(f"{identity_label} pointer is not an approved identity field")
        if location == "query" and pointer != "/file":
            raise SystemExit(f"{identity_label} query identity must use /file")
        if transform not in NATIVE_REMOTE_BOOK_TRANSFORMS:
            raise SystemExit(f"{identity_label} transform is invalid")
        identity_methods.update(methods)
    if not set(required_methods).issubset(identity_methods):
        raise SystemExit(f"{label} lacks an identity rule for a required method")
    if continuation is not None:
        continuation_label = f"{label} continuation"
        if not isinstance(continuation, dict):
            raise SystemExit(f"{continuation_label} must be null or an object")
        _require_exact_keys(
            continuation,
            {"kind", "pointer", "fromPointer"},
            label=continuation_label,
        )
        if continuation != {
            "kind": "rid", "pointer": "/rid", "fromPointer": "/from"
        }:
            raise SystemExit(f"{continuation_label} is invalid")


def validate_native_interface_manifest(
    manifest: object, *, label: str = NATIVE_INTERFACE_NAME
) -> dict[str, object]:
    """Validate the one authoritative native Reader interface classification."""
    if not isinstance(manifest, dict):
        raise SystemExit(f"{label} must be a JSON object")
    _require_exact_keys(
        manifest,
        {"contract", "routes", "scanIgnores"},
        label=label,
    )
    if manifest.get("contract") != NATIVE_INTERFACE_CONTRACT:
        raise SystemExit(f"{label} contract mismatch")
    routes = manifest.get("routes")
    ignores = manifest.get("scanIgnores")
    if not isinstance(routes, list) or not routes:
        raise SystemExit(f"{label} routes must be a non-empty array")
    if not isinstance(ignores, list):
        raise SystemExit(f"{label} scanIgnores must be an array")

    route_keys: list[tuple[str, str]] = []
    normalized_routes: list[dict[str, object]] = []
    for index, route in enumerate(routes):
        route_label = f"{label} routes[{index}]"
        if not isinstance(route, dict):
            raise SystemExit(f"{route_label} must be an object")
        _require_exact_keys(
            route,
            {
                "path", "match", "owner", "methods", "surfaces", "status",
                "remoteBook", "description",
            },
            label=route_label,
        )
        path = route.get("path")
        match = route.get("match")
        owner = route.get("owner")
        methods = route.get("methods")
        surfaces = route.get("surfaces")
        status = route.get("status")
        remote_book = route.get("remoteBook")
        description = route.get("description")
        if (
            not isinstance(path, str)
            or not re.fullmatch(
                r"/(?:pdf/api|api/assistant)/(?:[A-Za-z0-9._~:@%+-]+/)*"
                r"[A-Za-z0-9._~:@%+-]*",
                path,
            )
            or "//" in path
            or path in {"/pdf/api/", "/api/assistant/"}
        ):
            raise SystemExit(f"{route_label} has an unsafe or generalized path")
        if match not in NATIVE_INTERFACE_MATCHES:
            raise SystemExit(f"{route_label} match must be exact or segment")
        if match == "exact" and path.endswith("/"):
            raise SystemExit(f"{route_label} exact path must not end in slash")
        if match == "segment" and not path.endswith("/"):
            raise SystemExit(f"{route_label} segment path must end in slash")
        if owner not in NATIVE_INTERFACE_OWNERS:
            raise SystemExit(f"{route_label} owner is invalid")
        if status not in NATIVE_INTERFACE_STATUSES:
            raise SystemExit(f"{route_label} status is invalid")
        if status != "supported":
            raise SystemExit(
                f"{route_label} is not release-compatible: status={status!r}"
            )
        if not isinstance(methods, list) or not methods:
            raise SystemExit(f"{route_label} methods must be a non-empty array")
        if (
            any(method not in NATIVE_INTERFACE_METHODS for method in methods)
            or len(methods) != len(set(methods))
            or methods != sorted(methods, key=NATIVE_INTERFACE_METHODS.index)
        ):
            raise SystemExit(f"{route_label} methods must be unique and canonical")
        if (
            not isinstance(surfaces, list)
            or not surfaces
            or any(surface not in NATIVE_INTERFACE_SURFACES for surface in surfaces)
            or len(surfaces) != len(set(surfaces))
            or surfaces != sorted(surfaces)
        ):
            raise SystemExit(f"{route_label} surfaces must be unique and sorted")
        _validate_native_remote_book_policy(
            remote_book,
            owner=owner,
            route_methods=methods,
            label=f"{route_label} remoteBook",
        )
        if (
            not isinstance(description, str)
            or not description.strip()
            or len(description) > 240
        ):
            raise SystemExit(f"{route_label} description must be 1..240 characters")
        route_keys.append((path, str(match)))
        normalized_routes.append(route)

    if route_keys != sorted(route_keys):
        raise SystemExit(f"{label} routes must be sorted by path and match")
    if len(route_keys) != len(set(route_keys)):
        raise SystemExit(f"{label} contains duplicate route classifications")
    for index, left in enumerate(normalized_routes):
        for right in normalized_routes[index + 1:]:
            left_path = str(left["path"])
            right_path = str(right["path"])
            left_match = str(left["match"])
            right_match = str(right["match"])
            overlaps = (
                left_match == "segment" and right_path.startswith(left_path)
            ) or (
                right_match == "segment" and left_path.startswith(right_path)
            )
            if overlaps:
                raise SystemExit(
                    f"{label} has overlapping routes: {left_path!r}, {right_path!r}"
                )

    by_path = {
        str(route["path"]): route
        for route in normalized_routes
        if route["match"] == "exact"
    }
    required_book_identity = {
        "/api/assistant/route-text": ("json", "POST"),
    }
    for required_path, expected in required_book_identity.items():
        location, method = expected
        policy = by_path[required_path]["remoteBook"]
        if (
            policy["scope"] != "current"
            or policy["requiredMethods"] != [method]
            or policy["identities"] != [{
                "methods": [method],
                "location": location,
                "pointer": "/file",
                "transform": "exact",
            }]
            or policy["continuation"] is not None
        ):
            raise SystemExit(
                f"{label} has invalid current-book identity for {required_path}"
            )
    epub_assistant = by_path.get("/pdf/api/epub-assistant")
    if (
        epub_assistant is None
        or epub_assistant["remoteBook"]["identities"][0]["pointer"]
        != "/context/file"
        or epub_assistant["remoteBook"]["continuation"]
        != {"kind": "rid", "pointer": "/rid", "fromPointer": "/from"}
    ):
        raise SystemExit(f"{label} has invalid EPUB assistant continuation policy")
    favorites = by_path.get("/pdf/api/favorites")
    if (
        favorites is None
        or favorites["remoteBook"]["mode"] != "conditional"
        or favorites["remoteBook"]["requiredMethods"] != ["POST"]
    ):
        raise SystemExit(f"{label} has invalid favorites identity policy")

    ignore_keys: list[tuple[str, str, str]] = []
    for index, ignore in enumerate(ignores):
        ignore_label = f"{label} scanIgnores[{index}]"
        if not isinstance(ignore, dict):
            raise SystemExit(f"{ignore_label} must be an object")
        _require_exact_keys(
            ignore,
            {"surface", "resource", "path", "reason"},
            label=ignore_label,
        )
        surface = ignore.get("surface")
        resource = ignore.get("resource")
        path = ignore.get("path")
        reason = ignore.get("reason")
        if surface not in NATIVE_INTERFACE_SURFACES:
            raise SystemExit(f"{ignore_label} surface is invalid")
        if (
            not isinstance(resource, str)
            or not (
                (
                    resource.startswith("static/")
                    and resource.endswith((".js", ".mjs"))
                    and ".." not in PurePosixPath(resource).parts
                    and "\\" not in resource
                )
                or re.fullmatch(
                    r"shells/(?:pdf|epub)\.html#inline-[1-9][0-9]*",
                    resource,
                )
            )
        ):
            raise SystemExit(f"{ignore_label} resource is unsafe")
        if (
            not isinstance(path, str)
            or not re.fullmatch(
                r"/(?:pdf/api|api/assistant)/(?:[A-Za-z0-9._~:@%+-]+/)*"
                r"[A-Za-z0-9._~:@%+-]*",
                path,
            )
            or path in {"/pdf/api/", "/api/assistant/"}
        ):
            raise SystemExit(f"{ignore_label} path is invalid")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 240:
            raise SystemExit(f"{ignore_label} reason must be 1..240 characters")
        ignore_keys.append((str(surface), str(resource), path))
    if ignore_keys != sorted(ignore_keys) or len(ignore_keys) != len(set(ignore_keys)):
        raise SystemExit(f"{label} scanIgnores must be unique and sorted")
    return manifest


def load_native_interface_manifest(path: Path = NATIVE_INTERFACE_SOURCE) -> dict[str, object]:
    if not path.is_file():
        raise SystemExit(f"native Reader interface manifest is missing: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"cannot read native Reader interface manifest: {exc}") from exc
    return validate_native_interface_manifest(value, label=path.name)


def _native_interface_bootstrap(manifest: dict[str, object]) -> str:
    encoded = json.dumps(
        manifest, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )
    encoded = (
        encoded.replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )
    return f"<script>window.{NATIVE_INTERFACE_GLOBAL}={encoded};</script>\n"


def copy_raw_static(root: Path) -> None:
    native_runtime = STATIC / "pdf" / "native-local-runtime.js"
    if not native_runtime.is_file():
        raise SystemExit(
            "missing App-owned renderer bridge: "
            "_server_deploy/static/pdf/native-local-runtime.js"
        )

    for source_root, destination_root in (
        (STATIC / "pdf", Path("static/pdf")),
        (STATIC / "reader-runtime", Path("static/reader-runtime")),
        (STATIC / "icons", Path("static/icons")),
    ):
        if not source_root.is_dir():
            raise SystemExit(f"missing raw static source directory: {source_root}")
        for source in sorted(source_root.rglob("*")):
            if not source.is_file():
                continue
            relative = source.relative_to(source_root)
            if source_root == STATIC / "pdf":
                if relative.parts and relative.parts[0] == "reader.src":
                    continue
                if relative.as_posix() in {"reader.js", "pwa-extension-bridge.js"}:
                    continue
            if (
                source_root == STATIC / "reader-runtime"
                and f"static/reader-runtime/{relative.as_posix()}" in PWA_EXCLUDED_RESOURCES
            ):
                continue
            target = root / destination_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

    parts = sorted((STATIC / "pdf" / "reader.src").glob("*.js"))
    if not parts:
        raise SystemExit("reader.src contains no renderer parts")
    reader = b"".join(part.read_bytes() for part in parts)
    write_bytes(root, "static/pdf/reader.js", reader)


def require_raw_sources() -> None:
    required = (
        TEMPLATES / "pdf_reader.html",
        TEMPLATES / "epub_html_reader.html",
        STATIC / "pdf" / "native-local-runtime.js",
        STATIC / "reader-runtime" / "native-sync-bootstrap.js",
        STATIC / "pdf" / "html2canvas.min.js",
        STATIC / "pdf" / "epub-html.js",
        STATIC / "pdf" / "vendor" / "jszip.min.js",
        NATIVE_INTERFACE_SOURCE,
    )
    missing = [path.relative_to(ROOT).as_posix() for path in required if not path.is_file()]
    if missing:
        raise SystemExit(f"local Reader raw source closure is incomplete: {missing!r}")
    if not any((STATIC / "pdf" / "reader.src").glob("*.js")):
        raise SystemExit("local Reader raw source closure has no reader.src parts")


def install_external_assets(root: Path, archives: dict[str, Path]) -> None:
    with tarfile.open(archives[PDFJS.name], "r:gz") as archive:
        mappings = {
            "package/build/pdf.mjs": "static/pdfjs/pdf.mjs",
            "package/build/pdf.worker.mjs": "static/pdfjs/pdf.worker.mjs",
        }
        for source, destination in mappings.items():
            write_bytes(root, destination, _archive_file(archive, source))
        for member in sorted(archive.getmembers(), key=lambda value: value.name):
            if not member.isfile():
                continue
            for prefix, destination in (
                ("package/cmaps/", "static/pdfjs/cmaps/"),
                ("package/standard_fonts/", "static/pdfjs/standard_fonts/"),
            ):
                if member.name.startswith(prefix):
                    suffix = member.name[len(prefix):]
                    if suffix and "/" not in suffix and "\\" not in suffix:
                        write_bytes(root, destination + suffix, _archive_file(archive, member.name))
        write_bytes(root, "licenses/pdfjs-dist-LICENSE", _archive_file(archive, "package/LICENSE"))

    with tarfile.open(archives[MARKED.name], "r:gz") as archive:
        write_bytes(root, "static/qa/marked.js", _archive_file(archive, "package/marked.min.js"))
        write_bytes(root, "licenses/marked-LICENSE.md", _archive_file(archive, "package/LICENSE.md"))

    with tarfile.open(archives[MATHJAX.name], "r:gz") as archive:
        write_bytes(
            root,
            "static/qa/mathjax-full.js",
            _archive_file(archive, "package/es5/tex-chtml-full.js"),
        )
        font_prefix = "package/es5/output/chtml/fonts/woff-v2/"
        copied_fonts: set[str] = set()
        for member in sorted(archive.getmembers(), key=lambda value: value.name):
            if not member.isfile() or not member.name.startswith(font_prefix):
                continue
            name = member.name[len(font_prefix):]
            if not name or "/" in name or "\\" in name:
                continue
            copied_fonts.add(name)
            write_bytes(
                root,
                "static/qa/output/chtml/fonts/woff-v2/" + name,
                _archive_file(archive, member.name),
            )
        if copied_fonts != EXPECTED_MATHJAX_FONTS:
            raise SystemExit(
                "MathJax font closure drift: "
                f"missing={sorted(EXPECTED_MATHJAX_FONTS - copied_fonts)!r}, "
                f"extra={sorted(copied_fonts - EXPECTED_MATHJAX_FONTS)!r}"
            )
        write_bytes(root, "licenses/mathjax-full-LICENSE", _archive_file(archive, "package/LICENSE"))

    # html2canvas itself remains sourced from the checked-in raw static tree.
    # The matching pinned upstream archive supplies its full license and proves
    # that the checked-in bytes are the declared 1.4.1 release.
    with tarfile.open(archives[HTML2CANVAS.name], "r:gz") as archive:
        upstream = _archive_file(archive, "package/dist/html2canvas.min.js")
        if sha256_bytes(upstream) != EXPECTED_HTML2CANVAS_SHA256:
            raise SystemExit("pinned html2canvas package content drift")
        write_bytes(
            root,
            "licenses/html2canvas-LICENSE",
            _archive_file(archive, "package/LICENSE"),
        )

    with tarfile.open(archives[JSZIP.name], "r:gz") as archive:
        upstream = _archive_file(archive, "package/dist/jszip.min.js")
        if sha256_bytes(upstream) != EXPECTED_JSZIP_SHA256:
            raise SystemExit("pinned JSZip package content drift")
        write_bytes(
            root,
            "licenses/jszip-LICENSE.markdown",
            _archive_file(archive, "package/LICENSE.markdown"),
        )

    with tarfile.open(archives[DOMPURIFY.name], "r:gz") as archive:
        upstream = _archive_file(archive, "package/dist/purify.min.js")
        if sha256_bytes(upstream) != EXPECTED_DOMPURIFY_SHA256:
            raise SystemExit("pinned DOMPurify package content drift")
        write_bytes(root, "static/pdf/vendor/purify.min.js", upstream)
        write_bytes(
            root,
            "licenses/dompurify-LICENSE",
            _archive_file(archive, "package/LICENSE"),
        )


def _render_template(name: str, values: dict[str, object]) -> str:
    environment = Environment(
        autoescape=select_autoescape(("html", "xml")),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    source = (TEMPLATES / name).read_text(encoding="utf-8")
    return environment.from_string(source).render(**values)


def _remove_native_inapp_pwa_identity(shell: str) -> str:
    shell, replaced = re.subn(
        r'<script\b[^>]*\bsrc=["\']/static/reader-runtime/pwa-runtime\.js[^"\']*["\'][^>]*>\s*</script>',
        '<script src="/static/reader-runtime/native-sync-bootstrap.js"></script>',
        shell,
        count=1,
        flags=re.IGNORECASE,
    )
    if replaced != 1:
        raise SystemExit("Reader template no longer has exactly one PWA runtime slot")
    shell = re.sub(
        r'<script\b[^>]*\bsrc=["\']/(?:static/pdf/pwa-extension-bridge\.js|static/reader-runtime/pwa-service-bridge\.js)[^"\']*["\'][^>]*>\s*</script>',
        "",
        shell,
        flags=re.IGNORECASE,
    )
    shell = re.sub(
        r'<meta\b[^>]*\bname=["\'](?:apple-mobile-web-app-capable|apple-mobile-web-app-title|mobile-web-app-capable)["\'][^>]*>\s*',
        "",
        shell,
        flags=re.IGNORECASE,
    )
    shell = re.sub(
        r'<link\b[^>]*\brel=["\']apple-touch-icon["\'][^>]*>\s*',
        "",
        shell,
        flags=re.IGNORECASE,
    )
    shell = re.sub(
        r'<link\b[^>]*\brel=["\']manifest["\'][^>]*>\s*',
        "",
        shell,
        flags=re.IGNORECASE,
    )
    return shell


def _apply_csp_nonce_placeholder(shell: str) -> str:
    """Require every local Reader script to opt in to the per-launch CSP."""
    shell, script_count = re.subn(
        r"<script\b(?![^>]*\bnonce\s*=)",
        f'<script nonce="{CSP_NONCE_PLACEHOLDER}"',
        shell,
        flags=re.IGNORECASE,
    )
    if script_count < 1:
        raise SystemExit("Reader template contains no script tags to nonce")
    return shell


def build_pdf_shell(
    interface_manifest: dict[str, object] | None = None,
) -> str:
    interface_manifest = interface_manifest or load_native_interface_manifest()
    url_sentinel = "BW_SENTINEL_PDF_URL_79A7"
    file_sentinel = "BW_SENTINEL_FILE_REL_10F3"
    size_sentinel = 918273645
    page_sentinel = 918273646
    page_timestamp_sentinel = 918273647
    rendered = _render_template(
        "pdf_reader.html",
        {
            "reader_app": "native-local-pdf",
            "reader_route": "pdf",
            "file_name": PDF_PLACEHOLDERS[2],
            "pdf_url": url_sentinel,
            "file_rel": file_sentinel,
            "page": page_sentinel,
            "page_ts": page_timestamp_sentinel,
            "chars_ver": 0,
            "pdf_size": size_sentinel,
            "compressed": 0,
            "comp_avail": 0,
            "ui_shared": 1,
            "group": None,
            "web_url": None,
            "web_rbi": "",
            "web_bridge_nonce": "",
            "web_navigation_ticket": "",
            "shared_js_v": "local",
            "reader_js_v": "local",
            "js_v": "local",
        },
    )
    rendered = rendered.replace(json.dumps(url_sentinel), PDF_PLACEHOLDERS[0])
    rendered = rendered.replace(json.dumps(file_sentinel), PDF_PLACEHOLDERS[1])
    rendered = re.sub(
        rf"(?<![0-9]){size_sentinel}(?![0-9])",
        PDF_PLACEHOLDERS[3],
        rendered,
    )
    rendered = re.sub(
        rf"(?<![0-9]){page_sentinel}(?![0-9])",
        PDF_PLACEHOLDERS[4],
        rendered,
    )
    rendered = re.sub(
        rf"(?<![0-9]){page_timestamp_sentinel}(?![0-9])",
        PDF_PLACEHOLDERS[5],
        rendered,
    )
    bootstrap = (
        '<script>window.__BW_NATIVE_LOCAL_READER__=true;</script>\n'
        + _native_interface_bootstrap(interface_manifest)
        + '<script src="/static/pdf/vendor/purify.min.js"></script>\n'
        '<script src="/static/pdf/native-local-runtime.js"></script>\n'
    )
    marker = '<script src="/static/qa/marked.js"></script>'
    if marker not in rendered:
        raise SystemExit("PDF template no longer has the expected first Reader script")
    rendered = rendered.replace(marker, bootstrap + marker, 1)
    return _apply_csp_nonce_placeholder(_remove_native_inapp_pwa_identity(rendered))


def build_epub_shell(
    interface_manifest: dict[str, object] | None = None,
) -> str:
    interface_manifest = interface_manifest or load_native_interface_manifest()
    file_sentinel = "BW_SENTINEL_FILE_REL_B7C2"
    name_sentinel = "BW_SENTINEL_FILE_NAME_54DD"
    sha_sentinel = "BW_SENTINEL_EPUB_SHA_A091"
    position_sentinel = 918273648
    position_timestamp_sentinel = 918273649
    rendered = _render_template(
        "epub_html_reader.html",
        {
            "reader_app": "native-local-epub",
            "reader_route": "epub",
            "file_name": EPUB_PLACEHOLDERS[2],
            "file_rel": file_sentinel,
            "sha": sha_sentinel,
            "server_pos": position_sentinel,
            "server_pos_ts": position_timestamp_sentinel,
            "reader_js_v": "local",
            "is_fav": False,
            "fav_id": "",
        },
    )
    # The title/visible label use the HTML placeholder, while EPUB_CFG receives
    # a separately JSON-escaped replacement owned by the native loopback host.
    rendered = rendered.replace(json.dumps(file_sentinel), EPUB_PLACEHOLDERS[0])
    config_name = json.dumps(EPUB_PLACEHOLDERS[2])
    if config_name not in rendered:
        raise SystemExit("EPUB template fileName JSON slot was not rendered")
    rendered = rendered.replace(config_name, EPUB_PLACEHOLDERS[1], 1)
    rendered = rendered.replace(json.dumps(sha_sentinel), EPUB_PLACEHOLDERS[3])
    rendered = re.sub(
        rf"(?<![0-9]){position_sentinel}(?![0-9])",
        EPUB_PLACEHOLDERS[4],
        rendered,
    )
    rendered = re.sub(
        rf"(?<![0-9]){position_timestamp_sentinel}(?![0-9])",
        EPUB_PLACEHOLDERS[5],
        rendered,
    )
    bootstrap = (
        '<script>window.__BW_NATIVE_LOCAL_READER__=true;</script>\n'
        + _native_interface_bootstrap(interface_manifest)
        + '<script src="/static/pdf/vendor/jszip.min.js"></script>\n'
        '<script src="/static/pdf/vendor/purify.min.js"></script>\n'
        '<script src="/static/pdf/native-local-runtime.js"></script>\n'
    )
    marker = '<script src="/static/qa/marked.js"></script>'
    if marker not in rendered:
        raise SystemExit("EPUB template no longer has the expected first Reader script")
    rendered = rendered.replace(marker, bootstrap + marker, 1)
    return _apply_csp_nonce_placeholder(_remove_native_inapp_pwa_identity(rendered))


def _static_references(shell: str) -> set[str]:
    return {
        match.split("?", 1)[0].lstrip("/")
        for match in re.findall(
            r'(?:src|href)=["\'](/static/[^"\']+)["\']',
            shell,
            flags=re.IGNORECASE,
        )
    }


def _loaded_script_resources(shell: str) -> list[str]:
    resources: list[str] = []
    for match in re.findall(
        r'<script\b[^>]*\bsrc=["\'](/static/[^"\']+)["\'][^>]*>',
        shell,
        flags=re.IGNORECASE,
    ):
        relative = match.split("?", 1)[0].lstrip("/")
        if relative not in resources:
            resources.append(relative)
    return resources


def _strip_javascript_comments(source: str) -> str:
    """Remove JS comments without turning comment examples into consumers."""
    output: list[str] = []
    index = 0
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    while index < len(source):
        character = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if character in "\r\n":
                line_comment = False
                output.append(character)
            else:
                output.append(" ")
            index += 1
            continue
        if block_comment:
            if character == "*" and following == "/":
                output.extend((" ", " "))
                block_comment = False
                index += 2
            else:
                output.append(character if character in "\r\n" else " ")
                index += 1
            continue
        if quote:
            output.append(character)
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            index += 1
            continue
        if character in "'\"`":
            quote = character
            output.append(character)
            index += 1
            continue
        if character == "/" and following == "/":
            output.extend((" ", " "))
            line_comment = True
            index += 2
            continue
        if character == "/" and following == "*":
            output.extend((" ", " "))
            block_comment = True
            index += 2
            continue
        output.append(character)
        index += 1
    return "".join(output)


def _javascript_function_body(source: str, name: str) -> str:
    match = re.search(
        rf"\bfunction\s+{re.escape(name)}\s*\([^)]*\)\s*\{{",
        source,
    )
    if match is None:
        raise SystemExit(f"native-local-runtime is missing function {name}")
    opening = match.end() - 1
    depth = 0
    quote = ""
    escaped = False
    line_comment = False
    block_comment = False
    index = opening
    while index < len(source):
        character = source[index]
        following = source[index + 1] if index + 1 < len(source) else ""
        if line_comment:
            if character in "\r\n":
                line_comment = False
            index += 1
            continue
        if block_comment:
            if character == "*" and following == "/":
                block_comment = False
                index += 2
            else:
                index += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = ""
            index += 1
            continue
        if character in "'\"`":
            quote = character
            index += 1
            continue
        if character == "/" and following == "/":
            line_comment = True
            index += 2
            continue
        if character == "/" and following == "*":
            block_comment = True
            index += 2
            continue
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1:index]
        index += 1
    raise SystemExit(f"native-local-runtime function {name} is unterminated")


def _script_sources_for_surface(
    root: Path, *, surface: str, shell_relative: str
) -> list[tuple[str, str, str]]:
    """Return loaded external and executable inline script sources."""
    shell = (root / shell_relative).read_text(encoding="utf-8")
    sources: list[tuple[str, str, str]] = []
    for resource in _loaded_script_resources(shell):
        if resource in NATIVE_INTERFACE_SCAN_EXCLUDED_RESOURCES:
            continue
        path = root / PurePosixPath(resource)
        if path.is_file():
            sources.append((surface, resource, path.read_text(
                encoding="utf-8", errors="replace"
            )))
    inline_index = 0
    for match in re.finditer(
        r"<script\b(?P<attrs>[^>]*)>(?P<body>[\s\S]*?)</script\s*>",
        shell,
        flags=re.IGNORECASE,
    ):
        inline_index += 1
        if re.search(r"\bsrc\s*=", match.group("attrs"), re.IGNORECASE):
            continue
        body = match.group("body")
        # This generated bootstrap contains the manifest itself. Counting its
        # paths as consumers would make the reverse-coverage gate tautological.
        if f"window.{NATIVE_INTERFACE_GLOBAL}=" in body:
            continue
        sources.append((
            surface, f"{shell_relative}#inline-{inline_index}", body
        ))
    return sources


def _ast_bound_value(
    value: ast.AST | None, bindings: dict[str, ast.AST] | None
) -> ast.AST | None:
    if isinstance(value, ast.Name) and bindings and value.id in bindings:
        return bindings[value.id]
    return value


def _ast_string(
    value: ast.AST | None, bindings: dict[str, ast.AST] | None = None
) -> str | None:
    value = _ast_bound_value(value, bindings)
    return value.value if isinstance(value, ast.Constant) and isinstance(
        value.value, str
    ) else None


def _ast_http_methods(
    call: ast.Call,
    *,
    label: str,
    bindings: dict[str, ast.AST] | None = None,
) -> tuple[str, ...]:
    methods_node = next(
        (keyword.value for keyword in call.keywords if keyword.arg == "methods"),
        None,
    )
    if methods_node is None:
        return ("GET",)
    methods_node = _ast_bound_value(methods_node, bindings)
    if not isinstance(methods_node, (ast.List, ast.Tuple)):
        raise SystemExit(f"{label} has a non-literal Flask methods declaration")
    methods = tuple(_ast_string(item, bindings) or "" for item in methods_node.elts)
    if (
        not methods
        or any(method not in NATIVE_INTERFACE_METHODS for method in methods)
        or len(methods) != len(set(methods))
    ):
        raise SystemExit(f"{label} has invalid Flask methods")
    return tuple(sorted(methods, key=NATIVE_INTERFACE_METHODS.index))


def _canonical_flask_route(
    prefix: str, relative: str
) -> tuple[str, str] | None:
    if not relative.startswith("/"):
        return None
    path = prefix.rstrip("/") + relative
    if not path.startswith(("/pdf/api/", "/api/assistant/")):
        return None
    if "<" not in path:
        return path, "exact"
    match = re.fullmatch(r"(?P<prefix>.*?/)<(?:[^<>]+)>", path)
    if match is None:
        # A variable in the middle cannot be represented by the manifest's
        # safe segment-prefix matcher and must not be guessed.
        return None
    return match.group("prefix"), "segment"


def discover_native_pi_server_routes(
    sources: tuple[tuple[Path, str], ...] = NATIVE_INTERFACE_SERVER_SOURCES,
) -> dict[tuple[str, str], tuple[str, ...]]:
    """Read Flask route methods from source, without importing the server."""
    discovered: dict[tuple[str, str], set[str]] = {}

    def record(
        call: ast.Call,
        *,
        prefix: str,
        label: str,
        bindings: dict[str, ast.AST] | None = None,
    ) -> None:
        rule_node = call.args[0] if call.args else next(
            (keyword.value for keyword in call.keywords if keyword.arg == "rule"),
            None,
        )
        relative = _ast_string(rule_node, bindings)
        if relative is None:
            return
        canonical = _canonical_flask_route(prefix, relative)
        if canonical is None:
            return
        discovered.setdefault(canonical, set()).update(
            _ast_http_methods(call, label=label, bindings=bindings)
        )

    for path, prefix in sources:
        if not path.is_file():
            raise SystemExit(f"native Pi route source is missing: {path}")
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (OSError, SyntaxError) as error:
            raise SystemExit(f"cannot parse native Pi route source {path}: {error}") from error
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    if (
                        isinstance(decorator, ast.Call)
                        and isinstance(decorator.func, ast.Attribute)
                        and decorator.func.attr == "route"
                    ):
                        record(
                            decorator,
                            prefix=prefix,
                            label=f"{path.name}:{decorator.lineno}",
                        )
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_url_rule"
            ):
                record(
                    node, prefix=prefix, label=f"{path.name}:{node.lineno}"
                )
            if not isinstance(node, ast.For):
                continue
            if (
                not isinstance(node.target, (ast.Tuple, ast.List))
                or not all(isinstance(item, ast.Name) for item in node.target.elts)
                or not isinstance(node.iter, (ast.Tuple, ast.List))
            ):
                continue
            names = [item.id for item in node.target.elts]
            registrations = [
                call
                for statement in node.body
                for call in ast.walk(statement)
                if (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "add_url_rule"
                )
            ]
            for row in node.iter.elts:
                if (
                    not isinstance(row, (ast.Tuple, ast.List))
                    or len(row.elts) != len(names)
                ):
                    continue
                bindings = dict(zip(names, row.elts))
                for call in registrations:
                    record(
                        call,
                        prefix=prefix,
                        label=f"{path.name}:{call.lineno}",
                        bindings=bindings,
                    )
    return {
        key: tuple(sorted(methods, key=NATIVE_INTERFACE_METHODS.index))
        for key, methods in discovered.items()
    }


def validate_native_pi_server_routes(
    manifest: dict[str, object],
    *,
    sources: tuple[tuple[Path, str], ...] = NATIVE_INTERFACE_SERVER_SOURCES,
) -> None:
    server_routes = discover_native_pi_server_routes(sources)
    missing: list[str] = []
    drift: list[str] = []
    for route in manifest["routes"]:
        if route["owner"] != "pi":
            continue
        key = (str(route["path"]), str(route["match"]))
        actual = server_routes.get(key)
        if actual is None:
            missing.append(str(route["path"]))
            continue
        expected = tuple(route["methods"])
        if actual != expected:
            drift.append(
                f"{route['path']}: manifest={list(expected)!r}, "
                f"server={list(actual)!r}"
            )
    if missing:
        raise SystemExit(
            f"native Pi routes have no parsed server definition: {sorted(missing)!r}"
        )
    if drift:
        raise SystemExit(f"native Pi route method drift: {sorted(drift)!r}")


def validate_native_runtime_dispatch(
    manifest: dict[str, object],
    *,
    runtime_path: Path | None = None,
) -> set[str]:
    runtime_path = runtime_path or (STATIC / "pdf" / "native-local-runtime.js")
    source = runtime_path.read_text(encoding="utf-8", errors="replace")
    handler_bodies = {
        name: _javascript_function_body(source, name)
        for name in ("handleLocalState", "handleEPUB", "localFetch")
    }
    declared_by_path = {
        str(route["path"]): route
        for route in manifest["routes"]
        if route["match"] == "exact"
    }
    manifest_local_or_native = {
        str(route["path"])
        for route in manifest["routes"]
        if route["owner"] in {"local", "native"}
    }
    table_local_or_native = {
        path for path, entry in NATIVE_RUNTIME_INTERFACE_ENTRIES.items()
        if entry[0] in {"local", "native"}
    }
    if manifest_local_or_native != table_local_or_native:
        raise SystemExit(
            "native runtime dispatch table differs from local/native manifest: "
            f"missing={sorted(manifest_local_or_native - table_local_or_native)!r}, "
            f"stale={sorted(table_local_or_native - manifest_local_or_native)!r}"
        )

    runtime_literals: set[str] = set()
    for body in handler_bodies.values():
        runtime_literals.update(
            match.group("path")
            for match in NATIVE_INTERFACE_ROUTE_LITERAL.finditer(
                _strip_javascript_comments(body)
            )
            if match.group("path") not in {"/pdf/api/", "/api/assistant/"}
        )
    table_runtime_literals = {
        path for path, entry in NATIVE_RUNTIME_INTERFACE_ENTRIES.items()
        if entry[3] != "__native_owner__"
    }
    if runtime_literals != table_runtime_literals:
        raise SystemExit(
            "native runtime dispatch entry table drift: "
            f"unlisted={sorted(runtime_literals - table_runtime_literals)!r}, "
            f"stale={sorted(table_runtime_literals - runtime_literals)!r}"
        )

    for path, (owner, methods, surfaces, handler) in (
        NATIVE_RUNTIME_INTERFACE_ENTRIES.items()
    ):
        route = declared_by_path.get(path)
        if route is None:
            raise SystemExit(f"native runtime dispatch lacks manifest route: {path}")
        actual = (
            str(route["owner"]), tuple(route["methods"]), tuple(route["surfaces"])
        )
        expected = (owner, methods, surfaces)
        if actual != expected:
            raise SystemExit(
                f"native runtime dispatch semantics drift for {path}: "
                f"manifest={actual!r}, handler={expected!r}"
            )
        if handler == "__native_owner__":
            if "route.owner === 'native'" not in handler_bodies["localFetch"]:
                raise SystemExit("native-local-runtime lost its native-owner dispatch")
            continue
        body_paths = {
            match.group("path")
            for match in NATIVE_INTERFACE_ROUTE_LITERAL.finditer(
                _strip_javascript_comments(handler_bodies[handler])
            )
        }
        if path not in body_paths:
            raise SystemExit(
                f"native runtime dispatch table has no {handler} evidence for {path}"
            )
    return set(NATIVE_RUNTIME_INTERFACE_ENTRIES)


def _route_matches_declaration(path: str, route: dict[str, object]) -> bool:
    declared = str(route["path"])
    if route["match"] == "exact":
        return path == declared
    return path == declared or path.startswith(declared)


def validate_native_interface_coverage(
    root: Path, manifest: dict[str, object]
) -> None:
    """Bidirectionally audit consumers, native dispatch and Pi endpoints."""
    routes = list(manifest["routes"])
    ignore_keys = {
        (str(value["surface"]), str(value["resource"]), str(value["path"]))
        for value in manifest["scanIgnores"]
    }
    used_ignores: set[tuple[str, str, str]] = set()
    discoveries: list[tuple[str, str, str]] = []
    for surface, shell_relative in (("pdf", PDF_SHELL), ("epub", EPUB_SHELL)):
        for _, resource, source in _script_sources_for_surface(
            root, surface=surface, shell_relative=shell_relative
        ):
            # An ignore can intentionally document a literal in a comment, but
            # comments never become route-consumer evidence.
            for match in NATIVE_INTERFACE_ROUTE_LITERAL.finditer(source):
                key = (surface, resource, match.group("path"))
                if key in ignore_keys:
                    used_ignores.add(key)
            executable = _strip_javascript_comments(source)
            for match in NATIVE_INTERFACE_ROUTE_LITERAL.finditer(executable):
                discoveries.append((surface, resource, match.group("path")))

    for surface, resource, source_path in NATIVE_INTERFACE_SWIFT_CONSUMERS:
        if not source_path.is_file():
            raise SystemExit(
                f"native Reader Swift consumer is missing: {resource}"
            )
        executable = _strip_javascript_comments(
            source_path.read_text(encoding="utf-8", errors="replace")
        )
        for match in NATIVE_INTERFACE_ROUTE_LITERAL.finditer(executable):
            discoveries.append((surface, resource, match.group("path")))

    missing: list[str] = []
    wrong_surface: list[str] = []
    consumer_evidence: set[tuple[str, str]] = set()
    for surface, resource, path in sorted(set(discoveries)):
        key = (surface, resource, path)
        if key in ignore_keys:
            used_ignores.add(key)
            continue
        classified = [route for route in routes if _route_matches_declaration(path, route)]
        if not classified:
            missing.append(f"{surface}:{resource}:{path}")
            continue
        if surface not in classified[0]["surfaces"]:
            wrong_surface.append(f"{surface}:{resource}:{path}")
            continue
        consumer_evidence.add((str(classified[0]["path"]), str(classified[0]["match"])))
    if missing:
        raise SystemExit(
            "native Reader interface manifest is missing loaded route literals: "
            f"{missing!r}"
        )
    if wrong_surface:
        raise SystemExit(
            "native Reader interface route has the wrong surface classification: "
            f"{wrong_surface!r}"
        )
    stale_ignores = sorted(ignore_keys - used_ignores)
    if stale_ignores:
        raise SystemExit(
            f"native Reader interface manifest has stale scanIgnores: {stale_ignores!r}"
        )

    runtime_evidence = validate_native_runtime_dispatch(
        manifest, runtime_path=root / "static/pdf/native-local-runtime.js"
    )
    no_entry_evidence = sorted(
        str(route["path"])
        for route in routes
        if (
            (str(route["path"]), str(route["match"])) not in consumer_evidence
            and str(route["path"]) not in runtime_evidence
        )
    )
    if no_entry_evidence:
        raise SystemExit(
            "native Reader manifest routes have no loaded consumer or native "
            f"dispatch evidence: {no_entry_evidence!r}"
        )
    validate_native_pi_server_routes(manifest)


def validate_shell(root: Path, relative: str, placeholders: tuple[str, ...], *, epub: bool) -> None:
    shell = (root / relative).read_text(encoding="utf-8")
    if not shell.lstrip().lower().startswith("<!doctype html>"):
        raise SystemExit(f"{relative} must retain a valid HTML doctype")
    if "{{" in shell or "{%" in shell:
        raise SystemExit(f"unrendered Jinja remains in {relative}")
    for placeholder in placeholders:
        if shell.count(placeholder) < 1:
            raise SystemExit(f"{relative} is missing placeholder {placeholder}")
    script_tags = re.findall(r"<script\b[^>]*>", shell, flags=re.IGNORECASE)
    if not script_tags or any(
        f'nonce="{CSP_NONCE_PLACEHOLDER}"' not in tag for tag in script_tags
    ):
        raise SystemExit(f"{relative} contains a script outside the local CSP nonce")
    forbidden = (
        "pwa-extension-bridge.js",
        "pwa-service-bridge.js",
        "pwa-runtime.js",
        'rel="manifest"',
        "apple-mobile-web-app-capable",
        "apple-mobile-web-app-title",
        "apple-touch-icon",
        "mobile-web-app-capable",
    )
    for token in forbidden:
        if token in shell:
            raise SystemExit(f"native shell contains PWA identity token {token!r}")
    flag = shell.find("window.__BW_NATIVE_LOCAL_READER__=true")
    interface_manifest = shell.find(f"window.{NATIVE_INTERFACE_GLOBAL}=")
    purifier = shell.find('/static/pdf/vendor/purify.min.js')
    runtime = shell.find('/static/pdf/native-local-runtime.js')
    marked = shell.find('/static/qa/marked.js')
    if min(flag, interface_manifest, purifier, runtime, marked) < 0 or not (
        flag < interface_manifest < purifier < runtime < marked
    ):
        raise SystemExit(f"native runtime order is invalid in {relative}")
    if epub:
        jszip = shell.find('/static/pdf/vendor/jszip.min.js')
        if min(jszip, purifier) < 0 or not (
            interface_manifest < jszip < purifier < runtime
        ):
            raise SystemExit(
                "EPUB must load JSZip and DOMPurify before native-local-runtime"
            )
    sync_runtime = shell.find('/static/reader-runtime/sync-runtime.js')
    native_sync = shell.find('/static/reader-runtime/native-sync-bootstrap.js')
    if min(sync_runtime, native_sync) < 0 or sync_runtime >= native_sync:
        raise SystemExit(f"native sync bootstrap order is invalid in {relative}")
    missing = sorted(
        reference for reference in _static_references(shell)
        if not (root / reference).is_file()
    )
    if missing:
        raise SystemExit(f"{relative} references missing bundle assets: {missing!r}")


def validate_manifest(root: Path) -> dict[str, object]:
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        raise SystemExit("ReaderBundle manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("contract") != BUNDLE_CONTRACT:
        raise SystemExit("ReaderBundle manifest contract mismatch")
    expected_files = manifest.get("files")
    if not isinstance(expected_files, dict):
        raise SystemExit("ReaderBundle manifest files must be an object")
    unsafe_paths = []
    for relative, digest in expected_files.items():
        if not isinstance(relative, str) or not isinstance(digest, str):
            unsafe_paths.append(repr(relative))
            continue
        path = PurePosixPath(relative)
        if (
            not relative
            or relative != path.as_posix()
            or path.is_absolute()
            or any(part in {"", ".", ".."} for part in path.parts)
            or "\\" in relative
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            unsafe_paths.append(relative)
    if unsafe_paths:
        raise SystemExit(f"ReaderBundle manifest contains unsafe entries: {unsafe_paths!r}")
    actual_paths = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.name != MANIFEST_NAME
    }
    if set(expected_files) != actual_paths:
        raise SystemExit(
            "ReaderBundle manifest file set mismatch: "
            f"missing={sorted(set(expected_files) - actual_paths)!r}, "
            f"extra={sorted(actual_paths - set(expected_files))!r}"
        )
    changed = [
        relative for relative, expected in sorted(expected_files.items())
        if sha256_file(root / PurePosixPath(relative)) != expected
    ]
    if changed:
        raise SystemExit(f"ReaderBundle resource digest mismatch: {changed!r}")
    return manifest


def validate_bundle(root: Path, *, require_manifest: bool = True) -> dict[str, object]:
    if not root.is_dir():
        raise SystemExit(f"ReaderBundle is missing: {root}")
    retained_pwa_resources = sorted(
        relative for relative in PWA_EXCLUDED_RESOURCES if (root / relative).exists()
    )
    if retained_pwa_resources:
        raise SystemExit(
            f"ReaderBundle must not contain PWA lifecycle/takeover code: {retained_pwa_resources!r}"
        )
    if any(path.name in {"manifest.webmanifest", "sw.js", "service-worker.js"} for path in root.rglob("*")):
        raise SystemExit("ReaderBundle must not contain a web manifest or service worker")

    interface_manifest = load_native_interface_manifest(root / NATIVE_INTERFACE_NAME)
    validate_shell(root, PDF_SHELL, PDF_PLACEHOLDERS, epub=False)
    validate_shell(root, EPUB_SHELL, EPUB_PLACEHOLDERS, epub=True)
    validate_native_interface_coverage(root, interface_manifest)
    if not (root / "static/pdf/html2canvas.min.js").is_file():
        raise SystemExit("ReaderBundle is missing html2canvas")
    if sha256_file(root / "static/pdf/html2canvas.min.js") != EXPECTED_HTML2CANVAS_SHA256:
        raise SystemExit("ReaderBundle html2canvas differs from pinned html2canvas@1.4.1")
    if not (root / "static/pdf/epub-html.js").is_file():
        raise SystemExit("ReaderBundle is missing the EPUB renderer")
    if sha256_file(root / "static/pdf/vendor/jszip.min.js") != EXPECTED_JSZIP_SHA256:
        raise SystemExit("ReaderBundle JSZip differs from pinned jszip@3.10.1")
    if sha256_file(root / "static/pdf/vendor/purify.min.js") != EXPECTED_DOMPURIFY_SHA256:
        raise SystemExit("ReaderBundle DOMPurify differs from pinned dompurify@3.4.7")
    if not (root / "static/pdf/reader.js").is_file():
        raise SystemExit("ReaderBundle is missing the generated PDF renderer")
    if sha256_file(root / "static/qa/marked.js") != EXPECTED_MARKED_SHA256:
        raise SystemExit("ReaderBundle marked.js differs from pinned marked@9.1.6")
    for relative, expected in EXPECTED_PDFJS_FILES.items():
        if sha256_file(root / relative) != expected:
            raise SystemExit(f"ReaderBundle PDF.js byte drift: {relative}")
    cmap_root = root / "static/pdfjs/cmaps"
    standard_font_root = root / "static/pdfjs/standard_fonts"
    if not cmap_root.is_dir() or not standard_font_root.is_dir():
        raise SystemExit("ReaderBundle PDF.js support directories are missing")
    cmap_count = sum(1 for path in cmap_root.iterdir() if path.is_file())
    standard_font_count = sum(
        1 for path in standard_font_root.iterdir() if path.is_file()
    )
    if cmap_count != EXPECTED_PDFJS_CMAP_COUNT:
        raise SystemExit(f"ReaderBundle PDF.js CMap closure drift: {cmap_count}")
    if standard_font_count != EXPECTED_PDFJS_STANDARD_FONT_COUNT:
        raise SystemExit(
            f"ReaderBundle PDF.js standard font closure drift: {standard_font_count}"
        )
    fonts = {
        path.name
        for path in (root / "static/qa/output/chtml/fonts/woff-v2").glob("*.woff")
    }
    if fonts != EXPECTED_MATHJAX_FONTS:
        raise SystemExit("ReaderBundle MathJax font closure is incomplete")
    missing_licenses = sorted(
        relative for relative in EXTERNAL_LICENSES.values()
        if not (root / relative).is_file()
    )
    if missing_licenses:
        raise SystemExit(f"ReaderBundle third-party licenses are incomplete: {missing_licenses!r}")

    if not require_manifest:
        return {}
    return validate_manifest(root)


def source_input_manifest() -> dict[str, str]:
    inputs = {
        "_server_deploy/templates/pdf_reader.html": sha256_file(TEMPLATES / "pdf_reader.html"),
        "_server_deploy/templates/epub_html_reader.html": sha256_file(TEMPLATES / "epub_html_reader.html"),
        "ios/BWReader/native_reader_interface_manifest.json": sha256_file(NATIVE_INTERFACE_SOURCE),
        "ios/BWReader/App/NativeFormulaRecognition.swift": sha256_file(
            NATIVE_FORMULA_RECOGNITION_SOURCE
        ),
    }
    for part in sorted((STATIC / "pdf" / "reader.src").glob("*.js")):
        inputs[part.relative_to(ROOT).as_posix()] = sha256_file(part)
    return dict(sorted(inputs.items()))


def write_manifest(root: Path) -> dict[str, object]:
    files = {
        path.relative_to(root).as_posix(): sha256_file(path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != MANIFEST_NAME
    }
    manifest: dict[str, object] = {
        "contract": BUNDLE_CONTRACT,
        "generatedBy": "ios/BWReader/package_local_reader.py",
        "runtimeOwner": "native-app",
        "documentRoot": "ReaderBundle",
        "shells": {
            "pdf": {"path": PDF_SHELL, "placeholders": list(PDF_PLACEHOLDERS)},
            "epub": {"path": EPUB_SHELL, "placeholders": list(EPUB_PLACEHOLDERS)},
        },
        "excludedRuntimeResources": [
            *sorted(PWA_EXCLUDED_RESOURCES),
            "manifest.webmanifest",
            "sw.js",
            "service-worker.js",
        ],
        "externalSources": [
            {
                "name": package.name,
                "version": package.version,
                "url": package.url,
                "sha256": package.sha256,
                "licensePath": EXTERNAL_LICENSES[package.name],
            }
            for package in EXTERNAL_PACKAGES
        ],
        "sourceInputs": source_input_manifest(),
        "files": dict(sorted(files.items())),
    }
    (root / MANIFEST_NAME).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return manifest


def replace_output(staging: Path, output: Path) -> None:
    if output.exists():
        manifest = output / MANIFEST_NAME
        if not manifest.is_file():
            raise SystemExit(f"refusing to replace non-ReaderBundle directory: {output}")
        try:
            contract = json.loads(manifest.read_text(encoding="utf-8")).get("contract")
        except (OSError, ValueError):
            contract = None
        if contract != BUNDLE_CONTRACT:
            raise SystemExit(f"refusing to replace unknown generated directory: {output}")
        shutil.rmtree(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging, output)


def build(output: Path, cache_dir: Path, *, offline: bool) -> dict[str, object]:
    # Fail before any network access when the checked-in renderer closure is
    # incomplete. Downloading pinned archives cannot repair a missing App-owned
    # runtime bridge.
    require_raw_sources()
    interface_manifest = load_native_interface_manifest()
    archives = {
        package.name: obtain_archive(package, cache_dir, offline=offline)
        for package in EXTERNAL_PACKAGES
    }
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".ReaderBundle-", dir=output.parent))
    try:
        copy_raw_static(staging)
        install_external_assets(staging, archives)
        shutil.copyfile(
            NATIVE_INTERFACE_SOURCE, staging / NATIVE_INTERFACE_NAME
        )
        write_bytes(
            staging,
            PDF_SHELL,
            build_pdf_shell(interface_manifest).encode("utf-8"),
        )
        write_bytes(
            staging,
            EPUB_SHELL,
            build_epub_shell(interface_manifest).encode("utf-8"),
        )
        validate_bundle(staging, require_manifest=False)
        manifest = write_manifest(staging)
        validate_bundle(staging)
        replace_output(staging, output)
        return manifest
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache-dir", type=Path, default=DEFAULT_CACHE)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="refuse network access and use only already-verified cached archives",
    )
    parser.add_argument(
        "--verify",
        type=Path,
        metavar="READER_BUNDLE",
        help="verify an existing staged/archived ReaderBundle without downloading",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.verify is not None:
        manifest = validate_bundle(args.verify.resolve())
        print(f"verified={args.verify.resolve()}")
        print(f"files={len(manifest['files'])}")
        return 0
    manifest = build(args.output, args.cache_dir, offline=args.offline)
    print(f"bundle={args.output.resolve()}")
    print(f"files={len(manifest['files'])}")
    print(f"manifest_sha256={sha256_file(args.output.resolve() / MANIFEST_NAME)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
