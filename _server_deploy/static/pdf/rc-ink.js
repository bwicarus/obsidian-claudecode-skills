/* rc-ink.js — 墨迹引擎共享核心(纯几何/渲染/命中/撤销栈,2026-07-06 三份合并)。
 *
 * 消费方(此前各持一份几乎相同的实现,已改薄 wrapper 调这里):
 *   - pdf-tail.js   PDF 阅读器(per-page canvas pw.__inkCanvas,状态 _ink)
 *   - epub-html.js  EPUB 阅读器(per-section canvas el.__inkCv,状态 _epInk)
 *   - rc-stickynote.js 便签手写(ctl.cv,笔画字段 pts、pen-only)
 * 加载序:必须先于以上三个文件(pdf_reader.html 放 ui_shared 条件块**外**——pdf-tail 无条件加载)。
 *
 * 兼容超集(不改任何存量数据):
 *   - 点数组字段 s.p(阅读器)|| s.pts(便签),读取统一 _pts()
 *   - 笔画类型 s.t 缺省按 'pen'(便签笔画从不写 t)
 *   - 默认色/宽经 defs 覆盖(便签用用户当前 INK 色;阅读器保持 '#e74c3c'/2.5)
 * 指针状态机/保存策略/live canvas 不在此:三方真实分叉(PDF 快照重绘、EPUB 视口叠加
 * live canvas、便签 rAF 小画布),留在各自文件。
 */
(function () {
  'use strict';

  function _pts(s) { return s.p || s.pts || []; }

  // 画一条笔画:pen=平滑 quadratic(单点=点笔头)/ line / arrow / rect
  function drawStroke(ctx, s, W, H, dpr, defs) {
    var pts = _pts(s); if (!pts.length) return;
    ctx.strokeStyle = s.c || (defs && defs.color) || '#e74c3c';
    ctx.lineWidth = Math.max(0.6, (s.w || (defs && defs.width) || 2.5) * dpr);
    ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    var X = function (i) { return pts[i][0] * W; }, Y = function (i) { return pts[i][1] * H; };
    var t = s.t || 'pen';
    if (t === 'pen') {
      ctx.beginPath(); ctx.moveTo(X(0), Y(0));
      if (pts.length === 1) { ctx.lineTo(X(0) + 0.1, Y(0)); }
      else {
        for (var i = 1; i < pts.length - 1; i++) {
          var mx = (X(i) + X(i + 1)) / 2, my = (Y(i) + Y(i + 1)) / 2;
          ctx.quadraticCurveTo(X(i), Y(i), mx, my);
        }
        ctx.lineTo(X(pts.length - 1), Y(pts.length - 1));
      }
      ctx.stroke();
    } else if (t === 'line' && pts.length >= 2) {
      ctx.beginPath(); ctx.moveTo(X(0), Y(0)); ctx.lineTo(X(1), Y(1)); ctx.stroke();
    } else if (t === 'arrow' && pts.length >= 2) {
      ctx.beginPath(); ctx.moveTo(X(0), Y(0)); ctx.lineTo(X(1), Y(1)); ctx.stroke();
      var ang = Math.atan2(Y(1) - Y(0), X(1) - X(0)), ah = Math.max(9, ctx.lineWidth * 3.5);
      ctx.beginPath(); ctx.moveTo(X(1), Y(1));
      ctx.lineTo(X(1) - ah * Math.cos(ang - 0.42), Y(1) - ah * Math.sin(ang - 0.42));
      ctx.moveTo(X(1), Y(1));
      ctx.lineTo(X(1) - ah * Math.cos(ang + 0.42), Y(1) - ah * Math.sin(ang + 0.42));
      ctx.stroke();
    } else if (t === 'rect' && pts.length >= 2) {
      var x0 = X(0), y0 = Y(0), x1 = X(1), y1 = Y(1);
      ctx.strokeRect(Math.min(x0, x1), Math.min(y0, y1), Math.abs(x1 - x0), Math.abs(y1 - y0));
    }
  }

  // 点到线段距离(归一化坐标)
  function ptSeg(p, a, b) {
    var dx = b[0] - a[0], dy = b[1] - a[1], l2 = dx * dx + dy * dy;
    if (l2 === 0) return Math.hypot(p[0] - a[0], p[1] - a[1]);
    var t = ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / l2; t = Math.max(0, Math.min(1, t));
    return Math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy));
  }

  // 橡皮命中:rect 按边框带命中,其余分段 + 单点距离
  function hit(s, pt, thr) {
    var pts = _pts(s); if (!pts.length) return false;
    if (s.t === 'rect' && pts.length >= 2) {
      var x0 = Math.min(pts[0][0], pts[1][0]), x1 = Math.max(pts[0][0], pts[1][0]);
      var y0 = Math.min(pts[0][1], pts[1][1]), y1 = Math.max(pts[0][1], pts[1][1]);
      var nx = (Math.abs(pt[0] - x0) < thr || Math.abs(pt[0] - x1) < thr) && pt[1] > y0 - thr && pt[1] < y1 + thr;
      var ny = (Math.abs(pt[1] - y0) < thr || Math.abs(pt[1] - y1) < thr) && pt[0] > x0 - thr && pt[0] < x1 + thr;
      return nx || ny;
    }
    for (var i = 0; i < pts.length - 1; i++) if (ptSeg(pt, pts[i], pts[i + 1]) < thr) return true;
    if (pts.length === 1) return Math.hypot(pt[0] - pts[0][0], pt[1] - pts[0][1]) < thr;
    return false;
  }

  // viewport client 坐标 → canvas 归一化 [x,y](不可算 → null)
  function norm(cv, cx, cy) {
    if (!cv) return null;
    var r = cv.getBoundingClientRect();
    if (!r.width || !r.height) return null;
    return [(cx - r.left) / r.width, (cy - r.top) / r.height];
  }

  // 整 canvas 重绘(visible=false 时只清空;dpr 按物理宽/css 宽推,与两阅读器原实现一致)
  function redraw(cv, strokes, visible, defs) {
    if (!cv) return;
    var ctx = cv.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, cv.width, cv.height);
    if (visible === false) return;
    var cssW = parseFloat(cv.style.width) || cv.width;
    var dpr = (cv.width / cssW) || 1;
    var arr = strokes || [];
    for (var i = 0; i < arr.length; i++) drawStroke(ctx, arr[i], cv.width, cv.height, dpr, defs);
  }

  // 就地擦除(纯数据:调用侧自己 redraw/记 dirty/调度保存);返回是否删了东西
  function eraseAt(arr, pt, thr) {
    var removed = false;
    for (var i = arr.length - 1; i >= 0; i--) if (hit(arr[i], pt, thr)) { arr.splice(i, 1); removed = true; }
    return removed;
  }

  // 撤销栈(host 上的 __inkUndo/__inkRedo/__inkStrokes 约定,两阅读器同构)
  function pushUndo(host, cap) {
    if (!host.__inkUndo) host.__inkUndo = [];
    host.__inkUndo.push(JSON.stringify(host.__inkStrokes || []));
    if (host.__inkUndo.length > (cap || 40)) host.__inkUndo.shift();
    host.__inkRedo = [];
  }

  window.RCInk = { drawStroke: drawStroke, ptSeg: ptSeg, hit: hit, norm: norm,
                   redraw: redraw, eraseAt: eraseAt, pushUndo: pushUndo };
})();
