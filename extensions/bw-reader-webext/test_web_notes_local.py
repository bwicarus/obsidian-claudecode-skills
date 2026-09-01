#!/usr/bin/env python3
"""Real-Chromium regression for ordinary-web document notes.

This deliberately uses a disposable persistent profile, the unpacked extension,
its real MV3 service worker, real Ports and the background IndexedDB vault.  The
only test fixture is the same ``provider-ticket`` shaped verified-account record
used by ``test_smoke.py``; production authentication checks are not bypassed.

Covered invariants:

* creating a note never calls the legacy ``/pdf/api/notes`` endpoint;
* notes survive a page reload and a complete browser restart;
* a same-document second tab receives CHANGE without reloading;
* a tombstoned note never reappears after reload/restart;
* SPA URL changes select an isolated document and returning restores the first;
* the extension does not swallow ordinary host-page clicks.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile
import time
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page, sync_playwright


EXT = Path(
    os.environ.get(
        "BW_EXTENSION_ROOT",
        str(Path(__file__).resolve().parent),
    )
).resolve()
from browser_exe import CHROME
ALPHA_URL = "http://web-notes-contract.test/alpha?view=1"
BETA_URL = "http://web-notes-contract.test/beta?view=2"
NAMESPACE = "acct-v1-" + "b" * 64


PAGE_HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Ordinary web note contract</title>
  <style>
    html, body { margin: 0; min-height: 100%; font: 16px/1.5 sans-serif; }
    #host-button {
      position: relative;
      z-index: 1;
      margin: 24px;
      width: 180px;
      height: 52px;
    }
    #article {
      box-sizing: border-box;
      width: min(900px, calc(100vw - 80px));
      min-height: 1800px;
      margin: 16px auto;
      padding: 48px;
      background: #f4f6f8;
    }
  </style>
</head>
<body>
  <button id="host-button" type="button">Host page button</button>
  <article id="article">
    <h1>Ordinary page content</h1>
    <p>
      This article deliberately covers the viewport center so the public
      __bwWebNotes.create() path can resolve a real DOM anchor.
    </p>
  </article>
  <script>
    window.__hostClicks = 0;
    document.getElementById('host-button').addEventListener('click', () => {
      window.__hostClicks += 1;
      document.body.dataset.hostClicks = String(window.__hostClicks);
    });
  </script>
</body>
</html>"""


class ExtensionWorld:
    """Small CDP bridge into this page's extension isolated world."""

    def __init__(self, page: Page) -> None:
        self.page = page
        self.session: Any = None
        self.context_ids: list[int] = []
        self.extension_context_id: int | None = None
        self._attach_session()

    def _attach_session(self) -> None:
        self.session = self.page.context.new_cdp_session(self.page)
        self.session.on(
            "Runtime.executionContextCreated",
            lambda event: self._remember(event["context"]["id"]),
        )
        self.session.on(
            "Runtime.executionContextDestroyed",
            lambda event: self._forget(event["executionContextId"]),
        )
        self.session.on("Runtime.executionContextsCleared", lambda _event: self._clear())
        self.session.send("Runtime.enable")

    def _reattach_session(self) -> None:
        try:
            self.session.detach()
        except Exception:
            pass
        self._clear()
        self._attach_session()

    def _remember(self, context_id: int) -> None:
        if context_id not in self.context_ids:
            self.context_ids.append(context_id)

    def _forget(self, context_id: int) -> None:
        if context_id in self.context_ids:
            self.context_ids.remove(context_id)
        if self.extension_context_id == context_id:
            self.extension_context_id = None

    def _clear(self) -> None:
        self.context_ids.clear()
        self.extension_context_id = None

    def _raw_evaluate(
        self, context_id: int, expression: str, *, await_promise: bool
    ) -> Any:
        response = self.session.send(
            "Runtime.evaluate",
            {
                "contextId": context_id,
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": True,
            },
        )
        if response.get("exceptionDetails"):
            details = response["exceptionDetails"]
            remote = response.get("result", {})
            description = remote.get("description") or details.get("text")
            raise RuntimeError(str(description or "isolated-world evaluation failed"))
        return response.get("result", {}).get("value")

    def _find(self, timeout_ms: int = 15_000) -> int:
        deadline = time.monotonic() + timeout_ms / 1000
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self.extension_context_id in self.context_ids:
                return int(self.extension_context_id)
            for context_id in reversed(self.context_ids):
                try:
                    ready = self._raw_evaluate(
                        context_id,
                        """!!(
                          window.__bwDocumentNotes &&
                          window.__bwWebNotes &&
                          window.RC?.stickynote
                        )""",
                        await_promise=False,
                    )
                    if ready:
                        self.extension_context_id = context_id
                        return context_id
                except Exception as error:  # context may be destroyed mid-navigation
                    last_error = error
            time.sleep(0.05)
        probes: list[dict[str, Any]] = []
        for context_id in list(self.context_ids):
            try:
                probes.append({
                    "id": context_id,
                    "value": self._raw_evaluate(
                        context_id,
                        """({
                          chromeRuntime: !!globalThis.chrome?.runtime?.id,
                          documentNotes: typeof window.__bwDocumentNotes,
                          webNotes: typeof window.__bwWebNotes,
                          stickynote: typeof window.RC?.stickynote,
                          href: String(location.href)
                        })""",
                        await_promise=False,
                    ),
                })
            except Exception as probe_error:
                probes.append({"id": context_id, "error": str(probe_error)})
        raise AssertionError(
            "extension isolated world did not become ready"
            + f" (url={self.page.url!r}, contexts={self.context_ids!r}, "
              f"probes={probes!r})"
            + (f": {last_error}" if last_error else "")
        )

    def evaluate(
        self, expression: str, *, await_promise: bool = True, timeout_ms: int = 15_000
    ) -> Any:
        last_error: Exception | None = None
        deadline = time.monotonic() + timeout_ms / 1000
        reattached = False
        while time.monotonic() < deadline:
            try:
                context_id = self._find(
                    min(
                        1_500,
                        max(100, int((deadline - time.monotonic()) * 1000)),
                    )
                )
            except AssertionError as error:
                last_error = error
                if reattached or self.page.is_closed():
                    break
                # Chromium may recycle an isolated execution context after the
                # first extension DOM mutation. A fresh CDP session receives
                # Runtime.executionContextCreated for all currently live
                # worlds and cannot hide a genuinely missing content script.
                reattached = True
                self._reattach_session()
                continue
            try:
                return self._raw_evaluate(
                    context_id, expression, await_promise=await_promise
                )
            except Exception as error:
                last_error = error
                self._forget(context_id)
                time.sleep(0.05)
        raise AssertionError(f"isolated-world evaluation failed: {last_error}")

    def wait_for(
        self, expression: str, *, timeout_ms: int = 15_000, label: str = "condition"
    ) -> None:
        deadline = time.monotonic() + timeout_ms / 1000
        last_value: Any = None
        while time.monotonic() < deadline:
            try:
                last_value = self.evaluate(
                    expression,
                    await_promise=True,
                    timeout_ms=max(100, int((deadline - time.monotonic()) * 1000)),
                )
                if last_value:
                    return
            except Exception as error:
                last_value = str(error)
            time.sleep(0.08)
        raise AssertionError(f"timed out waiting for {label}: {last_value!r}")


def launch_context(playwright: Any, profile: str) -> tuple[BrowserContext, list[str]]:
    note_http_requests: list[str] = []
    context = playwright.chromium.launch_persistent_context(
        profile,
        executable_path=str(CHROME),
        headless=False,
        viewport={"width": 1280, "height": 800},
        args=[
            f"--disable-extensions-except={EXT}",
            f"--load-extension={EXT}",
            "--no-sandbox",
        ],
    )

    def record_request(request: Any) -> None:
        if urlparse(request.url).path == "/pdf/api/notes":
            note_http_requests.append(request.url)

    def reject_legacy_notes(route: Any) -> None:
        note_http_requests.append(route.request.url)
        route.fulfill(
            status=418,
            content_type="application/json",
            body='{"ok":false,"error":"legacy notes HTTP is forbidden in this test"}',
        )

    context.on("request", record_request)
    context.route("**/pdf/api/notes**", reject_legacy_notes)
    context.route(
        "http://web-notes-contract.test/**",
        lambda route: route.fulfill(
            status=200,
            content_type="text/html; charset=utf-8",
            body=PAGE_HTML,
        ),
    )
    return context, note_http_requests


def service_worker(context: BrowserContext) -> Any:
    workers = context.service_workers
    return workers[0] if workers else context.wait_for_event(
        "serviceworker", timeout=15_000
    )


def install_background_network_probe(worker: Any) -> None:
    # Context request events normally include service-worker fetches.  This
    # second, test-only observer makes the negative assertion explicit even on
    # Playwright builds that do not surface worker network events consistently.
    worker.evaluate(
        """() => {
          if (globalThis.__bwLegacyNotesNetworkProbe) return;
          const original = globalThis.fetch.bind(globalThis);
          const calls = [];
          globalThis.__bwLegacyNotesNetworkProbe = calls;
          globalThis.fetch = (input, init) => {
            const raw = typeof input === 'string' ? input : input?.url;
            try {
              if (new URL(String(raw || ''), location.href).pathname
                    === '/pdf/api/notes') {
                calls.push(String(raw));
              }
            } catch (_) {}
            return original(input, init);
          };
        }"""
    )


def background_note_calls(worker: Any) -> list[str]:
    return worker.evaluate(
        "() => (globalThis.__bwLegacyNotesNetworkProbe || []).slice()"
    )


def seed_verified_account(worker: Any) -> None:
    # This is intentionally the production record shape, not a test backdoor.
    worker.evaluate(
        """record => chrome.storage.local.set({
          readerActiveVerifiedAccountV1: record
        })""",
        {
            "schema": 1,
            "namespace": NAMESPACE,
            "verifiedAt": 1,
            "source": "provider-ticket",
        },
    )


def open_page(context: BrowserContext, url: str = ALPHA_URL) -> tuple[Page, ExtensionWorld]:
    page = context.new_page()
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    world = ExtensionWorld(page)
    page.goto(url, wait_until="domcontentloaded")
    page.wait_for_selector("#bw-reader-host", state="attached", timeout=15_000)
    world.wait_for(
        """window.__bwWebNotes.ready.then(
          ok => !!ok && !!window.__bwDocumentNotes
        )""",
        label="web notes READY",
    )
    assert not page_errors, page_errors
    return page, world


def note_state(world: ExtensionWorld) -> dict[str, Any]:
    return world.evaluate(
        """(() => {
          const notes = window.RC.stickynote.notes().map(note => ({
            noteId: String(note.noteId || note.id || ''),
            rev: Number(note.rev) || 0,
            text: String(note.text || ''),
            deleted: note.deleted === true,
            documentId: String(note.documentId || '')
          }));
          return {
            url: location.href,
            notes,
            domCount: window.__bwPinRoot
              ?.querySelectorAll('.rc-note').length || 0,
            domTexts: [...(window.__bwPinRoot
              ?.querySelectorAll('.rc-note-text') || [])]
              .map(element => element.value)
          };
        })()""",
        await_promise=False,
    )


def wait_note_count(world: ExtensionWorld, count: int, label: str) -> None:
    try:
        world.wait_for(
            f"""(() => {{
              const notes = window.RC.stickynote.notes();
              const dom = window.__bwPinRoot
                ?.querySelectorAll('.rc-note').length || 0;
              return notes.length === {count} && dom === {count};
            }})()""",
            label=label,
        )
    except AssertionError as error:
        diagnostic = world.evaluate(
            """(async () => ({
              identity: await window.__bwDocumentNotes.identity()
                .catch(error => ({ error: String(error?.message || error),
                                   code: String(error?.code || '') })),
              stored: await window.__bwDocumentNotes.list({
                includeDeleted: true, offset: 0, limit: 20
              }).catch(error => ({ error: String(error?.message || error),
                                   code: String(error?.code || '') })),
              projected: window.RC.stickynote.notes().map(note => ({
                noteId: note.noteId || note.id,
                rev: note.rev,
                deleted: note.deleted === true
              })),
              domCount: window.__bwPinRoot
                ?.querySelectorAll('.rc-note').length || 0,
              centerAnchor: window.__bwWebPins?.anchorAt(
                innerWidth / 2, innerHeight / 2
              ) || null,
              toasts: (window.__bwWebNotesTestToasts || []).slice(),
              createAtCenterCalls:
                Number(window.__bwWebNotesCreateAtCenterCalls || 0),
              uiText: String(window.__bwReaderDoc?.body?.innerText || '')
                .slice(-500)
            }))()"""
        )
        raise AssertionError(f"{error}; diagnostic={diagnostic!r}") from error


def note_locator(page: Page) -> Any:
    return page.locator("#bw-reader-pins").locator(".rc-note")


def wait_page_note_count(page: Page, count: int, label: str) -> None:
    try:
        page.wait_for_function(
            """expected => {
              const host = document.getElementById('bw-reader-pins');
              return (host?.shadowRoot?.querySelectorAll('.rc-note').length || 0)
                === expected;
            }""",
            arg=count,
            timeout=15_000,
        )
    except Exception as error:
        raise AssertionError(
            f"timed out waiting for {label}: "
            f"url={page.url!r}, count={note_locator(page).count()}"
        ) from error


def page_note_texts(page: Page) -> list[str]:
    textareas = note_locator(page).locator(".rc-note-text")
    return [textareas.nth(index).input_value() for index in range(textareas.count())]


def create_note_from_toolbar(page: Page) -> None:
    host = page.locator("#bw-reader-host")
    pill = host.locator("#bw-top-pill")
    if pill.get_attribute("data-collapsed") == "1":
        pill.click()
        page.wait_for_function(
            """() => document.getElementById('bw-reader-host')
              ?.shadowRoot?.getElementById('bw-top-pill')
              ?.getAttribute('data-collapsed') === '0'"""
        )
    host.locator("#bw-note-btn").click()


def patch_first_note_from_ui(page: Page, text: str) -> None:
    textarea = note_locator(page).first.locator(".rc-note-text")
    textarea.fill(text)
    textarea.evaluate("element => element.blur()")


def remove_first_note_from_ui(page: Page) -> None:
    page.once("dialog", lambda dialog: dialog.accept())
    note_locator(page).first.locator(".rc-note-del").dispatch_event("click")


def patch_first_note(world: ExtensionWorld, text: str) -> dict[str, Any]:
    return world.evaluate(
        f"""(async () => {{
          const note = window.RC.stickynote.notes()[0];
          if (!note) throw new Error('no note to patch');
          return window.__bwDocumentNotes.patch(
            String(note.noteId || note.id),
            {{ text: {text!r} }},
            {{
              ifRev: Number(note.rev) || 0,
              mutationId: 'contract-patch:' + crypto.randomUUID()
            }}
          );
        }})()"""
    )


def remove_first_note(world: ExtensionWorld) -> dict[str, Any]:
    return world.evaluate(
        """(async () => {
          const note = window.RC.stickynote.notes()[0];
          if (!note) throw new Error('no note to remove');
          return window.__bwDocumentNotes.remove(
            String(note.noteId || note.id),
            {
              ifRev: Number(note.rev) || 0,
              mutationId: 'contract-remove:' + crypto.randomUUID()
            }
          );
        })()"""
    )


def assert_no_legacy_http(
    requests: list[str], worker: Any, checkpoint: str
) -> None:
    worker_calls = background_note_calls(worker)
    assert not requests, {
        "checkpoint": checkpoint,
        "playwright_requests": requests,
    }
    assert not worker_calls, {
        "checkpoint": checkpoint,
        "background_fetch_calls": worker_calls,
    }


def main() -> None:
    assert EXT.is_dir(), EXT
    assert CHROME.is_file(), CHROME

    all_note_http_requests: list[str] = []
    with tempfile.TemporaryDirectory(prefix="bw-web-notes-local-") as profile:
        with sync_playwright() as playwright:
            # First browser lifetime: create through the real web-note UI entry,
            # patch through its public repository facade, then prove reload.
            context, note_http_requests = launch_context(playwright, profile)
            all_note_http_requests.extend(note_http_requests)
            try:
                worker = service_worker(context)
                install_background_network_probe(worker)
                seed_verified_account(worker)
                page, world = open_page(context)

                # Playwright's click performs hit-target checks, so this proves
                # the extension does not merely dispatch a synthetic DOM event.
                page.locator("#host-button").click()
                assert page.evaluate("() => window.__hostClicks") == 1

                create_note_from_toolbar(page)
                wait_page_note_count(page, 1, "new ordinary-web note")
                patch_first_note_from_ui(page, "local note survives reload")
                page.wait_for_function(
                    """() => document.getElementById('bw-reader-pins')
                      ?.shadowRoot?.querySelector('.rc-note-text')?.value
                        === 'local note survives reload'"""
                )
                page.wait_for_timeout(250)
                assert_no_legacy_http(note_http_requests, worker, "create/patch")

                page.reload(wait_until="domcontentloaded")
                page.wait_for_selector("#bw-reader-host", state="attached", timeout=15_000)
                wait_page_note_count(page, 1, "note restored after page reload")
                assert page_note_texts(page) == ["local note survives reload"]
                assert_no_legacy_http(note_http_requests, worker, "page reload")
            finally:
                context.close()

            # Second browser lifetime, same disposable profile: verify actual
            # IndexedDB/profile persistence, then cross-tab and SPA behavior.
            context, restarted_requests = launch_context(playwright, profile)
            all_note_http_requests.extend(restarted_requests)
            try:
                worker = service_worker(context)
                install_background_network_probe(worker)
                page_a, world_a = open_page(context)
                wait_page_note_count(page_a, 1, "note restored after browser restart")
                assert page_note_texts(page_a) == ["local note survives reload"]

                page_b, world_b = open_page(context)
                wait_page_note_count(
                    page_b, 1, "same-document second tab initial list"
                )
                patch_first_note_from_ui(page_a, "cross-tab CHANGE arrived")
                page_b.wait_for_function(
                    """() => document.getElementById('bw-reader-pins')
                      ?.shadowRoot?.querySelector('.rc-note-text')?.value
                        === 'cross-tab CHANGE arrived'"""
                )

                # Main-world history is the real SPA case: an extension
                # isolated-world monkeypatch cannot intercept this call.
                page_a.evaluate(
                    """url => {
                      history.pushState({}, '', url);
                      document.getElementById('article')
                        ?.setAttribute('data-route', 'beta');
                    }""",
                    BETA_URL,
                )
                wait_page_note_count(page_a, 0, "isolated SPA document")
                # The other tab is still on alpha and retains its note.
                assert page_b.url == ALPHA_URL
                assert page_note_texts(page_b) == ["cross-tab CHANGE arrived"]

                create_note_from_toolbar(page_a)
                wait_note_count(world_a, 1, "beta document note")
                patch_first_note_from_ui(page_a, "beta-only note")
                assert page_note_texts(page_a) == ["beta-only note"]
                assert page_note_texts(page_b) == ["cross-tab CHANGE arrived"]
                page_a.wait_for_timeout(250)

                page_a.evaluate(
                    """url => {
                      history.pushState({}, '', url);
                      document.getElementById('article')
                        ?.setAttribute('data-route', 'alpha');
                    }""",
                    ALPHA_URL,
                )
                wait_page_note_count(
                    page_a, 1, "alpha document restored after SPA return"
                )
                page_a.wait_for_function(
                    """() => document.getElementById('bw-reader-pins')
                      ?.shadowRoot?.querySelector('.rc-note-text')?.value
                        === 'cross-tab CHANGE arrived'"""
                )

                remove_first_note_from_ui(page_b)
                wait_page_note_count(page_b, 0, "alpha tombstone in deleting tab")
                wait_page_note_count(page_a, 0, "alpha tombstone CHANGE in peer tab")
                # Exercise the LIST reconciliation path after the tombstone.
                page_a.reload(wait_until="domcontentloaded")
                wait_page_note_count(page_a, 0, "alpha list after tombstone")
                assert_no_legacy_http(restarted_requests, worker, "sync/SPA/delete")
            finally:
                context.close()

            # Third browser lifetime: the deleted alpha note must remain absent.
            context, final_requests = launch_context(playwright, profile)
            all_note_http_requests.extend(final_requests)
            try:
                worker = service_worker(context)
                install_background_network_probe(worker)
                _page, world = open_page(context)
                wait_note_count(world, 0, "tombstone survives browser restart")
                listed = world.evaluate(
                    """window.__bwDocumentNotes.list({
                      includeDeleted: true, offset: 0, limit: 20
                    })"""
                )
                listed_notes = (
                    listed
                    if isinstance(listed, list)
                    else listed.get("notes", [])
                )
                alpha_tombstones = [
                    note for note in listed_notes
                    if note.get("deleted") is True
                ]
                assert len(alpha_tombstones) == 1, listed
                assert_no_legacy_http(final_requests, worker, "final restart")
            finally:
                context.close()

    assert not all_note_http_requests, all_note_http_requests
    print(
        "PASS: ordinary-web notes use real Port + IndexedDB locally; "
        "reload/restart, CHANGE, tombstone, SPA isolation and host clicks verified"
    )


if __name__ == "__main__":
    main()
