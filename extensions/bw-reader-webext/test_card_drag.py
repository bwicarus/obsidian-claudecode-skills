#!/usr/bin/env python3
"""Real Chromium regression: drag a shared tool card out of the side drawer onto a web page."""
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
CHROME = pathlib.Path.home() / ".cache/ms-playwright/chromium-1223/chrome-linux/chrome"


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def main() -> None:
    handler = lambda *a, **kw: Quiet(*a, directory=str(ROOT), **kw)  # noqa: E731
    with socketserver.TCPServer(("127.0.0.1", 0), handler) as server:
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_address[1]}/extensions/bw-reader-webext/README.md"
        with sync_playwright() as p:
            # Pi releases keep the pinned Chromium path.  Windows development uses
            # Playwright's own isolated browser when that Pi path is absent; the
            # empty user-data directory below remains an ephemeral test profile.
            browser_executable = CHROME if CHROME.exists() else pathlib.Path(p.chromium.executable_path)
            ctx = p.chromium.launch_persistent_context(
                "", executable_path=str(browser_executable), headless=False, viewport={"width": 1180, "height": 820},
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

                extension_world = None
                for world in worlds:
                    try:
                        probe = cdp.send("Runtime.evaluate", {
                            "contextId": world["id"], "expression": "!!window.__bwShadow && !!window.RC && !!chrome.runtime?.id",
                            "returnByValue": True,
                        })
                        if probe.get("result", {}).get("value") is True:
                            extension_world = world["id"]
                            break
                    except Exception:
                        continue
                if extension_world is None:
                    raise AssertionError(f"extension isolated world not found: {worlds}")

                made = cdp.send("Runtime.evaluate", {
                    "contextId": extension_world,
                    "expression": """(() => {
                      RC.sidedrawer.open('asst');
                      const thread = window.__bwShadow.getElementById('asst-thread');
                      if (!thread || !RC.voiceCard?.renderInflow || !RC.voiceCard?.dragToDock) return {ok:false, why:'shared card host missing'};
                      const r = RC.voiceCard.renderInflow(thread, {
                        text:'<strong data-drag-proof=\"1\">工具卡拖放测试</strong><div>来自助手侧栏</div>',
                        label:'测试工具卡', isHtml:true, type:'#7b6cff', icon:'🧰', form:'full', cid:'drag-regression'
                      });
                      if (!r?.el) return {ok:false, why:'card render failed'};
                      RC.voiceCard.pinBind(r.el, '测试工具卡', () => '工具卡拖放测试 来自助手侧栏', {}, r.bd);
                      RC.voiceCard.dragToDock(r.el, () => ({
                        label:'测试工具卡', raw:'<strong data-drag-proof=\"1\">工具卡拖放测试</strong><div>来自助手侧栏</div>',
                        isHtml:true, cid:'drag-regression'
                      }));
                      window.__dragTrace=[];
                      const hd=r.el.querySelector('.vc-card-hd');
                      ['pointerdown','pointermove','pointerup','pointercancel'].forEach(t=>hd.addEventListener(t,e=>window.__dragTrace.push([t,e.clientX,e.clientY,e.target?.className||''])));
                      return {ok:true};
                    })()""",
                    "returnByValue": True,
                }).get("result", {}).get("value")
                assert made and made.get("ok"), made
                time.sleep(0.45)  # wait for the drawer's slide-in transition before measuring the handle

                card = page.locator("#bw-reader-host").locator("#asst-thread .vc-card").last
                card.wait_for(state="visible")
                handle = card.locator(".vc-card-hd")

                # Shared voice-card drag binding is idempotent and single-pointer.
                # A foreign pointer cannot move/end/cancel the gesture; matching
                # pointercancel cleans the ghost without dropping, and the next
                # valid gesture produces exactly one copy with the latest payload.
                voice_pointer_contract = cdp.send("Runtime.evaluate", {
                    "contextId": extension_world,
                    "expression": """new Promise(resolve => {
                      const el=window.__bwShadow.querySelector('#asst-thread .vc-card:last-child');
                      const hd=el.querySelector('.vc-card-hd'),r=hd.getBoundingClientRect();
                      const original=RC.stickynote.createHtmlAt;
                      let drops=[],captured=null,releases=[];
                      hd.setPointerCapture=id=>{captured=id;};
                      hd.hasPointerCapture=id=>captured===id;
                      hd.releasePointerCapture=id=>{releases.push(id);captured=null;};
                      RC.stickynote.createHtmlAt=(x,y,payload)=>{drops.push({x,y,payload});return true;};
                      RC.voiceCard.dragToDock(el,()=>({label:'旧 payload',raw:'old',isHtml:true,cid:'drag-regression'}));
                      RC.voiceCard.dragToDock(el,()=>({label:'最新 payload',raw:'latest',isHtml:true,cid:'drag-regression'}));
                      const fire=(target,type,id,x,y,buttons)=>target.dispatchEvent(new PointerEvent(type,{
                        pointerId:id,pointerType:'touch',button:0,buttons,bubbles:true,clientX:x,clientY:y
                      }));
                      const x=r.left+20,y=r.top+12;
                      // 先证明蓄力前的 9px 快划会取消且 420ms 后不会突然追上手指。
                      fire(hd,'pointerdown',80,x,y,1);
                      fire(document,'pointermove',80,x+9,y,1);
                      setTimeout(()=>{
                        const prechargeCancelled=captured===null&&!window.__bwShadow.querySelector('.vc-drag-ghost')&&drops.length===0;
                        fire(hd,'pointerdown',81,x,y,1);
                        fire(document,'pointermove',82,280,260,1);
                        fire(document,'pointerup',82,280,260,0);
                        const foreignUpKept=captured===81&&drops.length===0;
                        setTimeout(()=>{
                          fire(document,'pointermove',81,280,260,1);
                          const ownerMoved=!!window.__bwShadow.querySelector('.vc-drag-ghost');
                          fire(document,'pointercancel',82,280,260,0);
                          const foreignCancelKept=!!window.__bwShadow.querySelector('.vc-drag-ghost');
                          fire(document,'pointercancel',81,280,260,0);
                          const cancelClean=!window.__bwShadow.querySelector('.vc-drag-ghost')&&drops.length===0&&el.style.opacity==='';
                          fire(hd,'pointerdown',83,x,y,1);
                          setTimeout(()=>{
                            fire(document,'pointermove',83,300,280,1);
                            fire(document,'pointerup',83,300,280,0);
                            setTimeout(()=>{
                              RC.stickynote.createHtmlAt=original;
                              resolve({prechargeCancelled,foreignUpKept,ownerMoved,foreignCancelKept,cancelClean,
                                drops,releases,bound:!!hd.__bwDragToDockBinding});
                            },40);
                          },460);
                        },460);
                      },460);
                    })""",
                    "awaitPromise": True,
                    "returnByValue": True,
                }).get("result", {}).get("value")
                assert voice_pointer_contract["prechargeCancelled"], voice_pointer_contract
                assert voice_pointer_contract["foreignUpKept"], voice_pointer_contract
                assert voice_pointer_contract["ownerMoved"], voice_pointer_contract
                assert voice_pointer_contract["foreignCancelKept"], voice_pointer_contract
                assert voice_pointer_contract["cancelClean"], voice_pointer_contract
                assert voice_pointer_contract["bound"], voice_pointer_contract
                assert voice_pointer_contract["releases"] == [80, 81, 83], voice_pointer_contract
                assert len(voice_pointer_contract["drops"]) == 1, voice_pointer_contract
                assert voice_pointer_contract["drops"][0]["payload"]["content"] == "latest", voice_pointer_contract
                # The idempotent binding contract intentionally keeps the newest
                # payload function. Restore the fixture payload before the main
                # end-to-end drag below so this isolated pointer test does not leak
                # its "latest" sentinel into the rest of the scenario.
                cdp.send("Runtime.evaluate", {
                    "contextId": extension_world,
                    "expression": """(() => {
                      const el=window.__bwShadow.querySelector('#asst-thread .vc-card[data-vc-cid="drag-regression"]');
                      RC.voiceCard.dragToDock(el,()=>({
                        label:'测试工具卡',
                        raw:'<strong data-drag-proof="1">工具卡拖放测试</strong><div>来自助手侧栏</div>',
                        isHtml:true,cid:'drag-regression'
                      }));
                    })()""",
                })

                hb = handle.bounding_box()
                assert hb, "tool card handle has no bounds"
                page.mouse.move(hb["x"] + min(36, hb["width"] / 4), hb["y"] + min(14, hb["height"] / 2))
                time.sleep(0.1)
                page.mouse.down()
                time.sleep(0.48)  # shared charged drag arms after 420ms; moving earlier deliberately cancels
                page.mouse.move(260, 300, steps=12)
                time.sleep(0.15)
                fx_during_drop = cdp.send("Runtime.evaluate", {
                    "contextId": extension_world,
                    "expression": """(() => {const e=window.__bwShadow.querySelector('.bw-web-anchor-fx'),r=e?.getBoundingClientRect();return e&&{display:getComputedStyle(e).display,w:r.width,h:r.height}})()""",
                    "returnByValue": True,
                }).get("result", {}).get("value")
                assert fx_during_drop and fx_during_drop["display"] == "block" and fx_during_drop["w"] > 10 and fx_during_drop["h"] > 10, fx_during_drop
                page.mouse.up()

                pin = page.locator("#bw-reader-pins").locator(".bw-page-pin")
                try:
                    pin.wait_for(state="visible", timeout=5000)
                except Exception:
                    diag = cdp.send("Runtime.evaluate", {
                        "contextId": extension_world,
                        "expression": """({
                          sideOpen: RC.sidedrawer.isOpen?.(), sideClass: window.__bwShadow.getElementById('ep-side')?.className,
                          sideRect: (()=>{const r=window.__bwShadow.getElementById('ep-side')?.getBoundingClientRect();return r&&{left:r.left,right:r.right,width:r.width}})(),
                          pins: window.__bwPinShadow.querySelectorAll('.bw-page-pin').length,
                          cards: window.__bwShadow.querySelectorAll('#asst-thread .vc-card').length,
                          trace: window.__dragTrace,
                          toast: window.__bwShadow.querySelector('#rc-toast')?.textContent || ''
                        })""", "returnByValue": True,
                    }).get("result", {}).get("value")
                    raise AssertionError(f"tool-card drop produced no visible pin: {diag}")
                assert "工具卡拖放测试" in pin.inner_text()
                page.mouse.click(82, 92)
                time.sleep(0.25)
                assert page.locator("#bw-reader-pins").locator(".bw-page-pin").count() == 1, "a later page click duplicated the last dropped card"
                chrome = cdp.send("Runtime.evaluate", {
                    "contextId": extension_world,
                    "expression": """(() => {
                      const p=window.__bwPinShadow.querySelector('.bw-page-pin'),s=getComputedStyle(p);
                      return {html:p.classList.contains('bw-html-pin'),cards:p.querySelectorAll('.vc-card').length,
                        header:p.querySelectorAll(':scope>.bw-page-pin-head').length,del:p.querySelectorAll('.bw-page-pin-del').length,
                        bg:s.backgroundColor,border:s.borderTopStyle,shadow:s.boxShadow};
                    })()""", "returnByValue": True,
                }).get("result", {}).get("value")
                assert chrome == {"html": True, "cards": 1, "header": 0, "del": 0, "bg": "rgba(0, 0, 0, 0)", "border": "none", "shadow": "none"}, chrome
                stored = cdp.send("Runtime.evaluate", {
                    "contextId": extension_world,
                    "expression": "window.__bwExtensionStore.get('webCardPinsV1').then(s => Object.values(s || {}).flat().find(p => p.kind === 'html' && p.html?.content?.includes('工具卡拖放测试') && p.anchor?.selector))",
                    "awaitPromise": True, "returnByValue": True,
                }).get("result", {}).get("value")
                assert stored and stored["cid"] == "drag-regression" and stored["html"]["cid"] == "drag-regression", stored
                identity = cdp.send("Runtime.evaluate", {
                    "contextId": extension_world,
                    "expression": """(() => ({source:window.__bwShadow.querySelector('#asst-thread .vc-card[data-vc-cid=\"drag-regression\"]')?.dataset.vcCid||'',pin:window.__bwPinShadow.querySelector('.bw-page-pin .vc-card')?.dataset.vcCid||''}))()""",
                    "returnByValue": True,
                }).get("result", {}).get("value")
                assert identity == {"source": "drag-regression", "pin": "drag-regression"}, identity

                # A page pin is owned by exactly one pointer. Foreign move/up/cancel
                # events cannot finish or drop it; matching cancel only restores the
                # original placement. Anchor hit-testing must not toggle either
                # extension host's visibility on every frame.
                pointer_contract = cdp.send("Runtime.evaluate", {
                    "contextId": extension_world,
                    "expression": """new Promise(resolve => {
                      const box=window.__bwPinShadow.querySelector('.bw-page-pin');
                      const h=box.querySelector('.vc-card-hd'),r=h.getBoundingClientRect();
                      const before={left:box.style.left,top:box.style.top,count:window.__bwPinShadow.querySelectorAll('.bw-page-pin').length};
                      let captured=null,releases=[],visibilityMutations=0;
                      h.setPointerCapture=id=>{captured=id;};
                      h.hasPointerCapture=id=>captured===id;
                      h.releasePointerCapture=id=>{releases.push(id);captured=null;};
                      const observers=[window.__bwReaderHost,window.__bwPinHost].map(host=>{
                        const mo=new MutationObserver(ms=>{visibilityMutations+=ms.filter(m=>m.attributeName==='style').length;});
                        mo.observe(host,{attributes:true,attributeFilter:['style']});return mo;
                      });
                      const fire=(target,type,id,x,y,buttons)=>target.dispatchEvent(new PointerEvent(type,{
                        pointerId:id,pointerType:'touch',button:0,buttons,bubbles:true,
                        clientX:x,clientY:y
                      }));
                      const x=r.left+20,y=r.top+12;
                      fire(h,'pointerdown',70,x,y,1);
                      fire(document,'pointermove',70,x+9,y,1);
                      setTimeout(()=>{
                        const prechargeCancelled=captured===null&&!box.classList.contains('bw-pin-dragging')&&box.style.transform==='';
                        fire(h,'pointerdown',71,x,y,1);
                        setTimeout(()=>{
                          fire(document,'pointermove',72,x+260,y+180,1);
                          fire(document,'pointerup',72,x+260,y+180,0);
                          const foreignUpKept=box.classList.contains('bw-pin-dragging')&&box.style.transform==='';
                          fire(document,'pointermove',71,x+90,y+70,1);
                          requestAnimationFrame(()=>{
                            const ownerMoved=box.classList.contains('bw-pin-dragging')&&box.style.transform!=='';
                            fire(document,'pointercancel',72,x+90,y+70,0);
                            const foreignCancelKept=box.classList.contains('bw-pin-dragging');
                            fire(document,'pointercancel',71,x+90,y+70,0);
                            setTimeout(()=>{
                              observers.forEach(m=>m.disconnect());
                              resolve({prechargeCancelled,foreignUpKept,ownerMoved,foreignCancelKept,captured,releases,
                                visibilityMutations,dragging:box.classList.contains('bw-pin-dragging'),
                                transform:box.style.transform,left:box.style.left,top:box.style.top,
                                count:window.__bwPinShadow.querySelectorAll('.bw-page-pin').length,before});
                            },40);
                          });
                        },460);
                      },460);
                    })""",
                    "awaitPromise": True,
                    "returnByValue": True,
                }).get("result", {}).get("value")
                assert pointer_contract["prechargeCancelled"], pointer_contract
                assert pointer_contract["foreignUpKept"], pointer_contract
                assert pointer_contract["ownerMoved"], pointer_contract
                assert pointer_contract["foreignCancelKept"], pointer_contract
                assert pointer_contract["releases"] == [70, 71], pointer_contract
                assert pointer_contract["visibilityMutations"] == 0, pointer_contract
                assert not pointer_contract["dragging"] and pointer_contract["transform"] == "", pointer_contract
                assert pointer_contract["count"] == pointer_contract["before"]["count"] == 1, pointer_contract
                assert pointer_contract["left"] == pointer_contract["before"]["left"], pointer_contract
                assert pointer_contract["top"] == pointer_contract["before"]["top"], pointer_contract

                # 学习卡的外壳 cid 与状态机 gid 是同一个主键；任一宿主翻面，另一宿主即时同步。
                flash_identity = cdp.send("Runtime.evaluate", {
                    "contextId": extension_world,
                    "expression": """(() => {
                      const wrap=document.createElement('div');wrap.id='flash-identity-regression';window.__bwShadow.appendChild(wrap);
                      const cards=[{type:'basic',front:'唯一编号正面',back:'共享状态背面',_st:'learn'}];
                      const made=[];
                      for(let i=0;i<2;i++){
                        const host=document.createElement('div');wrap.appendChild(host);
                        const el=RC.voiceCard.renderInto(host,{label:'学习卡片',cid:'fcg-identity-regression',mount:bd=>RC.flashcard.mountState(bd,cards,{gid:'fcg-identity-regression',nopin:true,bare:true})});
                        made.push(el);
                      }
                      const bodies=made.map(el=>el.querySelector('.vc-card-bd'));
                      const sameArray=bodies[0].__fc.cards===bodies[1].__fc.cards;
                      bodies[0].querySelector('[data-fc="reveal"]').click();
                      const out={cids:made.map(el=>el.dataset.vcCid),sameArray,backs:bodies.map(b=>!!b.querySelector('.fc-back')),show:bodies.map(b=>b.__fc.cards[0]._showBack)};
                      wrap.remove();return out;
                    })()""",
                    "returnByValue": True,
                }).get("result", {}).get("value")
                assert flash_identity == {
                    "cids": ["fcg-identity-regression", "fcg-identity-regression"],
                    "sameArray": True, "backs": [True, True], "show": [True, True],
                }, flash_identity

                # Anki 制卡结果与复习卡共用唯一 renderEntity 组合入口：
                # 一个 vc-card 外壳、稳定 id/cid/gid、受控评分不自行 POST，并且拖出
                # 快照保留来源/Anki 身份而显示投影继续隐藏 provenance。
                learning_entity = cdp.send("Runtime.evaluate", {
                    "contextId": extension_world,
                    "expression": """new Promise(resolve => {
                      const host=document.createElement('div');host.id='learning-entity-regression';
                      window.__bwShadow.appendChild(host);
                      window.__learningCardXss=0;
                      let reveals=0,rates=[];
                      const source={
                        id:9001,note_id:19001,entity_id:'card_ab12cd',entity_index:2,
                        source_ref:'note:000-proof.md',source_url:'obsidian://open?vault=Test&file=000-proof',
                        type:'basic',front:'统一卡正面',
                        back:'统一卡背面 来源：[[000-proof]] 原因：测试',
                        _displayFrontHtml:'<b>统一卡正面</b>',
                        _displayBackHtml:'<i>统一卡背面</i><img src="x" onerror="window.__learningCardXss=1"><a class="unsafe-card-link" href="javascript:window.__learningCardXss=2">坏链接</a>'
                      };
                      const made=RC.flashcard.renderEntity(host,{
                        label:'🎴 复习卡',card:source,gid:'anki_card_9001',mode:'review',
                        showBack:false,className:'rv-review-card',
                        onReveal:()=>{reveals++;},
                        onRate:ease=>{rates.push(ease);}
                      });
                      const before={
                        vc:host.querySelectorAll('.vc-card').length,
                        cid:made?.el?.dataset.vcCid||'',
                        learningId:made?.el?.dataset.learningCardId||'',
                        pinnable:made?.el?.classList.contains('vc-pinnable')||false,
                        bare:made?.bd?.classList.contains('fc-bare')||false,
                        back:!!made?.bd?.querySelector('.fc-back'),
                        pin:!!made?.bd?.querySelector('.fc-pin')
                      };
                      made.bd.querySelector('[data-fc="reveal"]').click();
                      const payload=made.el.querySelector('.vc-card-hd').__bwDragToDockBinding.payloadFn();
                      const cards=JSON.parse(payload.raw);
                      const visible=made.bd.textContent;
                      made.bd.querySelector('.fc-e[data-ease="3"]').click();
                      const drops=[],hd=made.el.querySelector('.vc-card-hd'),rect=hd.getBoundingClientRect();
                      const originalCreate=RC.stickynote.createCardAt;
                      RC.stickynote.createCardAt=(x,y,dropCards,gid)=>{drops.push({x,y,cards:dropCards,gid});return true;};
                      const fire=(target,type,x,y,buttons)=>target.dispatchEvent(new PointerEvent(type,{
                        pointerId:901,pointerType:'touch',button:0,buttons,bubbles:true,clientX:x,clientY:y
                      }));
                      fire(hd,'pointerdown',rect.left+18,rect.top+12,1);
                      setTimeout(()=>{
                        fire(document,'pointermove',250,280,1);
                        fire(document,'pointerup',250,280,0);
                        RC.stickynote.createCardAt=originalCreate;
                        const unsafeLink=made.bd.querySelector('.unsafe-card-link');
                        const out={before,reveals,rates,payload:{
                          id:payload.id,cid:payload.cid,gid:payload.gid,kind:payload.kind
                        },card:cards[0],visible,eases:made.bd.querySelectorAll('.fc-e').length,drops,
                          unsafe:{xss:window.__learningCardXss,onerror:!!made.bd.querySelector('[onerror]'),
                            href:unsafeLink?.getAttribute('href')||''}};
                        host.remove();resolve(out);
                      },460);
                    })""",
                    "awaitPromise": True,
                    "returnByValue": True,
                }).get("result", {}).get("value")
                assert learning_entity["before"] == {
                    "vc": 1,
                    "cid": "anki_card_9001",
                    "learningId": "anki_card_9001",
                    "pinnable": True,
                    "bare": True,
                    "back": False,
                    "pin": False,
                }, learning_entity
                assert learning_entity["reveals"] == 1, learning_entity
                assert learning_entity["rates"] == [3], learning_entity
                assert learning_entity["payload"] == {
                    "id": "anki_card_9001",
                    "cid": "anki_card_9001",
                    "gid": "anki_card_9001",
                    "kind": "cards",
                }, learning_entity
                assert learning_entity["eases"] == 4, learning_entity
                assert learning_entity["card"]["id"] == 9001, learning_entity
                assert learning_entity["card"]["note_id"] == 19001, learning_entity
                assert learning_entity["card"]["entity_id"] == "card_ab12cd", learning_entity
                assert learning_entity["card"]["entity_index"] == 2, learning_entity
                assert learning_entity["card"]["source_ref"] == "note:000-proof.md", learning_entity
                assert learning_entity["card"]["_showBack"] is True, learning_entity
                assert "统一卡背面" in learning_entity["visible"], learning_entity
                assert "来源：" not in learning_entity["visible"], learning_entity
                assert learning_entity["unsafe"] == {
                    "xss": 0,
                    "onerror": False,
                    "href": "",
                }, learning_entity
                assert len(learning_entity["drops"]) == 1, learning_entity
                assert learning_entity["drops"][0]["gid"] == "anki_card_9001", learning_entity
                assert learning_entity["drops"][0]["cards"][0]["id"] == 9001, learning_entity
                assert learning_entity["drops"][0]["cards"][0]["source_ref"] == "note:000-proof.md", learning_entity

                # 未保存草稿与保存后的卡面共用安全富文本投影：ruby/rt 在默认视图中
                # 立即渲染，原始标记只留在折叠编辑器；编辑后也不能绕过 sanitizer。
                ruby_draft = cdp.send("Runtime.evaluate", {
                    "contextId": extension_world,
                    "expression": """(() => {
                      const host=document.createElement('div');host.id='ruby-draft-regression';
                      window.__bwShadow.appendChild(host);window.__rubyDraftXss=0;
                      const made=RC.flashcard.renderEntity(host,{
                        label:'Reader 卡片草稿',gid:'card_abcd',mode:'draft',entityRegistered:false,
                        card:{type:'basic',front:'イスラム<ruby>教<rt>きょう</rt></ruby>で、<ruby>食<rt>た</rt></ruby>べてはいけないものは何ですか。',
                          back:'<ruby>豚肉<rt>ぶたにく</rt></ruby>、アルコール、<ruby>血液<rt>けつえき</rt></ruby>などです。'}
                      });
                      const previews=[...made.bd.querySelectorAll('.fc-draft-preview')];
                      const editor=made.bd.querySelector('.fc-draft-editor');
                      const ta=made.bd.querySelector('.fc-ed[data-f="front"]');
                      const before={
                        previews:previews.length,ruby:made.bd.querySelectorAll('.fc-draft-preview ruby').length,
                        rt:made.bd.querySelectorAll('.fc-draft-preview rt').length,
                        literal:previews.some(el=>el.textContent.includes('<ruby>')),
                        collapsed:!!editor&&!editor.open,raw:ta?.value||'',
                        save:!!made.bd.querySelector('.fc-add'),del:!!made.bd.querySelector('.fc-del')
                      };
                      ta.value='<ruby>語<rt>ご</rt></ruby><img src="x" onerror="window.__rubyDraftXss=1"><script>window.__rubyDraftXss=2</script>';
                      ta.dispatchEvent(new Event('input',{bubbles:true}));
                      const live=made.bd.querySelector('.fc-draft-preview[data-preview="front"]');
                      const after={ruby:live.querySelectorAll('ruby').length,rt:live.querySelectorAll('rt').length,
                        script:!!live.querySelector('script'),onerror:!!live.querySelector('[onerror]'),
                        xss:window.__rubyDraftXss,literal:live.textContent.includes('<ruby>')};
                      host.remove();return {before,after};
                    })()""",
                    "returnByValue": True,
                }).get("result", {}).get("value")
                assert ruby_draft["before"]["previews"] == 2, ruby_draft
                assert ruby_draft["before"]["ruby"] == 4, ruby_draft
                assert ruby_draft["before"]["rt"] == 4, ruby_draft
                assert ruby_draft["before"]["literal"] is False, ruby_draft
                assert ruby_draft["before"]["collapsed"] is True, ruby_draft
                assert "<ruby>" in ruby_draft["before"]["raw"], ruby_draft
                assert ruby_draft["before"]["save"] is True, ruby_draft
                assert ruby_draft["before"]["del"] is True, ruby_draft
                assert ruby_draft["after"] == {
                    "ruby": 1, "rt": 1, "script": False, "onerror": False,
                    "xss": 0, "literal": False,
                }, ruby_draft

                # 同 cid 的两个渲染实例共享选中状态：长按侧栏原卡，页面卡必须同时变紫框。
                selection_body = card.locator(".vc-card-bd")
                hb_sync = selection_body.bounding_box(); assert hb_sync
                page.mouse.move(hb_sync["x"] + min(28, hb_sync["width"] / 4), hb_sync["y"] + min(18, hb_sync["height"] / 2))
                page.mouse.down(); time.sleep(0.68); page.mouse.up(); time.sleep(0.12)
                synced = cdp.send("Runtime.evaluate", {
                    "contextId": extension_world,
                    "expression": """(() => ({source:window.__bwShadow.querySelector('#asst-thread .vc-card[data-vc-cid=\"drag-regression\"]')?.classList.contains('vc-picked'),pin:window.__bwPinShadow.querySelector('.bw-page-pin .vc-card[data-vc-cid=\"drag-regression\"]')?.classList.contains('vc-picked')}))()""",
                    "returnByValue": True,
                }).get("result", {}).get("value")
                assert synced == {"source": True, "pin": True}, synced

                before_scroll = pin.bounding_box()
                assert before_scroll
                top_before = cdp.send("Runtime.evaluate", {"contextId": extension_world, "expression": "window.__bwPinShadow.querySelector('.bw-page-pin').style.top", "returnByValue": True}).get("result", {}).get("value")
                page.evaluate("() => { document.body.style.minHeight='3200px'; scrollTo(0,180); }")
                time.sleep(0.25)
                after_scroll = pin.bounding_box()
                top_after = cdp.send("Runtime.evaluate", {"contextId": extension_world, "expression": "window.__bwPinShadow.querySelector('.bw-page-pin').style.top", "returnByValue": True}).get("result", {}).get("value")
                assert after_scroll and abs((before_scroll["y"] - after_scroll["y"]) - 180) < 4, (
                    before_scroll, after_scroll
                )
                assert top_after == top_before, (top_before, top_after)  # document layer scrolls natively; JS did not chase it

                # A pinned card uses document-level capture, so it keeps moving after the pointer leaves
                # its small header. During the gesture, the prospective bound page element is highlighted.
                pin_handle = pin.locator(".vc-card-hd")
                ph = pin_handle.bounding_box(); assert ph
                old_pin = pin.bounding_box(); assert old_pin
                page.mouse.move(ph["x"] + min(30, ph["width"] / 3), ph["y"] + min(12, ph["height"] / 2))
                page.mouse.down()
                time.sleep(0.48)
                page.mouse.move(610, 430, steps=18)
                mid_drag = cdp.send("Runtime.evaluate", {
                    "contextId": extension_world,
                    "expression": """(() => {const p=window.__bwPinShadow.querySelector('.bw-page-pin'),f=window.__bwShadow.querySelector('.bw-web-anchor-fx');return {drag:p.classList.contains('bw-pin-dragging'),transform:getComputedStyle(p).transform,fx:getComputedStyle(f).display};})()""",
                    "returnByValue": True,
                }).get("result", {}).get("value")
                assert mid_drag["drag"] and mid_drag["transform"] != "none" and mid_drag["fx"] == "block", mid_drag
                page.mouse.move(700, 500, steps=8)
                page.mouse.up()
                time.sleep(0.3)
                moved_pin = pin.bounding_box(); assert moved_pin
                assert abs(moved_pin["x"] - old_pin["x"]) > 220 and abs(moved_pin["y"] - old_pin["y"]) > 120, (old_pin, moved_pin)

                # The original side card remains in the conversation. Drag a copy to the bottom dock,
                # then drag that favorite upward and release over the page in one continuous gesture.
                hb = handle.bounding_box()
                assert hb
                page.mouse.move(hb["x"] + min(36, hb["width"] / 4), hb["y"] + min(14, hb["height"] / 2))
                time.sleep(0.1)
                page.mouse.down(); time.sleep(0.48); page.mouse.move(520, 805, steps=14); page.mouse.up()
                dock_btn = page.locator("#bw-reader-host").locator("#vc-dock-btn")
                dock_btn.wait_for(state="visible", timeout=5000)
                dock_btn.click()
                time.sleep(0.45)  # the first server-backed dock load can repaint the panel once
                favorite = page.locator("#bw-reader-host").locator("#vc-dock-panel .vc-dk-card").last
                favorite.wait_for(state="visible")
                favorite_cid = cdp.send("Runtime.evaluate", {"contextId": extension_world, "expression": """(()=>{const es=window.__bwShadow.querySelectorAll('#vc-dock-panel .vc-dk-card');return es[es.length-1]?.dataset.vcCid||''})()""", "returnByValue":True}).get("result",{}).get("value")
                assert favorite_cid == "drag-regression", favorite_cid
                cdp.send("Runtime.evaluate", {"contextId": extension_world, "expression": """(() => {
                  window.__favDragTrace=[]; const el=window.__bwShadow.querySelector('#vc-dock-panel .vc-dk-card:last-of-type');
                  if(el) ['pointerdown','pointermove','pointerup','pointercancel'].forEach(t=>el.addEventListener(t,e=>window.__favDragTrace.push([t,e.clientX,e.clientY])));
                })()"""})
                fb = cdp.send("Runtime.evaluate", {"contextId": extension_world, "expression": """(() => {
                  const es=window.__bwShadow.querySelectorAll('#vc-dock-panel .vc-dk-card'),e=es[es.length-1],r=e?.getBoundingClientRect();
                  return r&&{x:r.x,y:r.y,width:r.width,height:r.height};
                })()""", "returnByValue":True}).get("result",{}).get("value")
                if not fb:
                    panel_diag = cdp.send("Runtime.evaluate", {"contextId": extension_world, "expression": """({
                      panel:!!window.__bwShadow.querySelector('#vc-dock-panel'),
                      items:window.__bwShadow.querySelectorAll('#vc-dock-panel .vc-dk-card').length,
                      rect:(()=>{const e=window.__bwShadow.querySelector('#vc-dock-panel .vc-dk-card');const r=e?.getBoundingClientRect();return r&&{x:r.x,y:r.y,width:r.width,height:r.height,display:getComputedStyle(e).display,visibility:getComputedStyle(e).visibility}})(),
                      button:window.__bwShadow.querySelector('#vc-dock-btn')?.style.display||'',
                      side:RC.sidedrawer.isOpen?.()
                    })""", "returnByValue":True}).get("result",{}).get("value")
                    raise AssertionError(f"favorite card detached during panel repaint: {panel_diag}")
                # The source panel can repaint once after its server-backed load. Resolve and dispatch
                # synchronously in the extension world so all three events hit the same source node.
                cdp.send("Runtime.evaluate", {"contextId": extension_world, "expression": """(() => {
                  const es=window.__bwShadow.querySelectorAll('#vc-dock-panel .vc-dk-card'),e=es[es.length-1],r=e.getBoundingClientRect();
                  const fire=(t,x,y,buttons)=>e.dispatchEvent(new PointerEvent(t,{pointerId:17,pointerType:'mouse',button:0,buttons:buttons,bubbles:true,clientX:x,clientY:y}));
                  fire('pointerdown',r.x+r.width/2,r.y+r.height/2,1); fire('pointermove',430,280,1); fire('pointerup',430,280,0);
                })()"""})
                try:
                    page.locator("#bw-reader-pins").locator(".bw-page-pin").nth(1).wait_for(state="visible", timeout=5000)
                except Exception:
                    diag = cdp.send("Runtime.evaluate", {"contextId": extension_world, "expression": """Promise.all([window.__bwExtensionStore.get('webCardPinsV1')]).then(([s])=>({
                      pins:window.__bwPinShadow.querySelectorAll('.bw-page-pin').length,
                      pinText:Array.from(window.__bwPinShadow.querySelectorAll('.bw-page-pin')).map(x=>x.textContent),
                      stored:Object.values(s||{}).flat().map(x=>({id:x.id,kind:x.kind,label:x.html?.label,content:x.html?.content})),
                      dock:window.__bwShadow.querySelector('#vc-dock-panel')?.className||'closed',
                      floating:window.__bwShadow.querySelectorAll('#vc-cards .vc-card').length,
                      trace:window.__favDragTrace,
                      toast:window.__bwShadow.querySelector('#rc-toast')?.textContent||''
                    }))""", "awaitPromise":True, "returnByValue":True}).get("result",{}).get("value")
                    raise AssertionError(f"favorite direct drop failed: {diag}")
                assert page.locator("#bw-reader-pins").locator(".bw-page-pin").count() == 2
                pasted_cids = cdp.send("Runtime.evaluate", {"contextId": extension_world, "expression": "[...window.__bwPinShadow.querySelectorAll('.bw-page-pin .vc-card')].map(x=>x.dataset.vcCid)", "returnByValue":True}).get("result",{}).get("value")
                assert pasted_cids == ["drag-regression", "drag-regression"], pasted_cids

                # A side-thread card is the source; dragging it to the top-left trash only discards
                # the transient copy and must never erase the original conversation content.
                hb = handle.bounding_box(); assert hb
                page.mouse.move(hb["x"] + min(30, hb["width"] / 3), hb["y"] + min(12, hb["height"] / 2))
                page.mouse.down(); time.sleep(0.48); page.mouse.move(22, 20, steps=16); page.mouse.up(); time.sleep(0.35)
                source_card = page.locator("#bw-reader-host").locator("#asst-thread .vc-card").filter(has_text="工具卡拖放测试")
                assert source_card.count() >= 1 and source_card.first.is_visible(), "side-thread source card disappeared after trashing its drag copy"
                print("OK: card drag/persistence works; trashing a side copy preserves the conversation source")
            finally:
                with contextlib.suppress(Exception):
                    ctx.close()
        server.shutdown()


if __name__ == "__main__":
    main()
