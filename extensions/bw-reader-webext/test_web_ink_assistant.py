#!/usr/bin/env python3
"""Browser contract: ordinary-web ink reaches the shared assistant vision path."""

from __future__ import annotations

import base64
from io import BytesIO
import pathlib
import tempfile

from PIL import Image
from playwright.sync_api import sync_playwright


EXT = pathlib.Path(__file__).resolve().parent
CHROME = pathlib.Path.home() / ".cache/ms-playwright/chromium-1223/chrome-linux/chrome"
URL = "http://ink-assistant.test/article"

HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Shared ink vision</title>
<style>
html,body{margin:0;width:100%;min-height:1800px;background:#fff;color:#111}
#target{position:absolute;left:280px;top:260px;width:430px;height:300px;
  background:rgb(24,104,216);color:#fff;font:32px/300px sans-serif;text-align:center}
p{position:absolute;left:240px;top:600px;width:560px;font:24px/1.6 sans-serif}
</style></head><body>
<div id="target">WEB CONTENT</div>
<p>The red annotation around the blue panel must be visible to the shared
reader screenshot pipeline.</p>
</body></html>"""


def pen_event(session, event_type: str, x: float, y: float, *, pressed: bool) -> None:
    session.send("Input.dispatchMouseEvent", {
        "type": event_type,
        "x": x,
        "y": y,
        "button": "left" if pressed or event_type == "mouseReleased" else "none",
        "buttons": 1 if pressed else 0,
        "clickCount": 1 if event_type != "mouseMoved" else 0,
        "force": 0.65 if pressed else 0,
        "pointerType": "pen",
    })


def draw_pen_stroke(session, points: list[tuple[float, float]]) -> None:
    first, *middle, last = points
    pen_event(session, "mouseMoved", *first, pressed=False)
    pen_event(session, "mousePressed", *first, pressed=True)
    for point in middle:
        pen_event(session, "mouseMoved", *point, pressed=True)
    pen_event(session, "mouseMoved", *last, pressed=True)
    pen_event(session, "mouseReleased", *last, pressed=False)


def extension_world(session, contexts: list[dict]) -> int:
    for context in contexts:
        try:
            value = session.send("Runtime.evaluate", {
                "contextId": context["id"],
                "expression": "!!(window.__webAdapter && window.__bwWebInk)",
                "returnByValue": True,
            }).get("result", {}).get("value")
            if value:
                return context["id"]
        except Exception:
            continue
    raise AssertionError("extension isolated world not found")


def evaluate(session, context_id: int, expression: str, *, await_promise: bool = False):
    return session.send("Runtime.evaluate", {
        "contextId": context_id,
        "expression": expression,
        "awaitPromise": await_promise,
        "returnByValue": True,
    }).get("result", {}).get("value")


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="bw-web-ink-assistant-") as profile:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                profile,
                executable_path=str(CHROME),
                headless=False,
                viewport={"width": 1000, "height": 800},
                args=[
                    f"--disable-extensions-except={EXT}",
                    f"--load-extension={EXT}",
                    "--no-sandbox",
                ],
            )
            try:
                context.route(
                    URL,
                    lambda route: route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body=HTML,
                    ),
                )
                page = context.new_page()
                session = context.new_cdp_session(page)
                contexts: list[dict] = []
                session.on(
                    "Runtime.executionContextCreated",
                    lambda event: contexts.append(event["context"]),
                )
                session.send("Runtime.enable")
                page.goto(URL, wait_until="domcontentloaded")
                page.wait_for_selector("#bw-reader-host", state="attached", timeout=15_000)
                page.wait_for_function(
                    """() => document.querySelector('#bw-reader-pins')?.shadowRoot
                      ?.querySelector('.bw-ink-document')"""
                )
                page.wait_for_timeout(250)
                world = extension_world(session, contexts)

                empty = evaluate(
                    session,
                    world,
                    """(() => {
                      const c=window.__webAdapter.getContext();
                      return {
                        page_type:c.page_type,file:c.file,file_rel:c.file_rel,
                        page:c.page,pages:c.pages,total:c.total,
                        has_ink:c.has_ink,want_viewshot:c.want_viewshot,
                        customVoiceLog:Object.prototype.hasOwnProperty.call(
                          window.__webAdapter._host.asst,'voiceLog'),
                        wsBridge:typeof window.__bwReaderOpenWebSocket==='function',
                        ws:window.__bwReaderWsUrl('/voice-rt?mode=rtc&probe=1')
                      };
                    })()""",
                )
                assert empty == {
                    "page_type": "web",
                    "file": f"web:{URL}",
                    "file_rel": f"web:{URL}",
                    "page": 1,
                    "pages": [1],
                    "total": 1,
                    "has_ink": False,
                    "want_viewshot": False,
                    "customVoiceLog": False,
                    "wsBridge": True,
                    "ws": (
                        "wss://bwicarus.taile44d0c.ts.net"
                        "/voice-rt?mode=rtc&probe=1"
                    ),
                }, empty

                points = [
                    (250, 240), (360, 210), (520, 205), (690, 230),
                    (745, 350), (735, 500), (660, 585), (500, 610),
                    (340, 590), (235, 520), (215, 380), (250, 240),
                ]
                draw_pen_stroke(session, points)
                page.wait_for_function(
                    """() => document.querySelector('#bw-reader-pins').shadowRoot
                      .querySelectorAll('.bw-ink-document path').length === 1"""
                )

                result = evaluate(
                    session,
                    world,
                    """(async () => {
                      const c=window.__webAdapter.getContext();
                      const surface=window.__webAdapter.getVisualSurface();
                      const shot=await window.__webAdapter.captureShot();
                      return {
                        has_ink:c.has_ink,want_viewshot:c.want_viewshot,
                        ink:c.ink,surfaceWidth:surface.width,
                        surfaceHeight:surface.height,b64:shot&&shot.b64||''
                      };
                    })()""",
                    await_promise=True,
                )
                assert result["has_ink"] is True, result
                assert result["want_viewshot"] is True, result
                assert len(result["ink"]) == 1, result["ink"]
                stroke = result["ink"][0]
                assert set(stroke) == {"t", "c", "w", "p"}, stroke
                assert stroke["t"] == "pen" and stroke["c"] == "#ef4444", stroke
                assert all(
                    0 <= coordinate <= 1
                    for point in stroke["p"]
                    for coordinate in point
                ), stroke
                assert result["surfaceWidth"] >= 1000
                assert result["surfaceHeight"] >= 1800
                assert len(result["b64"]) > 3000

                image = Image.open(BytesIO(base64.b64decode(result["b64"]))).convert("RGB")
                pixels = list(image.getdata())
                red = sum(1 for r, g, b in pixels if r > 170 and g < 115 and b < 115)
                blue = sum(1 for r, g, b in pixels if b > 140 and b > r * 1.35 and b > g * 1.2)
                assert red > 120, {"red_pixels": red, "size": image.size}
                assert blue > 1000, {"blue_pixels": blue, "size": image.size}
                print(
                    "OK: WebAdapter canonical context, shared WS origin, "
                    "shared crop and webpage+ink composite pass"
                )
            finally:
                context.close()


if __name__ == "__main__":
    main()
