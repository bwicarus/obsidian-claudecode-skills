/* mfx —— Claude Design 导出的交互/特效脚本:Toast / 手势(左滑删除·KG hover 预览·模态下拉关) / 成功爆点·光标聚光。
   全是 idempotent(window.__mfx* 守卫),只在明确手势/特定按钮时介入,不改既有点击。 */
/* polish-fx2 · Toast 工具 + 操作反馈接线 */
(function(){
  if(window.mfxToast) return;
  window.mfxToast=function(msg,opts){
    opts=opts||{};
    var host=document.getElementById('mfx-toast-host');
    if(!host){host=document.createElement('div');host.id='mfx-toast-host';document.body.appendChild(host);}
    var t=document.createElement('div');t.className='mfx-toast';t.textContent=msg;
    if(opts.type)t.setAttribute('data-type',opts.type);
    host.appendChild(t);
    requestAnimationFrame(function(){t.classList.add('in');});
    setTimeout(function(){t.classList.remove('in');setTimeout(function(){t.remove();},300);},opts.duration||2200);
  };
  /* 真实接线：常见正向操作给确认提示 */
  document.addEventListener('click',function(e){
    var el=e.target.closest&&e.target.closest('.vi-anki,.gb-anki-btn,.kg-track-btn');
    if(!el||el.classList.contains('done')||el.classList.contains('on'))return;
    if(el.classList.contains('kg-track-btn'))window.mfxToast('已开始追踪该知识点',{type:'success'});
    else window.mfxToast('已加入 Anki 卡片',{type:'success'});
  },true);
})();

/* gestures-fx · 左滑删除 / 知识点 hover 预览 / 模态下拉关闭 —— 仅在明确手势时介入，不影响点击 */
(function(){
  if(window.__mfxGestures)return; window.__mfxGestures=true;
  function interactive(el){return el.closest&&el.closest('button,a,input,select,textarea,[contenteditable]');}

  /* 1) 生词卡左滑删除 */
  document.addEventListener('pointerdown',function(e){
    var item=e.target.closest&&e.target.closest('.vocab-item');
    if(!item||interactive(e.target))return;
    var sx=e.clientX,sy=e.clientY,dx=0,lock=null,active=false;
    function move(ev){
      dx=ev.clientX-sx;var dy=ev.clientY-sy;
      if(lock===null){if(Math.abs(dx)<6&&Math.abs(dy)<6)return;lock=Math.abs(dx)>Math.abs(dy)?'x':'y';}
      if(lock!=='x')return;
      ev.preventDefault();active=true;
      var t=Math.min(0,dx);
      item.style.transition='none';
      item.style.transform='translateX('+t+'px)';
      item.style.background='linear-gradient(90deg,var(--c-e1),rgba(248,113,113,'+Math.min(.30,Math.abs(t)/280)+'))';
    }
    function up(){
      document.removeEventListener('pointermove',move);document.removeEventListener('pointerup',up);
      if(!active)return;
      item.style.transition='transform .3s cubic-bezier(.22,1,.36,1),opacity .3s ease,background .3s';
      if(dx<-90){
        /* 接真后端:左滑=「这词我会了」→ 标记掌握(锁 mastery 100%),英→vocab-mark / 日→jp-vocab-mark;
           标后该词不再当生词(下划线消失、刷新不回来)。语义非破坏、可逆(字典框「✓已掌握」可取消)。 */
        var wEl=item.querySelector('.vi-word'); var w=wEl?(wEl.textContent||'').trim():'';
        var jp=/[぀-ヿ㐀-鿿]/.test(w);
        if(w){
          fetch(jp?'/pdf/api/jp-vocab-mark':'/pdf/api/vocab-mark',{method:'POST',
            headers:{'Content-Type':'application/json'},body:JSON.stringify({word:w,mark:'known'})})
            .then(function(r){return r.json();}).then(function(d){
              if(d&&d.ok===false){if(window.mfxToast)window.mfxToast('标记失败:'+(d.error||''),{type:'error'});return;}
              try{window.refreshVocabUnderlinesForAllPages&&window.refreshVocabUnderlinesForAllPages();}catch(_){}
            }).catch(function(){if(window.mfxToast)window.mfxToast('标记失败(网络)',{type:'error'});});
        }
        item.style.transform='translateX(-110%)';item.style.opacity='0';
        if(window.mfxToast)window.mfxToast(w?('已掌握「'+w+'」,移出生词本'):'已移出生词本',{type:'success'});
        setTimeout(function(){
          var h=item.offsetHeight;item.style.maxHeight=h+'px';item.style.overflow='hidden';
          requestAnimationFrame(function(){
            item.style.transition='max-height .28s ease,margin .28s,padding .28s,opacity .2s';
            item.style.maxHeight='0';item.style.marginTop='0';item.style.marginBottom='0';
            item.style.paddingTop='0';item.style.paddingBottom='0';
          });
        },300);
      }else{item.style.transform='';item.style.background='';}
    }
    document.addEventListener('pointermove',move);document.addEventListener('pointerup',up);
  });

  /* 2) 知识点节点 hover 预览卡 */
  var pv;
  document.addEventListener('pointerover',function(e){
    var node=e.target.closest&&e.target.closest('.kg-node');if(!node)return;
    if(window.matchMedia&&window.matchMedia('(hover:none)').matches)return;
    var main=node.querySelector('.kg-node-main')||node;
    if(!pv){pv=document.createElement('div');pv.id='mfx-kg-preview';document.body.appendChild(pv);}
    var titleEl=main.querySelector('strong,.kg-node-title,b');
    var title=titleEl?titleEl.textContent.trim():(main.textContent||'').trim().slice(0,40);
    var body=(main.textContent||'').trim();
    pv.innerHTML='';
    var t=document.createElement('div');t.className='mk-t';t.textContent=title;
    var b=document.createElement('div');b.className='mk-b';b.textContent=body.slice(0,180)+(body.length>180?'…':'');
    pv.appendChild(t);pv.appendChild(b);
    var r=node.getBoundingClientRect();
    pv.style.left=Math.max(8,Math.min(r.left,window.innerWidth-256))+'px';
    pv.style.top=Math.min(r.bottom+8,window.innerHeight-120)+'px';
    pv.classList.add('show');
  });
  document.addEventListener('pointerout',function(e){
    var node=e.target.closest&&e.target.closest('.kg-node');if(!node||!pv)return;
    if(e.relatedTarget&&node.contains(e.relatedTarget))return;
    pv.classList.remove('show');
  });

  /* 3) 结果模态：顶部下拉关闭 */
  document.addEventListener('pointerdown',function(e){
    var modal=e.target.closest&&e.target.closest('#result-modal');
    if(!modal||interactive(e.target))return;
    var r=modal.getBoundingClientRect();
    if(e.clientY-r.top>64)return;
    var sy=e.clientY,dy=0,active=false;
    function move(ev){dy=Math.max(0,ev.clientY-sy);if(dy>4)active=true;
      modal.style.transition='none';modal.style.transform='translateY('+dy+'px)';
      modal.style.opacity=String(Math.max(.4,1-dy/420));}
    function up(){
      document.removeEventListener('pointermove',move);document.removeEventListener('pointerup',up);
      modal.style.transition='transform .3s cubic-bezier(.22,1,.36,1),opacity .3s';
      if(active&&dy>120){
        modal.style.transform='translateY(70px)';modal.style.opacity='0';
        setTimeout(function(){var mask=document.getElementById('result-mask');if(mask)mask.classList.remove('open');
          modal.style.transform='';modal.style.opacity='';},300);
      }else{modal.style.transform='';modal.style.opacity='';}
    }
    document.addEventListener('pointermove',move);document.addEventListener('pointerup',up);
  });
})();

/* effects-fx · 成功爆点 + 模态光标聚光 */
(function(){
  if(window.__mfxEffects)return; window.__mfxEffects=true;
  var reduce=window.matchMedia&&matchMedia('(prefers-reduced-motion:reduce)').matches;

  document.addEventListener('pointermove',function(e){
    var m=e.target.closest&&e.target.closest('#result-modal');if(!m)return;
    var r=m.getBoundingClientRect();
    m.style.setProperty('--mx',(e.clientX-r.left)+'px');
    m.style.setProperty('--my',(e.clientY-r.top)+'px');
  });

  function burst(x,y){
    if(reduce)return;
    var ring=document.createElement('div');ring.className='mfx-burst';
    ring.style.left=x+'px';ring.style.top=y+'px';ring.style.border='2px solid var(--c-ok)';
    ring.style.animation='mfx-ring .6s ease-out forwards';
    document.body.appendChild(ring);setTimeout(function(){ring.remove();},620);
    for(var i=0;i<6;i++){
      var s=document.createElement('div');s.className='mfx-burst';
      s.style.left=x+'px';s.style.top=y+'px';s.style.width='5px';s.style.height='5px';s.style.background='var(--c-ok)';
      var ang=Math.PI*2*i/6,d=20+Math.random()*14;
      s.animate([{transform:'translate(0,0) scale(1)',opacity:1},
        {transform:'translate('+Math.cos(ang)*d+'px,'+Math.sin(ang)*d+'px) scale(.4)',opacity:0}],
        {duration:600,easing:'cubic-bezier(.22,1,.36,1)'});
      document.body.appendChild(s);(function(s){setTimeout(function(){s.remove();},640);})(s);
    }
  }
  document.addEventListener('click',function(e){
    var el=e.target.closest&&e.target.closest('.vi-anki,.gb-anki-btn,.kg-track-btn');
    if(!el||el.classList.contains('done')||el.classList.contains('on'))return;
    burst(e.clientX,e.clientY);
  },true);
})();
