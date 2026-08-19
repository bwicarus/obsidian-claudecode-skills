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
// 跟高亮同一套：页内绝对定位，位置由 charBoxes 的 left/top/width/height 直接给出
// （它们已经是渲染后的 CSS 像素）。**不要**用 fixed + JS 跟滚 —— 那是便签系统早就
// 写下的禁令（references/sticky-notes-design.md），页面一缩放就散架。
//
// ## 标记的形态（2026-08-20 用户重新定，替换了初版）
//
// 初版是「落下时展开一张卡、非活跃计时、到点收成球留在锚点上」。用户否掉了：
// 「圆球过多会遮挡视野」，以及更要命的一条 ——
// 「实际的书中字符可不像你的例子那样有足够的空白位置，这样会盖到其它字符」。
//
// 所以标记**不占正文面积**，改成：给被锚的词**描边**（中间不填色，字像素完全
// 不被碰）+ 右上角一个**页内序号**；卡片按需点开，一次只开一张。
// 填色即便走 mix-blend-mode:multiply 也仍然改了字的底色，而扫描书底色本来就
// 不匀 —— 描边是唯一完全不动原文的做法。
//
// 序号是它在**本页**的次序，不是全书连续：位置，不是身份。人和 AI 用同一套，
// 所以能说「把第 3 个删掉」。加卡/删卡后整页重排（_renumberMarks 每次全量重算）。

(function () {
  'use strict';

  var _pageBindPending = [];   // 绑不上的先存着：最常见的失败是"那页还没渲染"

  function _stripWs(s) { return String(s || '').replace(/\s+/g, ''); }

  function _resolveRange(boxes, want) {
    var from = want.from | 0, to = Math.max(want.from | 0, want.to | 0);
    var text = _stripWs(want.text);
    if (from < boxes.length && to < boxes.length) {
      if (!text) return { from: from, to: to };
      var got = '';
      for (var k = from; k <= to && k < boxes.length; k++) {
        if (boxes[k] && !boxes[k].sp && boxes[k].c) got += boxes[k].c;
      }
      if (_stripWs(got) === text) return { from: from, to: to };   // 序号仍然作数
    }
    // 序号对不上了（换过文字层 / 越界 / 那段字变了）→ 按文本重新找，
    // 用原序号挑最近的一处 —— 这就是"同一个词重复出现"时的消歧依据。
    if (!text) return null;
    var joined = '';
    var index = [];   // joined 里每个字符对应的 box 下标
    for (var i = 0; i < boxes.length; i++) {
      var c = boxes[i] && boxes[i].c;
      if (!c || boxes[i].sp) continue;
      joined += c;
      index.push(i);
    }
    var best = -1, bestDist = Infinity, at = joined.indexOf(text);
    while (at >= 0) {
      var start = index[at];
      var dist = Math.abs(start - from);
      if (dist < bestDist) { bestDist = dist; best = at; }
      at = joined.indexOf(text, at + 1);
    }
    if (best < 0) return null;
    var lo = index[best];
    var hi = index[Math.min(best + text.length - 1, index.length - 1)];
    return { from: lo, to: hi };
  }

  function _rangeRects(boxes, range) {
    var lines = [], cur = null;
    for (var i = range.from; i <= range.to && i < boxes.length; i++) {
      var b = boxes[i];
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

  /// 取这张卡的色调，推出边框/角标两个自定义属性。
  ///
  /// ⚠ 不能直接把色调原色当边框：实测在纸上只有 1.6~2.5:1，够不到 WCAG 对
  ///   图形元素的 3:1（1.5px 细边尤其吃亏）。掺 40% 深底压到 3.4~4.9:1。
  ///   角标同理 —— 卡片系统那套「浅色调字压半透明深填充」是给**深色卡面上的
  ///   SVG 图标**调的，图标靠形状识别 3:1 就够；搬到白纸上的数字实测只有
  ///   1.0~1.4:1（某些色几乎隐形）。
  function _bindTone(payload) {
    var tc = (payload && payload.tone) || '#b9a8ff';
    return '--pm-b:color-mix(in srgb,' + tc + ' 60%,#2a2440);' +
           '--pm-i:color-mix(in srgb,' + tc + ' 22%,#14101f);' +
           '--pm-h:color-mix(in srgb,' + tc + ' 30%,transparent);';
  }

  function _removeMark(layer, key) {
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
    ns.sort(function (a, b) {
      var ta = parseFloat(a.style.top) || 0, tb = parseFloat(b.style.top) || 0;
      if (Math.abs(ta - tb) > 6) return ta - tb;
      return (parseFloat(a.style.left) || 0) - (parseFloat(b.style.left) || 0);
    });
    for (var i = 0; i < ns.length; i++) {
      ns[i].textContent = String(i + 1);
      ns[i].title = '本页第 ' + (i + 1) + ' 张卡片';
    }
  }

  /// 点标记 → 展开卡片；再点一次收起。卡片仍是既有的 .pgbind-card 外观。
  function _toggleBindCard(layer, key, payload, near) {
    var exist = layer.querySelector('.pgbind-card[data-bindkey="' + key + '"]');
    if (exist) {
      try { exist.__bwBindTeardown && exist.__bwBindTeardown(); } catch (e) {}
      exist.remove();
      return;
    }
    // 一次只开一张，否则满页都是卡就回到了「圆球遮挡视野」那个老问题
    var others = layer.querySelectorAll('.pgbind-card');
    for (var i = 0; i < others.length; i++) {
      try { others[i].__bwBindTeardown && others[i].__bwBindTeardown(); } catch (e) {}
      others[i].remove();
    }
    var el = document.createElement('div');
    el.className = 'pgbind-card';
    el.dataset.bindkey = key;
    var w = Math.max(near.x1 - near.x0, 180);
    el.style.cssText =
      'position:absolute;left:' + near.x0 + 'px;top:' + (near.y1 + 6) + 'px;' +
      'width:' + w + 'px;z-index:7';
    el.innerHTML = '<div class="pgbind-hd">' +
      (window.RC && RC.esc ? RC.esc(payload.label || '卡片') : (payload.label || '卡片')) +
      '</div><div class="pgbind-bd">' +
      (payload.isHtml ? (payload.raw || '') :
        (window.RC && RC.esc ? RC.esc(payload.text || '') : (payload.text || ''))) +
      '</div>';
    el.addEventListener('click', function (ev) { ev.stopPropagation(); });
    layer.appendChild(el);
  }

  window.__pageBindCard = function (bind, payload) {
    try {
      if (!bind || bind.kind !== 'page-chars') return { ok: false, why: 'not-page-chars' };
      var page = parseInt(bind.page, 10);
      if (!(page > 0)) return { ok: false, why: 'bad-page' };
      var pw = document.querySelector('.page-wrap[data-page-num="' + page + '"]');
      if (!pw || pw.dataset.loaded !== '1') return { ok: false, why: 'page-not-rendered' };
      var boxes = pw.__charBoxes;
      if (!boxes || !boxes.length) return { ok: false, why: 'no-char-layer' };
      var range = _resolveRange(boxes, {
        from: parseInt(bind.from, 10) || 0,
        to: parseInt(bind.to, 10) || 0,
        text: bind.text || ''
      });
      // 最常见的一种：文字层换过一份（重新预处理 / 换 OCR 结果），助手拿到的
      // segments 下标跟当前这份对不上，按下标取出来的字跟 bind.text 不一致。
      // 把"想要什么"和"这个下标上实际是什么"一起报出去 —— 只说"失败"的话，
      // 分不清是下标偏了还是压根不是那一页。
      if (!range) {
        return {
          ok: false,
          why: 'range-unresolved',
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
      var rects = _rangeRects(boxes, range);
      if (!rects) return { ok: false, why: 'no-rect' };

      var layer = (typeof ensurePageLayer === 'function')
        ? ensurePageLayer(pw, 'pgbind-layer') : null;
      if (!layer) return { ok: false, why: 'no-layer' };
      pw.appendChild(layer);   // 排在 char-layer 之后，标记能接到点击

      // 同一处已经有卡就替换，别叠罗汉
      var key = 'b' + range.from + '_' + range.to;
      _removeMark(layer, key);

      // ── 标记本体：给被锚的词描边，**不填色** ──────────────────
      //   用户 2026-08-20 定：「实际的书中字符可不像你的例子那样有足够的空白
      //   位置，这样会盖到其它字符」——标记不能抢正文面积。填色即使走 multiply
      //   也仍然改了字的底色，而扫描书底色本来就不匀；描边完全不碰字像素。
      //   框往外放 PAD，别贴着字形。
      var PAD = 1.5;
      var tone = _bindTone(payload);
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
      }

      // ── 角标序号：骑在最后一段框的右上角外侧，落进行间空隙 ──────
      //   序号是它在**本页**的次序，不是全书连续 —— 位置，不是身份。
      //   人和 AI 用同一套，所以能说「把第 3 个删掉」。
      var last = rects[rects.length - 1];
      var n = document.createElement('span');
      n.className = 'pgmark-n';
      n.dataset.bindkey = key;
      n.style.cssText =
        'left:' + (last.x1 + PAD + 1) + 'px;top:' + (last.y0 - PAD - 4) + 'px;' + tone;
      layer.appendChild(n);

      // 卡片本身按需展开；标记与角标都是它的把手
      var open = function (ev) {
        if (ev) ev.stopPropagation();
        _toggleBindCard(layer, key, payload, last);
      };
      var hs = layer.querySelectorAll('[data-bindkey="' + key + '"]');
      for (var h = 0; h < hs.length; h++) hs[h].addEventListener('click', open);

      _renumberMarks(pw);
      return { ok: true, page: page, from: range.from, to: range.to };
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
    for (var i = from; i <= to && i < boxes.length; i++) {
      if (i < 0) continue;
      var b = boxes[i];
      if (b && !b.sp && b.c) out += b.c;
    }
    return out;
  }

  /// 绑不上的卡记下来，等那一页真的渲染出来再接回去。
  window.__pageBindDefer = function (bind, payload, card) {
    _pageBindPending.push({ bind: bind, payload: payload, card: card });
  };

  window.__pageBindRetry = function (pageNum) {
    if (!_pageBindPending.length) return;
    var rest = [];
    _pageBindPending.forEach(function (item) {
      if (parseInt(item.bind.page, 10) !== parseInt(pageNum, 10)) { rest.push(item); return; }
      // __pageBindCard 现在返回 {ok, why}，不是裸布尔 —— 非空对象恒为真，
      // 直接当条件用会把每次失败都判成成功。
      var res = null;
      try { res = window.__pageBindCard(item.bind, item.payload); } catch (e) {}
      if (!res || !res.ok) { rest.push(item); return; }
      // 补上了就把浮层那份关掉，否则同一内容两处并存。
      try {
        if (item.card && window.__vcCardClose) window.__vcCardClose(item.card);
      } catch (e) {}
    });
    _pageBindPending = rest;
  };
})();
