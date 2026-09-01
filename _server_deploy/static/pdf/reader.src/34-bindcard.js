// ══════════ 卡片绑到**书页正文**的字符锚（用户设计 C15 第二版，2026-08-19）══════════
//
// 第一版只能把卡钉到自建页的格子块（`bind.kind === 'upage-block'`）。用户的原话是
// 「我们的卡片是通过绑定元素来保证自己的位置和**自身的数据嵌入上下文的位置**的」——
// 那就不能只在自己造的纸上成立，真书正文里也得能钉。
//
// ## 锚为什么是「序号 + 文本 + revision」三件套
//
// - **只用文本不行**：用户明确提过「要考虑单词重复出现」。同一个词在一页里出现好
//   几次时，光靠文本说不清是哪一处。
// - **只用序号不行**：序号是某一份字符层里的下标。同一本书可以有多份文字层
//   （PDF 原文字层 / Pi / PC，书库里能切换），换一份序号就全变。
// - 所以：`rev` 对得上就按序号精确定位；对不上就按文本重新找，再用**原序号**挑
//   最接近的那一处消歧。两条都失败时 `text` 仍然说得清这张卡当初钉在哪句话上。
//
// ## 坐标系
//
// 正文框跟高亮同一套：页内绝对定位，位置由 charBoxes 的 left/top/width/height 直接给出
// （它们已经是渲染后的 CSS 像素），不靠 fixed 跟滚。右侧快捷轨和展开卡是刻意
// portal 到 body 的视口 UI：前者每帧读正文框 BCR 对齐，后者避免被 page-wrap 裁切。
//
// ## 标记的形态（2026-08-20 用户重新定，替换了初版）
//
// 初版是「落下时展开一张卡、非活跃计时、到点收成球留在锚点上」。用户否掉了：
// 「圆球过多会遮挡视野」，以及更要命的一条 ——
// 「实际的书中字符可不像你的例子那样有足够的空白位置，这样会盖到其它字符」。
//
// 所以标记**不占正文面积**，改成：给被锚的词画透明底分类色边框 + 右上角一个
// **页内序号**；卡片按需点开，一次只开一张。
// 不恢复实心填充：扫描书底色本来就不匀，重色块会直接盖住字形。
//
// 序号是它在**本页**的次序，不是全书连续：位置，不是身份。人和 AI 用同一套，
// 所以能说「把第 3 个删掉」。加卡/删卡后整页重排（_renumberMarks 每次全量重算）。

(function () {
  'use strict';

  var _pageBindPending = [];   // 绑不上的先存着：最常见的失败是"那页还没渲染"
  var _RAIL_W = 150, _DOT_MIN = 17, _DOT_MAX = 30, _RAIL_EDGE = 18;
  var _railRaf = 0, _railSleep = 0, _fallbackCard = null;
  var _BIND_TONES = {
    text: '#b9a8ff', qa: '#7dd3fc', image: '#34d399', number: '#fbbf24'
  };

  function _stripWs(s) { return String(s || '').replace(/\s+/g, ''); }

  /// 按**原始下标 `_oi`** 排一份视图。
  ///
  /// ⚠ 这是 2026-08-20 查出来的一个真 bug：`__charBoxes` 在 `_mapCharBoxes` 里被
  ///   **按阅读顺序重排过**（`cb.sort()` 先 baseline 后 left），而 AI 拿到的
  ///   `from/to` 来自服务端 chars 的**原序**（segments 两侧都是 enumerate 原序）。
  ///   也就是说 `boxes[i]` 的位置跟 `bind.from` 根本不是同一个坐标系。
  ///
  ///   之前没炸，是因为文本回退兜住了 —— 但代价是「按序号精确定位」那条路
  ///   **从来没命中过**，而且消歧时拿来比距离的参考下标也是错的。
  ///   典型的静默降级：功能看着在用，实际一直走的是兜底那条。
  function _byOi(boxes) {
    var v = boxes.slice();
    v.sort(function (a, b) { return (a._oi | 0) - (b._oi | 0); });
    return v;
  }

  /// 把 bind 解成一段字符框。四条路，返回的 `how` 就是定位质量：
  ///   exact / by-block / by-text / by-text-block-missed
  ///
  /// ⚠ from/to 现在是**可选**的（2026-08-23：助手可以只说「第 3 块 + 这句话」，
  ///   换算由这里做）。所以不能再用 `want.from | 0` —— 那会把"没给"变成 0，
  ///   于是消歧时偏向页首那一处，而且看不出它是猜的。
  function _resolveRange(boxes, want) {
    var hasRange = Number.isFinite(want.from) && Number.isFinite(want.to);
    var from = hasRange ? (want.from | 0) : -1;
    var to = hasRange ? Math.max(want.from | 0, want.to | 0) : -1;
    var text = _stripWs(want.text);
    var wantBlock = Number.isFinite(want.block) && want.block >= 1
      ? (want.block | 0) : 0;
    var ord = _byOi(boxes);

    // ① 序号是 _oi 语义：先按 _oi 取出这一段，比对文字
    if (hasRange) {
      var got = '', hit = [];
      for (var k = 0; k < ord.length; k++) {
        var oi = ord[k]._oi | 0;
        if (oi < from || oi > to) continue;
        hit.push(ord[k]);
        if (!ord[k].sp && ord[k].c) got += ord[k].c;
      }
      if (hit.length) {
        if (!text) return { lo: from, hi: to, boxes: hit, how: 'exact' };
        if (_stripWs(got) === text) {
          return { lo: from, hi: to, boxes: hit, how: 'exact' };   // 序号仍然作数
        }
      }
    }

    // 序号对不上了（换过文字层 / 越界 / 那段字变了），或者压根没给序号
    // → 按文本重新找。有块号就先只在那一块里找。
    if (!text) return null;

    // 块号 → 从 1 起的连号，与 pageTextSegments 印给助手的 [NN] 同一套推导。
    // ⚠ 两处必须用同一个推导，否则助手说的第 3 块跟这里算的不是同一块，
    //   而两边各自都自洽 —— 最难查的那类错。
    var blockNo = Object.create(null), blockSeq = 0;
    function numberOf(bk) {
      var key = String(bk);
      if (!blockNo[key]) { blockSeq += 1; blockNo[key] = blockSeq; }
      return blockNo[key];
    }

    function search(onlyBlock) {
      var joined = '', index = [];   // joined 每个字符 → ord 里的位置
      for (var i = 0; i < ord.length; i++) {
        var c = ord[i] && ord[i].c;
        if (!c || ord[i].sp) continue;
        if (onlyBlock && numberOf(ord[i].bk) !== onlyBlock) continue;
        joined += c;
        index.push(i);
      }
      var best = -1, bestDist = Infinity, at = joined.indexOf(text);
      while (at >= 0) {
        // 没给序号时不做距离偏好 —— 否则等于假装知道它在哪。
        var dist = hasRange ? Math.abs((ord[index[at]]._oi | 0) - from) : 0;
        if (dist < bestDist) { bestDist = dist; best = at; }
        at = joined.indexOf(text, at + 1);
      }
      if (best < 0) return null;
      var a = index[best];
      var b = index[Math.min(best + text.length - 1, index.length - 1)];
      return {
        lo: ord[a]._oi | 0, hi: ord[b]._oi | 0,
        boxes: ord.slice(a, b + 1)
      };
    }

    if (wantBlock) {
      // 先把连号建满，保证 numberOf 的编号与整页一致（只扫指定块会算错号）
      for (var n = 0; n < ord.length; n++) {
        if (ord[n] && ord[n].c && !ord[n].sp) numberOf(ord[n].bk);
      }
      var inBlock = search(wantBlock);
      if (inBlock) { inBlock.how = 'by-block'; return inBlock; }
    }
    var anywhere = search(0);
    if (!anywhere) return null;
    // ⚠ 如实说出走的哪条。带了块号却退回全页 = 块对不上了，必须看得见。
    anywhere.how = wantBlock ? 'by-text-block-missed' : 'by-text';
    return anywhere;
  }

  /// 按行切成多个矩形。整体包围盒不够用 —— 被锚的词跨行时，一个大框会把
  /// 两行之间的整片正文都圈进去。
  /// 吃的是 range.boxes（已按 _oi 选好的那些），不再按下标索引 —— 见 _byOi
  /// 那段注释：boxes 数组的位置跟 bind.from 不是同一个坐标系。
  function _rangeRects(range) {
    var picked = range.boxes.slice();
    // 画框要按几何顺序，所以这里按 baseline 再排一次
    picked.sort(function (a, b) {
      var d = (a.top + a.height) - (b.top + b.height);
      return Math.abs(d) > (Math.max(a.height, b.height) || 1) * 0.6 ? d : a.left - b.left;
    });
    var lines = [], cur = null;
    for (var i = 0; i < picked.length; i++) {
      var b = picked[i];
      if (!b || b.sp) continue;
      var base = b.top + b.height;
      // 同一行的判据用**行高的一半**：OCR 出来的同行字 baseline 会有零点几像素
      // 的抖动，用严格相等会把一行切成好几段。
      if (cur && Math.abs(base - cur.base) < b.height * 0.6) {
        cur.x0 = Math.min(cur.x0, b.left);
        cur.y0 = Math.min(cur.y0, b.top);
        cur.x1 = Math.max(cur.x1, b.left + b.width);
        cur.y1 = Math.max(cur.y1, base);
      } else {
        cur = { base: base, x0: b.left, y0: b.top,
                x1: b.left + b.width, y1: base };
        lines.push(cur);
      }
    }
    return lines.length ? lines : null;
  }

  /// 词锚只认四类视觉语义。旧工具卡色值也在这里归一，避免正文框、序号、
  /// 右侧浮标和展开卡各自沿用一套历史色表。
  function _bindCategory(payload) {
    payload = payload || {};
    var raw = String(payload.category || payload.kind || '').toLowerCase();
    var label = String(payload.label || '') + ' ' + String(payload.text || '').slice(0, 80);
    if (/image|images|video|配图|图片|图像|视频/.test(raw + ' ' + label)) return 'image';
    if (/number|numeric|metric|weather|数值|数字|数据|统计|温度|价格/.test(raw + ' ' + label)) return 'number';
    if (/qa|question|anki|quiz|问答|考点|出题|题目|学习卡/.test(raw + ' ' + label)) return 'qa';
    if (/text|文字|背景|辨析|摘要|翻译|解释|新闻/.test(raw + ' ' + label)) return 'text';
    // rc-toolchip 的旧类型色 → 本设计四类色。只作旧调用方兼容，新调用方应传 category。
    var old = String(payload.tone || '').toLowerCase();
    if (old === '#c77dff' || old === '#34d399' || old === '#ff7a59') return 'image';
    if (old === '#39d98a' || old === '#7dd3fc') return 'qa';
    if (old === '#2dd4bf' || old === '#fbbf24') return 'number';
    return 'text';
  }

  function _bindColor(payload) {
    return _BIND_TONES[_bindCategory(payload)] || _BIND_TONES.text;
  }

  /// 取这张卡的分类色调，推出正文框/数字/浮标共用的自定义属性。
  ///
  /// ⚠ 不能直接把色调原色当边框：实测在纸上只有 1.6~2.5:1，够不到 WCAG 对
  ///   图形元素的 3:1（1.5px 细边尤其吃亏）。掺 40% 深底压到 3.4~4.9:1。
  ///   角标同理 —— 卡片系统那套「浅色调字压半透明深填充」是给**深色卡面上的
  ///   SVG 图标**调的，图标靠形状识别 3:1 就够；搬到白纸上的数字实测只有
  ///   1.0~1.4:1（某些色几乎隐形）。
  function _bindTone(payload) {
    var tc = _bindColor(payload);
    return '--pm-t:' + tc + ';' +
           '--pm-b:color-mix(in srgb,' + tc + ' 60%,#2a2440);' +
           '--pm-i:color-mix(in srgb,' + tc + ' 22%,#14101f);' +
           '--pm-h:color-mix(in srgb,' + tc + ' 30%,transparent);';
  }

  function _scrollAncestor(el) {
    for (var p = el && el.parentElement; p && p !== document.body && p !== document.documentElement; p = p.parentElement) {
      var s = null; try { s = getComputedStyle(p); } catch (e) {}
      if (s && /(auto|scroll|overlay)/.test((s.overflowY || '') + ' ' + (s.overflow || ''))) return p;
    }
    return document.getElementById('main') || document.scrollingElement || document.documentElement;
  }

  function _wakeRail(rail) {
    if (!rail) return;
    rail.classList.add('awake');
    clearTimeout(_railSleep);
    _railSleep = setTimeout(function () { try { rail.classList.remove('awake'); } catch (e) {} }, 1600);
  }

  /// Scroll itself is compositor-driven, while the rail is a body/fixed portal.
  /// Waiting for the coalesced rAF before touching the portal therefore leaves
  /// one visibly stale frame behind the page.  Move only the dots by the known
  /// scroll delta immediately (no geometry read), then let the rAF below replace
  /// it with fresh anchor BCRs.  The tiny viewport indicator deliberately does
  /// not share this transform: its movement is proportional to total scroll.
  function _rootScrollTop() {
    var root = document.scrollingElement || document.documentElement || document.body;
    return Number((root && root.scrollTop) || window.scrollY || window.pageYOffset || 0);
  }

  function _primeRailScroll(rail) {
    if (!rail || !rail.__scrollEl) return;
    var scrollEl = rail.__scrollEl;
    var root = document.scrollingElement || document.documentElement || document.body;
    var localNow = Number(scrollEl.scrollTop || 0);
    var rootNow = _rootScrollTop();
    var localBase = Number(rail.__layoutScrollTop || 0);
    var rootBase = Number(rail.__layoutRootScrollTop || 0);
    var shift = -(localNow - localBase);
    if (scrollEl === root || scrollEl === document.documentElement || scrollEl === document.body) {
      // Root scrolling is already represented by scrollEl.scrollTop.
      rootNow = localNow;
    } else {
      shift -= (rootNow - rootBase);
    }
    rail.style.setProperty('--rail-y-shift', shift + 'px');
  }

  function _scheduleRails() {
    if (_railRaf) return;
    var raf = window.requestAnimationFrame || function (fn) { return setTimeout(fn, 0); };
    _railRaf = raf(function () {
      _railRaf = 0;
      var rails = document.querySelectorAll('.pgbind-rail');
      for (var i = 0; i < rails.length; i++) _layoutRail(rails[i]);
    });
  }

  function _collapseRailGroup(rail) {
    if (rail) rail.__expandedGroupKey = '';
  }

  function _railGroupKey(row) {
    var keys = [];
    for (var i = 0; i < row.length; i++) keys.push(String(row[i].dataset.bindkey || ''));
    keys.sort();
    return keys.join('|');
  }

  /// 同行重叠组第一次只展开，第二次点到明确编号才打开卡片。
  function _openRailDot(rail, dot, open, ev) {
    if (ev) ev.stopPropagation();
    var groupKey = dot && dot.__bwGroupKey;
    if (rail && dot && dot.__bwGroupSize > 1 && groupKey && rail.__expandedGroupKey !== groupKey) {
      rail.__expandedGroupKey = groupKey;
      _wakeRail(rail);
      _scheduleRails();
      return;
    }
    open(ev);
  }

  function _destroyRail(rail) {
    if (!rail) return;
    var scrollEl = rail.__scrollEl, onMove = rail.__onMove;
    try { if (scrollEl && onMove) scrollEl.removeEventListener('scroll', onMove); } catch (e) {}
    try { if (onMove) document.removeEventListener('scroll', onMove, true); } catch (e2) {}
    try { if (onMove) window.removeEventListener('scroll', onMove, true); } catch (e3) {}
    try { if (onMove) window.removeEventListener('resize', onMove); } catch (e4) {}
    try { if (rail.__onOutside) document.removeEventListener('pointerdown', rail.__onOutside, true); } catch (e5) {}
    try { if (rail.__rootObserver) rail.__rootObserver.disconnect(); } catch (e6) {}
    try { if (scrollEl && scrollEl.__pgbindRail === rail) scrollEl.__pgbindRail = null; } catch (e7) {}
    try { rail.remove(); } catch (e8) {}
  }

  function _railFor(pw) {
    var scrollEl = _scrollAncestor(pw);
    if (!scrollEl || !document.body) return null;
    if (scrollEl.__pgbindRail && scrollEl.__pgbindRail.isConnected) return scrollEl.__pgbindRail;
    var rail = document.createElement('div');
    rail.className = 'pgbind-rail';
    rail.setAttribute('aria-label', '本页卡片快捷入口');
    var view = document.createElement('div'); view.className = 'pgbind-rail-view';
    rail.appendChild(view);
    document.body.appendChild(rail);
    rail.__scrollEl = scrollEl; rail.__view = view; rail.__expandedGroupKey = '';
    rail.__layoutScrollTop = Number(scrollEl.scrollTop || 0);
    rail.__layoutRootScrollTop = _rootScrollTop();
    rail.style.setProperty('--rail-y-shift', '0px');
    scrollEl.__pgbindRail = rail;
    var onMove = function () {
      _primeRailScroll(rail);
      _collapseRailGroup(rail);
      _wakeRail(rail);
      _scheduleRails();
    };
    rail.__onMove = onMove;
    try { scrollEl.addEventListener('scroll', onMove, { passive: true }); } catch (e) {}
    // Element scroll does not bubble. Capture catches nested/internal scrollers;
    // window covers the document scrollingElement path in WebKit as well.
    try { document.addEventListener('scroll', onMove, { capture: true, passive: true }); } catch (e2) {}
    try { window.addEventListener('scroll', onMove, { capture: true, passive: true }); } catch (e3) {}
    try { window.addEventListener('resize', onMove); } catch (e4) {}
    var onOutside = function (ev) {
      if (!rail.__expandedGroupKey || (ev && ev.target && rail.contains(ev.target))) return;
      _collapseRailGroup(rail);
      _scheduleRails();
    };
    rail.__onOutside = onOutside;
    try { document.addEventListener('pointerdown', onOutside, true); } catch (e5) {}
    // #main 被刷新/宿主重建时，旧滚动根不会再发 scroll；监听 DOM 替换后主动
    // 撤掉 body 上的孤儿 fixed rail 与事件，不等下一次 resize 才收拾。
    try {
      if (window.MutationObserver && document.body) {
        rail.__rootObserver = new MutationObserver(function () {
          if (!scrollEl.isConnected) _destroyRail(rail);
        });
        rail.__rootObserver.observe(document.body, { childList: true, subtree: true });
      }
    } catch (e6) {}
    _wakeRail(rail); _scheduleRails();
    return rail;
  }

  function _layoutRail(rail) {
    var scrollEl = rail && rail.__scrollEl;
    if (!scrollEl || !scrollEl.isConnected) { _destroyRail(rail); return; }
    var sr = scrollEl.getBoundingClientRect();
    var top = Math.max(0, sr.top), bottom = Math.min(window.innerHeight || sr.bottom, sr.bottom);
    var h = Math.max(0, bottom - top);
    var padR = 0; try { padR = parseFloat(getComputedStyle(scrollEl).paddingRight) || 0; } catch (e) {}
    rail.style.left = Math.round(Math.max(sr.left, sr.right - padR - _RAIL_W)) + 'px';
    rail.style.top = Math.round(top) + 'px';
    rail.style.width = _RAIL_W + 'px'; rail.style.height = Math.round(h) + 'px';
    rail.style.display = h > 0 ? '' : 'none';

    var above = [], below = [], near = [];
    var dots = [].slice.call(rail.querySelectorAll('.pgbind-rail-dot'));
    for (var i = 0; i < dots.length; i++) {
      var d = dots[i], a = d.__bwAnchor;
      if (!a || !a.isConnected) { d.style.display = 'none'; continue; }
      var ar = a.getBoundingClientRect();
      if (!ar.width && !ar.height) { d.style.display = 'none'; continue; }
      d.style.display = '';
      var cy = ar.top + ar.height / 2 - top;
      var size = Math.max(_DOT_MIN, Math.min(_DOT_MAX, Math.round(ar.height || _DOT_MIN)));
      d.__bwCy = cy; d.__bwSize = size;
      if (cy >= 0 && cy <= h) near.push(d);
      else if (cy < 0) above.push(d); else below.push(d);
    }
    near.sort(function (a, b) { return a.__bwCy - b.__bwCy; });
    var rows = [], row = [], lastCy = -1e9;
    for (var n = 0; n < near.length; n++) {
      var next = near[n];
      if (row.length && Math.abs(next.__bwCy - lastCy) >
          Math.max(next.__bwSize, row[0].__bwSize) * 0.6) {
        rows.push(row); row = [];
      }
      row.push(next); lastCy = next.__bwCy;
    }
    if (row.length) rows.push(row);

    var liveGroups = Object.create(null);
    for (var g = 0; g < rows.length; g++) {
      rows[g].sort(function (a, b) {
        return (parseInt(a.textContent, 10) || 0) - (parseInt(b.textContent, 10) || 0);
      });
      if (rows[g].length > 1) liveGroups[_railGroupKey(rows[g])] = true;
    }
    if (rail.__expandedGroupKey && !liveGroups[rail.__expandedGroupKey]) _collapseRailGroup(rail);

    for (var r0 = 0; r0 < rows.length; r0++) {
      var group = rows[r0], groupKey = group.length > 1 ? _railGroupKey(group) : '';
      var expanded = !!groupKey && rail.__expandedGroupKey === groupKey;
      var rowCy = 0, rowSize = 0;
      for (var q0 = 0; q0 < group.length; q0++) {
        rowCy += group[q0].__bwCy;
        rowSize = Math.max(rowSize, group[q0].__bwSize);
      }
      rowCy /= group.length;
      var maxRight = Math.max(16, _RAIL_W - rowSize - 4);
      var spreadStep = group.length > 1
        ? Math.min(rowSize + 6, Math.max(6, (maxRight - 16) / (group.length - 1))) : 0;
      for (var q = 0; q < group.length; q++) {
        var dot = group[q], size0 = dot.__bwSize;
        dot.__bwGroupKey = groupKey; dot.__bwGroupSize = group.length;
        dot.classList.remove('far');
        dot.classList.toggle('grouped', group.length > 1);
        dot.classList.toggle('group-lead', group.length > 1 && q === 0);
        dot.classList.toggle('group-open', expanded);
        if (group.length > 1 && q === 0 && !expanded) {
          dot.dataset.groupCount = String(group.length);
          dot.setAttribute('aria-expanded', 'false');
          dot.setAttribute('aria-label', '本行 ' + group.length + ' 张卡片，点击展开');
          dot.title = '本行 ' + group.length + ' 张卡片，点击展开';
        } else {
          delete dot.dataset.groupCount;
          if (group.length > 1 && q === 0) dot.setAttribute('aria-expanded', 'true');
          else dot.removeAttribute('aria-expanded');
          if (dot.__bwCardLabel) {
            dot.setAttribute('aria-label', dot.__bwCardLabel);
            dot.title = dot.__bwCardLabel;
          }
        }
        dot.tabIndex = group.length > 1 && !expanded && q > 0 ? -1 : 0;
        dot.style.zIndex = String(group.length > 1 && !expanded ? 40 + group.length - q : 20 + group.length - q);
        dot.style.width = size0 + 'px'; dot.style.height = size0 + 'px';
        dot.style.borderRadius = Math.round(size0 * 0.325) + 'px';
        dot.style.fontSize = Math.max(9, Math.round(size0 * 0.42)) + 'px';
        dot.style.top = Math.max(_RAIL_EDGE, Math.min(h - _RAIL_EDGE, rowCy)) + 'px';
        // 收起时只错开一点并用组数量提示；第一次点击后才沿水平方向完整展开。
        var step = expanded ? spreadStep : 5;
        dot.style.right = Math.min(maxRight, 16 + q * step) + 'px';
      }
    }
    var farPlace = function (arr, lower) {
      arr.sort(function (a, b) { return a.__bwCy - b.__bwCy; });
      for (var k = 0; k < arr.length; k++) {
        var fd = arr[k];
        fd.__bwGroupKey = ''; fd.__bwGroupSize = 1;
        fd.classList.add('far');
        fd.classList.remove('grouped', 'group-lead', 'group-open');
        delete fd.dataset.groupCount; fd.removeAttribute('aria-expanded');
        fd.tabIndex = 0; fd.style.zIndex = ''; fd.style.right = '13px';
        if (fd.__bwCardLabel) {
          fd.setAttribute('aria-label', fd.__bwCardLabel);
          fd.title = fd.__bwCardLabel;
        }
        fd.style.top = (lower
          ? h - _RAIL_EDGE + 2 + (_RAIL_EDGE - 4) * ((k + 1) / (arr.length + 1))
          : 2 + (_RAIL_EDGE - 4) * ((k + 1) / (arr.length + 1))) + 'px';
      }
    };
    farPlace(above, false); farPlace(below, true);
    if (rail.__view) {
      var total = Math.max(scrollEl.clientHeight || 0, scrollEl.scrollHeight || 0, 1);
      rail.__view.style.top = ((scrollEl.scrollTop || 0) / total * h) + 'px';
      rail.__view.style.height = (Math.max(8, (scrollEl.clientHeight || h) / total * h)) + 'px';
    }
    // Commit against the latest scroll offsets in the same animation frame,
    // then clear the compositor-only provisional delta.  No stale delta can
    // leak into a second frame, even when several scroll events were coalesced.
    rail.__layoutScrollTop = Number(scrollEl.scrollTop || 0);
    rail.__layoutRootScrollTop = _rootScrollTop();
    rail.style.setProperty('--rail-y-shift', '0px');
  }

  function _removeRailMark(key) {
    var rails = document.querySelectorAll('.pgbind-rail');
    for (var i = 0; i < rails.length; i++) {
      var dots = rails[i].querySelectorAll('.pgbind-rail-dot[data-bindkey="' + key + '"]');
      for (var j = 0; j < dots.length; j++) dots[j].remove();
      // 最后一张卡删掉后连 2px view 也必须消失；空轨道不是功能入口，
      // 留着只会像一条来源不明的页面装饰。
      if (!rails[i].querySelector('.pgbind-rail-dot')) _destroyRail(rails[i]);
    }
    _scheduleRails();
  }

  function _setBindActive(key, on) {
    var all = document.querySelectorAll('[data-bindkey="' + key + '"]');
    for (var i = 0; i < all.length; i++) all[i].classList.toggle('on', !!on);
  }

  function _clearBindActive() {
    var all = document.querySelectorAll('.pgmark.on,.pgmark-n.on,.pgbind-rail-dot.on');
    for (var i = 0; i < all.length; i++) all[i].classList.remove('on');
  }

  function _removeMark(layer, key) {
    _removeRailMark(key);
    if (_fallbackCard && _fallbackCard.dataset.bindkey === key) _closeFallbackCard();
    var olds = layer.querySelectorAll('[data-bindkey="' + key + '"]');
    for (var i = 0; i < olds.length; i++) {
      try { olds[i].__bwBindTeardown && olds[i].__bwBindTeardown(); } catch (e) {}
      olds[i].remove();
    }
  }

  /// 本页所有标记按**纵向位置**重排序号（先行后列）。
  /// 加卡/删卡后整页重排 —— 序号是位置不是身份，所以每次都全量重算，
  /// 不去维护什么自增计数器（那种一定会跟实际顺序漂移）。
  function _renumberMarks(pw) {
    var layer = pw.querySelector('.pgbind-layer');
    if (!layer) return;
    var ns = [].slice.call(layer.querySelectorAll('.pgmark-n'));
    // ⚠ 排序基准是被锚词的**顶边**（dataset.by），不是角标自己的 style.top ——
    //   两者差一个固定偏移，但 by 才是内容坐标，跟 _rangeRects 同源。
    //   同行判据用**行高的一半**而不是固定 6px：同一行不同字号/字形的两个词，
    //   顶边差常常超过 6px，用固定阈值会把同一行判成上下关系，编号跟阅读顺序
    //   相反 —— 而人和 AI 说的「第 3 个」就此不是同一张。
    var byOf = function (el) { return parseFloat(el.dataset.by || el.style.top) || 0; };
    var bxOf = function (el) { return parseFloat(el.dataset.bx || el.style.left) || 0; };
    var lh = 0;
    for (var m0 = 0; m0 < ns.length; m0++) {
      var mk = layer.querySelector('.pgmark[data-bindkey="' + ns[m0].dataset.bindkey + '"]');
      var h0 = mk ? (parseFloat(mk.style.height) || 0) : 0;
      if (h0 > lh) lh = h0;
    }
    var rowTol = Math.max(6, lh * 0.5);
    ns.sort(function (a, b) {
      var ta = byOf(a), tb = byOf(b);
      if (Math.abs(ta - tb) > rowTol) return ta - tb;
      return bxOf(a) - bxOf(b);
    });
    // 同一个词上有多张卡时角标会完全重叠 → 点不中被压住的那个。按顺序横向错开。
    var seen = {};
    for (var i = 0; i < ns.length; i++) {
      ns[i].textContent = String(i + 1);
      ns[i].title = '本页第 ' + (i + 1) + ' 张卡片';
      var sig = ns[i].dataset.bx + ',' + ns[i].dataset.by;
      if (ns[i].dataset.bx == null) continue;
      var dup = seen[sig] || 0; seen[sig] = dup + 1;
      // ⚠ 角标往**词的内侧**放，右边缘对齐词的右边缘 —— 不要往外挑。
      //   往外只偏 2.5px 的话，在无空格排版（中日文）+ 紧贴墨迹的 OCR 文字层
      //   + 低缩放这三条同时成立时，白光晕会啃掉**后一个字**左上角的笔画。
      //   盖自己这个词的尾巴无所谓（它本来就被框起来了，是这张卡的地盘），
      //   盖邻字才是用户说的「遮挡」。宽度按位数估（9.5px 字重 700 约 5.4px/位）。
      //   序号变多位数时也要跟着退，所以这里算、不在建元素时算。
      var wEst = String(ns[i].textContent || '1').length * 5.4 + 1;
      ns[i].style.left = ((parseFloat(ns[i].dataset.bx) || 0) - wEst - dup * (wEst + 2)) + 'px';
      var rd = document.querySelector('.pgbind-rail-dot[data-bindkey="' + ns[i].dataset.bindkey + '"]');
      if (rd) {
        rd.textContent = ns[i].textContent;
        rd.__bwCardLabel = ns[i].title;
        rd.title = ns[i].title;
        rd.setAttribute('aria-label', ns[i].title);
      }
    }
    _scheduleRails();
  }

  function _closeFallbackCard() {
    if (!_fallbackCard) return;
    var old = _fallbackCard; _fallbackCard = null;
    _setBindActive(old.dataset.bindkey || '', false);
    try { old.remove(); } catch (e) {}
  }

  /// 只有没有持久便签宿主的兼容界面才会走这里。仍然调用 RC.voiceCard.renderInto，
  /// 并把 host portal 到 body；不再维护第二套 .pgbind-card 视觉。
  function _toggleBindCard(key, payload, source) {
    if (_fallbackCard && _fallbackCard.dataset.bindkey === key) { _closeFallbackCard(); return false; }
    _closeFallbackCard();
    if (!(window.RC && RC.voiceCard && RC.voiceCard.renderInto) || !document.body) return false;
    var host = document.createElement('div');
    host.className = 'pgbind-card-portal'; host.dataset.bindkey = key;
    host.style.width = Math.min(326, Math.max(120, (window.innerWidth || 360) - 16)) + 'px';
    document.body.appendChild(host);
    var card = null;
    try {
      card = RC.voiceCard.renderInto(host, {
        text: payload.isHtml ? (payload.raw || '') : (payload.text || ''),
        label: payload.label || '卡片', isHtml: !!payload.isHtml,
        type: _bindColor(payload), icon: payload.icon || '🎴', form: 'full', cid: payload.uid || key
      });
    } catch (e) {}
    if (!card) { host.remove(); return false; }
    host.addEventListener('click', function (ev) { ev.stopPropagation(); });
    var sr = source && source.getBoundingClientRect ? source.getBoundingClientRect() : { left: 8, right: 8, top: 8, height: 0 };
    var cr = host.getBoundingClientRect(), gap = 10, vw = window.innerWidth || 1024, vh = window.innerHeight || 768;
    var left = sr.left - cr.width - gap;
    if (left < 8) left = sr.right + gap;
    left = Math.max(8, Math.min(vw - cr.width - 8, left));
    var top = Math.max(8, Math.min(vh - cr.height - 8, sr.top + sr.height / 2 - cr.height / 2));
    host.style.left = left + 'px'; host.style.top = top + 'px';
    _fallbackCard = host; _setBindActive(key, true);
    return true;
  }

  function _expandRangeToVocabMark(pw, boxes, range) {
    try {
      var marks = pw && pw.__vocabMarks;
      if (!marks || !marks.length || !range || !range.boxes ||
          !range.boxes.length) return range;
      var probe = [range.boxes[0],
                   range.boxes[range.boxes.length - 1]];
      var mark = null;
      for (var p0 = 0; p0 < probe.length && !mark; p0++) {
        var c = probe[p0];
        if (!c || c._x0 === undefined) continue;
        var cx = (c._x0 + c._x1) / 2, cy = (c._y0 + c._y1) / 2;
        for (var m = 0; m < marks.length && !mark; m++) {
          for (var r0 = 0; r0 < (marks[m].rects || []).length; r0++) {
            var r = marks[m].rects[r0];
            if (cx >= r[0] && cx <= r[2] && cy >= r[1] && cy <= r[3]) {
              mark = marks[m];
              break;
            }
          }
        }
      }
      if (!mark) return range;
      var lo = range.lo, hi = range.hi;
      for (var i = 0; i < boxes.length; i++) {
        var b = boxes[i];
        if (!b || b._x0 === undefined) continue;
        var bx = (b._x0 + b._x1) / 2, by = (b._y0 + b._y1) / 2;
        for (var r1 = 0; r1 < (mark.rects || []).length; r1++) {
          var rr = mark.rects[r1];
          if (bx >= rr[0] && bx <= rr[2] && by >= rr[1] && by <= rr[3]) {
            var oi = (b._oi != null ? b._oi : i) | 0;
            if (oi < lo) lo = oi;
            if (oi > hi) hi = oi;
            break;
          }
        }
      }
      if (lo === range.lo && hi === range.hi) return range;
      var expanded = [];
      var ord = _byOi(boxes);
      for (var k = 0; k < ord.length; k++) {
        var oi2 = ord[k]._oi | 0;
        if (oi2 >= lo && oi2 <= hi) expanded.push(ord[k]);
      }
      return { lo: lo, hi: hi, boxes: expanded,
               how: (range.how || 'text') + '+vocab' };
    } catch (e) { return range; }
  }

  function _resolvePageBind(bind) {
    if (!bind || bind.kind !== 'page-chars') return { ok: false, why: 'not-page-chars' };
    var page = parseInt(bind.page, 10);
    if (!(page > 0)) return { ok: false, why: 'bad-page' };
    var pageCount = 0;
    try { pageCount = (typeof pdfDoc !== 'undefined' && pdfDoc) ? (parseInt(pdfDoc.numPages, 10) || 0) : 0; } catch (e) {}
    if (pageCount > 0 && page > pageCount) return { ok: false, why: 'bad-page', page: page };
    var pw = document.querySelector('.page-wrap[data-page-num="' + page + '"]');
    if (!pw || pw.dataset.loaded !== '1') return { ok: false, why: 'page-not-rendered', page: page };
    var boxes = pw.__charBoxes;
    if (!boxes || !boxes.length) return { ok: false, why: 'no-char-layer', page: page };
    // ⚠ from/to 可选：**不能**用 `parseInt(...) || 0` 兜底 —— 那会把"没给"
    //   变成 0，_resolveRange 就无从分辨"钉在开头"和"没说"。
    var _bFrom = parseInt(bind.from, 10);
    var _bTo = parseInt(bind.to, 10);
    var range = _resolveRange(boxes, {
      from: Number.isFinite(_bFrom) ? _bFrom : undefined,
      to: Number.isFinite(_bTo) ? _bTo : undefined,
      block: parseInt(bind.block, 10) || 0,
      text: bind.text || ''
    });
    if (!range) {
      return {
        ok: false, why: 'range-unresolved', page: page,
        detail: {
          chars: boxes.length,
          from: parseInt(bind.from, 10) || 0,
          to: parseInt(bind.to, 10) || 0,
          wanted: String(bind.text || '').slice(0, 40),
          gotAtIndex: _textAt(boxes, parseInt(bind.from, 10) || 0,
                              parseInt(bind.to, 10) || 0).slice(0, 40)
        }
      };
    }
    // 分词扩展（用户 2026-09-01：「真正按照分词支持跨行」）——
    // AI 给的 text 常在行尾截断（它读到的页面文本按行分块，跨行词只有
    // 前半，实锤 コチュジャ|ン）。解析结果命中生词分词标记（fugashi
    // 整词、rects 天然跨行）时扩展到整词；没有标记的词保持原匹配。
    if (range) range = _expandRangeToVocabMark(pw, boxes, range);
    var rects = _rangeRects(range);
    if (!rects) return { ok: false, why: 'no-rect', page: page };
    return { ok: true, page: page, pw: pw, boxes: boxes, range: range,
             rects: rects, last: rects[rects.length - 1] };
  }

  function _bindScreenPoint(g) {
    var ge = g.pw.__charLayer || g.pw;
    var gr = ge.getBoundingClientRect();
    var baseW = g.pw.__charsBaseW || ge.clientWidth || gr.width || 1;
    var screenK = (gr.width / baseW) || 1;
    return {
      x: gr.left + (g.last.x0 + g.last.x1) / 2 * screenK,
      y: gr.top + (g.last.y0 + g.last.y1) / 2 * screenK
    };
  }

  /// 只解析不建卡（2026-09-01 自动认领锚定用）：浮层卡的标题在页面
  /// 字符层能解析出区间时，把 bind 补进**现有**卡 —— persistBoundCard
  /// 是 create 流，不适用于已存在的卡。
  window.__pageBindResolveOnly = function (bind) {
    try {
      var g = _resolvePageBind(bind);
      if (!g || g.ok !== true || !g.range) {
        return { ok: false, why: (g && g.why) || 'unresolved' };
      }
      return { ok: true, page: g.page, from: g.range.lo, to: g.range.hi,
               // 实际解析到的文字：调用方(自愈)拿它与词逐字比对 ——
               // 不一致就拒绝写回,防止把一种坏换成另一种坏。
               text: _textAt(g.boxes, g.range.lo, g.range.hi) };
    } catch (e) {
      return { ok: false, why: 'exception' };
    }
  };

  /// AI page-chars 的唯一持久化入口。返回 Promise；只有便签仓完成 create 且
  /// 本地投影已 upsert 后才 resolve ok:true。rc-voicecall 不得先调 __pageBindCard
  /// 画临时 DOM 再宣称 bound。
  window.__pageBindPersist = function (bind, payload) {
    var g = null;
    try { g = _resolvePageBind(bind); } catch (e) {
      return Promise.resolve({ ok: false, why: 'exception', detail: { name: (e && e.name) || '' } });
    }
    // 同一本书的目标页尚未渲染，并不等于 bind 无效。不能拿当前页的屏幕点
    // 代替它，也不能等翻页后才首次写仓库；交给 stickynote 用目标页号生成
    // 确定性 PDF deferred anchor。其它失败（已有页但文字层/区间不成立）仍 fail closed。
    var deferred = !!(g && !g.ok && g.why === 'page-not-rendered');
    if (!g || (!g.ok && !deferred)) return Promise.resolve(g || { ok: false, why: 'unresolved' });
    if (!(window.RC && RC.stickynote && typeof RC.stickynote.persistBoundCard === 'function')) {
      return Promise.resolve({ ok: false, why: 'persistence-unavailable' });
    }
    payload = payload || {};
    var normalized = {
      uid: payload.uid || '', label: payload.label || '卡片',
      text: payload.text || '', raw: payload.raw || '', isHtml: !!payload.isHtml,
      icon: payload.icon || '', category: _bindCategory(payload), tone: _bindColor(payload)
    };
    var placement = deferred ? { deferredPdfPage: g.page } : _bindScreenPoint(g);
    // ⚠ block+text 形式的 bind 在 _resolvePageBind 里已经解析出了字符区间，
    //   但传下去的还是原始 bind —— persistBoundCard 只认数字 from/to，于是
    //   "解析明明成功了却被拒 bad-bind"。把算出的区间写回再传（拷贝，不改
    //   调用方对象）；identitySeed/去重 key 也因此拿到干净的数字区间。
    var bindOut = bind;
    if (g.ok && g.range) {
      bindOut = {};
      for (var bk in bind) {
        if (Object.prototype.hasOwnProperty.call(bind, bk)) bindOut[bk] = bind[bk];
      }
      bindOut.from = g.range.lo;
      bindOut.to = g.range.hi;
    }
    return Promise.resolve(RC.stickynote.persistBoundCard(bindOut, normalized, placement))
      .then(function (result) {
        if (!result || result.ok !== true) return result || { ok: false, why: 'persistence-failed' };
        return { ok: true, page: g.page,
                 from: g.ok ? g.range.lo : (parseInt(bind.from, 10) || 0),
                 to: g.ok ? g.range.hi : (parseInt(bind.to, 10) || 0),
                 noteId: result.noteId || '', persisted: true, deferred: deferred };
      }, function (error) {
        return { ok: false, why: 'persistence-failed',
                 detail: { name: (error && error.name) || '' } };
      });
  };

  window.__pageBindCard = function (bind, payload) {
    try {
      var geom = _resolvePageBind(bind);
      var uid = (payload && payload.uid) ? String(payload.uid).replace(/[^\w-]/g, '') : '';
      if (!geom.ok) {
        if (uid) _removeRailMark('u' + uid);
        return geom;
      }
      var page = geom.page, pw = geom.pw, boxes = geom.boxes;
      var range = geom.range, rects = geom.rects, last = geom.last;

      // 有持久化能力时，AI 直绑必须先走 __pageBindPersist。这里拒绝临时成功，
      // 防止旧调用方继续把当前 page-wrap 里的短命 DOM 当作已绑定。
      if (!(payload && typeof payload.onToggle === 'function') &&
          window.RC && RC.stickynote && typeof RC.stickynote.persistBoundCard === 'function') {
        return { ok: false, why: 'persistence-required' };
      }

      var layer = (typeof ensurePageLayer === 'function')
        ? ensurePageLayer(pw, 'pgbind-layer') : null;
      if (!layer) return { ok: false, why: 'no-layer' };
      pw.appendChild(layer);   // 排在 char-layer 之后，标记能接到点击

      // ── 标记的身份 ──────────────────────────────────────────
      //   ⚠ 曾经用「解析出来的字符区间」当 key（'b'+lo+'_'+hi）。它同时承担了
      //     两件不该混在一起的事：① 同一张卡重挂时替换自己的旧标记（对的）
      //     ② 同一个词上的第二张卡把第一张的标记也删掉（错的）。
      //     ②的后果不是报错 —— 第一张的宿主便签此前已被 _applyWordBind 设成
      //     display:none，标记又被撤掉，于是它**看不见也点不开**，而且永远
      //     恢复不了（它自己每次都 ok，走不到那条「失败才回到可见」的兜底）。
      //   身份来自调用方：便签传 note.id，AI 传卡片 cid。取不到才退回区间 key
      //   （老数据/没传 uid 的调用方，行为跟以前一样）。
      var key = uid ? ('u' + uid) : ('p' + page + 'b' + range.lo + '_' + range.hi);
      _removeMark(layer, key);

      // ── 标记本体：透明底分类色描边，不用盖字的填充或伪元素装饰 ──
      //   用户 2026-08-20 定：「实际的书中字符可不像你的例子那样有足够的空白
      //   位置，这样会盖到其它字符」——标记不能抢正文面积。填色即使走 multiply
      //   也仍然改了字的底色，而扫描书底色本来就不匀。
      //   框往外放 PAD，别贴着字形。
      var PAD = 2;
      var tone = _bindTone(payload);
      var lastMark = null;
      for (var r = 0; r < rects.length; r++) {
        var box = rects[r];
        var m = document.createElement('div');
        m.className = 'pgmark';
        m.dataset.bindkey = key;
        m.style.cssText =
          'left:' + (box.x0 - PAD) + 'px;top:' + (box.y0 - PAD) + 'px;' +
          'width:' + (box.x1 - box.x0 + PAD * 2) + 'px;' +
          'height:' + (box.y1 - box.y0 + PAD * 2) + 'px;' + tone;
        layer.appendChild(m);
        lastMark = m;
      }

      // ── 角标序号：骑在最后一段框的右上角外侧，落进行间空隙 ──────
      //   序号是它在**本页**的次序，不是全书连续 —— 位置，不是身份。
      //   人和 AI 用同一套，所以能说「把第 3 个删掉」。
      var n = document.createElement('span');
      n.className = 'pgmark-n';
      n.dataset.bindkey = key;
      n.dataset.bx = String(Math.round(last.x1));
      n.dataset.by = String(Math.round(last.y0));
      n.style.cssText =
        'left:' + (last.x1 + PAD + 1) + 'px;top:' + (last.y0 - PAD - 4) + 'px;' + tone;
      layer.appendChild(n);

      // 右侧 150px 透明浮标轨：覆盖正文，不参与 #main/page-container 布局。
      // 浮标的纵向位置只读词框实时 BCR，缩放/滚动/去边都自动落在同一视觉中心。
      var rail = _railFor(pw), dot = null;
      if (rail) {
        dot = document.createElement('button'); dot.type = 'button';
        dot.className = 'pgbind-rail-dot'; dot.dataset.bindkey = key;
        dot.style.cssText = tone; dot.__bwAnchor = lastMark || n;
        rail.appendChild(dot); _wakeRail(rail);
      }

      // 卡片本身按需展开；标记与角标都是它的把手
      var open = function (ev) {
        if (ev) ev.stopPropagation();
        // 宿主自己有卡（手动钉的便签就是）→ 把展开交回去，别在这儿另画一个。
        // 用户 2026-08-19：「打开的卡片应该是我们本来设计的卡片而不是你现在这个」。
        _clearBindActive();
        var source = ev && ev.currentTarget;
        var on = false;
        if (payload && typeof payload.onToggle === 'function') {
          on = !!payload.onToggle({ source: source, bindKey: key, category: _bindCategory(payload) });
        } else {
          on = _toggleBindCard(key, payload || {}, source);
        }
        _setBindActive(key, on);
      };
      var hs = layer.querySelectorAll('[data-bindkey="' + key + '"]');
      for (var h = 0; h < hs.length; h++) hs[h].addEventListener('click', open);
      if (dot) {
        dot.addEventListener('click', function (ev) { _openRailDot(rail, dot, open, ev); });
        dot.addEventListener('pointerenter', function () { _wakeRail(rail); });
      }

      _renumberMarks(pw);
      return { ok: true, page: page, from: range.lo, to: range.hi, key: key };
    } catch (e) {
      try { console.warn('[bind] __pageBindCard 失败', e); } catch (e2) {}
      return { ok: false, why: 'exception', detail: { name: (e && e.name) || '' } };
    }
  };

  /// 把 [from,to] 这段字取出来，用于回答"我在这个下标上看到的是什么"。
  /// 定位失败时最值钱的就是这一条 —— 有它才分得清「下标偏了」和「不是那页」。
  ///
  /// 跳过 sp（空白）的口径必须跟 _resolveRange 一致：口径不一样的话，
  /// 报出来的字跟它实际拿去比对的不是同一个东西，这条诊断会把人带偏。
  function _textAt(boxes, from, to) {
    var out = '';
    // ⚠ 按 _oi 取，不按数组位置 —— boxes 被按阅读顺序重排过（见 _byOi）。
    //   报错时用错坐标系，等于给出一段**看起来像但其实是别处**的文字，
    //   比不报还坏。
    var ord = _byOi(boxes);
    for (var i = 0; i < ord.length; i++) {
      var b = ord[i];
      if (!b || (b._oi | 0) < from || (b._oi | 0) > to) continue;
      if (b && !b.sp && b.c) out += b.c;
    }
    return out;
  }

  /// 撤掉某一处的标记。删卡走这里 —— 光删便签自己那个 DOM 是不够的，
  /// 描边和序号在另一个层里，不撤就永远留在页上，而且**后面所有序号都错位**
  /// （序号是位置不是身份，少一个就得整页重排）。
  window.__pageBindRemove = function (bind, uid) {
    try {
      if (!bind || bind.kind !== 'page-chars') return false;
      var page = parseInt(bind.page, 10);
      var uk = uid ? ('u' + String(uid).replace(/[^\w-]/g, '')) : '';
      var pw = document.querySelector('.page-wrap[data-page-num="' + page + '"]');
      if (!pw) { if (uk) _removeRailMark(uk); return false; }
      var layer = pw.querySelector('.pgbind-layer');
      if (!layer) { if (uk) _removeRailMark(uk); return false; }
      // 有身份就直接按身份撤 —— 撤的是**这张卡**的标记，不会误伤同一个词上
      // 的另一张。没有身份（老数据）才退回区间 key，行为跟以前一样。
      if (uk) {
        var had0 = !!layer.querySelector('[data-bindkey="' + uk + '"]');
        _removeMark(layer, uk);
        _renumberMarks(pw);
        return had0;
      }
      // key 是解析**之后**的区间，跟 bind.from/to 不一定相等（文字层换过时会
      // 重新定位）。所以按 from/to 反推 key 会漏 —— 用 _resolveRange 走一遍。
      var boxes = pw.__charBoxes;
      var key = null;
      if (boxes && boxes.length) {
        var rg = _resolveRange(boxes, {
          from: parseInt(bind.from, 10) || 0,
          to: parseInt(bind.to, 10) || 0,
          text: bind.text || ''
        });
        if (rg) key = 'p' + page + 'b' + rg.lo + '_' + rg.hi;
      }
      if (!key) key = 'p' + page + 'b' + (parseInt(bind.from, 10) || 0) + '_' + (parseInt(bind.to, 10) || 0);
      var had = !!layer.querySelector('[data-bindkey="' + key + '"]');
      _removeMark(layer, key);
      _renumberMarks(pw);
      return had;
    } catch (e) {
      try { console.warn('[bind] __pageBindRemove 失败', e); } catch (e2) {}
      return false;
    }
  };

  /// 绑不上的卡记下来，等那一页真的渲染出来再接回去。
  window.__pageBindDefer = function (bind, payload, card) {
    payload = payload || {};
    var uid = payload.uid ? String(payload.uid).replace(/[^\w-]/g, '') : '';
    var key = uid ? ('u' + uid) : [
      'p', parseInt(bind && bind.page, 10) || 0,
      parseInt(bind && bind.from, 10) || 0,
      parseInt(bind && bind.to, 10) || 0
    ].join(':');
    // renderInfo 有「已经建出浮层」和「没有浮层」两个登记出口。同一张 AI 卡
    // 只能占一个队列项；第二次登记只补 card 引用，不能再发一次 repository create。
    for (var i = 0; i < _pageBindPending.length; i++) {
      var old = _pageBindPending[i];
      if (old.pendingKey !== key) continue;
      if (!old.card && card) old.card = card;
      return old;
    }
    var item = { bind: bind, payload: payload, card: card, pendingKey: key,
                 inFlight: null, done: false, lastWhy: '' };
    _pageBindPending.push(item);
    return item;
  };

  function _finishPageBindPending(item, result) {
    item.inFlight = null;
    if (!result || result.ok !== true) {
      item.lastWhy = (result && result.why) || 'unknown';
      return false;
    }
    item.done = true;
    _pageBindPending = _pageBindPending.filter(function (pending) { return pending !== item; });
    // 权威便签已经 create+upsert（它自己的 _applyWordBind 也已画好标记）之后，
    // 才能关闭浮层副本；提前关会在仓库失败时把唯一可见内容一起丢掉。
    try {
      if (item.card && window.__vcCardClose) window.__vcCardClose(item.card);
    } catch (e) {}
    return true;
  }

  window.__pageBindRetry = function (pageNum) {
    if (!_pageBindPending.length) return;
    var rest = [];
    _pageBindPending.forEach(function (item) {
      if (item.done) return;
      if (parseInt(item.bind.page, 10) !== parseInt(pageNum, 10)) { rest.push(item); return; }
      if (item.inFlight) { rest.push(item); return; }

      if (item.payload && typeof item.payload.onToggle === 'function') {
        // 已经在 document-notes 里的手动/历史便签只需重画短命 DOM；它有宿主真卡，
        // 不应再 create 一份 HTML 便签。
        var res = null;
        try { res = window.__pageBindCard(item.bind, item.payload); } catch (e) {}
        if (!res || !res.ok) { item.lastWhy = (res && res.why) || 'unknown'; rest.push(item); return; }
        _finishPageBindPending(item, res);
        return;
      }

      // AI payload 没有持久宿主。启用 persistence-required 后再调 __pageBindCard
      // 只会稳定返回失败；必须重走唯一的 Promise 持久化入口。同一 item 的 inFlight
      // 是重入闸，persistBoundCard 内再按 uid+词区间做跨调用幂等。
      rest.push(item);
      if (typeof window.__pageBindPersist !== 'function') {
        item.lastWhy = 'persistence-unavailable';
        return;
      }
      try {
        item.inFlight = Promise.resolve(window.__pageBindPersist(item.bind, item.payload));
      } catch (e2) {
        item.lastWhy = 'persistence-failed';
        item.inFlight = null;
        return;
      }
      item.inFlight.then(function (result) {
        _finishPageBindPending(item, result);
      }, function () {
        item.inFlight = null;
        item.lastWhy = 'persistence-failed';
      });
    });
    // 异步成功回调会按对象身份再过滤一次；这里保留 inFlight 项供后续触发看见闸门。
    _pageBindPending = rest.filter(function (item) { return !item.done; });
  };
})();
