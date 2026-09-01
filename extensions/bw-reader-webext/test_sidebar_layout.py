#!/usr/bin/env python3
"""Chromium regression: docked sidebar reflows host page and long-press handle resizes it."""
from __future__ import annotations

import contextlib
import http.server
import pathlib
import socketserver
import threading
import time

from playwright.sync_api import sync_playwright


ROOT = pathlib.Path(__file__).resolve().parents[2]
EXT = pathlib.Path(__file__).resolve().parent
from browser_exe import CHROME


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def main() -> None:
    handler = lambda *a, **kw: Quiet(*a, directory=str(ROOT), **kw)  # noqa: E731
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}/extensions/bw-reader-webext/README.md"
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                "", executable_path=str(CHROME), headless=False, viewport={"width": 1180, "height": 820},
                args=[f"--disable-extensions-except={EXT}", f"--load-extension={EXT}"],
            )
            try:
                page = ctx.new_page()
                cdp = ctx.new_cdp_session(page)
                worlds: list[dict] = []
                cdp.on("Runtime.executionContextCreated", lambda e: worlds.append(e["context"]))
                cdp.send("Runtime.enable")
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector("#bw-reader-host", state="attached")
                time.sleep(0.5)
                ext_world = None
                for world in worlds:
                    try:
                        value = cdp.send("Runtime.evaluate", {
                            "contextId": world["id"], "expression": "!!window.__bwShadow && !!window.RC?.sidedrawer",
                            "returnByValue": True,
                        }).get("result", {}).get("value")
                        if value is True:
                            ext_world = world["id"]
                            break
                    except Exception:
                        continue
                assert ext_world is not None, worlds

                base_width = page.evaluate("() => document.documentElement.getBoundingClientRect().width")
                cdp.send("Runtime.evaluate", {"contextId": ext_world, "expression": "RC.sidedrawer.setWidth(420,true);RC.sidedrawer.setFloating(false);RC.sidedrawer.open('asst')"})
                time.sleep(0.85)
                docked = page.evaluate("() => ({w:document.documentElement.getBoundingClientRect().width,flag:document.documentElement.dataset.bwSideDocked,res:getComputedStyle(document.documentElement).getPropertyValue('--bw-side-reserve')})")
                assert docked["flag"] == "1" and abs(docked["w"] - (base_width - 420)) < 4 and docked["res"].strip() == "420px", docked

                cdp.send("Runtime.evaluate", {"contextId": ext_world, "expression": "RC.sidedrawer.setFloating(true)"})
                time.sleep(0.75)
                floating = page.evaluate("() => ({w:document.documentElement.getBoundingClientRect().width,flag:document.documentElement.dataset.bwSideDocked||''})")
                assert floating["flag"] == "" and abs(floating["w"] - base_width) < 4, floating

                # Restore docked mode, then hold the handle for >420 ms and drag 80 px left.
                cdp.send("Runtime.evaluate", {"contextId": ext_world, "expression": "RC.sidedrawer.setFloating(false);RC.sidedrawer.setWidth(420,true)"})
                time.sleep(0.65)  # drain the appearance reflow before counting resize commits
                result = cdp.send("Runtime.evaluate", {
                    "contextId": ext_world,
                    "expression": """new Promise(resolve=>{
                      const h=window.__bwShadow.getElementById('ep-side-handle'),r=h.getBoundingClientRect();
                      let resizeEvents=0;const onResize=()=>resizeEvents++;window.addEventListener('resize',onResize);
                      const fire=(t,id,x,buttons)=>h.dispatchEvent(new PointerEvent(t,{pointerId:id,pointerType:'mouse',button:0,buttons,bubbles:true,clientX:x,clientY:r.y+r.height/2}));
                      const x=r.x+r.width/2;
                      fire('pointerdown',44,x,1);
                      setTimeout(()=>{
                        fire('pointermove',45,x-140,1);fire('pointerup',45,x-140,0);
                        const foreignUpKept=h.classList.contains('resizing');
                        for(let dx=10;dx<=80;dx+=10)fire('pointermove',44,x-dx,1);
                        requestAnimationFrame(()=>{
                          const preview=window.__bwShadow.getElementById('ep-side').getBoundingClientRect().width;
                          const previewReserve=getComputedStyle(document.documentElement)
                            .getPropertyValue('--bw-side-reserve').trim();
                          const previewPageWidth=document.documentElement
                            .getBoundingClientRect().width;
                          const duringResizeEvents=resizeEvents;
                          fire('pointercancel',45,x-80,0);
                          const foreignCancelKept=h.classList.contains('resizing');
                          fire('pointerup',44,x-80,0);
                          setTimeout(()=>{
                            window.removeEventListener('resize',onResize);
                            resolve({w:RC.sidedrawer.getWidth(),side:window.__bwShadow.getElementById('ep-side').getBoundingClientRect().width,
                              open:RC.sidedrawer.isOpen(),foreignUpKept,foreignCancelKept,preview,
                              previewReserve,previewPageWidth,
                              duringResizeEvents,resizeEvents,debugPointer:getComputedStyle(window.__bwShadow.querySelector('.bw-ink-dbg')).pointerEvents});
                          },100);
                        });
                      },500);
                    })""", "awaitPromise": True, "returnByValue": True,
                }).get("result", {}).get("value")
                assert result["open"] and abs(result["w"] - 500) < 4 and abs(result["side"] - 500) < 4, result
                assert result["foreignUpKept"] and result["foreignCancelKept"], result
                assert abs(result["preview"] - 500) < 4, result
                assert result["previewReserve"] == "500px", result
                assert abs(result["previewPageWidth"] - (base_width - 500)) < 4, result
                assert result["duringResizeEvents"] == 0 and result["resizeEvents"] == 1, result
                assert result["debugPointer"] == "none", result
                time.sleep(0.2)
                resized = page.evaluate("() => document.documentElement.getBoundingClientRect().width")
                assert abs(resized - (base_width - 500)) < 4, resized

                cancelled = cdp.send("Runtime.evaluate", {
                    "contextId": ext_world,
                    "expression": """new Promise(resolve=>{
                      const h=window.__bwShadow.getElementById('ep-side-handle'),r=h.getBoundingClientRect(),x=r.x+r.width/2;
                      let resizeEvents=0;const onResize=()=>resizeEvents++;window.addEventListener('resize',onResize);
                      const fire=(t,id,px,buttons)=>h.dispatchEvent(new PointerEvent(t,{pointerId:id,pointerType:'touch',button:0,buttons,bubbles:true,clientX:px,clientY:r.y+r.height/2}));
                      fire('pointerdown',55,x,1);
                      setTimeout(()=>{
                        fire('pointermove',55,x-70,1);
                        requestAnimationFrame(()=>{
                          const preview=window.__bwShadow.getElementById('ep-side').getBoundingClientRect().width;
                          fire('pointercancel',56,x-70,0);
                          const foreignKept=h.classList.contains('resizing');
                          fire('pointercancel',55,x-70,0);
                          requestAnimationFrame(()=>{
                            window.removeEventListener('resize',onResize);
                            resolve({preview,foreignKept,w:RC.sidedrawer.getWidth(),
                              side:window.__bwShadow.getElementById('ep-side').getBoundingClientRect().width,
                              resizing:h.classList.contains('resizing'),resizeEvents});
                          });
                        });
                      },500);
                    })""",
                    "awaitPromise": True,
                    "returnByValue": True,
                }).get("result", {}).get("value")
                assert cancelled["preview"] > 550 and cancelled["foreignKept"], cancelled
                assert cancelled["w"] == 500 and abs(cancelled["side"] - 500) < 4, cancelled
                assert not cancelled["resizing"] and cancelled["resizeEvents"] == 0, cancelled

                minimum = cdp.send("Runtime.evaluate", {
                    "contextId": ext_world, "expression": "RC.sidedrawer.setWidth(1,true)", "returnByValue": True,
                }).get("result", {}).get("value")
                assert minimum == 300, minimum

                # Narrow sidebar: every built-in entry must still have a visible icon and fit before settings.
                # This catches the old extension-only bug where shell tabs had labels but no icons while the
                # narrow-screen stylesheet hid those labels, leaving several invisible blank buttons.
                top_tabs = cdp.send("Runtime.evaluate", {
                    "contextId": ext_world,
                    "expression": """new Promise(resolve=>setTimeout(()=>{
                      const sh=window.__bwShadow,bar=sh.getElementById('ep-side-tabbar'),setb=sh.getElementById('ep-side-set-btn');
                      const br=bar.getBoundingClientRect(),sr=setb.getBoundingClientRect();
                      const tabs=[...sh.querySelectorAll('#ep-side-tabs .ep-side-tab')].map(x=>{const r=x.getBoundingClientRect();return{
                        pane:x.dataset.pane,display:getComputedStyle(x).display,icon:!!x.querySelector('svg.si'),
                        width:r.width,left:r.left,right:r.right,inBar:r.left>=br.left-1&&r.right<=sr.left+1};});
                      resolve({tabs,barWidth:br.width,settingsLeft:sr.left});
                    },700))""",
                    "awaitPromise": True, "returnByValue": True,
                }).get("result", {}).get("value")
                expected = {"asst", "vocab", "kg", "hl", "toc", "grammar"}
                found = {t["pane"] for t in top_tabs["tabs"] if t["display"] != "none"}
                assert expected <= found, top_tabs
                assert all(t["icon"] and t["width"] >= 30 and t["inBar"] for t in top_tabs["tabs"] if t["pane"] in expected), top_tabs
                # 复习已统一为助手内部模式，不再占用一个顶栏 tab。按钮必须
                # 位于助手 pane，旧 review tab/pane 必须已清理，避免同一功能
                # 再次以两套 UI 并存。
                review_mode = cdp.send("Runtime.evaluate", {
                    "contextId": ext_world,
                    "expression": """(()=>{
                      const sh=window.__bwShadow,t=sh.getElementById('asst-review-toggle');
                      return {
                        toggle:!!t,
                        visible:!!t&&getComputedStyle(t).display!=='none',
                        inAssistant:!!t&&!!t.closest('#side-pane-asst'),
                        legacyTabs:sh.querySelectorAll('#ep-side-tabs [data-pane="review"]').length,
                        legacyPanes:sh.querySelectorAll('.ep-side-pane[data-pane="review"],#rc-review-pane').length
                      };
                    })()""",
                    "returnByValue": True,
                }).get("result", {}).get("value")
                assert review_mode == {
                    "toggle": True,
                    "visible": True,
                    "inAssistant": True,
                    "legacyTabs": 0,
                    "legacyPanes": 0,
                }, review_mode

                # Codex-style conversation rail: one mark per user+assistant round, hidden native tracks,
                # visible rounds highlighted, curved neighbor expansion, preview, click-to-jump.
                rail = cdp.send("Runtime.evaluate", {
                    "contextId": ext_world,
                    "expression": """new Promise(resolve=>{
                      const th=window.__bwShadow.getElementById('asst-thread'); th.innerHTML='';
                      for(let i=1;i<=6;i++){
                        const u=document.createElement('div');u.className='asst-msg asst-u';u.textContent='第'+i+'轮问题：怎样理解概念'+i;th.appendChild(u);
                        const a=document.createElement('div');a.className='asst-msg asst-a';a.textContent='第'+i+'轮回答重点：这是概念'+i+'的核心解释与例子。';a.style.minHeight='115px';th.appendChild(a);
                      }
                      setTimeout(()=>{
                        const marks=[...window.__bwShadow.querySelectorAll('.asst-turnmark')],m=marks[2];
                        m.dispatchEvent(new PointerEvent('pointerenter',{bubbles:true,pointerType:'mouse'}));
                        const tip=window.__bwShadow.getElementById('asst-turntip');
                        setTimeout(()=>{
                          const widths=marks.slice(0,5).map(x=>x.getBoundingClientRect().width),tipText=tip.textContent,tipShow=tip.classList.contains('show');
                          const before=th.scrollTop;m.click();
                          setTimeout(()=>resolve({n:marks.length,active:marks.filter(x=>x.classList.contains('active')).length,widths,
                            tip:tipText,tipShow,before,after:th.scrollTop,
                            threadSb:getComputedStyle(th).scrollbarWidth,taSb:getComputedStyle(window.__bwShadow.getElementById('asst-ta')).scrollbarWidth}),500);
                        },650);
                      },300);
                    })""",
                    "awaitPromise": True, "returnByValue": True,
                }).get("result", {}).get("value")
                assert rail["n"] == 6 and rail["active"] >= 1, rail
                assert rail["widths"][2] > rail["widths"][1] > rail["widths"][0], rail
                assert "第3轮问题" in rail["tip"] and "第3轮回答重点" in rail["tip"], rail
                assert rail["after"] > rail["before"] and rail["threadSb"] == "none" and rail["taSb"] == "none", rail
                print("OK: sidebar top tabs, reflow/resize and Codex-style turn rail all work")
            finally:
                with contextlib.suppress(Exception):
                    ctx.close()
        server.shutdown()


if __name__ == "__main__":
    main()
