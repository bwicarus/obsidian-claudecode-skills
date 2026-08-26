// 普通网页卡片钉住层：把卡片绑定到宿主页 DOM 元素，而不是只记录一次性的页面坐标。
// 拖动状态机对齐 rc-stickynote：document capture 保持手势、transform GPU 跟手、实时目标高亮、松手重取锚。
(() => {
  'use strict';
  if (window.__bwPwaProviderOnly || window.__bwPwaBridge || !window.RC || !window.__bwRoot || !window.__bwPinRoot) return;
  const RC=window.RC,root=window.__bwRoot,pinRoot=window.__bwPinRoot,host=window.__bwReaderHost,pinHost=window.__bwPinHost,KEY='webCardPinsV1',PAGE=location.href.split('#')[0];
  const DRAG_HOLD_MS=420,DRAG_SLOP=8;
  // MV3 service worker 冷启动窗口；三次退避足够覆盖，再多就该让用户知道了。
  const PIN_RESTORE_ATTEMPTS=3,PIN_RESTORE_BACKOFF_MS=120;
  let pins=[],dragging=null,placeRaf=0;
  const mkCid=()=>RC.voiceCard?.mkCid?.()||('c'+Date.now().toString(36)+Math.random().toString(36).slice(2,6));
  function ensureIdentity(p){let changed=false;if(p.kind==='flash'){if(!p.gid){const seed=p.cid||mkCid();p.gid=String(seed).startsWith('card_')||String(seed).startsWith('fcg_')?String(seed):('fcg_'+seed);changed=true;}if(p.cid!==p.gid){p.cid=p.gid;changed=true;}}else if(!p.cid){p.cid=(p.kind==='html'&&p.html?.cid)||mkCid();changed=true;}p.cid=String(p.cid);if(p.kind==='html'&&p.html&&p.html.cid!==p.cid){p.html.cid=p.cid;changed=true;}return changed;}
  const st=document.createElement('style');st.textContent=`
/* 1A + 6A：钉页态永远只有共享卡片本体；无第二层窗口、标题栏、静止删除按钮。 */
.bw-page-pin{position:absolute;z-index:850;pointer-events:auto;width:min(360px,calc(100vw - 24px));max-width:calc(100vw - 24px);background:transparent;border:0;border-radius:16px;box-shadow:none;color:inherit;overflow:visible}
.bw-page-pin-body{padding:0}.bw-page-pin .vc-card-hd{cursor:grab;user-select:none;touch-action:none}
.bw-page-pin .vc-card.vc-user-sized:not(.vc-dot):not(.vc-min){width:100%!important}
.bw-page-pin.bw-pin-dragging{z-index:980;will-change:transform;filter:drop-shadow(0 16px 25px rgba(0,0,0,.42));transition:none!important}
#bw-root>.bw-web-anchor-fx{position:fixed;display:none;z-index:820;pointer-events:none;border:2px solid rgba(35,148,255,.88);border-radius:5px;background:rgba(35,148,255,.14);box-shadow:0 0 0 2px rgba(255,255,255,.34),0 0 18px rgba(35,148,255,.46);transition:left .05s linear,top .05s linear,width .05s linear,height .05s linear}
`;window.__bwHead.appendChild(st);window.__bwPinHead?.appendChild(st.cloneNode(true));

  const fx=document.createElement('div');fx.className='bw-web-anchor-fx';root.appendChild(fx);
  async function persist(){const all=(await window.__bwExtensionStore.get(KEY))||{};all[PAGE]=pins.slice(-80).map(p=>{const q={...p};delete q._el;return q;});await window.__bwExtensionStore.set(KEY,all);}
  function schedulePlace(){if(placeRaf)return;placeRaf=requestAnimationFrame(()=>{placeRaf=0;placeAll();});}

  // elementsFromPoint 会把 shadow 内节点重定向成宿主；直接过滤两个扩展宿主即可继续
  // 取得下面的网页元素。拖动每帧切 visibility 会触发样式/绘制并造成卡顿与闪烁。
  function isReaderUiElement(el){
    if(!el)return false;
    return el===host||el===pinHost||el.id==='bw-reader-host'||el.id==='bw-reader-pins'||
      !!host?.contains?.(el)||!!pinHost?.contains?.(el);
  }
  function usefulElementAt(x,y){
    const els=document.elementsFromPoint(x,y);
    let el=els.find(e=>e&&e.nodeType===1&&!isReaderUiElement(e)&&e!==document.documentElement&&e!==document.body&&!/^(SCRIPT|STYLE|LINK|META|NOSCRIPT)$/.test(e.tagName));
    if(!el)return null;
    // 过小的纯装饰节点绑定到最近有可见面积的父元素；正文 span/a/img/td 等仍保持精确粒度。
    let r=el.getBoundingClientRect();
    while(el.parentElement&&el.parentElement!==document.body&&(r.width<8||r.height<8)){el=el.parentElement;r=el.getBoundingClientRect();}
    return r.width>0&&r.height>0?el:null;
  }
  function selectorFor(el){
    try{if(el.id&&document.querySelectorAll('#'+CSS.escape(el.id)).length===1)return '#'+CSS.escape(el.id);}catch(_){}
    const parts=[];let cur=el;
    while(cur&&cur.nodeType===1&&cur!==document.body&&parts.length<10){
      let part=cur.tagName.toLowerCase();
      const par=cur.parentElement;if(!par)break;
      const same=Array.from(par.children).filter(x=>x.tagName===cur.tagName);
      if(same.length>1)part+=':nth-of-type('+(same.indexOf(cur)+1)+')';
      parts.unshift(part);
      if(par.id){parts.unshift('#'+CSS.escape(par.id));break;}
      cur=par;
    }
    return (parts[0]?.startsWith('#')?'':'body>')+parts.join('>');
  }
  function makeAnchor(x,y){
    const el=usefulElementAt(x,y);if(!el)return null;
    const r=el.getBoundingClientRect(),sel=selectorFor(el);
    if(!sel)return null;
    return {kind:'web',selector:sel,rx:Math.max(0,Math.min(1,(x-r.left)/Math.max(1,r.width))),ry:Math.max(0,Math.min(1,(y-r.top)/Math.max(1,r.height))),tag:el.tagName.toLowerCase(),hint:(el.textContent||el.getAttribute?.('alt')||'').trim().replace(/\s+/g,' ').slice(0,80)};
  }
  function anchorAt(x,y){
    const pts=[[x,y],[x,y-18],[x,y+18],[x-24,y],[x+24,y]];
    for(const pt of pts){const a=makeAnchor(pt[0],pt[1]);if(a)return a;}
    return null;
  }
  function resolveAnchor(a){
    if(!a?.selector)return null;
    let el=null;try{el=document.querySelector(a.selector);}catch(_){}
    if(!el||host?.contains(el))return null;
    const r=el.getBoundingClientRect();if(!r.width||!r.height)return null;
    return {el,rect:r,x:r.left+Math.max(0,Math.min(1,Number(a.rx)||0))*r.width,y:r.top+Math.max(0,Math.min(1,Number(a.ry)||0))*r.height};
  }
  function showAnchorFx(x,y){
    const a=makeAnchor(x,y),m=resolveAnchor(a);
    if(!m){hideAnchorFx();return null;}
    const r=m.rect;fx.style.left=(r.left-3)+'px';fx.style.top=(r.top-3)+'px';fx.style.width=(r.width+6)+'px';fx.style.height=(r.height+6)+'px';fx.style.display='block';
    return a;
  }
  function hideAnchorFx(){fx.style.display='none';}

  function place(box,p){
    if(box.classList.contains('bw-pin-dragging'))return;
    const m=resolveAnchor(p.anchor);
    if(m){box.style.left=(scrollX+m.x)+'px';box.style.top=(scrollY+m.y)+'px';return;}
    box.style.left=(Number(p.x)||0)+'px';box.style.top=(Number(p.y)||0)+'px';
  }
  function placeAll(){pinRoot.querySelectorAll('.bw-page-pin').forEach(box=>{const p=pins.find(x=>x.id===box.dataset.id);if(p)place(box,p);});}

  function bindDrag(box,handles,p){
    handles=(Array.isArray(handles)?handles:[handles]).filter(Boolean);
    function paint(g){box.style.transform='translate3d('+g.dx+'px,'+g.dy+'px,0) scale(1.025)';const r=box.getBoundingClientRect();showAnchorFx(r.left+1,r.top+1);const x=r.left+1,y=r.top+1;RC.voiceCard?.trash?.show?.(true);RC.voiceCard?.trash?.hot?.(RC.voiceCard.trash.inZone(x,y));RC.voiceCard?.favorite?.hint?.(RC.voiceCard.favorite.inZone(x,y));}
    function move(session,e){
      const g=dragging;if(!g||g.box!==box||session!==g.session)return;
      g.dx=session.dx;g.dy=session.dy;g.moved=!!session.moved;
      if(!g.raf)g.raf=requestAnimationFrame(()=>{g.raf=0;if(dragging===g)paint(g);});
    }
    function finish(session,e,aborted){
      const g=dragging;if(!g||g.box!==box)return;
      if(session&&session!==g.session)return;
      if(g.raf){cancelAnimationFrame(g.raf);g.raf=0;if(!aborted&&g.moved)paint(g);}dragging=null;hideAnchorFx();RC.voiceCard?.trash?.show?.(false);RC.voiceCard?.favorite?.hint?.(false);
      if(!aborted&&g.moved){
        const r=box.getBoundingClientRect(),x=r.left+1,y=r.top+1;
        if(RC.voiceCard?.trash?.inZone?.(x,y)){pins=pins.filter(q=>q.id!==p.id);box.remove();persist();RC.toast?.('已删除');return;}
        if(RC.voiceCard?.favorite?.inZone?.(x,y)){const rec=p.kind==='html'?{label:p.html?.label||'工具卡片',raw:p.html?.content||'',isHtml:!!p.html?.isHtml,text:p.html?.content||'',kind:'html',cid:p.cid}:{label:'学习卡片',raw:JSON.stringify(p.cards||[]),isHtml:false,text:RC.stickynote?.cardContextText?.(p.cards||[])||'',kind:'cards',cid:p.cid,gid:p.gid};RC.voiceCard.favorite.save(rec);box.classList.remove('bw-pin-dragging');box.style.transform='';place(box,p);return;}
        p.x=scrollX+r.left;p.y=scrollY+r.top;p.anchor=anchorAt(x,y);
      }
      box.classList.remove('bw-pin-dragging');box.style.transform='';place(box,p);if(!aborted&&g.moved)persist();
    }
    if(!RC.voiceCard?.bindChargedDrag)return;
    RC.voiceCard.bindChargedDrag(handles,{
      holdMs:DRAG_HOLD_MS,
      slop:DRAG_SLOP,
      dragSlop:4,
      feedbackEl:box,
      canStart(e){
        const control=e.target.closest?.('button,a,input,textarea,select');
        return !dragging&&
          (e.button===undefined||e.button===0)&&
          (!control||control.classList.contains('vc-card-dot'));
      },
      onReady(session,e){
        if(dragging)return;
        e?.stopPropagation?.();
        dragging={box,session,dx:0,dy:0,moved:false,raf:0};
        box.classList.add('bw-pin-dragging');
      },
      onMove:move,
      onEnd(session,e){finish(session,e,false);},
      onCancel(session,e){finish(session,e,true);}
    });
  }

  function mount(p){
    ensureIdentity(p);
    const box=document.createElement('section');box.className='bw-page-pin'+(p.kind==='html'?' bw-html-pin':'');box.dataset.id=p.id;place(box,p);
    const label=p.kind==='html'?(p.html?.label||'工具卡片'):'学习卡片';
    box.innerHTML='<div class="bw-page-pin-body"></div>';pinRoot.appendChild(box);
    const body=box.querySelector('.bw-page-pin-body');
    const onSize=size=>{box.style.width=size?(Math.max(180,Math.min(720,Number(size.w)||360))+'px'):'';};
    if(p.kind==='html'&&p.html){
      const h=p.html;let mounted=false;
      try{mounted=!!RC.voiceCard?.renderInto?.(body,{text:h.content||'',label:h.label||'卡片',isHtml:!!h.isHtml,type:h.type||'',icon:h.icon||'🗂',form:h.form||'full',cid:p.cid,onSize,onForm:f=>{h.form=f;persist();}});}catch(_){}
      if(!mounted){const d=document.createElement('div');if(h.isHtml)d.innerHTML=h.content||'';else d.textContent=h.content||'';body.appendChild(d);}try{RC.typeset?.(body);}catch(_){}
    }else{
      let mounted=false;
      const onStateChange=(snapshot,reason)=>{
        if(!Array.isArray(snapshot)||!snapshot.length)return;
        try{p.cards=JSON.parse(JSON.stringify(snapshot));}catch(_){p.cards=snapshot;}
        persist();
      };
      try{mounted=!!RC.voiceCard?.renderInto?.(body,{label:'学习卡片',type:'#b9a8ff',icon:'🎴',form:p.form||'full',cid:p.cid,onSize,onForm:f=>{p.form=f;persist();},mount:bd=>RC.flashcard?.mountState?.(bd,p.cards||[],{gid:p.gid,nopin:true,bare:true,onStateChange})});}catch(_){}
      if(!mounted)RC.flashcard?.mountState?.(body,p.cards||[],{gid:p.gid||p.id,nopin:true,onStateChange});
    }
    // 与 PWA 固定卡共用 rc-stickynote 的唯一上下文绑定：
    // placement id(wp_*) 不进入语义身份，owner 负责稳定 cid/处处高亮，
    // 只有展开正文承担长按；卡头只负责下方 bindDrag 的蓄力拖动。
    if(p.kind==='flash'){
      const cardEl=body.querySelector('.vc-card')||body.querySelector('.fc-wrap')||body;
      const stateBody=cardEl.querySelector?.('.vc-card-bd')||body;
      RC.stickynote?.bindCardSelection?.(cardEl,()=>stateBody?.__fc?.cards||p.cards||[],p.gid,'web-page-placement');
    }else if(p.kind==='html'){
      const cardEl=body.querySelector('.vc-card')||body;
      RC.stickynote?.bindHtmlCardSelection?.(cardEl,()=>p.html||{},p.cid,'web-page-placement');
    }
    const hd=body.querySelector('.vc-card-hd')||body.querySelector('.fc-card')||body;
    bindDrag(box,[hd,body.querySelector('.vc-card-dot')],p);
    // ⚓ 锚定到正文（2026-08-27 用户对齐诉求：App 的自由便签卡有这个按钮，
    // 网页钉页卡一直没有）。行为对齐 rc-stickynote.anchorFreeCard 的选区
    // 路径：先在网页上选中一段文字，点 ⚓ → web-bind 的选区控制器折成
    // page-chars(page=1) 锚 → persistBoundCard 共享层落库（仓库写入/幂等/
    // tombstone 原样）→ 本卡转移（删除钉页实例，角标由 web-bind 渲染）。
    // 没有选区就明确提示，绝不猜一个位置。flash（学习卡）走 note payload
    // 的锚定通道，形状不同，这里先只做 html 卡（AI 结果卡=实际场景）。
    if(p.kind==='html'){
      const hd0=body.querySelector('.vc-card-hd');
      if(hd0&&!hd0.querySelector('.bw-pin-anchor')){
        const ab=document.createElement('button');
        ab.type='button';ab.className='bw-pin-anchor';ab.title='锚定到正文';
        ab.setAttribute('aria-label','锚定到正文');
        ab.innerHTML='<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="5" r="3"/><path d="M12 8v14"/><path d="M5 12H2a10 10 0 0 0 20 0h-3"/></svg>';
        ab.style.cssText='margin-left:auto;width:22px;height:22px;border-radius:50%;background:rgba(255,255,255,.14);border:none;color:#e8e8ee;display:flex;align-items:center;justify-content:center;cursor:pointer;padding:0;flex:none';
        ab.addEventListener('pointerdown',e=>e.stopPropagation());
        ab.addEventListener('click',async e=>{
          e.stopPropagation();e.preventDefault();
          const sel=window.__bwSelectionController?.current?.();
          if(!sel||!sel.anchor){RC.toast?.('请先选中网页正文的一段文字，再点锚定');return;}
          ab.disabled=true;
          let pt={x:innerWidth/2,y:innerHeight/2};
          try{const r=getSelection().getRangeAt(0).getBoundingClientRect();if(r)pt={x:r.left+r.width/2,y:r.top+r.height/2};}catch(_){}
          const h=p.html||{};
          const res=await Promise.resolve(RC.stickynote?.persistBoundCard?.(sel.anchor,{
            raw:h.content||'',isHtml:!!h.isHtml,text:h.content||'',
            label:h.label||'卡片',contextText:sel.context||'',
            uid:p.cid||'',tone:h.type||''
          },pt)).catch(err=>({ok:false,why:String(err?.message||err).slice(0,80)}));
          ab.disabled=false;
          if(res&&res.ok===true){
            // 转移不并存（与 pinCardToPage 同规矩）：绑定卡即唯一实例。
            pins=pins.filter(q=>q.id!==p.id);box.remove();persist();
            RC.toast?.('✅ 已锚定到选中文字');
          }else{
            // 失败要出声且卡原地不动 —— 不能先删卡再报失败。
            RC.toast?.('锚定失败：'+((res&&res.why)||'共享落库层不可用'));
          }
        });
        hd0.appendChild(ab);
      }
    }
  }
  function newPin(kind,x,y,extra){const p={id:'wp_'+Date.now().toString(36)+Math.random().toString(36).slice(2,6),kind,x:scrollX+x,y:scrollY+y,anchor:anchorAt(x,y),...extra};ensureIdentity(p);pins.push(p);mount(p);persist();return p;}
  RC.stickynote=RC.stickynote||{};
  RC.actions.bind('pin.card',p=>{const gid=p.gid||('fcg_'+mkCid());newPin('flash',p.x,p.y,{cards:p.cards,gid,cid:gid});RC.toast?.('📌 已绑定到当前网页元素');return true;},{owner:'web-extension',runtime:'extension',storage:'extension-local-gateway'});
  RC.actions.bind('pin.html',p=>{const h=p.html;if(!h||!h.content)return false;const cid=h.cid||p.cid||mkCid();newPin('html',p.x,p.y,{cid,html:{content:String(h.content||''),isHtml:!!h.isHtml,label:String(h.label||'卡片'),type:String(h.type||''),icon:String(h.icon||''),form:String(h.form||'full'),cid}});RC.toast?.('📌 工具卡已绑定到当前网页元素');return true;},{owner:'web-extension',runtime:'extension',storage:'extension-local-gateway'});
  RC.actions.bind('pin.anchorFx',p=>p.show?showAnchorFx(p.x,p.y):hideAnchorFx(),{owner:'web-extension',runtime:'extension',storage:'none'});
  RC.stickynote.createCardAt=(x,y,cards,gid)=>RC.actions.run('pin.card',{x,y,cards,gid:gid||''});
  RC.stickynote.createHtmlAt=(x,y,htmlObj)=>RC.actions.run('pin.html',{x,y,html:htmlObj});
  RC.stickynote.anchorFx={show:(x,y)=>RC.actions.run('pin.anchorFx',{show:true,x,y}),hide:()=>RC.actions.run('pin.anchorFx',{show:false})};
  window.__bwWebPins={anchorAt,resolveAnchor,placeAll};
  // 上下滚动由文档坐标层原生完成；只在 resize/DOM 布局改变时重算元素锚点。
  addEventListener('resize',schedulePlace,{passive:true});
  try{new MutationObserver(schedulePlace).observe(document.body,{childList:true,subtree:true,characterData:true,attributes:true,attributeFilter:['style','class','hidden','open']});}catch(_){}
  // 刷新后卡片"消失"的根因：MV3 的 service worker 会休眠，页面重载时
  // content script 立刻请求，worker 若正在冷启动，chrome.runtime.sendMessage
  // 会带着 lastError 回来。原先这里 .catch(()=>{}) 把它吞了 —— 数据其实还在
  // 磁盘上，只是这一次没读出来，而且一声不吭。
  //
  // 所以：短暂失败重试几次（冷启动是暂时的），真的读不到就出声。
  // 绝不能静默留下一个空页面 —— 用户会以为卡片被删了，而且没有任何线索。
  function restorePins(attempt){
    return window.__bwExtensionStore.get(KEY).then(all=>{
      pins=(all?.[PAGE]||[]);let migrated=false;pins.forEach(p=>{if(ensureIdentity(p))migrated=true;mount(p);});
      // 0.2.5 以前只有 x/y：对当前视野内的旧卡原位补一次 DOM 锚，不要求用户删除重贴。
      pins.forEach(p=>{if(p.anchor)return;const x=(Number(p.x)||0)-scrollX,y=(Number(p.y)||0)-scrollY;if(x>=0&&y>=0&&x<innerWidth&&y<innerHeight){const a=anchorAt(x,y);if(a){p.anchor=a;migrated=true;}}});
      if(migrated)persist();schedulePlace();
      return true;
    }).catch(err=>{
      // 退避重试：worker 冷启动通常几十毫秒内就绪。
      if(attempt<PIN_RESTORE_ATTEMPTS){
        return new Promise(r=>setTimeout(r,PIN_RESTORE_BACKOFF_MS*attempt))
          .then(()=>restorePins(attempt+1));
      }
      // 说出来。页面上已固定的卡片没有渲染出来，用户看到的是"卡片没了"，
      // 而真相是"这次没读到"—— 两者的处理完全不同（重试 vs 重新做一张）。
      try{console.warn('[bw] 网页固定卡片未能载入：',err&&err.message||err);}catch(_){}
      try{RC.toast?.('固定卡片暂时载入失败，刷新页面可重试');}catch(_){}
      return false;
    });
  }
  restorePins(1);
})();
