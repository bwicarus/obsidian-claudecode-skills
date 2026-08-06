#!/usr/bin/env python3
"""Chromium contract for bounded-hybrid, session-only ordinary-web ink.

CDP's synthetic ``pointerType='pen'`` checks the JavaScript ownership model.
It cannot exercise Windows' hardware palm rejection or direct-manipulation
compositor, so a physical Surface/Wacom pass remains part of release QA.
"""

from __future__ import annotations

import pathlib
import tempfile
import os

from playwright.sync_api import sync_playwright


EXT = pathlib.Path(__file__).resolve().parent
PLAYWRIGHT_CHROME = (
    pathlib.Path.home()
    / ".cache/ms-playwright/chromium-1223/chrome-linux/chrome"
)
WINDOWS_PLAYWRIGHT = pathlib.Path(
    os.environ.get("LOCALAPPDATA", "")
) / "ms-playwright"
WINDOWS_CHROMIUMS = sorted(
    WINDOWS_PLAYWRIGHT.glob("chromium-*/chrome-win64/chrome.exe"),
    reverse=True,
)
WINDOWS_CHROME = pathlib.Path(
    r"C:\Program Files\Google\Chrome\Application\chrome.exe"
)
CHROME = (
    WINDOWS_CHROMIUMS[0]
    if WINDOWS_CHROMIUMS
    else WINDOWS_CHROME
    if WINDOWS_CHROME.exists()
    else PLAYWRIGHT_CHROME
)
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


def finger_double_tap(session, page, x: float, y: float) -> None:
    for _ in range(2):
        touch(session, "touchStart", x, y)
        touch(session, "touchEnd")
        page.wait_for_timeout(45)


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


def extension_world(session, contexts: list[dict]) -> int:
    for context in contexts:
        try:
            value = session.send("Runtime.evaluate", {
                "contextId": context["id"],
                "expression": "!!window.__bwWebInk",
                "returnByValue": True,
            }).get("result", {}).get("value")
            if value:
                return context["id"]
        except Exception:
            continue
    raise AssertionError(f"extension isolated world not found: {contexts}")


def isolated_value(session, context_id: int, expression: str):
    return session.send("Runtime.evaluate", {
        "contextId": context_id,
        "expression": expression,
        "returnByValue": True,
    }).get("result", {}).get("value")


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
                session = context.new_cdp_session(page)
                worlds: list[dict] = []
                session.on(
                    "Runtime.executionContextCreated",
                    lambda event: worlds.append(event["context"]),
                )
                session.send("Runtime.enable")
                page.goto(URL, wait_until="domcontentloaded")
                page.wait_for_selector("#bw-reader-host", state="attached",
                                       timeout=15_000)
                page.wait_for_function(
                    """() => !!document.querySelector('#bw-reader-host')
                      ?.shadowRoot?.querySelector('.bw-ink-canvas')"""
                )
                page.wait_for_timeout(150)
                world = extension_world(session, worlds)

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

                # Before a Pencil has supplied a location, the palette retains its
                # centered bottom fallback rather than guessing a target point.
                fallback_palette = page.evaluate(
                    """() => {
                      const el=document.querySelector('#bw-reader-host').shadowRoot
                        .querySelector('.bw-ink-tools');
                      const r=el.getBoundingClientRect();
                      return {left:r.left,right:r.right,bottom:r.bottom,
                        viewportWidth:innerWidth,viewportHeight:innerHeight,
                        clientWidth:document.documentElement.clientWidth,
                        clientHeight:document.documentElement.clientHeight,
                        located:el.classList.contains('located')};
                    }"""
                )
                assert not fallback_palette["located"], fallback_palette
                assert abs(
                    (fallback_palette["left"] + fallback_palette["right"]) / 2
                    - fallback_palette["clientWidth"] / 2
                ) < 2, fallback_palette
                assert abs(
                    fallback_palette["clientHeight"]
                    - fallback_palette["bottom"] - 18
                ) < 2, fallback_palette

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

                session.send("Emulation.setTouchEmulationEnabled", {
                    "enabled": True,
                    "maxTouchPoints": 5,
                })

                # Pencil hover moves the palette above the tip. Near a viewport
                # edge it may flip below, but must remain fully clamped on screen.
                pen_event(session, "mouseMoved", 500, 500, pressed=False)
                page.wait_for_function(
                    """() => document.querySelector('#bw-reader-host').shadowRoot
                      .querySelector('.bw-ink-tools').classList.contains('located')"""
                )
                hover_palette = page.evaluate(
                    """() => {
                      const r=document.querySelector('#bw-reader-host').shadowRoot
                        .querySelector('.bw-ink-tools').getBoundingClientRect();
                      return {left:r.left,right:r.right,top:r.top,bottom:r.bottom};
                    }"""
                )
                assert hover_palette["bottom"] <= 500 - 10, hover_palette
                pen_event(session, "mouseMoved", 3, 3, pressed=False)
                edge_palette = page.evaluate(
                    """() => new Promise(resolve => requestAnimationFrame(() => {
                      const r=document.querySelector('#bw-reader-host').shadowRoot
                        .querySelector('.bw-ink-tools').getBoundingClientRect();
                      resolve({left:r.left,right:r.right,top:r.top,bottom:r.bottom,
                        width:innerWidth,height:innerHeight});
                    }))"""
                )
                assert edge_palette["left"] >= 7, edge_palette
                assert edge_palette["top"] >= 7, edge_palette
                assert edge_palette["right"] <= edge_palette["width"] - 7, edge_palette
                assert edge_palette["bottom"] <= edge_palette["height"] - 7, edge_palette

                # Finger double tap obeys the cross-surface setting. Missing or
                # invalid values default to temporary eraser; selection toggles
                # only between selection and pen; none is a true no-op.
                page.evaluate(
                    "() => localStorage.removeItem('rc-ink-double-tap-action')"
                )
                finger_double_tap(session, page, 220, 220)
                assert page.evaluate(
                    """() => document.querySelector('#bw-reader-host').shadowRoot
                      .querySelector('[data-tool="eraser"]').classList.contains('on')"""
                )
                finger_double_tap(session, page, 220, 220)
                assert page.evaluate(
                    """() => document.querySelector('#bw-reader-host').shadowRoot
                      .querySelector('[data-tool="pen"]').classList.contains('on')"""
                )
                page.evaluate(
                    "() => localStorage.setItem('rc-ink-double-tap-action','selection')"
                )
                finger_double_tap(session, page, 260, 220)
                assert page.evaluate(
                    """() => document.querySelector('#bw-reader-host').shadowRoot
                      .querySelector('[data-tool="selection"]')
                      .classList.contains('on')"""
                )
                finger_double_tap(session, page, 260, 220)
                assert page.evaluate(
                    """() => document.querySelector('#bw-reader-host').shadowRoot
                      .querySelector('[data-tool="pen"]').classList.contains('on')"""
                )
                page.evaluate(
                    """() => {
                      localStorage.setItem('rc-ink-double-tap-action','none');
                      window.noneTapPrevented=[];
                      document.addEventListener('pointerup',event=>{
                        if(event.pointerType==='touch')
                          window.noneTapPrevented.push(event.defaultPrevented);
                      },true);
                    }"""
                )
                finger_double_tap(session, page, 300, 220)
                assert page.evaluate(
                    """() => document.querySelector('#bw-reader-host').shadowRoot
                      .querySelector('[data-tool="pen"]').classList.contains('on')"""
                )
                assert page.evaluate("() => window.noneTapPrevented") == [False, False]

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
                last_point_palette = page.evaluate(
                    """() => new Promise(resolve => requestAnimationFrame(() => {
                      const r=document.querySelector('#bw-reader-host').shadowRoot
                        .querySelector('.bw-ink-tools').getBoundingClientRect();
                      resolve({left:r.left,right:r.right,top:r.top,bottom:r.bottom});
                    }))"""
                )
                assert last_point_palette["bottom"] <= 545 - 10, last_point_palette

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

                # Ordinary-web ink is session-only, but a responsive width change
                # must not erase committed strokes. Historical webInkV1 data also
                # remains untouched.
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
                before_resize = page.evaluate(
                    """() => {
                      const path = document.querySelector('#bw-reader-pins').shadowRoot
                        .querySelector('.bw-ink-document path');
                      return {
                        color: path.getAttribute('stroke'),
                        width: path.getAttribute('stroke-width'),
                        data: path.getAttribute('d')
                      };
                    }"""
                )
                page.set_viewport_size({"width": 900, "height": 800})
                page.wait_for_function(
                    """() => document.querySelector('#bw-reader-pins').shadowRoot
                      .querySelectorAll('.bw-ink-document path').length === 1"""
                )
                preserved = page.evaluate(
                    """() => {
                      const path = document.querySelector('#bw-reader-pins').shadowRoot
                        .querySelector('.bw-ink-document path');
                      return {
                        color: path.getAttribute('stroke'),
                        width: path.getAttribute('stroke-width'),
                        data: path.getAttribute('d')
                      };
                    }"""
                )
                assert preserved == before_resize, {
                    "before": before_resize,
                    "after": preserved,
                }
                assert page.evaluate(
                    "() => localStorage.getItem('webInkV1')"
                ) == "legacy-web-ink-must-remain"

                # Selection pen creates closed, labelled regions without changing
                # the ordinary pen schema. Labels are derived from creation time +
                # stable id, so their order remains deterministic after deletion.
                page.evaluate(
                    """() => document.querySelector('#bw-reader-host').shadowRoot
                      .querySelector('.bw-ink-tools [data-tool="selection"]').click()"""
                )
                draw_pen_stroke(
                    session,
                    [(300, 300), (390, 300), (390, 390), (300, 390)],
                )
                page.wait_for_function(
                    """() => document.querySelector('#bw-reader-pins').shadowRoot
                      .querySelectorAll('.bw-ink-document path[data-region-id]')
                      .length === 1"""
                )
                first_region = isolated_value(
                    session,
                    world,
                    """(() => {
                      const pins=document.querySelector('#bw-reader-pins').shadowRoot;
                      const path=pins.querySelector('.bw-ink-document path[data-region-id]');
                      const snapshot=window.__bwWebInk.exportSnapshot();
                      const region=snapshot.strokes.find(s=>s.t==='region');
                      return {
                        fill:path.getAttribute('fill'),
                        fillOpacity:path.getAttribute('fill-opacity'),
                        closed:path.getAttribute('d').trim().endsWith('Z'),
                        pathId:path.dataset.regionId,
                        text:pins.querySelector('.bw-ink-document text')?.textContent,
                        region
                      };
                    })()"""
                )
                assert first_region["fill"] == "#0a84ff", first_region
                assert float(first_region["fillOpacity"]) > 0, first_region
                assert first_region["closed"], first_region
                assert first_region["pathId"] == first_region["region"]["id"], first_region
                assert first_region["text"].startswith("#1 "), first_region
                assert first_region["region"]["kind"] == "selection", first_region
                assert first_region["region"]["w"] == 2, first_region
                assert first_region["region"]["closed"] is True, first_region
                assert first_region["region"]["ordinal"] == 1, first_region
                assert first_region["region"]["label"].startswith("#1 "), first_region
                assert first_region["region"]["createdAtEpochMs"] > 0, first_region
                assert first_region["region"]["orderKey"].endswith(
                    ":" + first_region["region"]["id"]
                ), first_region
                assert first_region["region"]["p"][0] == first_region["region"]["p"][-1], first_region

                draw_pen_stroke(
                    session,
                    [(500, 300), (580, 300), (580, 380), (500, 380)],
                )
                page.wait_for_function(
                    """() => document.querySelector('#bw-reader-pins').shadowRoot
                      .querySelectorAll('.bw-ink-document path[data-region-id]')
                      .length === 2"""
                )
                labels = page.evaluate(
                    """() => Array.from(document.querySelector('#bw-reader-pins')
                      .shadowRoot.querySelectorAll('.bw-ink-document text'))
                      .map(node=>node.textContent)"""
                )
                assert labels[0].startswith("#1 ") and labels[1].startswith("#2 "), labels

                # Exercise the per-path point bound without exposing a test-only
                # API. More than 64 regions must remain: selections have no
                # special count limit and must never evict older user regions.
                page.evaluate(
                    """() => {
                      const send=(type,id,x,y,buttons)=>document.dispatchEvent(
                        new PointerEvent(type,{bubbles:true,cancelable:true,
                          pointerType:'pen',pointerId:id,clientX:x,clientY:y,
                          button:type==='pointerup'?0:0,buttons}));
                      const region=(id,x,y)=>{
                        send('pointerdown',id,x,y,1);
                        send('pointermove',id,x+24,y,1);
                        send('pointermove',id,x+24,y+24,1);
                        send('pointermove',id,x,y+24,1);
                        send('pointerup',id,x,y+24,0);
                      };
                      for(let i=0;i<63;i++)region(100+i,80+(i%12)*55,470+(i%3)*38);
                      const id=999,cx=450,cy=360,r=180;
                      send('pointerdown',id,cx+r,cy,1);
                      for(let i=1;i<620;i++){
                        const a=Math.PI*2*i/620;
                        send('pointermove',id,cx+Math.cos(a)*r,cy+Math.sin(a)*r,1);
                      }
                      send('pointerup',id,cx+r,cy,0);
                    }"""
                )
                bounded_regions = isolated_value(
                    session,
                    world,
                    """(() => {
                      const snapshot=window.__bwWebInk.exportSnapshot();
                      const regions=snapshot.strokes.filter(s=>s.t==='region');
                      const pins=document.querySelector('#bw-reader-pins').shadowRoot;
                      return {
                        count:regions.length,
                        pathCount:pins.querySelectorAll(
                          '.bw-ink-document path[data-region-id]').length,
                        labelCount:pins.querySelectorAll('.bw-ink-document text').length,
                        ordinals:regions.map(r=>r.ordinal).sort((a,b)=>a-b),
                        maxPoints:Math.max(...regions.map(r=>r.p.length)),
                        allClosed:regions.every(r=>r.closed&&r.p[0][0]===r.p.at(-1)[0]
                          &&r.p[0][1]===r.p.at(-1)[1]),
                        ids:regions.map(r=>r.id)
                      };
                    })()"""
                )
                assert bounded_regions["count"] == 66, bounded_regions
                assert bounded_regions["pathCount"] == 66, bounded_regions
                assert bounded_regions["labelCount"] == 66, bounded_regions
                assert bounded_regions["ordinals"] == list(range(1, 67)), bounded_regions
                assert bounded_regions["maxPoints"] == 512, bounded_regions
                assert bounded_regions["allClosed"], bounded_regions

                # Responsive width changes retain committed region identities just
                # as they retain pen strokes; only an unfinished active path may go.
                page.set_viewport_size({"width": 840, "height": 800})
                page.wait_for_timeout(100)
                after_region_resize = isolated_value(
                    session,
                    world,
                    """window.__bwWebInk.exportSnapshot().strokes
                      .filter(s=>s.t==='region').map(s=>s.id)"""
                )
                assert after_region_resize == bounded_regions["ids"], {
                    "before": bounded_regions["ids"],
                    "after": after_region_resize,
                }

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
                    "hover-clamped palette, configurable double-tap, closed "
                    "selection regions/metadata/bounds, host clicks and session "
                    "reflow pass "
                    "(Windows direct manipulation still needs a physical pen)"
                )
            finally:
                context.close()


if __name__ == "__main__":
    main()
