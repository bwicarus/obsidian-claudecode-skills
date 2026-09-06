#!/usr/bin/env python3
"""Live PWA smoke test without an extension: the native fallback keeps a complete host contract."""
from __future__ import annotations

import os
import pathlib
import sys

from playwright.sync_api import sync_playwright


BASE = "https://bwicarus-2.taile44d0c.ts.net"
BOOK = "%E8%B5%84%E6%BA%90%2Fbooks%2F%E5%BF%9C%E7%94%A8%E6%83%85%E5%A0%B1%E6%8A%80%E8%A1%93%E8%80%85.pdf"
from browser_exe import CHROME


def session_cookie() -> str:
    os.environ.setdefault("WEBAPP_DATA", "/home/bwicarus/webapp/data")
    sys.path.insert(0, "/home/bwicarus/claude/scripts")
    sys.path.insert(0, "/home/bwicarus/webapp")
    for line in open("/home/bwicarus/webapp/.env", encoding="utf-8"):
        if line.startswith("SECRET_KEY="):
            os.environ["SECRET_KEY"] = line.strip().split("=", 1)[1]
    from app import app
    from flask.sessions import SecureCookieSessionInterface

    return SecureCookieSessionInterface().get_signing_serializer(app).dumps(
        {"user_id": 1, "username": "bwicarus"}
    )


def main() -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=str(CHROME), headless=True, args=["--no-sandbox"])
        try:
            ctx = browser.new_context(viewport={"width": 1180, "height": 820})
            ctx.add_cookies([{"name": "session", "value": session_cookie(), "url": BASE}])
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(f"{BASE}/pdf/view?file={BOOK}&page=37&ui=shared", wait_until="domcontentloaded", timeout=60000)
            page.wait_for_function("() => window.RC?.adapterAudit?.().kind === 'pdf'", timeout=60000)
            page.wait_for_function("() => window.RC?.adapter?.().currentLocation?.().total > 0", timeout=60000)
            page.wait_for_function(
                "() => window.__BW_READER_RUNTIME__?.mode() === 'pwa-fallback'",
                timeout=60000,
            )
            asset_refs = page.evaluate("""() => {
              const h=document.createElement('div');
              RC.assistant.renderMd(h, '纯文本 #img_a1b2c3\\n\\n![旧式图片](#img_d4e5f6)\\n\\n[#img_0a1b2c](#img_0a1b2c)\\n\\n`#img_deadbe`', true);
              return {
                ids:[...h.querySelectorAll('img')].map(x=>x.dataset.aid),
                broken:[...h.querySelectorAll('img')].filter(x=>(x.getAttribute('src')||'').startsWith('#')).length,
                numberedLinks:h.querySelectorAll('a[href*="img_"]').length,
                code:h.querySelector('code')?.textContent || ''
              };
            }""")
            assert asset_refs == {
                "ids": ["img_a1b2c3", "img_d4e5f6", "img_0a1b2c"],
                "broken": 0,
                "numberedLinks": 0,
                "code": "#img_deadbe",
            }, asset_refs
            state = page.evaluate("""async () => {
              const runtimeStatus = await window.__BW_READER_RUNTIME__.status();
              return {
              audit: RC.adapterAudit(),
              actions: RC.actions.audit(),
              ui: {version: RC.ui?.version, kit: !!document.getElementById('rc-ui-kit'), surface: getComputedStyle(document.documentElement).getPropertyValue('--rc-bg-surface').trim()},
              location: RC.adapter().currentLocation(),
              selection: RC.contract.selection({text:'probe',sentence:'sent',anchor:{kind:'pdf',page:37},rect:{client:{left:1}},custom:9}),
              extensionActive: window.__BW_EXTENSION_HANDOFF__?.active === true,
              nativeToolbar: !!document.getElementById('sel-toolbar'),
              legacyEscape: new URL(location.href).searchParams.get('ui') === 'legacy',
              runtime: {
                mode: window.__BW_READER_RUNTIME__.mode(),
                uiOwner: runtimeStatus.uiOwner,
                extensionConnected: runtimeStatus.extensionConnected,
                globalDevice: runtimeStatus.storage.global.deviceId,
                documentDevice: runtimeStatus.storage.document.deviceId
              }
            }; }""")
            assert state["audit"]["contract"] == "reader-host/1"
            assert state["audit"]["kind"] == "pdf"
            assert state["audit"]["baseMissing"] == []
            assert state["audit"]["selectionMissing"] == []
            assert state["audit"]["assistantHostMissing"] == []
            expected_actions = {"highlight.save", "ink.toggle", "note.create", "reading.ruby.toggle", "translation.page.toggle", "pin.anchorFx", "pin.card", "pin.html"}
            assert {x["name"] for x in state["actions"]} == expected_actions
            assert {x["owner"] for x in state["actions"]} == {"pwa"}
            assert {x["runtime"] for x in state["actions"]} == {"native"}
            assert {x["storage"] for x in state["actions"]} == {"book-sidecar", "device-local", "none"}
            assert state["ui"] == {"version": "reader-ui/2", "kit": True, "surface": "#10162a"}
            assert state["location"]["unit"] == "page"
            assert state["location"]["total"] > 0
            assert state["selection"]["context"] == "sent"
            assert state["selection"]["ctx"] == "sent"
            assert state["selection"]["anchor"] == {"kind": "pdf", "page": 37}
            assert state["selection"]["rect"] == {"client": {"left": 1}}
            assert state["selection"]["custom"] == 9
            assert state["extensionActive"] is False
            assert state["nativeToolbar"] is True
            assert state["legacyEscape"] is False
            assert state["runtime"]["mode"] == "pwa-fallback"
            assert state["runtime"]["uiOwner"] == "pwa"
            assert state["runtime"]["extensionConnected"] is False
            assert state["runtime"]["globalDevice"].startswith("pwa-install-v1-")
            assert (
                state["runtime"]["documentDevice"]
                == state["runtime"]["globalDevice"]
            )
            ink_on = page.evaluate("() => RC.actions.run('ink.toggle')")
            ink_class_on = page.evaluate(
                "() => document.body.classList.contains('ink-mode')"
            )
            assert ink_on == {"ok": True, "active": True} and ink_class_on, {
                "result": ink_on,
                "ink_mode": ink_class_on,
            }
            ink_off = page.evaluate("() => RC.actions.run('ink.toggle')")
            ink_class_off = page.evaluate(
                "() => document.body.classList.contains('ink-mode')"
            )
            assert ink_off == {"ok": True, "active": False} and not ink_class_off, {
                "result": ink_off,
                "ink_mode": ink_class_off,
            }
            ruby_on = page.evaluate("() => RC.actions.run('reading.ruby.toggle')")
            assert ruby_on["ruby"] is True and ruby_on["translate"] is False
            ruby_off = page.evaluate("() => RC.actions.run('reading.ruby.toggle')")
            assert ruby_off["ruby"] is False and ruby_off["translate"] is False
            assert not errors, "page errors: " + " | ".join(errors[:10])
            print("OK: native PWA contract complete; no extension handoff; original toolbar remains available")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
