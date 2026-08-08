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
    CSP_NONCE_PLACEHOLDER,
)
EPUB_PLACEHOLDERS = (
    "__BW_LOCAL_FILE_REL_JSON__",
    "__BW_LOCAL_FILE_NAME_JSON__",
    "__BW_LOCAL_FILE_NAME_HTML__",
    "__BW_LOCAL_EPUB_SHA_JSON__",
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


def build_pdf_shell() -> str:
    url_sentinel = "BW_SENTINEL_PDF_URL_79A7"
    file_sentinel = "BW_SENTINEL_FILE_REL_10F3"
    size_sentinel = 918273645
    rendered = _render_template(
        "pdf_reader.html",
        {
            "reader_app": "native-local-pdf",
            "reader_route": "pdf",
            "file_name": PDF_PLACEHOLDERS[2],
            "pdf_url": url_sentinel,
            "file_rel": file_sentinel,
            "page": 1,
            "page_ts": 0,
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
    bootstrap = (
        '<script>window.__BW_NATIVE_LOCAL_READER__=true;</script>\n'
        '<script src="/static/pdf/vendor/purify.min.js"></script>\n'
        '<script src="/static/pdf/native-local-runtime.js"></script>\n'
    )
    marker = '<script src="/static/qa/marked.js"></script>'
    if marker not in rendered:
        raise SystemExit("PDF template no longer has the expected first Reader script")
    rendered = rendered.replace(marker, bootstrap + marker, 1)
    return _apply_csp_nonce_placeholder(_remove_native_inapp_pwa_identity(rendered))


def build_epub_shell() -> str:
    file_sentinel = "BW_SENTINEL_FILE_REL_B7C2"
    name_sentinel = "BW_SENTINEL_FILE_NAME_54DD"
    sha_sentinel = "BW_SENTINEL_EPUB_SHA_A091"
    rendered = _render_template(
        "epub_html_reader.html",
        {
            "reader_app": "native-local-epub",
            "reader_route": "epub",
            "file_name": EPUB_PLACEHOLDERS[2],
            "file_rel": file_sentinel,
            "sha": sha_sentinel,
            "server_pos": None,
            "server_pos_ts": 0,
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
    bootstrap = (
        '<script>window.__BW_NATIVE_LOCAL_READER__=true;</script>\n'
        '<script src="/static/pdf/vendor/jszip.min.js"></script>\n'
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
    purifier = shell.find('/static/pdf/vendor/purify.min.js')
    runtime = shell.find('/static/pdf/native-local-runtime.js')
    marked = shell.find('/static/qa/marked.js')
    if min(flag, purifier, runtime, marked) < 0 or not (
        flag < purifier < runtime < marked
    ):
        raise SystemExit(f"native runtime order is invalid in {relative}")
    if epub:
        jszip = shell.find('/static/pdf/vendor/jszip.min.js')
        if min(jszip, purifier) < 0 or not (flag < jszip < purifier < runtime):
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

    validate_shell(root, PDF_SHELL, PDF_PLACEHOLDERS, epub=False)
    validate_shell(root, EPUB_SHELL, EPUB_PLACEHOLDERS, epub=True)
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
        write_bytes(staging, PDF_SHELL, build_pdf_shell().encode("utf-8"))
        write_bytes(staging, EPUB_SHELL, build_epub_shell().encode("utf-8"))
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
