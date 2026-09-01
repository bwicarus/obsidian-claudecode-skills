#!/usr/bin/env python3
"""Real Chromium contract for favorite payloads and long-press/drag arbitration."""
from __future__ import annotations

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
    handler = lambda *args, **kwargs: Quiet(  # noqa: E731
        *args, directory=str(ROOT), **kwargs
    )
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = (
            f"http://127.0.0.1:{server.server_address[1]}"
            "/extensions/bw-reader-webext/README.md"
        )
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                "",
                executable_path=str(CHROME),
                headless=True,
                viewport={"width": 1180, "height": 820},
                args=[
                    f"--disable-extensions-except={EXT}",
                    f"--load-extension={EXT}",
                ],
            )
            try:
                page = context.new_page()
                cdp = context.new_cdp_session(page)
                worlds: list[dict] = []
                cdp.on(
                    "Runtime.executionContextCreated",
                    lambda event: worlds.append(event["context"]),
                )
                cdp.send("Runtime.enable")
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector("#bw-reader-host", state="attached")
                time.sleep(0.5)

                extension_world = None
                for world in worlds:
                    try:
                        result = cdp.send(
                            "Runtime.evaluate",
                            {
                                "contextId": world["id"],
                                "expression": (
                                    "!!window.__bwShadow && !!window.RC"
                                    " && !!chrome.runtime?.id"
                                ),
                                "returnByValue": True,
                            },
                        )
                        if result.get("result", {}).get("value") is True:
                            extension_world = world["id"]
                            break
                    except Exception:
                        continue
                if extension_world is None:
                    raise AssertionError("extension isolated world not found")

                prepared = cdp.send(
                    "Runtime.evaluate",
                    {
                        "contextId": extension_world,
                        "expression": """(() => {
                          const back='完整背面'.repeat(5000);
                          const source={
                            id:'anki-731',card_id:731,note_id:91,
                            source:{file:'知识/矩阵.md',heading:'相似变换'},
                            front:'为什么？',back,
                            _displayFrontHtml:'<b>为什么</b>',
                            _displayBackHtml:'<p>投影背面</p>'
                          };
                          const good=RC.voiceCard.favorite.prepare({
                            id:'anki_entity_731',label:'学习卡',kind:'cards',
                            cid:'anki_entity_731',gid:'anki_entity_731',
                            raw:JSON.stringify([source]),text:'为什么？ / 完整背面'
                          });
                          const huge=RC.voiceCard.favorite.prepare({
                            id:'huge',kind:'cards',cid:'huge',gid:'huge',
                            raw:JSON.stringify([{front:'Q',back:'大'.repeat(100000)}])
                          });
                          const plainRaw='<p>'+('x'.repeat(25000))+'</p>';
                          const plain=RC.voiceCard.favorite.prepare({
                            id:'plain',kind:'weather',raw:plainRaw,isHtml:true
                          });
                          return {
                            goodOk:good.ok,
                            hasRaw:Object.prototype.hasOwnProperty.call(good.record||{},'raw'),
                            version:good.record?.payload?.version,
                            kind:good.record?.payload?.kind,
                            count:good.record?.payload?.cards?.length,
                            backLen:good.record?.payload?.cards?.[0]?.back?.length,
                            source:good.record?.payload?.cards?.[0]?.source,
                            projection:good.record?.payload?.cards?.[0]?._displayBackHtml,
                            cid:good.record?.cid,gid:good.record?.gid,
                            hugeOk:huge.ok,
                            plainOk:plain.ok,
                            plainSame:plain.record?.raw===plainRaw
                          };
                        })()""",
                        "returnByValue": True,
                    },
                ).get("result", {}).get("value")
                assert prepared == {
                    "goodOk": True,
                    "hasRaw": False,
                    "version": 1,
                    "kind": "cards",
                    "count": 1,
                    "backLen": 20000,
                    "source": {"file": "知识/矩阵.md", "heading": "相似变换"},
                    "projection": "<p>投影背面</p>",
                    "cid": "anki_entity_731",
                    "gid": "anki_entity_731",
                    "hugeOk": False,
                    "plainOk": True,
                    "plainSame": True,
                }, prepared

                arbitration = cdp.send(
                    "Runtime.evaluate",
                    {
                        "contextId": extension_world,
                        "expression": """new Promise(resolve => {
                          RC.sidedrawer.open('asst');
                          const host=window.__bwShadow.getElementById('asst-thread');
                          const r=RC.voiceCard.renderInflow(host,{
                            text:'阈值仲裁',label:'测试卡',cid:'gesture-arbiter',
                            type:'#7b6cff',icon:'🎴',form:'full'
                          });
                          RC.voiceCard.pinBind(
                            r.el,'测试卡',()=> '阈值仲裁',{},
                            r.el.querySelector('.vc-card-bd')
                          );
                          RC.voiceCard.dragToDock(r.el,()=>({
                            label:'测试卡',raw:'阈值仲裁',cid:'gesture-arbiter'
                          }));
                          const hd=r.el.querySelector('.vc-card-hd');
                          const rect=hd.getBoundingClientRect();
                          const fire=(target,type,x,y,buttons)=>target.dispatchEvent(
                            new PointerEvent(type,{
                              pointerId:991,pointerType:'touch',button:0,buttons,
                              bubbles:true,clientX:x,clientY:y
                            })
                          );
                          const x=rect.left+20,y=rect.top+12;
                          fire(hd,'pointerdown',x,y,1);
                          // 蓄力完成前移动 9px 会取消卡头拖动；正文长按位于
                          // 独立 pressTarget，卡头手势不能误选整卡。
                          fire(document,'pointermove',x+9,y,1);
                          setTimeout(()=>{
                            const ghost=!!window.__bwShadow.querySelector('.vc-drag-ghost');
                            const picked=r.el.classList.contains('vc-picked');
                            fire(document,'pointercancel',x+9,y,0);
                            resolve({ghost,picked,cancelHook:
                              typeof r.el.__bwCancelPinHold==='function'});
                          },650);
                        })""",
                        "awaitPromise": True,
                        "returnByValue": True,
                    },
                ).get("result", {}).get("value")
                assert arbitration == {
                    "ghost": False,
                    "picked": False,
                    "cancelHook": True,
                }, arbitration
            finally:
                context.close()

    print("PASS: structured favorite payload + drag/long-press arbitration")


if __name__ == "__main__":
    main()
