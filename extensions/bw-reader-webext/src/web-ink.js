// 普通网页绘图层：照搬 PDF/便签的主动笔机制(pointerType 判定/自动落笔/双击切工具)，plumbing 适配网页宿主
// ——PDF 靠 .page-wrap capture 拦截落笔，网页没有内容元素，改挂 document capture。跨设备：
//   · Apple Pencil / Surface Pen / Wacom 等主动笔落笔即画(W3C pointerType==='pen' 通吃，无需开关，抬笔即普通浏览)
//   · 手指永不画(只滚页 / 快速双击切 笔↔临时橡皮)   · 桌面「手写模式」开关(set) 让鼠标也能画(无笔退化)
//   · Surface/Wacom 笔尾橡皮擦：pen 带 eraser 位(pointerdown button===5 / buttons&32)时这一笔当橡皮，翻回笔尖即画
//   · 笔与图层唯一绑定(用户拍板)+自动混合(v6)：Windows 在 pointerdown 前就决定 direct manipulation，
//     所以仅靠落笔后的 preventDefault/capture 无法可靠阻止页面随笔滚动。笔尖悬停时只在其周围放置
//     32px 的 touch-action:none 预命中区；抬笔或任意手指落下时同步撤销，无恢复定时器。
//     同一点附近保持撤销，直到笔尖明确移动 8px 才重新武装，避免旧 140px shield 的触摸尾巴。
//     iOS 另靠 stylus touchstart 拦滚 _blk(Safari 专有兼容路径)。
// 抬笔后转文档坐标 SVG 原生随页滚动。判定规则与 pdf-tail.js:184 / 173-182、04-render.js:159 对齐。
(() => {
  'use strict';
  if (window.__bwPwaProviderOnly || window.__bwPwaBridge || window.__bwWebInk || !window.__bwRoot || !window.__bwPinRoot) return;
  const root=window.__bwRoot,pinRoot=window.__bwPinRoot,KEY='webInkV1',NS='http://www.w3.org/2000/svg';
  let strokes=[],undo=[],active=null,on=false,tool='pen',color='#ef4444',width=3,raf=0,renderRaf=0,sizeRaf=0,saveT=0;
  let layoutWidth=0,layoutRaf=0;
  let lastTap=null,touchTap=null,suppressTapClick=null,quickErase=false,revertT=0,prevTool='pen',dbgT=0;   // 照搬 PDF _ink：临时橡皮 / 手指双击切换 / 空闲回笔 + 诊断浮标
  const SHIELD_SIZE=32,SHIELD_REARM_DISTANCE=8;
  const touchPointers=new Set();
  let shieldPoint=null,shieldHold=null;
  const st=document.createElement('style');st.textContent=`
/* canvas 恒 pointer-events:none(纯显示层，照搬 PDF .ink-layer)；只有 32px 笔尖预命中区参与仲裁。 */
#bw-root>.bw-ink-canvas{position:fixed;inset:0;width:100vw;height:100vh;z-index:20;pointer-events:none;touch-action:auto}
#bw-root>.bw-ink-shield{position:fixed;left:${-SHIELD_SIZE/2}px;top:${-SHIELD_SIZE/2}px;width:${SHIELD_SIZE}px;height:${SHIELD_SIZE}px;z-index:30;display:none;border-radius:50%;pointer-events:auto;touch-action:none;background:transparent;contain:strict;will-change:transform}
#bw-root>.bw-ink-shield.show{display:block}
.bw-ink-document{position:absolute;left:0;top:0;z-index:20;overflow:visible;pointer-events:none}
.bw-ink-tools{position:fixed;left:50%;bottom:18px;z-index:1000;transform:translateX(-50%);display:none;align-items:center;gap:6px;padding:7px 9px;border:1px solid rgba(255,255,255,.2);border-radius:13px;background:rgba(12,19,36,.9);box-shadow:0 8px 28px rgba(0,0,0,.45);backdrop-filter:blur(14px)}
.bw-ink-tools.show{display:flex}.bw-ink-tools button{height:30px;min-width:31px;border:1px solid #2a3a63;border-radius:7px;background:#16203a;color:#cfe0ff;cursor:pointer}.bw-ink-tools button.on{border-color:#60a5fa;background:#244470}.bw-ink-tools input[type=color]{width:31px;height:30px;padding:2px;border:1px solid #2a3a63;border-radius:7px;background:#16203a}.bw-ink-tools input[type=range]{width:82px;accent-color:#60a5fa}
.bw-ink-dbg{position:fixed;right:10px;bottom:58px;z-index:1001;padding:4px 9px;border-radius:8px;background:rgba(12,19,36,.92);color:#8ff3c0;font:12px/1.35 ui-monospace,monospace;pointer-events:none;opacity:0;transition:opacity .25s;white-space:nowrap}
`;window.__bwHead.appendChild(st);window.__bwPinHead?.appendChild(st.cloneNode(true));
  const svg=document.createElementNS(NS,'svg');svg.classList.add('bw-ink-document');svg.setAttribute('aria-hidden','true');pinRoot.appendChild(svg);
  const cv=document.createElement('canvas');cv.className='bw-ink-canvas';root.appendChild(cv);const ctx=cv.getContext('2d');
  const sh=document.createElement('div');sh.className='bw-ink-shield';sh.setAttribute('aria-hidden','true');root.appendChild(sh);
  const tools=document.createElement('div');tools.className='bw-ink-tools';tools.innerHTML='<button data-tool="pen" class="on" title="画笔">✏️</button><button data-tool="eraser" title="橡皮">⌫</button><input type="color" value="#ef4444" title="笔色"><input type="range" min="1" max="12" step=".5" value="3" title="粗细"><button data-act="undo" title="撤销">↶</button><button data-act="clear" title="清空本页">🗑</button><button data-act="close" title="退出手写模式">✓</button>';root.appendChild(tools);
  const dbg=document.createElement('div');dbg.className='bw-ink-dbg';root.appendChild(dbg);
  function showDbg(txt){dbg.textContent=txt;dbg.style.opacity='1';clearTimeout(dbgT);dbgT=setTimeout(()=>{dbg.style.opacity='0';},1700);}
  function hideShield(){sh.classList.remove('show');}
  function placeShield(x,y){
    if(!Number.isFinite(x)||!Number.isFinite(y))return false;
    shieldPoint={x,y};
    sh.style.transform=`translate3d(${Math.round(x)}px,${Math.round(y)}px,0)`;
    return true;
  }
  function releaseShieldAt(x,y){
    if(placeShield(x,y))shieldHold={x,y};
    else if(shieldPoint)shieldHold={x:shieldPoint.x,y:shieldPoint.y};
    hideShield();
  }
  function armShieldAt(x,y){
    if(active||touchPointers.size||!placeShield(x,y)){hideShield();return false;}
    if(shieldHold){
      if(Math.hypot(x-shieldHold.x,y-shieldHold.y)<SHIELD_REARM_DISTANCE){
        hideShield();return false;
      }
      shieldHold=null;
    }
    sh.classList.add('show');
    return true;
  }
  function onPenHover(e){
    if((e.pointerType!=='pen'&&e.pointerType!=='eraser')||e.buttons!==0)return;
    armShieldAt(e.clientX,e.clientY);
  }
  function onPenOut(e){
    if((e.pointerType!=='pen'&&e.pointerType!=='eraser')||e.relatedTarget||active)return;
    shieldPoint=null;shieldHold=null;hideShield();
  }
  function onTouchPresenceDown(e){
    if(e.pointerType!=='touch')return;
    touchPointers.add(e.pointerId);
    if(shieldPoint)shieldHold={x:shieldPoint.x,y:shieldPoint.y};
    hideShield();
  }
  function onTouchPresenceEnd(e){
    if(e.pointerType==='touch')touchPointers.delete(e.pointerId);
  }
  const docPoint=e=>({x:e.clientX+scrollX,y:e.clientY+scrollY,p:e.pressure>0?e.pressure:.5});
  function fitDocument(){const de=document.documentElement,b=document.body,w=Math.max(innerWidth,de?.scrollWidth||0,b?.scrollWidth||0),h=Math.max(innerHeight,de?.scrollHeight||0,b?.scrollHeight||0);svg.setAttribute('width',w);svg.setAttribute('height',h);svg.style.width=w+'px';svg.style.height=h+'px';}
  function documentSize(){const de=document.documentElement,b=document.body;return{width:Math.max(1,innerWidth,de?.scrollWidth||0,b?.scrollWidth||0),height:Math.max(1,innerHeight,de?.scrollHeight||0,b?.scrollHeight||0)};}
  // 网页坐标只在这里转换一次，交给共享层后与 PDF/EPUB 使用同一 canonical stroke。
  function exportSnapshot(){
    const size=documentSize();
    const canonical=strokes.map(s=>({
      t:'pen',c:s.color||'#ef4444',w:Number(s.width)||3,
      p:(s.pts||[]).map(pt=>[
        Math.max(0,Math.min(1,Number(pt.x||0)/size.width)),
        Math.max(0,Math.min(1,Number(pt.y||0)/size.height))
      ])
    })).filter(s=>s.p.length);
    return{page:1,hasInk:canonical.length>0,strokes:canonical,width:size.width,height:size.height};
  }
  function emitInkChange(){
    try{window.dispatchEvent(new CustomEvent('rc:inkchange',{detail:{source:'web'}}));}catch(_){}
  }
  function fit(){const d=Math.max(1,devicePixelRatio||1);cv.width=Math.round(innerWidth*d);cv.height=Math.round(innerHeight*d);cv.style.width=innerWidth+'px';cv.style.height=innerHeight+'px';ctx.setTransform(d,0,0,d,0,0);fitDocument();draw();}
  function path(s){const pts=s.pts||[];if(!pts.length)return;ctx.beginPath();ctx.lineCap='round';ctx.lineJoin='round';ctx.strokeStyle=s.color;ctx.lineWidth=s.width;ctx.moveTo(pts[0].x-scrollX,pts[0].y-scrollY);for(let i=1;i<pts.length;i++)ctx.lineTo(pts[i].x-scrollX,pts[i].y-scrollY);if(pts.length===1)ctx.lineTo(pts[0].x-scrollX+.1,pts[0].y-scrollY+.1);ctx.stroke();}
  function draw(){ctx.clearRect(0,0,innerWidth,innerHeight);if(active&&!active.erase)path(active);}
  function svgD(s){const p=s.pts||[];if(!p.length)return '';let d='M '+p[0].x+' '+p[0].y;for(let i=1;i<p.length;i++)d+=' L '+p[i].x+' '+p[i].y;if(p.length===1)d+=' l .1 .1';return d;}
  function renderSvg(){renderRaf=0;fitDocument();const f=document.createDocumentFragment();strokes.forEach(s=>{const d=svgD(s);if(!d)return;const p=document.createElementNS(NS,'path');p.setAttribute('d',d);p.setAttribute('fill','none');p.setAttribute('stroke',s.color||'#ef4444');p.setAttribute('stroke-width',String(s.width||3));p.setAttribute('stroke-linecap','round');p.setAttribute('stroke-linejoin','round');p.setAttribute('vector-effect','non-scaling-stroke');f.appendChild(p);});svg.replaceChildren(f);}
  function scheduleRender(){if(!renderRaf)renderRaf=requestAnimationFrame(renderSvg);}
  function scheduleDraw(){if(!raf)raf=requestAnimationFrame(()=>{raf=0;draw();});}
  // 网页笔迹不持久化(用户拍板,与阅读器不同):只活在当前页面会话,刷新/关页即清。persist 留空壳保调用点不动。
  function persist(){}
  function currentLayoutWidth(){const r=document.documentElement.getBoundingClientRect();return Math.round(r.width||document.documentElement.clientWidth||innerWidth||0);}
  function discardForLayoutChange(nextWidth){
    nextWidth=Math.round(Number(nextWidth)||0);
    if(!nextWidth)return;
    const changed=layoutWidth&&Math.abs(nextWidth-layoutWidth)>1;
    layoutWidth=nextWidth;
    if(!changed||(!strokes.length&&!active))return;
    if(active){
      const current=active;
      try{if(active.captureEl?.hasPointerCapture(active.id))active.captureEl.releasePointerCapture(active.id);}catch(_){}
      document.removeEventListener('pointermove',onMove,true);
      document.removeEventListener('pointerup',onUp,true);
      document.removeEventListener('pointercancel',onUp,true);
      document.removeEventListener('lostpointercapture',onLostCapture,true);
      document.removeEventListener('selectstart',preventSel,true);
      active=null;
      if(current.nativePen)releaseShieldAt(current.cx,current.cy);
    }
    strokes=[];undo=[];draw();renderSvg();persist();emitInkChange();
    window.RC?.toast?.('网页排版宽度已变化，临时笔迹已清除');
  }
  function observeLayoutWidth(){
    if(layoutRaf)return;
    layoutRaf=requestAnimationFrame(()=>{layoutRaf=0;discardForLayoutChange(currentLayoutWidth());});
  }
  function snapshot(){undo.push(JSON.stringify(strokes));if(undo.length>30)undo.shift();}
  function hitStroke(s,p){const rr=Math.max(12,(s.width||3)*2);return (s.pts||[]).some(q=>Math.hypot(q.x-p.x,q.y-p.y)<=rr);}
  // ── 工具态 UI / 临时橡皮 / 手指双击切换(照搬 pdf-tail.js:74/85-123) ──
  function syncToolUI(){tools.querySelectorAll('[data-tool]').forEach(x=>x.classList.toggle('on',x.dataset.tool===tool));}
  function armRevert(ms){clearTimeout(revertT);revertT=setTimeout(()=>{if(active&&active.erase){armRevert(400);return;}exitQuickErase(true);},ms);}
  function exitQuickErase(toPen){clearTimeout(revertT);revertT=0;quickErase=false;if(toPen){tool=prevTool||'pen';syncToolUI();window.RC?.toast?.('✏️ 已回到笔');}else syncToolUI();}
  function doubleTapSwitch(){   // 手指快速双击 切 笔↔临时橡皮(浏览器拿不到 Apple Pencil 双击笔身 → 手指双击替代)
    if(tool==='eraser'){exitQuickErase(true);}
    else{prevTool=tool;tool='eraser';quickErase=true;syncToolUI();armRevert(2500);window.RC?.toast?.('🧹 临时橡皮(空闲自动回笔)');}
  }
  // 双击必须由两次「抬手且未移动」的轻点组成。旧版在 pointerdown 就记一次 tap，
  // 连续两次快速滚动也会被误认成双击，并 preventDefault 第二次滚动。
  function trackTouchTapMove(e){
    if(!touchTap||e.pointerId!==touchTap.id)return;
    if(Math.hypot(e.clientX-touchTap.x,e.clientY-touchTap.y)>12){
      touchTap.moved=true;
      lastTap=null;
    }
  }
  function finishTouchTap(e){
    if(!touchTap||e.pointerId!==touchTap.id)return;
    const tap=touchTap;touchTap=null;
    const elapsed=performance.now()-tap.started;
    if(e.type==='pointercancel'||tap.moved||elapsed>280){lastTap=null;return;}
    const now=performance.now();
    if(lastTap&&now-lastTap.t<350&&Math.hypot(e.clientX-lastTap.x,e.clientY-lastTap.y)<32){
      e.preventDefault();e.stopPropagation();
      lastTap=null;
      suppressTapClick={t:performance.now(),x:e.clientX,y:e.clientY};
      doubleTapSwitch();
      return;
    }
    lastTap={t:now,x:e.clientX,y:e.clientY};
  }
  function suppressRecognizedDoubleTapClick(e){
    const s=suppressTapClick;
    if(!s)return;
    if(performance.now()-s.t>600){suppressTapClick=null;return;}
    if(Math.hypot((e.clientX||0)-s.x,(e.clientY||0)-s.y)>36)return;
    suppressTapClick=null;e.preventDefault();e.stopImmediatePropagation();
  }
  // pen 落点该放行不画：扩展自己的工具条 + 网页可交互元素(让 Apple Pencil 仍能点按钮/链接、在输入框写字)
  function _ignore(e){const t=e.target,cp=e.composedPath?e.composedPath():[];
    if(cp.includes(tools))return true;
    if(t&&t.closest&&t.closest('button,a,input,textarea,select,summary,label,[role="button"],[contenteditable="true"],[contenteditable=""]'))return true;
    return false;}
  const preventSel=e=>e.preventDefault();   // pen 画时不选中网页文字
  // ── 指针状态机(document capture)：照搬 pdf-tail.js:_inkPointerDown 的判定，plumbing 挂 document ──
  function onDown(e){
    if(active)return;                                     // 正在画：忽略第二根指/手掌
    // ⚠ 非左键守卫只许管鼠标：笔尾橡皮的 pointerdown 按 W3C 规范报 button===5(移动中 buttons&32)，
    //   0.2.28/29 在这里被一刀切的 e.button>0 吞掉 = 笔尾橡皮从没工作过的真根因(Excalidraw #5281 同坑)。
    if(e.pointerType==='mouse'&&e.button>0)return;        // 鼠标非左键忽略
    if(e.pointerType==='pen'&&e.button!==0&&e.button!==5)return;   // 笔:笔尖(0)/笔尾橡皮(5)放行;侧键(2=右键)忽略
    if(_ignore(e))return;                                 // 放行工具条/网页交互元素
    // 手指双击候选只在抬手时确认；此处绝不 preventDefault，原生滚动从第一下就可开始。
    if(e.pointerType==='touch'&&(on||strokes.length)){
      touchTap={id:e.pointerId,started:performance.now(),x:e.clientX,y:e.clientY,moved:false};
    }
    // Surface/Wacom 笔尾橡皮擦(W3C:pointerdown button===5 / 移动中 buttons&32;个别设备报 pointerType 'eraser')
    // → 这一笔当橡皮，不改持久工具，翻回笔尖即画(Apple Pencil 无笔尾，不报此位，无害)。
    const _erSig=e.pointerType==='eraser'||(e.pointerType==='pen'&&(((e.buttons&32)!==0)||e.button===5));
    if(_erSig)showDbg('🧹 笔尾橡皮 type='+e.pointerType+' btn='+e.button+' buttons='+e.buttons);   // 【临时诊断】仅橡皮信号才闪;确认后移除
    // 绘制：Apple Pencil / Surface Pen 等主动笔始终(pointerType pen/eraser 通吃)；鼠标仅桌面手写模式；手指永不画(照搬 pdf-tail.js:184)
    if(!(e.pointerType==='pen'||e.pointerType==='eraser'||(e.pointerType==='mouse'&&on)))return;
    e.preventDefault();e.stopPropagation();
    // Windows 在落笔前已由 32px shield 的 touch-action:none 完成仲裁；这里只捕获当前 pointerId。
    // 鼠标桌面手写模式仍捕获 documentElement，不占用笔尖 shield。
    const nativePen=e.pointerType==='pen'||e.pointerType==='eraser';
    if(nativePen){placeShield(e.clientX,e.clientY);sh.classList.add('show');}
    const captureEl=nativePen?sh:document.documentElement;
    try{captureEl.setPointerCapture(e.pointerId);}catch(_){}
    const p=docPoint(e);snapshot();
    const eraserNow=(tool==='eraser')||_erSig;
    if(eraserNow){
      if(quickErase)clearTimeout(revertT);                // 正在擦 → 暂停自动回笔(抬笔再重启)
      strokes=strokes.filter(s=>!hitStroke(s,p));scheduleRender();persist();
      active={erase:true,id:e.pointerId,penEraser:tool!=='eraser',captureEl,nativePen,cx:e.clientX,cy:e.clientY};
    }else{
      active={id:e.pointerId,color,width,pts:[p],cx:e.clientX,cy:e.clientY,captureEl,nativePen};
    }
    document.addEventListener('pointermove',onMove,true);
    document.addEventListener('pointerup',onUp,true);
    document.addEventListener('pointercancel',onUp,true);
    document.addEventListener('lostpointercapture',onLostCapture,true);
    document.addEventListener('selectstart',preventSel,true);
  }
  function onMove(e){if(!active||e.pointerId!==active.id)return;e.preventDefault();const p=docPoint(e);active.cx=e.clientX;active.cy=e.clientY;if(active.erase){const n=strokes.filter(s=>!hitStroke(s,p));if(n.length!==strokes.length){strokes=n;scheduleRender();persist();}return;}const a=active.pts,b=a[a.length-1];if(!b||Math.hypot(p.x-b.x,p.y-b.y)>=1.5){a.push(p);scheduleDraw();}}
  function onUp(e){
    if(!active||e.pointerId!==active.id)return;
    const current=active;
    if(current.finishing)return;
    current.finishing=true;
    try{if(current.captureEl?.hasPointerCapture(e.pointerId))current.captureEl.releasePointerCapture(e.pointerId);}catch(_){}
    document.removeEventListener('pointermove',onMove,true);
    document.removeEventListener('pointerup',onUp,true);
    document.removeEventListener('pointercancel',onUp,true);
    document.removeEventListener('lostpointercapture',onLostCapture,true);
    document.removeEventListener('selectstart',preventSel,true);
    const wasQuickErase=current.erase&&!current.penEraser;  // 只有手指双击的「临时橡皮」才自动回笔；笔尾橡皮是硬件态，抬笔即结束(笔尖回来自动是笔)
    if(current.nativePen)releaseShieldAt(current.cx,current.cy);
    delete current.captureEl;delete current.finishing;
    if(!current.erase&&current.pts.length){strokes.push(current);if(strokes.length>600)strokes=strokes.slice(-600);}
    active=null;draw();renderSvg();persist();emitInkChange();
    if(wasQuickErase&&quickErase)armRevert(900);          // 临时橡皮：擦完抬笔停 0.9s 没再擦 → 自动回笔
  }
  function onLostCapture(e){if(active&&!active.finishing&&e.pointerId===active.id)onUp(e);}
  // Apple Pencil 不滚页：stylus 触摸的 touchstart/touchmove preventDefault(照搬 04-render.js:159 _blk)。手指照常滚。
  const _blk=e=>{for(const t of e.touches){if(t.touchType==='stylus'){e.preventDefault();break;}}};
  document.addEventListener('pointerover',onPenHover,true);
  document.addEventListener('pointermove',onPenHover,true);
  document.addEventListener('pointerout',onPenOut,true);
  document.addEventListener('pointerdown',onTouchPresenceDown,true);
  document.addEventListener('pointerup',onTouchPresenceEnd,true);
  document.addEventListener('pointercancel',onTouchPresenceEnd,true);
  document.addEventListener('pointerdown',onDown,true);
  document.addEventListener('pointermove',trackTouchTapMove,true);
  document.addEventListener('pointerup',finishTouchTap,true);
  document.addEventListener('pointercancel',finishTouchTap,true);
  document.addEventListener('click',suppressRecognizedDoubleTapClick,true);
  document.addEventListener('touchstart',_blk,{passive:false,capture:true});
  document.addEventListener('touchmove',_blk,{passive:false,capture:true});
  tools.addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;if(b.dataset.tool){tool=b.dataset.tool;clearTimeout(revertT);quickErase=false;syncToolUI();return;}if(b.dataset.act==='undo'&&undo.length){strokes=JSON.parse(undo.pop());draw();renderSvg();persist();emitInkChange();}if(b.dataset.act==='clear'&&strokes.length&&confirm('清空当前网页的全部笔迹？')){snapshot();strokes=[];draw();renderSvg();persist();emitInkChange();}if(b.dataset.act==='close')set(false);});
  tools.querySelector('input[type=color]').oninput=e=>color=e.target.value;tools.querySelector('input[type=range]').oninput=e=>width=Number(e.target.value)||3;
  // set(v)：只切「桌面手写模式」(鼠标可画) + 显示/隐藏工具条。Apple Pencil 不受它管，始终自动落笔。
  function set(v){on=!!v;tools.classList.toggle('show',on);window.RC?.toast?.(on?'桌面手写模式已开(鼠标可画；Apple Pencil 始终可画)':'已退出桌面手写模式(Apple Pencil 仍随时可画)');return on;}
  // 画的过程中页面被滚(手指/滚轮)→ 按最近笔尖屏幕位 + 新滚动量补点,笔迹在文档坐标里连续拖出竖线(用户预期行为)
  addEventListener('scroll',()=>{if(active&&!active.erase){if(active.cx!==undefined)active.pts.push({x:active.cx+scrollX,y:active.cy+scrollY,p:.5});scheduleDraw();}},{passive:true});addEventListener('resize',()=>{observeLayoutWidth();fit();},{passive:true});
  try{new ResizeObserver(observeLayoutWidth).observe(document.documentElement);}catch(_){}
  try{new MutationObserver(()=>{if(!sizeRaf)sizeRaf=requestAnimationFrame(()=>{sizeRaf=0;fitDocument();});}).observe(document.body,{childList:true,subtree:true,attributes:true,attributeFilter:['style','class','hidden','open']});}catch(_){}
  layoutWidth=currentLayoutWidth();fit();
  // 历史 webInkV1 不再读取或写入，但保留原数据；当前网页笔迹只活在本次会话。
  window.__bwWebInk={
    toggle:()=>set(!on),set,
    state:()=>({on,tool,color,width,pencil:'always'}),
    exportSnapshot
  };
  window.RC?.actions?.bind?.('ink.toggle',()=>({
    ok:true,
    active:window.__bwWebInk.toggle()
  }),{owner:'web-extension',runtime:'extension',storage:'session'});
})();
