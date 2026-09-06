#!/usr/bin/env python3
"""Live PWA+扩展 2A 合同：扩展唯一共享 UI，PWA 保留原生书籍 DocumentHost。"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright


BASE = "https://bwicarus-2.taile44d0c.ts.net"
BOOK = "%E8%B5%84%E6%BA%90%2Fbooks%2F%E5%BF%9C%E7%94%A8%E6%83%85%E5%A0%B1%E6%8A%80%E8%A1%93%E8%80%85.pdf"
EXT = pathlib.Path(__file__).resolve().parent
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
    with tempfile.TemporaryDirectory(prefix="bw-pwa-takeover-") as profile:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                profile,
                executable_path=str(CHROME),
                headless=False,
                viewport={"width": 1180, "height": 820},
                args=[
                    f"--disable-extensions-except={EXT}",
                    f"--load-extension={EXT}",
                    "--no-sandbox",
                ],
            )
            try:
                context.add_cookies(
                    [{"name": "session", "value": session_cookie(), "url": BASE}]
                )
                context.add_init_script(
                    """
                    try { localStorage.setItem('rcWebTrStyle', 'inline'); } catch (_) {}
                    window.__BW_PROVIDER_TEST_EVENTS__ = [];
                    [
                      'bw:extension-provider-ready',
                      'bw:extension-provider-error',
                      'bw:extension-provider-unhealthy',
                      'bw:extension-provider-disconnected',
                      'bw:reader-runtime-provider-attached',
                      'bw:reader-runtime-provider-conflict',
                      'bw:reader-runtime-provider-error',
                      'bw:reader-runtime-ready',
                      'bw:reader-runtime-error'
                    ].forEach((type) => document.addEventListener(type, (event) => {
                      let detail = null;
                      try {
                        detail = JSON.parse(JSON.stringify(event.detail || null));
                      } catch (_) {}
                      window.__BW_PROVIDER_TEST_EVENTS__.push({ type, detail });
                    }));
                    """
                )
                page = context.new_page()
                errors: list[str] = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(
                    f"{BASE}/pdf/view?file={BOOK}&page=37",
                    wait_until="domcontentloaded",
                    timeout=60_000,
                )
                page.wait_for_function(
                    "() => !!document.documentElement.dataset"
                    ".bwReaderExtensionProvider",
                    timeout=10_000,
                )
                try:
                    page.wait_for_function(
                        "() => window.__BW_READER_RUNTIME__?.mode() === "
                        "'pwa-extension-provider'",
                        timeout=60_000,
                    )
                except PlaywrightTimeoutError as error:
                    diagnostics = page.evaluate(
                        """async () => {
                          const runtime = window.__BW_READER_RUNTIME__;
                          let status = null;
                          try { status = runtime ? await runtime.status() : null; } catch (_) {}
                          return {
                            mode: runtime?.mode?.() || '',
                            status,
                            marker: document.documentElement.dataset
                              .bwReaderExtensionProvider || '',
                            extensionBridgeConnected:
                              !!BWReaderRuntime.extensionProvider?.connected?.(),
                            extensionBridgeCurrent:
                              !!BWReaderRuntime.extensionProvider?.current?.(),
                            namespacePresent:
                              !!BWReaderRuntime.pwaRuntime?.namespace?.(),
                            providerTicketPresent:
                              !!window.__USER__?.storage_provider_ticket,
                            events: window.__BW_PROVIDER_TEST_EVENTS__ || [],
                          };
                        }"""
                    )
                    raise AssertionError(
                        "PWA provider did not attach: "
                        + json.dumps(diagnostics, ensure_ascii=False, default=str)
                    ) from error
                page.wait_for_function(
                    "() => window.RC?.documentHost?.current?.()?.kind === 'pdf'",
                    timeout=60_000,
                )
                page.wait_for_function(
                    """() => (
                      document.documentElement.dataset.bwReaderExtensionActive === '1' &&
                      document.documentElement.dataset.bwReaderUiOwner === 'extension' &&
                      !!document.getElementById('bw-reader-host')?.shadowRoot
                        ?.querySelector('#header')
                    )""",
                    timeout=60_000,
                )

                state = page.evaluate(
                    """async () => {
                      const runtime = window.__BW_READER_RUNTIME__;
                      const status = await runtime.status();
                      const local = BWReaderRuntime.pwaRuntime.localStores();
                      const shadow = await local.global.get(
                        'user-settings',
                        'setting:translation.display-style'
                      );
                      const active = await runtime.storage().get(
                        'user-settings',
                        'setting:translation.display-style'
                      );
                      const nativeHost = RC.documentHost.current();
                      const runtimeHost = runtime.documentHost();
                      const extensionRoot = document.getElementById('bw-reader-host');
                      const extensionTopbar = extensionRoot?.shadowRoot
                        ?.querySelector('#header');
                      const nativeTopbar = document.getElementById('header');
                      const nativeToolbar = document.getElementById('sel-toolbar');
                      const visible = (element) => !!element &&
                        getComputedStyle(element).display !== 'none' &&
                        getComputedStyle(element).visibility !== 'hidden';
                      const localHost = window.__bwReaderLocalApi;
                      return {
                        mode: runtime.mode(),
                        uiOwner: status.uiOwner,
                        uiMounted: status.uiMounted,
                        extensionConnected: status.extensionConnected,
                        storage: {
                          global: status.storage.global.deviceId,
                          document: status.storage.document.deviceId,
                          device: status.storage.device.deviceId,
                        },
                        host: {
                          runtimeKind: runtimeHost.kind,
                          nativeKind: nativeHost.kind,
                          runtimeId: runtimeHost.documentId,
                          nativeId: nativeHost.documentId,
                        },
                        marker: document.documentElement.dataset
                          .bwReaderExtensionProvider || '',
                        takeoverMarker: document.documentElement.dataset
                          .bwReaderExtension || '',
                        owner: document.documentElement.dataset.bwReaderUiOwner || '',
                        active: document.documentElement.dataset
                          .bwReaderExtensionActive || '',
                        extensionHost: !!extensionRoot,
                        extensionTopbar: {
                          present: !!extensionTopbar,
                          visible: visible(extensionTopbar),
                        },
                        nativeTopbar: {
                          present: !!nativeTopbar,
                          visible: visible(nativeTopbar),
                        },
                        nativeToolbar: {
                          present: !!nativeToolbar,
                          visible: visible(nativeToolbar),
                        },
                        visibleTopbars: Number(visible(extensionTopbar)) +
                          Number(visible(nativeTopbar)),
                        localHost: {
                          contract: localHost?.contract || '',
                          mode: localHost?.mode || '',
                        },
                        adapterHandedOff: !!RC.adapter?.()?.__bwExtensionHandoff,
                        shadowRaw: shadow?.value?.rawValue || '',
                        activeRaw: active?.value?.rawValue || '',
                      };
                    }"""
                )
                assert state["mode"] == "pwa-extension-provider"
                assert state["uiOwner"] == "pwa"
                assert state["uiMounted"] is True
                assert state["extensionConnected"] is True
                assert state["storage"]["global"].startswith(
                    "extension-install-v1-"
                ), json.dumps(state, ensure_ascii=False, default=str)
                assert state["storage"]["document"].startswith("pwa-install-v1-")
                assert state["storage"]["device"] == state["storage"]["document"]
                assert state["host"]["runtimeKind"] == state["host"]["nativeKind"] == "pdf"
                assert state["host"]["runtimeId"] == state["host"]["nativeId"]
                assert state["marker"]
                assert state["takeoverMarker"] == state["marker"]
                assert state["owner"] == "extension"
                assert state["active"] == "1"
                assert state["extensionHost"] is True
                assert state["extensionTopbar"] == {"present": True, "visible": True}
                assert state["nativeTopbar"] == {"present": True, "visible": False}
                assert state["nativeToolbar"] == {"present": True, "visible": False}
                assert state["visibleTopbars"] == 1
                assert state["localHost"] == {"contract": "book-host/1", "mode": "pdf"}
                assert state["adapterHandedOff"] is True
                assert state["shadowRaw"] == state["activeRaw"] == "inline"
                assert not errors, "page errors: " + " | ".join(errors[:10])

                # GOODBYE 必须原子归还 PWA：书籍宿主不替换，原生共享顶栏恢复。
                page.evaluate(
                    """() => window.postMessage({
                      protocol: 'bw-reader-pwa/1',
                      direction: 'to-page',
                      type: 'GOODBYE',
                      id: 'live-goodbye',
                      payload: { reason: 'live-contract-test' }
                    }, location.origin)"""
                )
                page.wait_for_function(
                    """() => (
                      !document.documentElement.dataset.bwReaderExtensionActive &&
                      document.documentElement.dataset.bwReaderUiOwner === 'pwa' &&
                      getComputedStyle(document.getElementById('header')).display !== 'none'
                    )""",
                    timeout=10_000,
                )
                goodbye_state = page.evaluate(
                    """() => {
                      const host = RC.documentHost.current();
                      return {
                        owner: document.documentElement.dataset.bwReaderUiOwner || '',
                        active: document.documentElement.dataset
                          .bwReaderExtensionActive || '',
                        nativeTopbarVisible:
                          getComputedStyle(document.getElementById('header'))
                            .display !== 'none',
                        adapterHandedOff: !!RC.adapter?.()?.__bwExtensionHandoff,
                        hostKind: host?.kind || '',
                        hostId: host?.documentId || '',
                      };
                    }"""
                )
                assert goodbye_state["owner"] == "pwa"
                assert goodbye_state["active"] == ""
                assert goodbye_state["nativeTopbarVisible"] is True
                assert goodbye_state["adapterHandedOff"] is False
                assert goodbye_state["hostKind"] == state["host"]["nativeKind"]
                assert goodbye_state["hostId"] == state["host"]["nativeId"]

                # 再取得一次页面租约，然后把页面世界时钟加速；即使扩展仍按 5 秒
                # 心跳，2.5 秒租约巡检也应观察到 >15 秒并自动恢复 PWA。
                page.evaluate(
                    """() => window.postMessage({
                      protocol: 'bw-reader-pwa/1',
                      direction: 'to-page',
                      type: 'TAKEOVER',
                      id: 'live-retakeover',
                      payload: { uiOwner: 'extension' }
                    }, location.origin)"""
                )
                page.wait_for_function(
                    "() => document.documentElement.dataset"
                    ".bwReaderExtensionActive === '1'",
                    timeout=10_000,
                )
                page.evaluate(
                    """() => {
                      const nativeNow = Date.now.bind(Date);
                      const started = nativeNow();
                      window.__BW_TEST_RESTORE_DATE_NOW__ = () => {
                        Date.now = nativeNow;
                        delete window.__BW_TEST_RESTORE_DATE_NOW__;
                      };
                      Date.now = () => started + (nativeNow() - started) * 25;
                    }"""
                )
                page.wait_for_function(
                    """() => (
                      !document.documentElement.dataset.bwReaderExtensionActive &&
                      document.documentElement.dataset.bwReaderUiOwner === 'pwa'
                    )""",
                    timeout=10_000,
                )
                page.evaluate(
                    "() => window.__BW_TEST_RESTORE_DATE_NOW__?.()"
                )
                lease_state = page.evaluate(
                    """() => ({
                      owner: document.documentElement.dataset.bwReaderUiOwner || '',
                      active: document.documentElement.dataset
                        .bwReaderExtensionActive || '',
                      nativeTopbarVisible:
                        getComputedStyle(document.getElementById('header'))
                          .display !== 'none',
                      adapterHandedOff: !!RC.adapter?.()?.__bwExtensionHandoff,
                      hostKind: RC.documentHost.current()?.kind || '',
                    })"""
                )
                assert lease_state == {
                    "owner": "pwa",
                    "active": "",
                    "nativeTopbarVisible": True,
                    "adapterHandedOff": False,
                    "hostKind": "pdf",
                }
                print(
                    "OK: extension owns the only shared topbar; PWA keeps the native "
                    "PDF host; GOODBYE and lease expiry restore PWA atomically"
                )
            finally:
                context.close()


if __name__ == "__main__":
    main()
