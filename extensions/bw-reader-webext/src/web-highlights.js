// 普通网页持久高亮：exact quote + prefix/suffix + DOM 文本位置复合锚；渲染优先 CSS Highlights API。
(() => {
  'use strict';
  if (window.__bwPwaProviderOnly || window.__bwPwaBridge || window.__bwWebHighlights) return;
  const PAGE = location.href.split('#')[0];
  const STORE = 'webHighlightsV1';
  const COLORS = ['#fff59d','#a7f3d0','#a3d4ff','#fda4af'];
  let currentRange = null, records = [], live = new Map(), applyTimer = null;

  const css = document.createElement('style');
  css.id = 'bw-web-highlight-css'; document.head.appendChild(css);
  const excluded = n => {
    const p = n.parentElement;
    return !p || !!p.closest('script,style,noscript,textarea,input,select,option,[contenteditable="true"],#bw-reader-host');
  };
  function textIndex() {
    const nodes=[], starts=[]; let text='';
    const w=document.createTreeWalker(document.body||document.documentElement,NodeFilter.SHOW_TEXT,{acceptNode:n=>excluded(n)||!n.nodeValue?NodeFilter.FILTER_REJECT:NodeFilter.FILTER_ACCEPT});
    let n; while((n=w.nextNode())){starts.push(text.length);nodes.push(n);text+=n.nodeValue;}
    return {nodes,starts,text};
  }
  function absoluteOffset(idx,node,off){const i=idx.nodes.indexOf(node);return i<0?-1:idx.starts[i]+off;}
  function pointAt(idx,pos){
    for(let i=idx.nodes.length-1;i>=0;i--){if(idx.starts[i]<=pos)return {node:idx.nodes[i],off:Math.max(0,Math.min(idx.nodes[i].nodeValue.length,pos-idx.starts[i]))};}
    return null;
  }
  function locate(rec){
    const idx=textIndex(); if(!rec.exact)return null;
    let from=0,best=-1,bscore=-1;
    for(;;){const at=idx.text.indexOf(rec.exact,from);if(at<0)break;let sc=0;
      if(rec.prefix&&idx.text.slice(Math.max(0,at-rec.prefix.length),at)===rec.prefix)sc+=2;
      if(rec.suffix&&idx.text.slice(at+rec.exact.length,at+rec.exact.length+rec.suffix.length)===rec.suffix)sc+=2;
      if(sc>bscore){best=at;bscore=sc;}from=at+Math.max(1,rec.exact.length);
    }
    if(best<0)return null;const a=pointAt(idx,best),b=pointAt(idx,best+rec.exact.length);if(!a||!b)return null;
    const r=document.createRange();try{r.setStart(a.node,a.off);r.setEnd(b.node,b.off);return r;}catch(_){return null;}
  }
  function paint(rec,range){
    live.set(rec.id,range);
    if(window.CSS&&CSS.highlights&&window.Highlight){
      const name='bw_'+rec.id.replace(/[^a-zA-Z0-9_-]/g,'');CSS.highlights.set(name,new Highlight(range));
      css.textContent += `::highlight(${name}){background:${rec.color?(rec.color+'99'):'transparent'};text-decoration:${rec.color?'none':'underline dashed #64748b 1.5px'}}`;
      return;
    }
    if(range.startContainer===range.endContainer){const m=document.createElement('mark');m.dataset.bwHighlight=rec.id;m.style.background=rec.color||'#fff59d';try{range.surroundContents(m);}catch(_){}}
  }
  function applyAll(){
    css.textContent='';live.clear();
    if(window.CSS&&CSS.highlights)for(const k of Array.from(CSS.highlights.keys()))if(String(k).startsWith('bw_'))CSS.highlights.delete(k);
    records.forEach(rec=>{const r=locate(rec);if(r)paint(rec,r);});
  }
  async function load(){const all=(await window.__bwExtensionStore.get(STORE))||{};records=Array.isArray(all[PAGE])?all[PAGE]:[];applyAll();}
  async function persist(){const all=(await window.__bwExtensionStore.get(STORE))||{};all[PAGE]=records.slice(-300);await window.__bwExtensionStore.set(STORE,all);}

  // CSS Highlight API 没有可点击 DOM；按 Range 的实时 client rect 做命中，触屏/鼠标双击都能打开原版编辑器。
  function hitAt(x,y){
    for(let i=records.length-1;i>=0;i--){const rec=records[i],r=live.get(rec.id)||locate(rec);if(!r)continue;
      for(const q of Array.from(r.getClientRects()))if(x>=q.left-5&&x<=q.right+5&&y>=q.top-5&&y<=q.bottom+5)return {rec,range:r,rect:q};
    }return null;
  }
  const editAnchor=document.createElement('span');
  editAnchor.style.cssText='position:fixed;width:2px;height:2px;pointer-events:none;z-index:1';window.__bwRoot.appendChild(editAnchor);
  let lastOpen=0,lastOpenId='';
  function openEditor(hit){
    if(!hit?.rec||!window.RC?.highlight)return;
    const now=Date.now();if(hit.rec.id===lastOpenId&&now-lastOpen<500)return;lastOpen=now;lastOpenId=hit.rec.id;
    const rec=hit.rec,q=hit.rect||hit.range?.getBoundingClientRect?.();
    editAnchor.style.left=Math.max(0,q?.left||innerWidth/2)+'px';editAnchor.style.top=Math.max(0,q?.top||innerHeight/2)+'px';
    RC.highlight.closeEditor?.();
    RC.highlight.openEditor({colors:COLORS,current:rec.color||'',note:rec.note||'',preview:rec.text||rec.exact||'',sentence:rec.sentence||'',body:rec.body||'',kind:rec.kind||'note',anchorEl:editAnchor,placeBelow:true,silent:true,
      onColor:async c=>{rec.color=c;await persist();applyAll();},
      onNote:async t=>{rec.note=String(t||'');await persist();},
      onDelete:async()=>{if(!confirm('删除这条网页高亮？'))return false;await window.__bwWebHighlights.remove(rec.id);RC.toast?.('已删除');return true;}
    });
  }
  document.addEventListener('selectionchange',()=>{try{const s=getSelection();if(s&&s.rangeCount&&!s.isCollapsed&&!window.__bwShadow.contains(s.anchorNode))currentRange=s.getRangeAt(0).cloneRange();}catch(_){}},{passive:true});

  window.__bwWebHighlights={
    async save(color,meta){
      let r=currentRange;try{const s=getSelection();if(s&&s.rangeCount&&!s.isCollapsed)r=s.getRangeAt(0).cloneRange();}catch(_){}
      if(!r||r.collapsed)throw new Error('原网页选区已经失效，请重新选择');
      const exact=String(r.toString()||'').trim();if(!exact)throw new Error('没有可高亮的文字');
      const idx=textIndex(),start=absoluteOffset(idx,r.startContainer,r.startOffset),end=absoluteOffset(idx,r.endContainer,r.endOffset);
      if(start<0||end<start)throw new Error('这个动态区域暂时无法建立稳定锚点');
      const rec={id:'wh_'+Date.now().toString(36)+Math.random().toString(36).slice(2,7),url:PAGE,text:exact,exact,
        prefix:idx.text.slice(Math.max(0,start-48),start),suffix:idx.text.slice(end,end+48),color:color||'#fff59d',
        note:String(meta?.body||meta?.note||''),sentence:String(meta?.sentence||''),kind:String(meta?.kind||'note'),time:Date.now()};
      records.push(rec);paint(rec,r.cloneRange());await persist();RC.toast?.('已固定到当前网页');return rec;
    },
    list(){return records.slice();},
    jump(id){const r=live.get(id)||locate(records.find(x=>x.id===id)||{});if(!r)return false;const el=r.startContainer.parentElement;el?.scrollIntoView?.({behavior:'smooth',block:'center'});return true;},
    async remove(id){records=records.filter(x=>x.id!==id);await persist();applyAll();},
    refresh(){applyAll();}
  };
  window.RC?.actions?.bind?.('highlight.save',p=>window.__bwWebHighlights.save(p.color,p),{owner:'web-extension',runtime:'extension',storage:'extension-local-gateway'});
  let activeHit=null,activePointer=null;
  const gesture=RC.highlight?.gesture?.({doubleTapMs:420,moveTol:12,onDoubleTap:id=>{const rec=records.find(x=>x.id===id),r=rec&&(live.get(id)||locate(rec));if(rec&&r)openEditor({rec,range:r,rect:r.getBoundingClientRect()});}});
  if(gesture){
    document.addEventListener('pointerdown',e=>{if(e.pointerType==='mouse'&&e.button!==0)return;if(window.__bwReaderHost?.contains(e.composedPath?.()[0]))return;const h=hitAt(e.clientX,e.clientY);if(!h)return;activeHit=h;activePointer=e.pointerId;gesture.down(h.rec.id,e.clientX,e.clientY);},{capture:true,passive:true});
    document.addEventListener('pointermove',e=>{if(e.pointerId===activePointer)gesture.move(e.clientX,e.clientY);},{capture:true,passive:true});
    document.addEventListener('pointerup',e=>{if(e.pointerId!==activePointer)return;const h=hitAt(e.clientX,e.clientY);gesture.up(h&&activeHit&&h.rec.id===activeHit.rec.id?h.rec.id:'');activeHit=null;activePointer=null;},{capture:true,passive:true});
    document.addEventListener('pointercancel',e=>{if(e.pointerId===activePointer){gesture.cancel();activeHit=null;activePointer=null;}},{capture:true,passive:true});
  }
  document.addEventListener('dblclick',e=>{const h=hitAt(e.clientX,e.clientY);if(!h)return;e.preventDefault();e.stopPropagation();openEditor(h);},true);
  load().catch(()=>{});
  const mo=new MutationObserver(()=>{clearTimeout(applyTimer);applyTimer=setTimeout(applyAll,800);});
  if(document.body)mo.observe(document.body,{childList:true,subtree:true});
})();
