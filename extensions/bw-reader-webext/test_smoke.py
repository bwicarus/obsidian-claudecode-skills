#!/usr/bin/env python3
"""Chromium smoke：后台依赖可加载，普通网页恢复完整扩展运行时。"""

from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import base64
import os
import pathlib
import tempfile
from threading import Thread

from playwright.sync_api import sync_playwright


ROOT = pathlib.Path(
    os.environ.get(
        "BW_WEBEXT_TEST_ROOT",
        pathlib.Path(__file__).resolve().parents[2],
    )
).resolve()
EXT = pathlib.Path(
    os.environ.get(
        "BW_WEBEXT_DIR",
        pathlib.Path(__file__).resolve().parent,
    )
).resolve()
from browser_exe import CHROME


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *args: object) -> None:
        return


def main() -> None:
    handler = partial(QuietHandler, directory=str(ROOT))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with tempfile.TemporaryDirectory(prefix="bw-webext-smoke-") as profile:
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
                    workers = context.service_workers
                    worker = workers[0] if workers else context.wait_for_event(
                        "serviceworker", timeout=15_000
                    )
                    dependencies = worker.evaluate(
                        """() => ({
                          indexeddb: !!globalThis.BWReaderRuntime?.indexedDBStore
                            ?.createIndexedDBDataStore,
                          registry: !!globalThis.BWReaderRuntime?.dataRegistry
                            ?.providerCollections,
                          ownerLease:
                            globalThis.BWReaderRuntime?.syncOwnerLease
                            ?.CONTRACT === 'owner-lease/1',
                        })"""
                    )
                    assert dependencies == {
                        "indexeddb": True,
                        "registry": True,
                        "ownerLease": True,
                    }

                    page = context.new_page()
                    url = (
                        f"http://127.0.0.1:{server.server_port}"
                        "/extensions/bw-reader-webext/README.md"
                    )
                    page.goto(url, wait_until="domcontentloaded")
                    page.wait_for_selector(
                        "#bw-reader-host", state="attached", timeout=15_000
                    )
                    context.route(
                        "https://bwicarus.taile44d0c.ts.net/pdf/api/dict-quick**",
                        lambda route: route.fulfill(
                            status=200,
                            content_type="application/json",
                            body='{"ok":true,"word":"contract"}',
                        ),
                    )
                    pixel = base64.b64decode(
                        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1"
                        "HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAA"
                        "SUVORK5CYII="
                    )
                    asset_requests: list[str] = []

                    def contract_image(route) -> None:
                        asset_requests.append(route.request.url)
                        route.fulfill(
                            status=200,
                            content_type="image/png",
                            body=pixel,
                        )

                    context.route(
                        "https://bwicarus.taile44d0c.ts.net/pdf/api/asset/contract**",
                        contract_image,
                    )
                    retry_requests = {"count": 0, "urls": []}

                    def retry_image(route) -> None:
                        retry_requests["count"] += 1
                        retry_requests["urls"].append(route.request.url)
                        if retry_requests["count"] == 1:
                            route.fulfill(
                                status=503,
                                content_type="application/json",
                                body='{"ok":false}',
                            )
                        else:
                            route.fulfill(
                                status=200,
                                content_type="image/png",
                                body=pixel,
                            )

                    context.route(
                        "https://bwicarus.taile44d0c.ts.net/pdf/api/asset/retry**",
                        retry_image,
                    )
                    voice_requests: list[dict[str, object]] = []

                    def voice_clip(route) -> None:
                        request = route.request
                        voice_requests.append(
                            {
                                "url": request.url,
                                "body": request.post_data_buffer,
                                "content_type": request.headers.get(
                                    "content-type", ""
                                ),
                                "authorized": request.headers.get(
                                    "authorization", ""
                                )
                                == "Bearer test-device-token",
                            }
                        )
                        route.fulfill(
                            status=200,
                            content_type="application/json",
                            body='{"ok":true,"id":"contract"}',
                        )

                    context.route(
                        "https://bwicarus.taile44d0c.ts.net"
                        "/api/assistant/voice-clip**",
                        voice_clip,
                    )
                    blocked_binary_requests: list[str] = []

                    def unexpected_binary_chat(route) -> None:
                        blocked_binary_requests.append(route.request.url)
                        route.fulfill(
                            status=500,
                            content_type="application/json",
                            body='{"ok":false}',
                        )

                    context.route(
                        "https://bwicarus.taile44d0c.ts.net"
                        "/api/assistant/chat?contract-binary=1",
                        unexpected_binary_chat,
                    )
                    session = page.context.new_cdp_session(page)
                    worlds: list[dict] = []
                    session.on(
                        "Runtime.executionContextCreated",
                        lambda event: worlds.append(event["context"]),
                    )
                    session.send("Runtime.enable")
                    page.reload(wait_until="domcontentloaded")
                    page.wait_for_selector(
                        "#bw-reader-host", state="attached", timeout=15_000
                    )
                    extension_state = None
                    extension_world = None
                    for world in worlds:
                        try:
                            candidate = session.send(
                                "Runtime.evaluate",
                                {
                                    "contextId": world["id"],
                                    "expression": """(() => ({
                                      facade: !!window.__bwReaderDoc,
                                      adapter: window.RC?.adapter?.()?.kind || '',
                                      shell: !!window.__bwShadow?.getElementById('header'),
                                      highlights: !!window.__bwWebHighlights,
                                      pins: !!window.__bwWebPins,
                                      ink: !!window.__bwWebInk,
                                      pwa: !!window.__bwPwaBridge,
                                      providerOnly: !!window.__bwPwaProviderOnly,
                                      wsBridge:
                                        typeof window.__bwReaderOpenWebSocket
                                        === 'function',
                                      settingsVersion: window.__bwSettingsSync || 0,
                                      bookControlsHidden: [
                                        'bw-book-prev','bw-book-jump','bw-book-scrub',
                                        'bw-book-next','bw-book-zoom-out','bw-book-fit',
                                        'bw-book-zoom-in','bw-book-layout','bw-book-crop',
                                        'bw-book-fullscreen','bw-book-settings',
                                        'bw-book-favorite','bw-book-user-page'
                                      ].every(id => {
                                        const el=window.__bwShadow?.getElementById(id);
                                        return !!el && el.hidden && el.disabled;
                                      }),
                                      webControlsVisible: [
                                        'ruby-toggle','pagetr-toggle','bw-ink-btn',
                                        'bw-note-btn','bw-search-btn'
                                      ].every(id => {
                                        const el=window.__bwShadow?.getElementById(id);
                                        return !!el && !el.hidden && !el.disabled;
                                      }),
                                    }))()""",
                                    "returnByValue": True,
                                },
                            ).get("result", {}).get("value")
                            if candidate and candidate["facade"]:
                                extension_world = world["id"]
                                extension_state = candidate
                                break
                        except Exception:
                            continue
                    assert extension_state == {
                        "facade": True,
                        "adapter": "web",
                        "shell": True,
                        "highlights": True,
                        "pins": True,
                        "ink": True,
                        "pwa": False,
                        "providerOnly": False,
                        "wsBridge": True,
                        "settingsVersion": 2,
                        "bookControlsHidden": True,
                        "webControlsVisible": True,
                    }, extension_state

                    # 模拟“书籍 PWA 已经验证过一次”的持久账户。普通网页请求只依赖
                    # 扩展后台保存的 namespace/token，不需要同标签页或任意 PWA provider。
                    namespace = "acct-v1-" + "a" * 64
                    credential_key = (
                        "bw.reader.account.v1:"
                        + namespace
                        + ":extension%3Acredentials-v1"
                    )
                    await_values = {
                        "readerActiveVerifiedAccountV1": {
                            "schema": 1,
                            "namespace": namespace,
                            "verifiedAt": 1,
                            "source": "provider-ticket",
                        },
                        credential_key: {
                            "schema": 1,
                            "activeCandidateId": "credential-v1-contract",
                            "candidates": {
                                "credential-v1-contract": {
                                    "id": "credential-v1-contract",
                                    "fingerprint": "test-only",
                                    "token": "test-device-token",
                                    "createdAt": 1,
                                    "verifiedAt": 1,
                                    "active": True,
                                }
                            },
                        },
                    }
                    worker.evaluate(
                        "(values) => chrome.storage.local.set(values)",
                        await_values,
                    )
                    # 第三方网页不能直接跨站建 WSS；用真实 MV3 port + 后台 fake
                    # 覆盖 facade 的 WebSocket 同接口、JSON/base64 二进制桥和关闭语义。
                    worker.evaluate(
                        """() => {
                          const state = globalThis.__bwWsContract = {
                            urls: [], sent: [], closed: []
                          };
                          class ContractWebSocket {
                            constructor(url) {
                              this.url = String(url);
                              this.readyState = 0;
                              this.binaryType = 'blob';
                              this.onopen = this.onmessage =
                                this.onerror = this.onclose = null;
                              state.urls.push(this.url);
                              queueMicrotask(() => {
                                if (this.readyState !== 0) return;
                                this.readyState = 1;
                                this.onopen?.({type:'open'});
                              });
                            }
                            send(data) {
                              if (typeof data === 'string') {
                                state.sent.push({type:'text',data});
                                queueMicrotask(() => this.onmessage?.({
                                  data:'echo:' + data
                                }));
                                return;
                              }
                              const bytes = data instanceof ArrayBuffer
                                ? new Uint8Array(data)
                                : new Uint8Array(
                                  data.buffer, data.byteOffset, data.byteLength
                                );
                              state.sent.push({
                                type:'binary',bytes:Array.from(bytes)
                              });
                              const reply = Uint8Array.from(
                                Array.from(bytes).reverse()
                              ).buffer;
                              queueMicrotask(() => this.onmessage?.({
                                data:reply
                              }));
                            }
                            close(code = 1000, reason = '') {
                              if (this.readyState >= 2) return;
                              this.readyState = 3;
                              state.closed.push({code,reason});
                              queueMicrotask(() => this.onclose?.({
                                code,reason,wasClean:true
                              }));
                            }
                          }
                          Object.defineProperty(globalThis, 'WebSocket', {
                            configurable:true,writable:true,
                            value:ContractWebSocket
                          });
                        }"""
                    )
                    ws_bridge = session.send(
                        "Runtime.evaluate",
                        {
                            "contextId": extension_world,
                            "expression": """new Promise((resolve, reject) => {
                              const ws=window.__bwReaderOpenWebSocket(
                                '/voice-rt?mode=tts'
                              );
                              const result={
                                states:[ws.readyState],propertyOpen:0,
                                listenerOpen:0,removedOpen:0,messages:[]
                              };
                              const removed=()=>{ result.removedOpen++; };
                              ws.addEventListener('open',()=>{
                                result.listenerOpen++;
                              });
                              ws.addEventListener('open',removed);
                              ws.removeEventListener('open',removed);
                              ws.binaryType='arraybuffer';
                              ws.onopen=()=>{
                                result.propertyOpen++;
                                result.states.push(ws.readyState);
                                ws.send('contract');
                                ws.send(Uint8Array.from(
                                  [0,127,128,255]
                                ).buffer);
                              };
                              ws.onmessage=(event)=>{
                                result.messages.push(
                                  typeof event.data === 'string'
                                    ? event.data
                                    : Array.from(new Uint8Array(event.data))
                                );
                                if(result.messages.length===2) {
                                  ws.close(1000,'done');
                                }
                              };
                              ws.onerror=()=>reject(
                                new Error('unexpected bridge error')
                              );
                              ws.onclose=(event)=>{
                                result.states.push(ws.readyState);
                                result.close={
                                  code:event.code,reason:event.reason,
                                  wasClean:event.wasClean
                                };
                                resolve(result);
                              };
                              setTimeout(
                                ()=>reject(new Error('bridge timeout')),3000
                              );
                            })""",
                            "awaitPromise": True,
                            "returnByValue": True,
                        },
                    ).get("result", {}).get("value")
                    assert ws_bridge == {
                        "states": [0, 1, 3],
                        "propertyOpen": 1,
                        "listenerOpen": 1,
                        "removedOpen": 0,
                        "messages": [
                            "echo:contract",
                            [255, 128, 127, 0],
                        ],
                        "close": {
                            "code": 1000,
                            "reason": "done",
                            "wasClean": True,
                        },
                    }, ws_bridge
                    ws_background = worker.evaluate(
                        "() => globalThis.__bwWsContract"
                    )
                    assert ws_background == {
                        "urls": [
                            "wss://bwicarus.taile44d0c.ts.net"
                            "/voice-rt?mode=tts"
                        ],
                        "sent": [
                            {"type": "text", "data": "contract"},
                            {
                                "type": "binary",
                                "bytes": [0, 127, 128, 255],
                            },
                        ],
                        "closed": [{"code": 1000, "reason": "done"}],
                    }, ws_background
                    lookup = session.send(
                        "Runtime.evaluate",
                        {
                            "contextId": extension_world,
                            "expression": """new Promise(resolve =>
                              chrome.runtime.sendMessage({
                                type: 'LOOKUP',
                                payload: {
                                  text: 'contract',
                                  context: 'contract context',
                                  url: location.href
                                }
                              }, resolve)
                            )""",
                            "awaitPromise": True,
                            "returnByValue": True,
                        },
                    ).get("result", {}).get("value")
                    assert lookup == {
                        "ok": True,
                        "data": {"ok": True, "word": "contract"},
                    }, lookup
                    voice_upload = session.send(
                        "Runtime.evaluate",
                        {
                            "contextId": extension_world,
                            "expression": """window.__bwReaderFetch(
                              '/api/assistant/voice-clip?id=contract',
                              {
                                method:'POST',
                                headers:{'Content-Type':'audio/webm;codecs=opus'},
                                body:new Blob([
                                  new Uint8Array([0,1,2,127,128,254,255])
                                ],{type:'audio/webm;codecs=opus'})
                              }
                            ).then(r=>r.json())""",
                            "awaitPromise": True,
                            "returnByValue": True,
                        },
                    ).get("result", {}).get("value")
                    assert voice_upload == {
                        "ok": True,
                        "id": "contract",
                    }, voice_upload
                    assert voice_requests == [
                        {
                            "url": (
                                "https://bwicarus.taile44d0c.ts.net"
                                "/api/assistant/voice-clip?id=contract"
                            ),
                            "body": bytes([0, 1, 2, 127, 128, 254, 255]),
                            "content_type": "audio/webm;codecs=opus",
                            "authorized": True,
                        }
                    ], voice_requests
                    rejected_route = session.send(
                        "Runtime.evaluate",
                        {
                            "contextId": extension_world,
                            "expression": """new Promise(resolve => {
                              const id=31001;
                              const port=chrome.runtime.connect({name:'bw-fetch'});
                              port.onMessage.addListener(m => {
                                if(m && m.id===id) {
                                  resolve({type:m.type,code:m.code,error:m.error});
                                  port.disconnect();
                                }
                              });
                              port.postMessage({
                                id,
                                url:'https://bwicarus.taile44d0c.ts.net'
                                  +'/api/assistant/chat?contract-binary=1',
                                init:{
                                  method:'POST',
                                  headers:{'Content-Type':'application/octet-stream'},
                                  bodyB64:'YmxvY2tlZA==',
                                  bodyBytes:7
                                }
                              });
                            })""",
                            "awaitPromise": True,
                            "returnByValue": True,
                        },
                    ).get("result", {}).get("value")
                    assert rejected_route == {
                        "type": "error",
                        "code": "BW_FETCH_BODY",
                        "error": "blocked: binary body not allowed",
                    }, rejected_route
                    rejected_oversize = session.send(
                        "Runtime.evaluate",
                        {
                            "contextId": extension_world,
                            "expression": """new Promise(resolve => {
                              const id=31002;
                              const port=chrome.runtime.connect({name:'bw-fetch'});
                              port.onMessage.addListener(m => {
                                if(m && m.id===id) {
                                  resolve({type:m.type,code:m.code,error:m.error});
                                  port.disconnect();
                                }
                              });
                              port.postMessage({
                                id,
                                url:'https://bwicarus.taile44d0c.ts.net'
                                  +'/api/assistant/voice-clip?id=oversize',
                                init:{
                                  method:'POST',
                                  headers:{'Content-Type':'audio/webm'},
                                  bodyB64:'YQ==',
                                  bodyBytes:8388609
                                }
                              });
                            })""",
                            "awaitPromise": True,
                            "returnByValue": True,
                        },
                    ).get("result", {}).get("value")
                    assert rejected_oversize == {
                        "type": "error",
                        "code": "BW_FETCH_BODY",
                        "error": "blocked: invalid binary request body",
                    }, rejected_oversize
                    assert not blocked_binary_requests, blocked_binary_requests
                    assert len(voice_requests) == 1, voice_requests
                    content_storage = session.send(
                        "Runtime.evaluate",
                        {
                            "contextId": extension_world,
                            "expression": """chrome.storage.local.get(null)
                              .then(value => ({readable:true,keys:Object.keys(value)}))
                              .catch(() => ({readable:false,keys:[]}))""",
                            "awaitPromise": True,
                            "returnByValue": True,
                        },
                    ).get("result", {}).get("value")
                    assert content_storage == {
                        "readable": False,
                        "keys": [],
                    }, content_storage
                    redacted = worker.evaluate(
                        "(key) => chrome.storage.local.get(key).then(v => v[key])",
                        credential_key,
                    )
                    assert redacted["private"] is True
                    assert "candidates" not in redacted
                    private_image = session.send(
                        "Runtime.evaluate",
                        {
                            "contextId": extension_world,
                            "expression": """new Promise(resolve => {
                              const img = document.createElement('img');
                              img.src = '/pdf/api/asset/contract';
                              window.__bwRoot.appendChild(img);
                              const started = Date.now();
                              let repeated = false;
                              const poll = () => {
                                if (img.src.startsWith('blob:') && img.complete) {
                                  if (!repeated) {
                                    repeated = true;
                                    img.setAttribute('src','/pdf/api/asset/contract');
                                    setTimeout(poll, 30);
                                  } else {
                                    resolve({blob:true,width:img.naturalWidth,
                                      repeated:true});
                                  }
                                } else if (Date.now() - started > 4000) {
                                  resolve({blob:img.src.startsWith('blob:'),
                                    width:img.naturalWidth,src:img.src,
                                    repeated});
                                } else setTimeout(poll, 30);
                              };
                              poll();
                            })""",
                            "awaitPromise": True,
                            "returnByValue": True,
                        },
                    ).get("result", {}).get("value")
                    assert private_image == {
                        "blob": True,
                        "width": 1,
                        "repeated": True,
                    }, private_image
                    assert len(asset_requests) == 1, asset_requests
                    assert "proxy=1" in asset_requests[0], asset_requests
                    retry_image_state = session.send(
                        "Runtime.evaluate",
                        {
                            "contextId": extension_world,
                            "expression": """new Promise(resolve => {
                              const img = document.createElement('img');
                              img.src = '/pdf/api/asset/retry';
                              window.__bwRoot.appendChild(img);
                              const started = Date.now();
                              let retried = false;
                              const poll = () => {
                                if (!retried && img.dataset.bwPrivateImageError) {
                                  retried = true;
                                  img.removeAttribute('src');
                                  img.setAttribute('src','/pdf/api/asset/retry');
                                }
                                if (retried && img.src.startsWith('blob:') &&
                                    img.complete) {
                                  resolve({blob:true,width:img.naturalWidth,
                                    retried:true});
                                } else if (Date.now() - started > 4000) {
                                  resolve({blob:img.src.startsWith('blob:'),
                                    width:img.naturalWidth,retried,
                                    error:img.dataset.bwPrivateImageError||''});
                                } else setTimeout(poll, 30);
                              };
                              poll();
                            })""",
                            "awaitPromise": True,
                            "returnByValue": True,
                        },
                    ).get("result", {}).get("value")
                    assert retry_image_state == {
                        "blob": True,
                        "width": 1,
                        "retried": True,
                    }, retry_image_state
                    assert retry_requests["count"] == 2
                    assert all(
                        "proxy=1" in url for url in retry_requests["urls"]
                    ), retry_requests

                    session.send(
                        "Runtime.evaluate",
                        {
                            "contextId": extension_world,
                            "expression": (
                                "localStorage.setItem("
                                "'eph-gp-floating','1')"
                            ),
                        },
                    )
                    page.wait_for_timeout(350)
                    saved_settings = worker.evaluate(
                        "() => chrome.storage.local.get("
                        "'bwReaderExtensionPreferencesV2')"
                    )
                    assert (
                        saved_settings["bwReaderExtensionPreferencesV2"]
                        ["values"]["eph-gp-floating"]
                        == "1"
                    ), saved_settings
                    second = context.new_page()
                    second.goto(
                        f"http://localhost:{server.server_port}"
                        "/extensions/bw-reader-webext/README.md",
                        wait_until="domcontentloaded",
                    )
                    second.wait_for_selector(
                        "#bw-reader-host", state="attached", timeout=15_000
                    )
                    second.wait_for_function(
                        "() => localStorage.getItem('eph-gp-floating') === '1'",
                        timeout=5_000,
                    )
                    print(
                        "OK: full web runtime, persistent-account network, cross-site settings"
                    )
                finally:
                    context.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    main()
