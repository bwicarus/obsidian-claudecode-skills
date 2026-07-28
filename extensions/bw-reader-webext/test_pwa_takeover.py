#!/usr/bin/env python3
"""Chromium contract：书籍 PWA 两阶段接管，普通 WebAdapter 不在书页启动。"""

from __future__ import annotations

import json
import pathlib
import tempfile

from playwright.sync_api import sync_playwright


EXT = pathlib.Path(__file__).resolve().parent
CHROME = pathlib.Path.home() / ".cache/ms-playwright/chromium-1223/chrome-linux/chrome"
URL = "https://bwicarus.taile44d0c.ts.net/pdf/view?file=contract.pdf"
EXPECTED_VERSION = json.loads(
    (EXT / "manifest.json").read_text(encoding="utf-8")
)["version"]

HTML = """<!doctype html>
<html><head>
  <meta name="bw-reader-app" content="pdf">
  <meta name="bw-reader-route" content="pdf">
  <title>Contract PDF</title>
</head><body>
  <div id="native-ui">PWA fallback UI</div>
  <script>
  (() => {
    const root = document.documentElement;
    let heartbeats = 0;
    window.__anchorFxStats = {
      inFlight: 0,
      maxInFlight: 0,
      payloads: [],
      contextRequests: 0
    };
    let inkActive = false;
    const reply = (type, payload, id) => window.postMessage({
      protocol: 'bw-reader-pwa/1',
      direction: 'to-extension',
      type, payload, id: id || null
    }, location.origin);
    addEventListener('message', (event) => {
      if (event.source !== window || event.origin !== location.origin) return;
      const message = event.data || {};
      if (
        message.__bwReaderPreference === 'preference-store/1' &&
        message.direction === 'extension-to-page'
      ) {
        window.postMessage({
          __bwReaderPreference: 'preference-store/1',
          direction: 'page-to-extension',
          type: message.type === 'HELLO' ? 'READY' : 'RESULT',
          requestId: message.requestId,
          payload: message.type === 'HELLO'
            ? { allowedKeys: ['eph-gp-floating'], values: {} }
            : { ok: true, values: {} }
        }, location.origin);
        return;
      }
      if (
        message.protocol !== 'bw-reader-pwa/1' ||
        message.direction !== 'to-page'
      ) return;
      if (message.type === 'HELLO') {
        reply('READY', {
          mode: 'pdf',
          file: 'contract.pdf',
          currentLocation: { unit: 'page', index: 0, total: 3 },
          capabilities: {
            highlight: true,
            ink: true,
            stickyNote: true,
            pageTranslate: true,
            navigation: true,
            zoom: true,
            layout: true,
            crop: true,
            fullscreen: true,
            bookSettings: true,
            favorite: true,
            userPage: true,
            jumpPage: true,
            pinCard: true,
            pinHtmlCard: true,
            anchorFx: true
          },
          selection: null
        });
      } else if (message.type === 'GET_CONTEXT') {
        window.__anchorFxStats.contextRequests += 1;
        reply('RESULT', {
          ok: true,
          result: {
            file: 'contract.pdf',
            total_pages: 3,
            page: 1,
            current_location: { unit: 'page', index: 0, total: 3 }
          }
        }, message.id);
      } else if (message.type === 'LOCAL_ACTION') {
        root.dataset.lastAction = String(message.payload?.action || '');
        if (message.payload?.action === 'anchor_fx') {
          const stats = window.__anchorFxStats;
          stats.inFlight += 1;
          stats.maxInFlight = Math.max(stats.maxInFlight, stats.inFlight);
          stats.payloads.push({...message.payload?.payload});
          setTimeout(() => {
            stats.inFlight -= 1;
            reply('RESULT', { ok: true, result: true }, message.id);
          }, 120);
        } else if (message.payload?.action === 'toggle_ink') {
          inkActive = !inkActive;
          reply('RESULT', {
            ok: true,
            result: { ok: true, active: inkActive }
          }, message.id);
        } else {
          reply('RESULT', { ok: true, result: true }, message.id);
        }
      } else if (message.type === 'TAKEOVER') {
        root.dataset.takeover = '1';
        document.getElementById('native-ui').hidden = true;
        reply('RESULT', { ok: true, result: { owner: 'extension' } }, message.id);
      } else if (message.type === 'HEARTBEAT') {
        heartbeats += 1;
        root.dataset.heartbeats = String(heartbeats);
      } else if (message.type === 'GOODBYE') {
        delete root.dataset.takeover;
        document.getElementById('native-ui').hidden = false;
        root.dataset.goodbye = '1';
      } else if (message.type === 'CLEAR_SELECTION') {
        reply('RESULT', { ok: true, result: true }, message.id);
      }
    });
  })();
  </script>
</body></html>"""


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="bw-pwa-takeover-") as profile:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                profile,
                executable_path=str(CHROME),
                headless=False,
                args=[
                    f"--disable-extensions-except={EXT}",
                    f"--load-extension={EXT}",
                    "--no-sandbox",
                ],
            )
            try:
                context.route(
                    "https://bwicarus.taile44d0c.ts.net/pdf/view**",
                    lambda route: route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body=HTML,
                    ),
                )
                page = context.new_page()
                session = context.new_cdp_session(page)
                worlds: list[dict] = []
                session.on(
                    "Runtime.executionContextCreated",
                    lambda event: worlds.append(event["context"]),
                )
                session.send("Runtime.enable")
                page.goto(URL, wait_until="domcontentloaded")
                page.wait_for_selector("#bw-reader-host", state="attached")
                page.wait_for_function(
                    "() => document.documentElement.dataset.takeover === '1'",
                    timeout=15_000,
                )
                state = page.evaluate(
                    """() => ({
                      takeover: document.documentElement.dataset.takeover || '',
                      provider: document.documentElement.dataset
                        .bwReaderExtensionProvider || '',
                      marker: document.documentElement.dataset
                        .bwReaderExtension || '',
                      nativeHidden: document.getElementById('native-ui').hidden,
                    })"""
                )
                assert state["takeover"] == "1"
                assert state["provider"] == state["marker"] == EXPECTED_VERSION
                assert state["nativeHidden"] is True

                # All hosts expose the same stateful ink action result. The shell
                # must read result.active rather than treating every object as true.
                page.evaluate(
                    """() => document.querySelector('#bw-reader-host').shadowRoot
                      .querySelector('#bw-ink-btn').click()"""
                )
                page.wait_for_function(
                    """() => document.querySelector('#bw-reader-host').shadowRoot
                      .querySelector('#bw-ink-btn').classList.contains('active')"""
                )
                page.evaluate(
                    """() => document.querySelector('#bw-reader-host').shadowRoot
                      .querySelector('#bw-ink-btn').click()"""
                )
                page.wait_for_function(
                    """() => !document.querySelector('#bw-reader-host').shadowRoot
                      .querySelector('#bw-ink-btn').classList.contains('active')"""
                )
                assert page.evaluate(
                    "() => document.documentElement.dataset.lastAction"
                ) == "toggle_ink"

                extension_world = None
                extension_state = None
                for world in worlds:
                    try:
                        candidate = session.send(
                            "Runtime.evaluate",
                            {
                                "contextId": world["id"],
                                "expression": """(() => ({
                                  bridge: !!window.__bwPwaBridge,
                                  takenOver: !!window.__bwPwaBridge?.takenOver,
                                  adapter: window.RC?.adapter?.()?.kind || '',
                                  webAdapter: !!window.__bwWebHighlights,
                                  takeoverState: window.__bwRoot?.dataset
                                    .pwaTakeover || '',
                                }))()""",
                                "returnByValue": True,
                            },
                        ).get("result", {}).get("value")
                        if candidate and candidate["bridge"]:
                            extension_world = world["id"]
                            extension_state = candidate
                            break
                    except Exception:
                        continue
                assert extension_state == {
                    "bridge": True,
                    "takenOver": True,
                    "adapter": "pwa-pdf",
                    "webAdapter": False,
                    "takeoverState": "ready",
                }, extension_state
                session.send(
                    "Runtime.evaluate",
                    {
                        "contextId": extension_world,
                        "expression": (
                            "window.__bwShadow.getElementById("
                            "'bw-book-next').click()"
                        ),
                    },
                )
                page.wait_for_function(
                    "() => document.documentElement.dataset.lastAction === "
                    "'change_page'"
                )

                # 高频落点反馈必须 latest-wins 串行送达：任一时刻最多一个 RPC，
                # 最后坐标不能被中间帧覆盖，拖拽结束后最终一定 hide。
                page.evaluate(
                    """() => {
                      const s = window.__anchorFxStats;
                      s.inFlight = 0; s.maxInFlight = 0;
                      s.payloads.length = 0; s.contextRequests = 0;
                    }"""
                )
                session.send(
                    "Runtime.evaluate",
                    {
                        "contextId": extension_world,
                        "expression": """new Promise(resolve => {
                          let i = 0;
                          const tick = () => {
                            if (i < 20) {
                              RC.actions.run('pin.anchorFx', {
                                show: true, x: i, y: i
                              });
                              i += 1;
                              setTimeout(tick, 16);
                              return;
                            }
                            RC.actions.run('pin.anchorFx', {show: false});
                            resolve(true);
                          };
                          tick();
                        })""",
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                )
                page.wait_for_function(
                    """() => {
                      const s = window.__anchorFxStats;
                      return s.inFlight === 0 &&
                        s.payloads.length >= 2 &&
                        s.payloads.at(-1)?.show === false;
                    }""",
                    timeout=4_000,
                )
                anchor_stats = page.evaluate(
                    """() => structuredClone(window.__anchorFxStats)"""
                )
                assert anchor_stats["maxInFlight"] == 1, anchor_stats
                assert anchor_stats["payloads"][-1]["show"] is False, anchor_stats
                delivered_shows = [
                    payload
                    for payload in anchor_stats["payloads"]
                    if payload.get("show") is True
                ]
                assert delivered_shows[-1] == {
                    "show": True,
                    "x": 19,
                    "y": 19,
                }, anchor_stats
                assert len(anchor_stats["payloads"]) < 21, anchor_stats
                assert anchor_stats["contextRequests"] == 0, anchor_stats

                page.wait_for_function(
                    "() => Number(document.documentElement.dataset.heartbeats || 0) >= 1",
                    timeout=8_000,
                )
                session.send(
                    "Runtime.evaluate",
                    {
                        "contextId": extension_world,
                        "expression": "window.__bwPwaBridge.release()",
                    },
                )
                page.wait_for_function(
                    "() => document.documentElement.dataset.goodbye === '1'"
                )
                released = page.evaluate(
                    """() => ({
                      takeover: document.documentElement.dataset.takeover || '',
                      nativeHidden: document.getElementById('native-ui').hidden,
                    })"""
                )
                assert released == {"takeover": "", "nativeHidden": False}
                print("OK: book PWA takeover is atomic, leased, and reversible")
            finally:
                context.close()


if __name__ == "__main__":
    main()
