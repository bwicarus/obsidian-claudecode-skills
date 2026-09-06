#!/usr/bin/env python3
"""Build an App Store Connect Safari Web Extension Packager input ZIP.

The Chromium manifest remains the source of truth. This script creates a
temporary Safari/iOS manifest without modifying the installed Windows build:

- MV3 service worker only (no cross-browser background.scripts fallback)
- nativeMessaging for the containing BWReader app bridge
- unlimitedStorage for the cross-site translation/card caches
- active Pi backend only
- opaque 1024 px App Store icon
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import zipfile

from PIL import Image


HERE = Path(__file__).resolve().parent
EXTENSIONS = HERE.parent
# call.html/call.js host the optional full-page bridge. The inline computer
# button is different: facade.js embeds its extension-owned document into the
# current HTTP(S) page so microphone and WSS stay out of Safari's reclaimable
# background worker. That one HTML resource is therefore deliberately exposed
# below through a narrow web_accessible_resources declaration.
ROOT_FILES = (
    "background.js",
    "content.js",
    "popup.html",
    "popup.js",
    "call.html",
    "call.js",
    "inline-computer-voice.html",
    "inline-computer-voice.js",
    # Imported by call.js as a module; omitting it here would ship a page whose
    # import fails at load, with nothing on screen to say why.
    "ctxlink.js",
)
# call.html is what the sidebar button now embeds (in its compact form), so it
# is the document web pages must be allowed to frame. inline-computer-voice.html
# stays listed while it remains in the package.
INLINE_COMPUTER_VOICE_RESOURCES = [{
    "resources": ["call.html", "inline-computer-voice.html"],
    "matches": ["https://*/*", "http://*/*"],
}]
ROOT_DIRS = ("src", "vendor", "icons")
BACKGROUND_SCRIPTS = (
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
    "background.js",
)
BACKGROUND_IMPORTS = BACKGROUND_SCRIPTS[:-1]
# 2026-09-06：服务器是 Windows 桥（bwicarus-2）；Pi 的 webapp 已停。
ACTIVE_ORIGIN = "https://bwicarus-2.taile44d0c.ts.net/"
# The Windows bridge. Narrow on purpose: one named host, nothing wildcarded.
#
# The extension already talks to this machine -- the computer-voice link has run
# over wss:// to it all along -- but WebSocket does not consult host permissions
# and fetch does, so posting a snapshot needs it stated. Adding it grants no
# reach the extension did not already have; it only makes the existing reach
# usable by the one request that needs it.
BRIDGE_ORIGIN = "https://bwicarus-2.taile44d0c.ts.net/"
OPENAI_REALTIME_ORIGIN = "https://api.openai.com/"
TRUSTED_PWA_MATCHES = {
    ACTIVE_ORIGIN + "pdf/view",
    ACTIVE_ORIGIN + "pdf/view?*",
    ACTIVE_ORIGIN + "pdf/epub/view",
    ACTIVE_ORIGIN + "pdf/epub/view?*",
    ACTIVE_ORIGIN + "pdf/html/view",
    ACTIVE_ORIGIN + "pdf/html/view?*",
    ACTIVE_ORIGIN + "pdf/fav/open",
    ACTIVE_ORIGIN + "pdf/fav/open?*",
}
# Must match the product name registered in App Store Connect for BUNDLE_ID --
# the upload is rejected outright when they differ. This is also the name shown
# in the iPad Safari extension list, so it is the user-facing one.
APP_NAME = "bwicarus-test"
BUNDLE_ID = "space.bwicarus.bwreader2"
SKU = "bw-reader-ipad-002"
PRIMARY_LANGUAGE = "zh-Hans"
SAFARI_ICON = "icons/icon-1024-safari.png"
# 诊断结论:极简包 + 这六个全不透明 RGB 图标可通过 Apple 校验(RGBA 透明 512 疑似打包失败根因)。
# 完整包沿用同一批字节级相同的图标(icons/icon-<n>-opaque.png,从诊断包提取),RGBA 旧图整个排除出包。
OPAQUE_ICONS = {str(n): f"icons/icon-{n}-opaque.png" for n in (48, 64, 96, 128, 256, 512)}
CHROMIUM_ICON = "icons/icon-512.png"   # RGBA 透明,Windows/Chromium 渠道继续用;绝不进 Safari 包


def add_tree(
    archive: zipfile.ZipFile,
    source: Path,
    prefix: Path,
    *,
    exclude: set[str] | None = None,
) -> None:
    exclude = exclude or set()
    for path in sorted(source.rglob("*")):
        rel = (prefix / path.relative_to(source)).as_posix()
        if path.is_file() and path.name not in {".DS_Store"} and rel not in exclude:
            archive.write(path, (prefix / path.relative_to(source)).as_posix())


def safari_manifest(*, compat: bool = False) -> dict:
    manifest = json.loads((HERE / "manifest.json").read_text(encoding="utf-8"))
    manifest["name"] = APP_NAME
    # Safari uses nativeMessaging only to hand a user-approved computer-voice
    # request to the containing BWReader app.  The app remains the sole owner of
    # microphone/audio/WSS; the extension never starts a second voice runtime.
    # offscreen remains Windows-Chrome-only.
    # activeTab + scripting let the popup read the page it was opened over, at
    # the moment the user opens it, and only then. The alternative -- a listener
    # living in every page's content script -- was tried and broke the popup
    # outright on ordinary sites; nothing permanent should be added to every page
    # for something needed once per call.
    #
    # activeTab is granted by the click itself and expires with it, so this is
    # narrower than a host permission despite reading arbitrary pages.
    manifest["permissions"] = (
        ["storage", "alarms", "nativeMessaging"]
        if compat
        else [
            "storage",
            "alarms",
            "nativeMessaging",
            "unlimitedStorage",
            "activeTab",
            "scripting",
        ]
    )
    manifest["host_permissions"] = [
        ACTIVE_ORIGIN + "*",
        BRIDGE_ORIGIN + "*",
        OPENAI_REALTIME_ORIGIN + "*",
    ]
    manifest["background"] = (
        {"scripts": list(BACKGROUND_SCRIPTS), "persistent": False}
        if compat
        else {"service_worker": "background.js"}
    )
    manifest["icons"] = (
        dict(OPAQUE_ICONS)
        if compat
        else {**OPAQUE_ICONS, "1024": SAFARI_ICON}
    )
    manifest["web_accessible_resources"] = INLINE_COMPUTER_VOICE_RESOURCES
    return manifest


def required_package_files(manifest: dict) -> set[str]:
    """Return every explicit and implicit runtime resource Safari must receive."""
    required = set(ROOT_FILES)
    background = manifest["background"]
    if background.get("service_worker"):
        required.add(background["service_worker"])
        # MV3 only names the service worker in the manifest. Its importScripts()
        # dependencies are still release-critical package resources.
        required.update(BACKGROUND_IMPORTS)
    required.update(background.get("scripts") or [])
    required.add(manifest["action"]["default_popup"])
    required.update(manifest.get("icons", {}).values())
    for block in manifest.get("content_scripts", []):
        required.update(block.get("js", []))
        required.update(block.get("css", []))
    return required


def validate_background_imports() -> None:
    """Keep the MV3 worker and compat manifest on one dependency order."""
    source = (HERE / "background.js").read_text(encoding="utf-8")
    positions = [source.find(f'"{name}"') for name in BACKGROUND_IMPORTS]
    missing = [
        name
        for name, position in zip(BACKGROUND_IMPORTS, positions)
        if position < 0
    ]
    if missing:
        raise SystemExit(
            "background.js is missing Safari runtime imports: "
            + ", ".join(missing)
        )
    if positions != sorted(positions) or len(set(positions)) != len(positions):
        raise SystemExit(
            "background.js runtime import order differs from Safari compat"
        )


def validate(manifest: dict, *, compat: bool = False) -> None:
    with Image.open(HERE / SAFARI_ICON) as icon:
        if icon.size != (1024, 1024) or icon.mode not in ("RGB", "P"):
            raise SystemExit("Safari icon must be 1024x1024 and fully opaque")
    expected_icons = dict(OPAQUE_ICONS) if compat else {**OPAQUE_ICONS, "1024": SAFARI_ICON}
    if manifest.get("icons") != expected_icons:
        raise SystemExit("Safari manifest must declare the complete opaque icon set")
    # 全部 manifest 引用的图标必须不透明(RGB/P 无 alpha)且尺寸与键一致——RGBA 透明图标疑似 Apple 打包失败根因
    for size, rel in expected_icons.items():
        with Image.open(HERE / rel) as im:
            if im.mode not in ("RGB", "P") or ("transparency" in im.info):
                raise SystemExit(
                    f"icon {rel} must be fully opaque RGB (got {im.mode})"
                )
            if im.size != (int(size), int(size)):
                raise SystemExit(
                    f"icon {rel} size {im.size} != declared {size}"
                )
    required = required_package_files(manifest)
    missing = sorted(rel for rel in required if not (HERE / rel).is_file())
    if missing:
        raise SystemExit("manifest references missing files: " + ", ".join(missing))
    validate_background_imports()
    if manifest.get("manifest_version") != 3:
        raise SystemExit("Safari package requires Manifest V3")
    if manifest.get("host_permissions") != [
        ACTIVE_ORIGIN + "*",
        BRIDGE_ORIGIN + "*",
        OPENAI_REALTIME_ORIGIN + "*",
    ]:
        raise SystemExit(
            "Safari host permissions must remain exactly the active Pi, the "
            "Windows bridge, and OpenAI Realtime"
        )
    if manifest.get("web_accessible_resources") != INLINE_COMPUTER_VOICE_RESOURCES:
        raise SystemExit(
            "Safari may expose only the inline computer-voice frame to HTTP(S) pages"
        )
    expected_permissions = {"storage", "alarms", "nativeMessaging"} if compat else {
        "storage",
        "alarms",
        "nativeMessaging",
        "unlimitedStorage",
        "activeTab",
        "scripting",
    }
    permissions = manifest.get("permissions") or []
    if len(permissions) != len(expected_permissions) or set(permissions) != expected_permissions:
        raise SystemExit("Safari permissions do not match the selected package profile")
    if manifest.get("optional_permissions") or manifest.get("optional_host_permissions"):
        raise SystemExit("Safari package must not declare optional permissions")
    blocks = manifest.get("content_scripts") or []
    if len(blocks) != 2:
        raise SystemExit("Safari must load the PWA marker plus the full webpage runtime")
    block = blocks[0]
    if set(block) != {"matches", "js", "run_at", "all_frames"}:
        raise SystemExit("Safari provider marker block contains unexpected fields")
    matches = block.get("matches") or []
    if len(matches) != 8 or set(matches) != TRUSTED_PWA_MATCHES:
        raise SystemExit(
            "Safari PWA marker must remain on the four exact book routes "
            "with queryless and query-bearing patterns"
        )
    if (
        block.get("js") != ["src/pwa-marker.js"]
        or block.get("run_at") != "document_start"
        or block.get("all_frames") is not False
    ):
        raise SystemExit("Safari PWA marker profile mismatch")
    runtime = blocks[1]
    source_manifest = json.loads(
        (HERE / "manifest.json").read_text(encoding="utf-8")
    )
    source_blocks = source_manifest.get("content_scripts") or []
    expected_runtime_js = (
        source_blocks[1].get("js")
        if len(source_blocks) == 2 and isinstance(source_blocks[1], dict)
        else None
    )
    if (
        runtime.get("matches") != ["https://*/*", "http://*/*"]
        or runtime.get("run_at") != "document_idle"
        or runtime.get("all_frames") is not False
        or not isinstance(expected_runtime_js, list)
        or runtime.get("js") != expected_runtime_js
    ):
        raise SystemExit(
            "Safari full webpage runtime must exactly match the Chromium "
            "manifest order"
        )
    expected_background = (
        {"scripts": list(BACKGROUND_SCRIPTS), "persistent": False}
        if compat
        else {"service_worker": "background.js"}
    )
    if manifest.get("background") != expected_background:
        raise SystemExit("Safari background profile mismatch")


def write_package(
    package: Path,
    manifest: dict,
    *,
    compat: bool = False,
) -> None:
    """Write and then audit one Safari packager-input archive."""
    with tempfile.TemporaryDirectory(prefix="bw-safari-package-") as tmp:
        manifest_path = Path(tmp) / "manifest.json"
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        with zipfile.ZipFile(
            package,
            "w",
            zipfile.ZIP_DEFLATED,
            compresslevel=9,
        ) as archive:
            archive.write(manifest_path, "manifest.json")
            for name in ROOT_FILES:
                archive.write(HERE / name, name)
            for name in ROOT_DIRS:
                # RGBA 透明的 Chromium 图标永不进 Safari 包(疑似 Apple 打包失败根因);
                # compat 再去掉 1024。
                exclude = {CHROMIUM_ICON}
                if compat and name == "icons":
                    exclude.add(SAFARI_ICON)
                add_tree(
                    archive,
                    HERE / name,
                    Path(name),
                    exclude=exclude if name == "icons" else set(),
                )
    validate_package(package, manifest, compat=compat)


def validate_package(
    package: Path,
    manifest: dict,
    *,
    compat: bool = False,
) -> None:
    """Validate the ZIP boundary, including MV3's implicit worker imports."""
    with zipfile.ZipFile(package) as archive:
        names = archive.namelist()
        if not names or names[0] != "manifest.json":
            raise SystemExit("manifest.json must be at ZIP root")
        if len(names) != len(set(names)):
            raise SystemExit("duplicate path in ZIP")
        if any(name.startswith("/") or ".." in Path(name).parts for name in names):
            raise SystemExit("unsafe path in ZIP")
        packed_manifest = json.loads(archive.read("manifest.json"))
        if packed_manifest != manifest:
            raise SystemExit("packed manifest mismatch")
        validate(packed_manifest, compat=compat)
        missing = sorted(required_package_files(packed_manifest) - set(names))
        if missing:
            raise SystemExit(
                "Safari package is missing runtime resources: "
                + ", ".join(missing)
            )
        for resource in BACKGROUND_IMPORTS:
            if archive.read(resource) != (HERE / resource).read_bytes():
                raise SystemExit(
                    f"Safari package runtime resource mismatch: {resource}"
                )
        if archive.read("background.js") != (HERE / "background.js").read_bytes():
            raise SystemExit("Safari package background.js mismatch")
        if CHROMIUM_ICON in names:
            raise SystemExit("Safari package must exclude the Chromium RGBA icon")
        if compat and SAFARI_ICON in names:
            raise SystemExit("Safari compat package must exclude the 1024 icon")
        if not compat and SAFARI_ICON not in names:
            raise SystemExit("Safari standard package must contain the 1024 icon")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--compat",
        action="store_true",
        help="use the most conservative Apple manifest for upload diagnosis",
    )
    args = parser.parse_args()
    manifest = safari_manifest(compat=args.compat)
    validate(manifest, compat=args.compat)
    version = str(manifest["version"])
    suffix = "-compat" if args.compat else ""
    package = EXTENSIONS / f"bw-reader-webext-{version}-safari-ios{suffix}.zip"

    write_package(package, manifest, compat=args.compat)

    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    print(f"package={package}")
    print(f"version={version}")
    print(f"bundle_id={BUNDLE_ID}")
    print(f"sku={SKU}")
    print(f"primary_language={PRIMARY_LANGUAGE}")
    print(f"sha256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
