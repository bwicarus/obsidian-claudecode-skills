#!/usr/bin/env python3
"""在真实 Chromium IndexedDB 中运行 DataStore 浏览器契约。"""

from __future__ import annotations

from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[2]
PAGE = "/tests/reader_contract/indexeddb-store.browser.html"


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *args: object) -> None:
        return


def main() -> int:
    handler = partial(QuietHandler, directory=str(ROOT))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    errors: list[str] = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.on(
                    "console",
                    lambda message: errors.append(message.text)
                    if message.type == "error"
                    else None,
                )
                page.goto(f"http://127.0.0.1:{server.server_port}{PAGE}")
                page.wait_for_function(
                    "window.__BW_IDB_TEST_RESULT__ !== undefined",
                    timeout=30_000,
                )
                result = page.evaluate("window.__BW_IDB_TEST_RESULT__")
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    if not result.get("passed"):
        if errors:
            print("\n".join(errors))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    print(f"IndexedDB browser contract: {result['assertions']} assertions passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
