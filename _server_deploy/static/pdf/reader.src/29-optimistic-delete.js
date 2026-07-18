// 29-optimistic-delete.js — 自建页乐观删除的**就地 reconcile**(不刷新页面)。
//   pdf-uishared.js 删除时乐观移除那页 DOM(窗口内不动别页页号→匹配旧文件);后台真删 job 全部完成后
//   调本函数按新文件对齐:重编号(DOM 顺序=正确新序)+ 更新页数/mtime + 软重取注解层。
//   **图片模式 + 连续排版**才做;spread / canvas / DOM 页数跟后端不一致 / 任何异常 → 返回 false 让调用方 reload 兜底。
//   放在最后一个源文件:整包同一个 module,运行时(用户删除后)所有模块级量已初始化,闭包按引用取。
window.__upReconcileDelete = function (newMeta) {
  try {
    if (!_imgMode || readMode !== 'continuous') { try{localStorage.setItem('_recon_dbg',((localStorage.getItem('_recon_dbg')||'')+'|gate:mode '+_imgMode+' '+readMode).slice(-1500));}catch(_){} return false; }   // 非图片/非连续:退回 reload(spread 行分组/canvas 字节都动不了)
    var M = newMeta && parseInt(newMeta.page_count, 10);
    if (!M || M < 1) { try{localStorage.setItem('_recon_dbg',((localStorage.getItem('_recon_dbg')||'')+'|gate:meta').slice(-1500));}catch(_){} return false; }
    var container = document.getElementById('page-container');
    if (!container) { try{localStorage.setItem('_recon_dbg',((localStorage.getItem('_recon_dbg')||'')+'|gate:container').slice(-1500));}catch(_){} return false; }
    // 页元素 = 真页 .page-wrap + 本会话虚拟插入页 .pdf-upage,按 **DOM 顺序**(= 视觉页序 = 正确新序)。
    var kids = Array.prototype.filter.call(container.children, function (el) {
      return el.classList && (el.classList.contains('page-wrap') || el.classList.contains('pdf-upage'));
    });
    if (kids.length > M && window._upJustDeleted) {
      // 自愈:实测(2026-07-17 heisenbug)删除后偶发多出一个页元素(异步挂载把刚删的页又放回来)→
      //   把 uid 在「刚删除」名单里的僵尸元素清掉再对账,而不是直接放弃 reload。
      kids = kids.filter(function (el) {
        var uid = el.dataset && el.dataset.uid;
        if (uid && window._upJustDeleted[uid]) { try { el.remove(); } catch (_) {} return false; }
        return true;
      });
    }
    if (kids.length !== M) {
      try {
        var pw2 = 0, up2 = 0, dup = {}, seen = {};
        kids.forEach(function (el) {
          if (el.classList.contains('pdf-upage')) up2++; else pw2++;
          var pn = el.dataset.pageNum || ('u:' + (el.dataset.uid || '?'));
          if (seen[pn]) dup[pn] = (dup[pn] || 1) + 1; seen[pn] = 1;
        });
        localStorage.setItem('_recon_dbg', ((localStorage.getItem('_recon_dbg') || '') + '|gate:count dom=' + kids.length + ' backend=' + M + ' pw=' + pw2 + ' up=' + up2 + ' dup=' + JSON.stringify(dup)).slice(-1500));
      } catch (_) {}
      return false;
    }   // DOM 页数跟后端不一致(有未完成插入 job / spread 残留等)→ 稳妥退回 reload
    // ① 元数据:页数 + mtime(页图请求带新版号 v → 命中新文件页、绕过 immutable/SW 旧缓存)
    if (typeof pdfDoc !== 'undefined' && pdfDoc) pdfDoc.numPages = M;
    if (window.__imgMeta) { window.__imgMeta.page_count = M; if (newMeta.mtime) window.__imgMeta.mtime = newMeta.mtime; }
    var pt = document.getElementById('page-total'); if (pt) pt.textContent = '/ ' + M;
    // ② 重编号(DOM 顺序 → 1..M):真页 dataset.pageNum;虚拟插入页同步 __upRec.page;未渲染占位文字改写。
    kids.forEach(function (el, i) {
      var nn = i + 1;
      if (parseInt(el.dataset.pageNum, 10) !== nn) {
        el.dataset.pageNum = String(nn);
        if (el.__upRec) el.__upRec.page = nn;
        if (el.dataset.loaded === '0' && /第\s*\d+\s*页/.test(el.textContent || '')) el.textContent = '… 第 ' + (window._dispPage ? window._dispPage(nn) : nn) + ' 页';
      }
    });
    if (typeof currentPage === 'number' && currentPage > M) currentPage = M;
    // ③ 清按页号的前端缓存(键已过期):页图档位 + 预取去重。已渲染页图内容不变(同一物理页),不必重取。
    try { for (var k in _imgRasterW) delete _imgRasterW[k]; } catch (e) {}
    try { _prefetched.clear(); } catch (e) {}
    // ④ 软重取注解层(后端已迁移 → 正确数据,各自按新页号重渲/重挂,幂等清旧)。都不刷新页面。
    try { if (window._inkLoadAll) window._inkLoadAll(); } catch (e) {}
    try { if (typeof loadAllHighlights === 'function') loadAllHighlights(); } catch (e) {}
    try { if (window.RC && RC.userpages && RC.userpages.load) RC.userpages.load(); } catch (e) {}
    try { if (window.RC && RC.stickynote && RC.stickynote.loadAll) RC.stickynote.loadAll(); } catch (e) {}
    return true;
  } catch (e) { try{localStorage.setItem('_recon_dbg',((localStorage.getItem('_recon_dbg')||'')+'|reconcile-throw:'+e.message).slice(-1500));}catch(_){} try { window.dlog && window.dlog('reconcile fail: ' + e.message, '#ff6b6b'); } catch (_) {} return false; }
};
