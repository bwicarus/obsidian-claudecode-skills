#!/usr/bin/env python3
"""Real Chromium regression for pinned Anki card context selection on ordinary web pages."""
from __future__ import annotations

import contextlib
import http.server
import json
import pathlib
import socketserver
import threading
import time

from playwright.sync_api import sync_playwright


ROOT = pathlib.Path(__file__).resolve().parents[2]
EXT = pathlib.Path(__file__).resolve().parent
from browser_exe import CHROME
NAMESPACE = "acct-v1-" + "c" * 64


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_POST(self):  # noqa: N802 - stdlib handler API
        if self.path == "/pdf/api/review-answer":
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw or b"{}")
            # A deliberately unresolved review lets the regression reload the
            # extension after the synchronous review-pending snapshot reached
            # local storage but before the server can accept/reject it.
            if body.get("card_id") == 4343:
                time.sleep(10)
            payload = json.dumps(
                {"ok": True, "next": {"interval": 7, "label": "7 天"}}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                self.wfile.write(payload)
            return
        self.send_error(404)


class ThreadedTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def extension_world(cdp, worlds: list[dict]) -> int:
    for world in worlds:
        try:
            probe = cdp.send(
                "Runtime.evaluate",
                {
                    "contextId": world["id"],
                    "expression": (
                        "!!window.__bwShadow && !!window.__bwPinShadow && "
                        "!!window.RC?.stickynote?.bindCardSelection && "
                        "!!window.BWReaderRuntime?.contextSelections"
                    ),
                    "returnByValue": True,
                },
            )
            if probe.get("result", {}).get("value") is True:
                return world["id"]
        except Exception:
            continue
    raise AssertionError(f"extension isolated world not found: {worlds}")


def evaluate(cdp, context_id: int, expression: str, *, await_promise: bool = False):
    return cdp.send(
        "Runtime.evaluate",
        {
            "contextId": context_id,
            "expression": expression,
            "awaitPromise": await_promise,
            "returnByValue": True,
        },
    ).get("result", {}).get("value")


def main() -> None:
    handler = lambda *a, **kw: Quiet(*a, directory=str(ROOT), **kw)  # noqa: E731
    with ThreadedTCPServer(("127.0.0.1", 0), handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = (
            f"http://127.0.0.1:{server.server_address[1]}"
            "/extensions/bw-reader-webext/README.md"
        )
        with sync_playwright() as playwright:
            ctx = playwright.chromium.launch_persistent_context(
                "",
                executable_path=str(CHROME),
                headless=False,
                viewport={"width": 1180, "height": 820},
                args=[
                    f"--disable-extensions-except={EXT}",
                    f"--load-extension={EXT}",
                ],
            )
            try:
                workers = ctx.service_workers
                worker = workers[0] if workers else ctx.wait_for_event(
                    "serviceworker",
                    timeout=15_000,
                )
                seeded = worker.evaluate(
                    """async ({namespace}) => {
                      await chrome.storage.local.set({
                        readerActiveVerifiedAccountV1: {
                          schema: 1,
                          namespace,
                          verifiedAt: Date.now(),
                          source: 'provider-ticket'
                        }
                      });
                      await ensurePersistentAccount();
                      const captured = await capturePersistentAccount();
                      await accountStorage.saveVerifiedToken(
                        captured.entry.accountContext,
                        captured.lease,
                        'pinned-card-test-token'
                      );
                      const originalFetch = globalThis.fetch.bind(globalThis);
                      globalThis.__pinnedReviewCalls = [];
                      globalThis.fetch = async (input, init = {}) => {
                        const requestUrl = new URL(
                          typeof input === 'string' ? input : input.url
                        );
                        if (requestUrl.pathname === '/pdf/api/review-answer') {
                          let body = null;
                          try {
                            body = init.body ? JSON.parse(init.body) : null;
                          } catch (_) {}
                          globalThis.__pinnedReviewCalls.push(body);
                          if (body && body.card_id === 4343) {
                            return await new Promise(() => {});
                          }
                          return new Response(JSON.stringify({
                            ok: true,
                            next: {interval: 7, label: '7 天'}
                          }), {
                            status: 200,
                            headers: {'Content-Type': 'application/json'}
                          });
                        }
                        return originalFetch(input, init);
                      };
                      return captured.lease.namespace === namespace;
                    }""",
                    {"namespace": NAMESPACE},
                )
                assert seeded
                page = ctx.new_page()
                cdp = ctx.new_cdp_session(page)
                worlds: list[dict] = []
                cdp.on(
                    "Runtime.executionContextCreated",
                    lambda event: worlds.append(event["context"]),
                )
                cdp.send("Runtime.enable")
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_selector("#bw-reader-host", state="attached")
                page.wait_for_timeout(500)
                isolated = extension_world(cdp, worlds)

                made = evaluate(
                    cdp,
                    isolated,
                    """(() => {
                      const cards=[{
                        id:4242,note_id:84242,entity_id:'card_f00baa',entity_index:4,
                        type:'basic',front:'固定卡正面',back:'固定卡背面',
                        source_ref:'note:000-fixed.md',reason:'当前页面关联知识节点',
                        _st:'learn',_showBack:false
                      }];
                      const ok=RC.actions.run('pin.card',{
                        x:360,y:300,cards,gid:'anki_card_4242'
                      });
                      return {
                        ok:ok!==false,
                        shared:typeof RC.stickynote?.bindCardSelection==='function',
                        text:RC.stickynote?.cardContextText?.(cards)||''
                      };
                    })()""",
                )
                assert made == {
                    "ok": True,
                    "shared": True,
                    "text": "卡片 1\n正面：固定卡正面\n背面：固定卡背面",
                }, made
                pin = page.locator("#bw-reader-pins").locator(
                    ".bw-page-pin:not(.bw-html-pin)"
                )
                pin.wait_for(state="visible", timeout=5000)

                selected = evaluate(
                    cdp,
                    isolated,
                    """new Promise(resolve => {
                      const card=window.__bwPinShadow.querySelector('.bw-page-pin .vc-card');
                      const reveal=card.querySelector('[data-fc="reveal"]');
                      const r=reveal.getBoundingClientRect();
                      const fire=(target,type,id,x,y,buttons)=>target.dispatchEvent(
                        new PointerEvent(type,{pointerId:id,pointerType:'touch',button:0,
                          buttons,bubbles:true,clientX:x,clientY:y})
                      );
                      const x=r.left+Math.min(40,r.width/3),y=r.top+Math.min(30,r.height/3);
                      const hit=window.__bwPinShadow.elementFromPoint(x,y);
                      fire(hit,'pointerdown',501,x,y,1);
                      setTimeout(()=>{
                        fire(hit,'pointerup',501,x,y,0);
                        // 浏览器会在长按抬手后合成 click；选择手势必须吞掉它，
                        // 不能顺带把正面翻开。
                        hit.click();
                        setTimeout(()=>{
                          const snap=BWReaderRuntime.contextSelections.snapshot();
                          resolve({
                            hitReveal:hit===reveal||reveal.contains(hit),
                            picked:card.classList.contains('vc-picked'),
                            showBack:!!card.querySelector('.vc-card-bd').__fc.cards[0]._showBack,
                            items:snap.items.filter(item=>item.id==='card:anki_card_4242')
                          });
                        },20);
                      },650);
                    })""",
                    await_promise=True,
                )
                assert selected["hitReveal"], selected
                assert selected["picked"], selected
                assert selected["showBack"] is False, selected
                assert len(selected["items"]) == 1, selected
                item = selected["items"][0]
                assert item["id"] == "card:anki_card_4242", item
                assert item["source"] == {
                    "cid": "anki_card_4242",
                    "gid": "anki_card_4242",
                }, item
                assert item["meta"]["contract"] == "anki-card-context/1", item
                assert item["meta"]["host"] == "web-page-placement", item
                assert item["meta"]["cards"][0]["id"] == 4242, item
                assert item["meta"]["cards"][0]["note_id"] == 84242, item
                assert item["meta"]["cards"][0]["entity_id"] == "card_f00baa", item
                assert item["meta"]["cards"][0]["source_ref"] == "note:000-fixed.md", item
                assert "正面：固定卡正面" in item["text"], item
                assert "背面：固定卡背面" in item["text"], item

                # A second body hold toggles the same semantic entity off.
                toggled_off = evaluate(
                    cdp,
                    isolated,
                    """new Promise(resolve => {
                      const card=window.__bwPinShadow.querySelector('.bw-page-pin .vc-card');
                      const reveal=card.querySelector('[data-fc="reveal"]'),r=reveal.getBoundingClientRect();
                      const x=r.left+32,y=r.top+24;
                      const hit=window.__bwPinShadow.elementFromPoint(x,y);
                      const fire=(type,id)=>hit.dispatchEvent(new PointerEvent(type,{
                        pointerId:id,pointerType:'touch',button:0,buttons:type==='pointerup'?0:1,
                        bubbles:true,clientX:x,clientY:y
                      }));
                      fire('pointerdown',502);
                      setTimeout(()=>{
                        fire('pointerup',502);
                        resolve({
                          picked:card.classList.contains('vc-picked'),
                          selected:BWReaderRuntime.contextSelections.isSelected(
                            'card:anki_card_4242'
                          )
                        });
                      },650);
                    })""",
                    await_promise=True,
                )
                assert toggled_off == {"picked": False, "selected": False}, toggled_off

                # After the long-press click-suppression window expires, a normal
                # short tap on the same real reveal target must still flip the card.
                page.wait_for_timeout(1000)
                short_reveal = evaluate(
                    cdp,
                    isolated,
                    """(() => {
                      const card=window.__bwPinShadow.querySelector('.bw-page-pin .vc-card');
                      const reveal=card.querySelector('[data-fc="reveal"]'),r=reveal.getBoundingClientRect();
                      const x=r.left+32,y=r.top+24;
                      const hit=window.__bwPinShadow.elementFromPoint(x,y);
                      const fire=(type,buttons)=>hit.dispatchEvent(new PointerEvent(type,{
                        pointerId:520,pointerType:'touch',button:0,buttons,bubbles:true,
                        clientX:x,clientY:y
                      }));
                      fire('pointerdown',1);fire('pointerup',0);hit.click();
                      return {
                        hitReveal:hit===reveal||reveal.contains(hit),
                        showBack:!!card.querySelector('.vc-card-bd').__fc.cards[0]._showBack,
                        selected:BWReaderRuntime.contextSelections.isSelected(
                          'card:anki_card_4242'
                        )
                      };
                    })()""",
                )
                assert short_reveal == {
                    "hitReveal": True,
                    "showBack": True,
                    "selected": False,
                }, short_reveal

                # The card header owns a sticky 420ms drag recognizer. Before it
                # charges there is no drag; after it charges the same pointer can
                # move the placement, and it never selects the card.
                header_drag = evaluate(
                    cdp,
                    isolated,
                    """new Promise(resolve => {
                      const box=window.__bwPinShadow.querySelector('.bw-page-pin');
                      const card=box.querySelector('.vc-card'),hd=card.querySelector('.vc-card-hd');
                      const r=hd.getBoundingClientRect();
                      const fire=(target,type,id,x,y,buttons)=>target.dispatchEvent(
                        new PointerEvent(type,{pointerId:id,pointerType:'touch',button:0,
                          buttons,bubbles:true,clientX:x,clientY:y})
                      );
                      const x=r.left+30,y=r.top+14;
                      fire(hd,'pointerdown',503,x,y,1);
                      setTimeout(()=>{
                        const before={
                          dragging:box.classList.contains('bw-pin-dragging'),
                          transform:box.style.transform||'',
                          selected:BWReaderRuntime.contextSelections.isSelected(
                            'card:anki_card_4242'
                          )
                        };
                        setTimeout(()=>{
                          const charged=box.classList.contains('bw-pin-dragging');
                          fire(document,'pointermove',503,x+36,y+20,1);
                          requestAnimationFrame(()=>{
                            const moved=box.style.transform||'';
                            fire(document,'pointercancel',503,x+36,y+20,0);
                            setTimeout(()=>resolve({
                              before,charged,moved,
                              selected:BWReaderRuntime.contextSelections.isSelected(
                                'card:anki_card_4242'
                              ),
                              cleaned:!box.classList.contains('bw-pin-dragging')&&
                                box.style.transform===''
                            }),20);
                          });
                        },260);
                      },220);
                    })""",
                    await_promise=True,
                )
                assert header_drag["before"] == {
                    "dragging": False,
                    "transform": "",
                    "selected": False,
                }, header_drag
                assert header_drag["charged"] is True, header_drag
                assert header_drag["moved"] not in ("", "none"), header_drag
                assert header_drag["selected"] is False, header_drag
                assert header_drag["cleaned"] is True, header_drag

                # Rating buttons still own their pointer gesture and cannot select
                # the whole placement. The reveal surface itself was tested above:
                # short tap flips, while long press selects without flipping.
                controls = evaluate(
                    cdp,
                    isolated,
                    """new Promise(resolve => {
                      const card=window.__bwPinShadow.querySelector('.bw-page-pin .vc-card');
                      const fire=(target,type,id,r)=>target.dispatchEvent(
                        new PointerEvent(type,{pointerId:id,pointerType:'touch',button:0,
                          buttons:type==='pointerup'?0:1,bubbles:true,
                          clientX:r.left+20,clientY:r.top+20})
                      );
                      const rate=card.querySelector('.fc-e[data-ease="3"]');
                      const er=rate.getBoundingClientRect();
                      const hit=window.__bwPinShadow.elementFromPoint(
                        er.left+20,er.top+20
                      );
                      fire(hit,'pointerdown',505,er);
                      setTimeout(()=>{
                        fire(hit,'pointerup',505,er);
                        resolve({
                          hitRate:hit===rate||rate.contains(hit),
                          afterRateHold:BWReaderRuntime.contextSelections.isSelected(
                            'card:anki_card_4242'
                          ),
                          back:!!card.querySelector('.fc-back')
                        });
                      },650);
                    })""",
                    await_promise=True,
                )
                assert controls == {
                    "hitRate": True,
                    "afterRateHold": False,
                    "back": True,
                }, controls

                made_tool = evaluate(
                    cdp,
                    isolated,
                    """(() => {
                      const ok=RC.actions.run('pin.html',{
                        x:700,y:500,
                        html:{
                          cid:'tool_fixed_weather',
                          label:'天气工具卡',
                          kind:'weather',
                          isHtml:true,
                          content:'<div class="tool-hold-target">洛阳天气 23–31°C</div>'+
                            '<button type="button" class="tool-own-button">工具按钮</button>'+
                            '<a class="tool-own-link" href="#tool-source">来源链接</a>'
                        }
                      });
                      return {
                        ok:ok!==false,
                        shared:typeof RC.stickynote?.bindHtmlCardSelection==='function'
                      };
                    })()""",
                )
                assert made_tool == {"ok": True, "shared": True}, made_tool
                page.locator("#bw-reader-pins").locator(
                    ".bw-page-pin.bw-html-pin"
                ).wait_for(state="visible", timeout=5000)

                tool_selected = evaluate(
                    cdp,
                    isolated,
                    """new Promise(resolve => {
                      const box=window.__bwPinShadow.querySelector(
                        '.bw-page-pin.bw-html-pin'
                      );
                      const owner=box.querySelector('.vc-card');
                      const target=owner.querySelector('.tool-hold-target');
                      window.__toolBodyClicks=0;
                      target.addEventListener('click',()=>{
                        window.__toolBodyClicks+=1;
                      });
                      const r=target.getBoundingClientRect();
                      const x=r.left+Math.min(40,r.width/3),y=r.top+r.height/2;
                      const hit=window.__bwPinShadow.elementFromPoint(x,y);
                      const fire=(type,buttons)=>hit.dispatchEvent(new PointerEvent(type,{
                        pointerId:530,pointerType:'touch',button:0,buttons,bubbles:true,
                        clientX:x,clientY:y
                      }));
                      fire('pointerdown',1);
                      setTimeout(()=>{
                        fire('pointerup',0);
                        hit.click();
                        const item=BWReaderRuntime.contextSelections.snapshot().items.find(
                          entry=>entry.id==='card:tool_fixed_weather'
                        );
                        resolve({
                          hitBody:hit===target||target.contains(hit),
                          picked:owner.classList.contains('vc-picked'),
                          cid:owner.dataset.vcCid,
                          bodyClicks:window.__toolBodyClicks,
                          item
                        });
                      },650);
                    })""",
                    await_promise=True,
                )
                assert tool_selected["hitBody"], tool_selected
                assert tool_selected["picked"], tool_selected
                assert tool_selected["cid"] == "tool_fixed_weather", tool_selected
                assert tool_selected["bodyClicks"] == 0, tool_selected
                tool_item = tool_selected["item"]
                assert tool_item["id"] == "card:tool_fixed_weather", tool_item
                assert tool_item["meta"]["contract"] == "tool-card-context/1", (
                    tool_item
                )
                assert tool_item["meta"]["host"] == "web-page-placement", tool_item
                assert "洛阳天气 23–31°C" in tool_item["text"], tool_item

                tool_toggled_off = evaluate(
                    cdp,
                    isolated,
                    """new Promise(resolve => {
                      const owner=window.__bwPinShadow.querySelector(
                        '.bw-page-pin.bw-html-pin .vc-card'
                      );
                      const target=owner.querySelector('.tool-hold-target');
                      const r=target.getBoundingClientRect(),x=r.left+30,y=r.top+r.height/2;
                      const hit=window.__bwPinShadow.elementFromPoint(x,y);
                      const fire=(type,buttons)=>hit.dispatchEvent(new PointerEvent(type,{
                        pointerId:531,pointerType:'touch',button:0,buttons,bubbles:true,
                        clientX:x,clientY:y
                      }));
                      fire('pointerdown',1);
                      setTimeout(()=>{
                        fire('pointerup',0);
                        resolve({
                          picked:owner.classList.contains('vc-picked'),
                          selected:BWReaderRuntime.contextSelections.isSelected(
                            'card:tool_fixed_weather'
                          )
                        });
                      },650);
                    })""",
                    await_promise=True,
                )
                assert tool_toggled_off == {
                    "picked": False,
                    "selected": False,
                }, tool_toggled_off

                tool_controls = evaluate(
                    cdp,
                    isolated,
                    """new Promise(resolve => {
                      const owner=window.__bwPinShadow.querySelector(
                        '.bw-page-pin.bw-html-pin .vc-card'
                      );
                      const button=owner.querySelector('.tool-own-button');
                      const link=owner.querySelector('.tool-own-link');
                      const hold=(target,id)=>new Promise(done=>{
                        const r=target.getBoundingClientRect();
                        const x=r.left+r.width/2,y=r.top+r.height/2;
                        const hit=window.__bwPinShadow.elementFromPoint(x,y);
                        const fire=(type,buttons)=>hit.dispatchEvent(new PointerEvent(type,{
                          pointerId:id,pointerType:'touch',button:0,buttons,bubbles:true,
                          clientX:x,clientY:y
                        }));
                        fire('pointerdown',1);
                        setTimeout(()=>{fire('pointerup',0);done(hit===target||target.contains(hit));},650);
                      });
                      (async()=>{
                        const hitButton=await hold(button,532);
                        const afterButton=BWReaderRuntime.contextSelections.isSelected(
                          'card:tool_fixed_weather'
                        );
                        const hitLink=await hold(link,533);
                        resolve({
                          hitButton,afterButton,hitLink,
                          afterLink:BWReaderRuntime.contextSelections.isSelected(
                            'card:tool_fixed_weather'
                          ),
                          picked:owner.classList.contains('vc-picked')
                        });
                      })();
                    })""",
                    await_promise=True,
                )
                assert tool_controls == {
                    "hitButton": True,
                    "afterButton": False,
                    "hitLink": True,
                    "afterLink": False,
                    "picked": False,
                }, tool_controls

                favorite = evaluate(
                    cdp,
                    isolated,
                    """new Promise(resolve => {
                      const box=window.__bwPinShadow.querySelector('.bw-page-pin');
                      const hd=box.querySelector('.vc-card-hd'),r=hd.getBoundingClientRect();
                      const fav=RC.voiceCard.favorite,trash=RC.voiceCard.trash;
                      const original={inZone:fav.inZone,save:fav.save,trashIn:trash.inZone};
                      let saved=null;
                      fav.inZone=()=>true;fav.save=record=>{saved=record;return true;};
                      trash.inZone=()=>false;
                      const fire=(target,type,id,x,y,buttons)=>target.dispatchEvent(
                        new PointerEvent(type,{pointerId:id,pointerType:'touch',button:0,
                          buttons,bubbles:true,clientX:x,clientY:y})
                      );
                      const x=r.left+28,y=r.top+14;
                      fire(hd,'pointerdown',506,x,y,1);
                      setTimeout(()=>{
                        fire(document,'pointermove',506,x+80,y+70,1);
                        requestAnimationFrame(()=>{
                          fire(document,'pointerup',506,x+80,y+70,0);
                          fav.inZone=original.inZone;fav.save=original.save;
                          trash.inZone=original.trashIn;
                          setTimeout(()=>resolve(saved),20);
                        });
                      },460);
                    })""",
                    await_promise=True,
                )
                assert favorite["kind"] == "cards", favorite
                assert favorite["cid"] == favorite["gid"] == "anki_card_4242", favorite
                assert "正面：固定卡正面" in favorite["text"], favorite
                assert "背面：固定卡背面" in favorite["text"], favorite
                raw_cards = json.loads(favorite["raw"])
                assert raw_cards[0]["id"] == 4242, raw_cards
                assert raw_cards[0]["entity_id"] == "card_f00baa", raw_cards
                assert raw_cards[0]["source_ref"] == "note:000-fixed.md", raw_cards

                persisted = evaluate(
                    cdp,
                    isolated,
                    """new Promise(resolve => {
                      const rate=window.__bwPinShadow.querySelector(
                        '.bw-page-pin .fc-e[data-ease="3"]'
                      );
                      rate.click();
                      let tries=0;
                      const poll=()=>{
                        window.__bwExtensionStore.get('webCardPinsV1').then(all=>{
                          const placed=Object.values(all||{}).flat().find(
                            item=>item.gid==='anki_card_4242'
                          );
                          const card=placed?.cards?.[0];
                          if(card?._st==='done'&&card?._next?.interval===7){
                            resolve({gid:placed.gid,cid:placed.cid,card});
                          }else if(++tries<80)setTimeout(poll,25);
                          else{
                            resolve({
                              timeout:true,placed,
                              live:window.__bwPinShadow.querySelector(
                                '.bw-page-pin .vc-card-bd'
                              )?.__fc?.cards?.[0]
                            });
                          }
                        });
                      };
                      poll();
                    })""",
                    await_promise=True,
                )
                assert not persisted.get("timeout"), persisted
                assert persisted["gid"] == persisted["cid"] == "anki_card_4242", persisted
                assert persisted["card"]["_st"] == "done", persisted
                assert persisted["card"]["_next"]["interval"] == 7, persisted
                assert persisted["card"]["source_ref"] == "note:000-fixed.md", persisted

                # Reload reconstructs the placement from extension storage. The
                # same gid and accepted review state must survive; a fresh long
                # press must read this mounted live clone, not the original input.
                page.reload(wait_until="domcontentloaded")
                page.wait_for_selector("#bw-reader-host", state="attached")
                page.wait_for_timeout(650)
                isolated = extension_world(cdp, worlds)
                pin = page.locator("#bw-reader-pins").locator(
                    ".bw-page-pin:not(.bw-html-pin)"
                )
                pin.wait_for(state="visible", timeout=5000)
                restored = evaluate(
                    cdp,
                    isolated,
                    """new Promise(resolve => {
                      const card=window.__bwPinShadow.querySelector('.bw-page-pin .vc-card');
                      const done=card.querySelector('.fc-donehd'),r=done.getBoundingClientRect();
                      const x=r.left+Math.min(36,r.width/3),y=r.top+r.height/2;
                      const hit=window.__bwPinShadow.elementFromPoint(x,y);
                      const fire=(type,id)=>hit.dispatchEvent(new PointerEvent(type,{
                        pointerId:id,pointerType:'touch',button:0,
                        buttons:type==='pointerup'?0:1,bubbles:true,
                        clientX:x,clientY:y
                      }));
                      fire('pointerdown',507);
                      setTimeout(()=>{
                        fire('pointerup',507);
                        const item=BWReaderRuntime.contextSelections.snapshot().items.find(
                          entry=>entry.id==='card:anki_card_4242'
                        );
                        const state=card.querySelector('.vc-card-bd').__fc;
                        resolve({
                          cid:card.dataset.vcCid,
                          gid:state.gid,
                          st:state.cards[0]._st,
                          interval:state.cards[0]._next?.interval,
                          hitDone:hit===done||done.contains(hit),
                          selectedSt:item?.meta?.cards?.[0]?._st,
                          selectedInterval:item?.meta?.cards?.[0]?._next?.interval,
                          source:item?.meta?.cards?.[0]?.source_ref
                        });
                      },650);
                    })""",
                    await_promise=True,
                )
                assert restored == {
                    "cid": "anki_card_4242",
                    "gid": "anki_card_4242",
                    "st": "done",
                    "interval": 7,
                    "hitDone": True,
                    "selectedSt": "done",
                    "selectedInterval": 7,
                    "source": "note:000-fixed.md",
                }, restored

                # Crash/reload boundary: review-pending is a durable state, not
                # an animation-only flag. The endpoint for card_id=4343 stays
                # unresolved long enough to reload immediately after the local
                # write. Reload must remain fail-closed: the card is done/
                # pending (or queued by the aborted request) and exposes no
                # reveal/rating control that could create a second review aid.
                pending_saved = evaluate(
                    cdp,
                    isolated,
                    """new Promise(resolve => {
                      RC.actions.run('pin.card',{
                        x:720,y:300,
                        gid:'anki_card_pending_4343',
                        cards:[{
                          id:4343,card_id:4343,note_id:84343,
                          entity_id:'card_pending',entity_index:0,
                          type:'basic',front:'待决正面',back:'待决背面',
                          source_ref:'note:pending.md',_st:'learn',_showBack:false
                        }]
                      });
                      const boxes=()=>Array.from(
                        window.__bwPinShadow.querySelectorAll('.bw-page-pin')
                      );
                      const stateOf=box=>box?.querySelector('.vc-card-bd')?.__fc;
                      const box=boxes().find(item=>
                        stateOf(item)?.gid==='anki_card_pending_4343'
                      );
                      box.querySelector('[data-fc="reveal"]').click();
                      box.querySelector('.fc-e[data-ease="3"]').click();
                      let tries=0;
                      const poll=()=>{
                        window.__bwExtensionStore.get('webCardPinsV1').then(all=>{
                          const placed=Object.values(all||{}).flat().find(
                            item=>item.gid==='anki_card_pending_4343'
                          );
                          const card=placed?.cards?.[0];
                          if(card?._st==='done'&&card?._ratingPending===true){
                            resolve({gid:placed.gid,cid:placed.cid,card});
                          }else if(++tries<80)setTimeout(poll,25);
                          else resolve({timeout:true,placed,live:stateOf(box)?.cards?.[0]});
                        });
                      };
                      poll();
                    })""",
                    await_promise=True,
                )
                assert not pending_saved.get("timeout"), pending_saved
                assert pending_saved["gid"] == pending_saved["cid"] == (
                    "anki_card_pending_4343"
                ), pending_saved
                assert pending_saved["card"]["_st"] == "done", pending_saved
                assert pending_saved["card"]["_ratingPending"] is True, pending_saved
                assert pending_saved["card"]["source_ref"] == (
                    "note:pending.md"
                ), pending_saved

                page.reload(wait_until="domcontentloaded")
                page.wait_for_selector("#bw-reader-host", state="attached")
                page.wait_for_timeout(650)
                isolated = extension_world(cdp, worlds)
                crash_restored = evaluate(
                    cdp,
                    isolated,
                    """new Promise(resolve => {
                      const boxes=Array.from(
                        window.__bwPinShadow.querySelectorAll('.bw-page-pin')
                      );
                      const stateOf=box=>box?.querySelector('.vc-card-bd')?.__fc;
                      const box=boxes.find(item=>
                        stateOf(item)?.gid==='anki_card_pending_4343'
                      );
                      const cardEl=box?.querySelector('.vc-card');
                      const done=cardEl?.querySelector('.fc-donehd');
                      if(!box||!cardEl||!done){
                        resolve({missing:true,count:boxes.length});
                        return;
                      }
                      const r=done.getBoundingClientRect();
                      const x=r.left+Math.min(36,r.width/3),y=r.top+r.height/2;
                      const hit=window.__bwPinShadow.elementFromPoint(x,y);
                      const fire=(type,id)=>hit.dispatchEvent(new PointerEvent(type,{
                        pointerId:id,pointerType:'touch',button:0,
                        buttons:type==='pointerup'?0:1,bubbles:true,
                        clientX:x,clientY:y
                      }));
                      fire('pointerdown',508);
                      setTimeout(()=>{
                        fire('pointerup',508);
                        const st=stateOf(box);
                        const item=BWReaderRuntime.contextSelections.snapshot().items.find(
                          entry=>entry.id==='card:anki_card_pending_4343'
                        );
                        resolve({
                          gid:st?.gid,
                          card:st?.cards?.[0],
                          hitDone:hit===done||done.contains(hit),
                          ratingButtons:cardEl.querySelectorAll('.fc-e').length,
                          revealControls:cardEl.querySelectorAll(
                            '[data-fc="reveal"]'
                          ).length,
                          status:cardEl.querySelector('.fc-donehd')?.textContent||'',
                          selected:item?.meta?.cards?.[0]
                        });
                      },650);
                    })""",
                    await_promise=True,
                )
                assert not crash_restored.get("missing"), crash_restored
                assert crash_restored["gid"] == "anki_card_pending_4343", crash_restored
                assert crash_restored["card"]["_st"] == "done", crash_restored
                assert crash_restored["card"]["_ratingPending"] is True, crash_restored
                assert crash_restored["hitDone"] is True, crash_restored
                assert crash_restored["ratingButtons"] == 0, crash_restored
                assert crash_restored["revealControls"] == 0, crash_restored
                assert (
                    "提交评分" in crash_restored["status"]
                    or "待同步" in crash_restored["status"]
                ), crash_restored
                assert crash_restored["selected"]["_st"] == "done", crash_restored
                assert crash_restored["selected"]["_ratingPending"] is True, (
                    crash_restored
                )
                assert crash_restored["selected"]["source_ref"] == (
                    "note:pending.md"
                ), crash_restored

                print(
                    "OK: pinned Anki card body selects card:<gid>; "
                    "header drag/controls remain isolated; favorite and review "
                    "state survive accepted and pending crash-reload boundaries"
                )
            finally:
                with contextlib.suppress(Exception):
                    ctx.close()
        server.shutdown()


if __name__ == "__main__":
    main()
