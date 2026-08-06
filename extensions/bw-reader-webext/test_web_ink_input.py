#!/usr/bin/env python3
"""Chromium contract for bounded-hybrid, session-only ordinary-web ink.

CDP's synthetic ``pointerType='pen'`` checks the JavaScript ownership model.
It cannot exercise Windows' hardware palm rejection or direct-manipulation
compositor, so a physical Surface/Wacom pass remains part of release QA.
"""

from __future__ import annotations

import pathlib
import tempfile

from playwright.sync_api import sync_playwright


EXT = pathlib.Path(__file__).resolve().parent
CHROME = pathlib.Path.home() / ".cache/ms-playwright/chromium-1223/chrome-linux/chrome"
URL = "http://ink-input.test/"

HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Ink input contract</title>
<style>
html,body{margin:0;min-height:6200px;background:#fff;color:#111}
#host-button{position:fixed;left:20px;top:92px;width:120px;height:52px;z-index:2}
#article{margin:180px auto 0;width:760px;font:20px/1.8 sans-serif}
</style></head><body>
<button id="host-button">host button</button>
<main id="article">Ordinary web content</main>
<script>
window.hostClicks = 0;
localStorage.setItem('webInkV1', 'legacy-web-ink-must-remain');
document.getElementById('host-button').addEventListener('click', () => {
  window.hostClicks += 1;
});
</script></body></html>"""


def touch(session, event_type: str, x: float | None = None,
          y: float | None = None) -> None:
    points = []
    if x is not None and y is not None:
        points = [{
            "x": x,
            "y": y,
            "id": 1,
            "radiusX": 2,
            "radiusY": 2,
            "force": 1,
        }]
    session.send("Input.dispatchTouchEvent", {
        "type": event_type,
        "touchPoints": points,
    })


def swipe_up(session, x: float, start_y: float, end_y: float) -> None:
    touch(session, "touchStart", x, start_y)
    distance = start_y - end_y
    for fraction in (0.18, 0.38, 0.6, 0.8, 1):
        touch(session, "touchMove", x, start_y - distance * fraction)
    touch(session, "touchEnd")


def pen_event(session, event_type: str, x: float, y: float,
              *, pressed: bool) -> None:
    session.send("Input.dispatchMouseEvent", {
        "type": event_type,
        "x": x,
        "y": y,
        "button": "left" if pressed or event_type == "mouseReleased" else "none",
        "buttons": 1 if pressed else 0,
        "clickCount": 1 if event_type != "mouseMoved" else 0,
        "force": 0.6 if pressed else 0,
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


def ink_path_count(page) -> int:
    return page.evaluate(
        """() => document.querySelector('#bw-reader-pins')?.shadowRoot
          ?.querySelectorAll('.bw-ink-document path').length || 0"""
    )


def settle_at(page, y: int) -> None:
    """Let compositor fling finish before measuring a pen-only gesture."""
    page.wait_for_timeout(2_500)
    page.evaluate("value => scrollTo(0, value)", y)
    page.wait_for_function(
        "value => Math.abs(scrollY - value) < 4",
        arg=y,
    )


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="bw-web-ink-input-") as profile:
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
                page.goto(URL, wait_until="domcontentloaded")
                page.wait_for_selector("#bw-reader-host", state="attached",
                                       timeout=15_000)
                page.wait_for_function(
                    """() => !!document.querySelector('#bw-reader-host')
                      ?.shadowRoot?.querySelector('.bw-ink-canvas')"""
                )

                # The display canvas never participates in input. Windows requires
                # touch-action:none before pen contact, so the only hit-testable ink
                # surface is a bounded 32px hotspot that starts disarmed.
                surface = page.evaluate(
                    """() => {
                      const r=document.querySelector('#bw-reader-host').shadowRoot;
                      const canvas=r.querySelector('.bw-ink-canvas');
                      const shield=r.querySelector('.bw-ink-shield');
                      return {
                        shieldCount:r.querySelectorAll('.bw-ink-shield').length,
                        canvasPointerEvents:getComputedStyle(canvas).pointerEvents,
                        canvasTouchAction:getComputedStyle(canvas).touchAction,
                        shieldPointerEvents:getComputedStyle(shield).pointerEvents,
                        shieldTouchAction:getComputedStyle(shield).touchAction,
                        shieldWidth:getComputedStyle(shield).width,
                        shieldHeight:getComputedStyle(shield).height,
                        shieldShown:shield.classList.contains('show')
                      };
                    }"""
                )
                assert surface == {
                    "shieldCount": 1,
                    "canvasPointerEvents": "none",
                    "canvasTouchAction": "auto",
                    "shieldPointerEvents": "auto",
                    "shieldTouchAction": "none",
                    "shieldWidth": "32px",
                    "shieldHeight": "32px",
                    "shieldShown": False,
                }, surface

                # Show the tool palette. Active pens draw regardless of this desktop
                # mouse-mode toggle, but the eraser button is needed later.
                page.evaluate(
                    """() => document.querySelector('#bw-reader-host').shadowRoot
                      .querySelector('#bw-ink-btn').click()"""
                )
                page.wait_for_function(
                    """() => document.querySelector('#bw-reader-host').shadowRoot
                      .querySelector('#bw-ink-btn').classList.contains('active')"""
                )

                # Palette settings must be captured by the next stroke rather than
                # only changing the visible controls.  Safari may commit its native
                # color/range controls with ``change`` only, so exercise that exact
                # path instead of Chromium's usual continuous ``input`` event.
                palette_state = page.evaluate(
                    """() => {
                      const root=document.querySelector('#bw-reader-host').shadowRoot;
                      const color=root.querySelector('.bw-ink-tools input[type=color]');
                      const width=root.querySelector('.bw-ink-tools input[type=range]');
                      color.value='#007aff';
                      color.dispatchEvent(new Event('change',{bubbles:true}));
                      width.value='9';
                      width.dispatchEvent(new Event('change',{bubbles:true}));
                      return {color:color.value,width:Number(width.value)};
                    }"""
                )
                assert palette_state["color"] == "#007aff", palette_state
                assert palette_state["width"] == 9, palette_state

                session = context.new_cdp_session(page)
                session.send("Emulation.setTouchEmulationEnabled", {
                    "enabled": True,
                    "maxTouchPoints": 5,
                })

                # Baseline: extension ink hosts do not intercept ordinary touch.
                before_touch = page.evaluate("() => scrollY")
                swipe_up(session, 500, 650, 260)
                page.wait_for_function(
                    "before => scrollY > before + 180",
                    arg=before_touch,
                    timeout=5_000,
                )
                settle_at(page, 1200)

                # Pointer capture belongs to the tiny pre-hit surface and only to
                # the pen's pointerId. It must be released after the stroke.
                page.evaluate(
                    """() => {
                      const el=document.querySelector('#bw-reader-host').shadowRoot
                        .querySelector('.bw-ink-shield');
                      el.dataset.captureGot='0';el.dataset.captureLost='0';
                      el.addEventListener('gotpointercapture',(event)=>{
                        if(event.pointerType!=='pen') return;
                        el.dataset.captureGot=String(
                          Number(el.dataset.captureGot||0)+1);
                      });
                      el.addEventListener('lostpointercapture',(event)=>{
                        if(event.pointerType!=='pen') return;
                        el.dataset.captureLost=String(
                          Number(el.dataset.captureLost||0)+1);
                      });
                    }"""
                )
                pen_event(session, "mouseMoved", 500, 500, pressed=False)
                page.wait_for_function(
                    """() => document.querySelector('#bw-reader-host').shadowRoot
                      .querySelector('.bw-ink-shield').classList.contains('show')"""
                )
                hotspot = page.evaluate(
                    """() => {
                      const el=document.querySelector('#bw-reader-host').shadowRoot
                        .querySelector('.bw-ink-shield');
                      const b=el.getBoundingClientRect();
                      return {width:b.width,height:b.height,left:b.left,top:b.top,
                        right:b.right,bottom:b.bottom};
                    }"""
                )
                assert hotspot["width"] <= 32 and hotspot["height"] <= 32, hotspot
                assert hotspot["left"] <= 500 <= hotspot["right"], hotspot
                assert hotspot["top"] <= 500 <= hotspot["bottom"], hotspot
                before_paths = ink_path_count(page)
                before_pen_scroll = page.evaluate("() => scrollY")
                draw_pen_stroke(
                    session,
                    [(500, 500), (520, 510), (545, 525), (575, 545)],
                )
                page.wait_for_function(
                    """n => document.querySelector('#bw-reader-pins').shadowRoot
                      .querySelectorAll('.bw-ink-document path').length === n + 1""",
                    arg=before_paths,
                )
                rendered_tool = page.evaluate(
                    """() => {
                      const path=document.querySelector('#bw-reader-pins').shadowRoot
                        .querySelector('.bw-ink-document path:last-child');
                      return {
                        pathColor:path.getAttribute('stroke'),
                        pathWidth:Number(path.getAttribute('stroke-width'))
                      };
                    }"""
                )
                assert rendered_tool == {
                    "pathColor": "#007aff",
                    "pathWidth": 9,
                }, rendered_tool
                after_pen_scroll = page.evaluate("() => scrollY")
                assert abs(after_pen_scroll - before_pen_scroll) < 4, {
                    "before": before_pen_scroll,
                    "after": after_pen_scroll,
                }
                capture = page.evaluate(
                    """() => ({
                      got:Number(document.querySelector('#bw-reader-host').shadowRoot
                        .querySelector('.bw-ink-shield').dataset.captureGot||0),
                      lost:Number(document.querySelector('#bw-reader-host').shadowRoot
                        .querySelector('.bw-ink-shield').dataset.captureLost||0)
                    })"""
                )
                assert capture == {"got": 1, "lost": 1}, capture

                # Pen-up synchronously disarms the hotspot. Incidental sub-8px
                # hover jitter must not re-arm it, and there is no idle timer.
                assert page.evaluate(
                    """() => !document.querySelector('#bw-reader-host').shadowRoot
                      .querySelector('.bw-ink-shield').classList.contains('show')"""
                )
                pen_event(session, "mouseMoved", 579, 545, pressed=False)
                assert page.evaluate(
                    """() => !document.querySelector('#bw-reader-host').shadowRoot
                      .querySelector('.bw-ink-shield').classList.contains('show')"""
                )

                # The first finger gesture starts at the exact pen-up area with no
                # idle wait. Because the hotspot is already gone, native scroll wins.
                after_pen = page.evaluate("() => scrollY")
                swipe_up(session, 575, 545, 245)
                page.wait_for_function(
                    "before => scrollY > before + 150",
                    arg=after_pen,
                    timeout=5_000,
                )

                # Rapid swipes are not taps. The old pointerdown-only recognizer
                # converted the second swipe into the temporary eraser and blocked
                # it; the qualified pointerup recognizer must leave the pen selected.
                settle_at(page, 1600)
                rapid_before = page.evaluate("() => scrollY")
                swipe_up(session, 300, 650, 480)
                swipe_up(session, 300, 650, 480)
                page.wait_for_function(
                    "before => scrollY > before + 120",
                    arg=rapid_before,
                    timeout=5_000,
                )
                assert page.evaluate(
                    """() => document.querySelector('#bw-reader-host').shadowRoot
                      .querySelector('.bw-ink-tools [data-tool="pen"]')
                      .classList.contains('on')"""
                )

                # Explicit eraser uses the same pen ownership path. It removes the
                # stroke and must also release touch immediately at pen-up.
                settle_at(page, 1200)
                page.evaluate(
                    """() => document.querySelector('#bw-reader-host').shadowRoot
                      .querySelector('.bw-ink-tools [data-tool="eraser"]').click()"""
                )
                draw_pen_stroke(
                    session,
                    [(520, 510), (530, 515), (540, 520)],
                )
                page.wait_for_function(
                    """() => document.querySelector('#bw-reader-pins').shadowRoot
                      .querySelectorAll('.bw-ink-document path').length === 0"""
                )
                after_eraser = page.evaluate("() => scrollY")
                swipe_up(session, 540, 520, 250)
                page.wait_for_function(
                    "before => scrollY > before + 140",
                    arg=after_eraser,
                    timeout=5_000,
                )
                capture = page.evaluate(
                    """() => ({
                      got:Number(document.querySelector('#bw-reader-host').shadowRoot
                        .querySelector('.bw-ink-shield').dataset.captureGot||0),
                      lost:Number(document.querySelector('#bw-reader-host').shadowRoot
                        .querySelector('.bw-ink-shield').dataset.captureLost||0)
                    })"""
                )
                assert capture == {"got": 2, "lost": 2}, capture

                # Ordinary-web ink is deliberately temporary. Responsive width
                # changes invalidate document coordinates, so committed strokes are
                # discarded. Historical webInkV1 data remains untouched.
                settle_at(page, 1000)
                page.evaluate(
                    """() => document.querySelector('#bw-reader-host').shadowRoot
                      .querySelector('.bw-ink-tools [data-tool="pen"]').click()"""
                )
                draw_pen_stroke(
                    session,
                    [(400, 400), (430, 420), (460, 440)],
                )
                page.wait_for_function(
                    """() => document.querySelector('#bw-reader-pins').shadowRoot
                      .querySelectorAll('.bw-ink-document path').length === 1"""
                )
                page.set_viewport_size({"width": 900, "height": 800})
                page.wait_for_function(
                    """() => document.querySelector('#bw-reader-pins').shadowRoot
                      .querySelectorAll('.bw-ink-document path').length === 0"""
                )
                assert page.evaluate(
                    "() => localStorage.getItem('webInkV1')"
                ) == "legacy-web-ink-must-remain"

                # Any finger landing while the tiny hotspot is armed removes it
                # synchronously. A first gesture that starts inside those 32px is
                # the explicit hybrid tradeoff; the hotspot must never linger.
                pen_event(session, "mouseMoved", 700, 300, pressed=False)
                page.wait_for_function(
                    """() => document.querySelector('#bw-reader-host').shadowRoot
                      .querySelector('.bw-ink-shield').classList.contains('show')"""
                )
                touch(session, "touchStart", 700, 300)
                page.wait_for_function(
                    """() => !document.querySelector('#bw-reader-host').shadowRoot
                      .querySelector('.bw-ink-shield').classList.contains('show')"""
                )
                touch(session, "touchEnd")

                # Mouse/touch activation on the host page remains intact.
                page.mouse.click(80, 118)
                touch(session, "touchStart", 80, 118)
                touch(session, "touchEnd")
                page.wait_for_function("() => window.hostClicks === 2")
                print(
                    "OK: bounded 32px pen pre-hit, pen/eraser pointer ownership, "
                    "synchronous disarm, immediate post-stroke touch scroll, "
                    "qualified double-tap, host clicks and session reflow pass "
                    "(Windows direct manipulation still needs a physical pen)"
                )
            finally:
                context.close()


if __name__ == "__main__":
    main()
