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

  var REGION_MAX_POINTS = 512;

  function _pts(s) { return s.p || s.pts || []; }

  function _regionPoints(s) { return _pts(s).slice(0, REGION_MAX_POINTS); }

  function _regionTimestamp(s) {
    var value = Number(s && s.createdAtEpochMs);
    return Number.isFinite(value) && value > 0 ? value : 0;
  }

  function _regionTimeLabel(s) {
    var stamp = _regionTimestamp(s);
    if (!stamp) return '--:--';
    var date = new Date(stamp);
    if (!Number.isFinite(date.getTime())) return '--:--';
    var pad = function (value) { return String(value).padStart(2, '0'); };
    return pad(date.getHours()) + ':' + pad(date.getMinutes());
  }

  function _pointInPolygon(point, points) {
    var inside = false;
    for (var i = 0, j = points.length - 1; i < points.length; j = i++) {
      var xi = points[i][0], yi = points[i][1], xj = points[j][0], yj = points[j][1];
      var crosses = ((yi > point[1]) !== (yj > point[1])) &&
        (point[0] < (xj - xi) * (point[1] - yi) / ((yj - yi) || Number.EPSILON) + xi);
      if (crosses) inside = !inside;
    }
    return inside;
  }

  function _regionNumbers(strokes) {
    var regions = (strokes || []).filter(function (stroke) { return stroke && stroke.t === 'region'; });
    regions.sort(function (a, b) {
      var byTime = _regionTimestamp(a) - _regionTimestamp(b);
      if (byTime) return byTime;
      var aid = String(a.id || ''), bid = String(b.id || '');
      return aid < bid ? -1 : (aid > bid ? 1 : 0);
    });
    var numbers = new Map();
    for (var i = 0; i < regions.length; i++) numbers.set(regions[i], i + 1);
    return numbers;
  }

  // 画一条笔画:pen=平滑 quadratic(单点=点笔头)/ line / arrow / rect
  function drawStroke(ctx, s, W, H, dpr, defs) {
    var pts = s && s.t === 'region' ? _regionPoints(s) : _pts(s); if (!pts.length) return;
    ctx.strokeStyle = s.c || (defs && defs.color) || '#e74c3c';
    ctx.lineWidth = Math.max(0.6, (s.w || (defs && defs.width) || 2.5) * dpr);
    ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    var X = function (i) { return pts[i][0] * W; }, Y = function (i) { return pts[i][1] * H; };
    var t = s.t || 'pen';
    if (t === 'region' && pts.length >= 3) {
      ctx.save();
      ctx.beginPath(); ctx.moveTo(X(0), Y(0));
      for (var rp = 1; rp < pts.length; rp++) ctx.lineTo(X(rp), Y(rp));
      ctx.closePath();
      ctx.fillStyle = ctx.strokeStyle;
      ctx.globalAlpha = 0.18;
      try { ctx.fill('evenodd'); } catch (_) { ctx.fill(); }
      ctx.globalAlpha = 0.92;
      ctx.stroke();

      var minX = X(0), minY = Y(0);
      for (var rb = 1; rb < pts.length; rb++) {
        minX = Math.min(minX, X(rb)); minY = Math.min(minY, Y(rb));
      }
      var number = defs && Number.isFinite(defs.regionNumber) ? defs.regionNumber : '?';
      var label = '#' + number + ' ' + _regionTimeLabel(s);
      var fontSize = Math.max(10, 11 * dpr), pad = Math.max(3, 3 * dpr);
      ctx.font = '600 ' + fontSize + 'px -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif';
      ctx.textBaseline = 'top';
      var labelWidth = (ctx.measureText ? ctx.measureText(label).width : label.length * fontSize * 0.62) + pad * 2;
      var labelHeight = fontSize + pad * 2;
      var labelX = Math.max(0, Math.min(W - labelWidth, minX));
      var labelY = Math.max(0, Math.min(H - labelHeight, minY - labelHeight - 2 * dpr));
      ctx.globalAlpha = 0.82; ctx.fillStyle = '#111827';
      ctx.fillRect(labelX, labelY, labelWidth, labelHeight);
      ctx.globalAlpha = 1; ctx.fillStyle = '#ffffff';
      ctx.fillText(label, labelX + pad, labelY + pad);
      ctx.restore();
    } else if (t === 'pen') {
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
    var pts = s && s.t === 'region' ? _regionPoints(s) : _pts(s); if (!pts.length) return false;
    if (s.t === 'region' && pts.length >= 3) {
      if (_pointInPolygon(pt, pts)) return true;
      for (var r = 0; r < pts.length; r++) {
        if (ptSeg(pt, pts[r], pts[(r + 1) % pts.length]) < thr) return true;
      }
      return false;
    }
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

  // 将浮动工具栏放到最近一次 Pencil hover/落笔点上方；没有锚点时恢复调用方 CSS 的旧固定位置。
  function positionToolbarAbove(toolbar, anchor) {
    if (!toolbar) return;
    if (!anchor || !Number.isFinite(anchor.x) || !Number.isFinite(anchor.y)) {
      ['left', 'top', 'right', 'bottom', 'transform'].forEach(function (name) { toolbar.style.removeProperty(name); });
      return;
    }
    var rect = toolbar.getBoundingClientRect();
    if (!(rect.width > 0 && rect.height > 0)) return;
    var margin = 8, gap = 14;
    var maxLeft = Math.max(margin, (window.innerWidth || rect.width) - rect.width - margin);
    var maxTop = Math.max(margin, (window.innerHeight || rect.height) - rect.height - margin);
    var left = Math.max(margin, Math.min(maxLeft, anchor.x - rect.width / 2));
    var top = Math.max(margin, Math.min(maxTop, anchor.y - rect.height - gap));
    toolbar.style.left = left + 'px'; toolbar.style.top = top + 'px';
    toolbar.style.right = 'auto'; toolbar.style.bottom = 'auto'; toolbar.style.transform = 'none';
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
    var arr = strokes || [], regionNumbers = _regionNumbers(arr);
    for (var i = 0; i < arr.length; i++) {
      var drawDefs = defs;
      if (arr[i] && arr[i].t === 'region') {
        drawDefs = Object.assign({}, defs || {}, { regionNumber: regionNumbers.get(arr[i]) });
      }
      drawStroke(ctx, arr[i], cv.width, cv.height, dpr, drawDefs);
    }
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
                   redraw: redraw, eraseAt: eraseAt, pushUndo: pushUndo,
                   positionToolbarAbove: positionToolbarAbove,
                   REGION_MAX_POINTS: REGION_MAX_POINTS };
})();
