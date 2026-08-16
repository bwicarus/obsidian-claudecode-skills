// 拖选期间要临时禁点的呼吸高亮 .hl（查词/词组/解释）——防拖选经过它们被截获(丢 move/up + 误弹)
const _OVL_HL_SEL = '.word-hl-layer .hl, .phrase-hl-layer .hl, .explain-hl-layer .hl';
// 根治点击归属:char-layer 收到每次点击后,用**几何**命中(getBoundingClientRect,与 .hl 的 pointer-events 状态无关)
// 判断是否落在查询高亮(词组/查词/解释)上。命中 → 交给该高亮动作(不选字/不查词);否则正常选字。
// 这是"状态无关的裁决",不依赖 pointer-events 抢占 → 根除"穿透到 char-layer 误查手指下的字"整类 bug。
function _overlayHlHitAtClient(pw, cx, cy) {
  const hls = document.querySelectorAll(_OVL_HL_SEL);   // 全文档搜(不限本页 pw):双页模式下词组高亮可能在别的 pw;getBoundingClientRect 包含判断只会命中点击点上的那个,不误命中别页
  for (let i = hls.length - 1; i >= 0; i--) {   // 逆序:后插入(DOM 靠后)= z:6 平级里视觉在上,先命中它
    const r = hls[i].getBoundingClientRect();
    if (r.width && r.height && cx >= r.left && cx <= r.right && cy >= r.top && cy <= r.bottom) {
      const layer = hls[i].closest('.phrase-hl-layer, .word-hl-layer, .explain-hl-layer');
      const kind = !layer ? '' : (layer.classList.contains('phrase-hl-layer') ? 'phrase'
        : layer.classList.contains('explain-hl-layer') ? 'explain' : 'word');
      return { kind, layer, el: hls[i] };
    }
  }
  return null;
}

// ⚡ charBoxes 的像素坐标是 loadCharsAndBindLayer 当时的 scale 烘焙的;refit/双页切换等竞态后
// 可能与当前显示尺寸脱节(实测 応用情報 p37 偏 19% → 页面右缘/底部整片点不中,而振假名/下划线
// 反而是准的——它们渲染时实时用 clientWidth/pageWPt 算比例)。交互入口处按**实时尺寸**重标定:
// boxes 自带 pt 坐标(_x0/_y0/_x1/_y1),O(N) 重算 left/top/width/height,比例没变就跳过。
function _syncCharBoxScale(pw) {
  const cb = pw && pw.__charBoxes;
  if (!cb || !cb.length || !pw.__pageWPt || !pw.__pageHPt) return;
  const ref = pw.__charLayer || pw.querySelector('.char-layer') || pw;
  const w = ref.clientWidth, h = ref.clientHeight;
  if (!w || !h) return;
  const sx = w / pw.__pageWPt, sy = h / pw.__pageHPt;
  if (Math.abs((pw.__cbSX || 0) - sx) < 0.001 && Math.abs((pw.__cbSY || 0) - sy) < 0.001) return;
  if (cb[0]._x0 == null) return;   // 旧结构无 pt 字段 → 放弃(下次重载自然修复)
  for (const c of cb) {
    if (c._x0 == null) continue;
    c.left = c._x0 * sx; c.width  = (c._x1 - c._x0) * sx;
    c.top  = c._y0 * sy; c.height = (c._y1 - c._y0) * sy;
  }
  pw.__cbSX = sx; pw.__cbSY = sy;
}
function _findCharAt(charBoxes, x, y) {
  // 先尝试落在某 char bbox 内（优先 non-space）
  for (let i = 0; i < charBoxes.length; i++) {
    const c = charBoxes[i];
    if (c.sp) continue;
    if (x >= c.left && x <= c.left + c.width && y >= c.top && y <= c.top + c.height) {
      return i;
    }
  }
  let best = -1, bestD = Infinity;
  // 同行内 X 距离最近的 non-space
  for (let i = 0; i < charBoxes.length; i++) {
    const c = charBoxes[i];
    if (c.sp) continue;
    const yIn = (y >= c.top - 2 && y <= c.top + c.height + 2);
    if (yIn) {
      const dx = (x < c.left) ? (c.left - x) : (x > c.left + c.width) ? (x - c.left - c.width) : 0;
      if (dx < bestD) { bestD = dx; best = i; }
    }
  }
  if (best >= 0) return best;
  // 整体 Manhattan 距离兜底
  for (let i = 0; i < charBoxes.length; i++) {
    const c = charBoxes[i];
    if (c.sp) continue;
    const cx = c.left + c.width / 2, cy = c.top + c.height / 2;
    const d = Math.abs(x - cx) + Math.abs(y - cy) * 3;
    if (d < bestD) { bestD = d; best = i; }
  }
  return best;
}

function _charBlockId(c) {
  return (c && c.bk != null && c.bk >= 0)
    ? c.bk
    : ((!c || c.w == null || c.w < 0) ? -1 : Math.floor(c.w / 1000000));
}

function _charBlockGeometry(chars, sIdx, eIdx) {
  const relevant = new Set();
  for (let i = sIdx; i <= eIdx; i++) {
    const id = _charBlockId(chars[i]);
    if (id >= 0) relevant.add(id);
  }
  const blocks = new Map();
  for (let i = 0; i < chars.length; i++) {
    const c = chars[i], id = _charBlockId(c);
    if (id < 0 || !relevant.has(id) || !c) continue;
    const left = Number(c.left), top = Number(c.top);
    const width = Number(c.width), height = Number(c.height);
    if (![left, top, width, height].every(Number.isFinite) || width < 0 || height < 0) continue;
    let block = blocks.get(id);
    if (!block) {
      block = {
        id,
        left,
        top,
        right: left + width,
        bottom: top + height,
        charWidths: [],
        charHeights: [],
      };
      blocks.set(id, block);
    } else {
      block.left = Math.min(block.left, left);
      block.top = Math.min(block.top, top);
      block.right = Math.max(block.right, left + width);
      block.bottom = Math.max(block.bottom, top + height);
    }
    if (!c.sp && width > 0) block.charWidths.push(width);
    if (!c.sp && height > 0) block.charHeights.push(height);
  }
  const median = (values, fallback) => {
    if (!values.length) return fallback;
    values.sort((a, b) => a - b);
    return values[Math.floor(values.length / 2)];
  };
  for (const block of blocks.values()) {
    block.width = Math.max(0, block.right - block.left);
    block.height = Math.max(0, block.bottom - block.top);
    block.charWidth = Math.max(1, median(block.charWidths, block.width || 1));
    block.charHeight = Math.max(1, median(block.charHeights, block.height || 1));
    block.axis = block.width > block.height * 1.15
      ? 'horizontal'
      : (block.height > block.width * 1.15 ? 'vertical' : 'ambiguous');
  }
  return blocks;
}

function _charBlockGap(a0, a1, b0, b1) {
  return Math.max(0, Math.max(a0, b0) - Math.min(a1, b1));
}

function _charBlockOverlapRatio(a0, a1, b0, b1) {
  const overlap = Math.max(0, Math.min(a1, b1) - Math.max(a0, b0));
  const span = Math.max(1, Math.min(a1 - a0, b1 - b0));
  return overlap / span;
}

// Apple Vision 常给每一视觉行独立 bk。横排相邻行须 X 投影重叠且 Y 间距小；
// 竖排相邻列须 Y 投影重叠且 X 间距小。方向明确相反时绝不靠近，避免同一
// 视觉行上的多个气泡被横向串起来。
function _charBlocksConnected(a, b) {
  const xOverlap = _charBlockOverlapRatio(a.left, a.right, b.left, b.right);
  const yOverlap = _charBlockOverlapRatio(a.top, a.bottom, b.top, b.bottom);
  const yGap = _charBlockGap(a.top, a.bottom, b.top, b.bottom);
  const xGap = _charBlockGap(a.left, a.right, b.left, b.right);
  const horizontal = a.axis !== 'vertical' && b.axis !== 'vertical'
    && xOverlap >= 0.3
    && yGap <= Math.max(a.charHeight, b.charHeight) * 1.8;
  const vertical = a.axis !== 'horizontal' && b.axis !== 'horizontal'
    && yOverlap >= 0.3
    && xGap <= Math.max(a.charWidth, b.charWidth) * 1.8;
  return horizontal || vertical;
}

function _charConnectedBlockPath(blocks, startId, endId) {
  const nodes = Array.from(blocks.values());
  const parent = new Map([[startId, null]]);
  const queue = [startId];
  for (let cursor = 0; cursor < queue.length; cursor++) {
    const id = queue[cursor];
    if (id === endId) break;
    const current = blocks.get(id);
    if (!current) continue;
    for (const candidate of nodes) {
      if (parent.has(candidate.id) || !_charBlocksConnected(current, candidate)) continue;
      parent.set(candidate.id, id);
      queue.push(candidate.id);
    }
  }
  if (!parent.has(endId)) return null;
  const path = new Set();
  for (let id = endId; id != null; id = parent.get(id)) path.add(id);
  return path;
}

// 块号是不透明身份，不能用 min/max 表达区间。同块严格只取该块；不同块时
// 只取端点之间的几何连通路径。若端点不连通，宁可只保留两个端点块，也不把
// 视觉上夹在中间、语义上属于别的气泡/栏的块吞进来。
// 区间内应当被选中的块集合。
//
// 与 _charConnectedBlockPath 的差别是「路径」vs「区间」:那个只保留 BFS 走过
// 的一条链,这个保留区间内所有与端点连成一片的块。同一段落里每行单独成块的
// PDF(实测:料理师)靠后者才能整段选中,而旁边气泡因为与本段不连通仍被排除。
function _charSpanBlocks(blocks, startId, endId) {
  const nodes = Array.from(blocks.values());
  const allowed = new Set([startId, endId]);
  // 反复扩张到不动点:A 连 B、B 连 C 时,C 也属于同一片(多行段落就是这样
  // 一行接一行连起来的),只做一轮会漏掉隔行的块。
  let grew = true;
  while (grew) {
    grew = false;
    for (const candidate of nodes) {
      if (allowed.has(candidate.id)) continue;
      for (const id of allowed) {
        const current = blocks.get(id);
        if (current && _charBlocksConnected(current, candidate)) {
          allowed.add(candidate.id);
          grew = true;
          break;
        }
      }
    }
  }
  return allowed;
}

function _charRangeBlockFilter(chars, sIdx, eIdx) {
  if (chars[sIdx] && chars[eIdx]
      && chars[sIdx]._selectionBlockFilter === false
      && chars[eIdx]._selectionBlockFilter === false) return () => true;
  const sb = _charBlockId(chars[sIdx]), eb = _charBlockId(chars[eIdx]);
  if (sb < 0 || eb < 0) return () => true;
  if (sb === eb) return (c) => _charBlockId(c) === sb || (_charBlockId(c) < 0 && !!c.sp);
  const blocks = _charBlockGeometry(chars, sIdx, eIdx);
  // 用户拖出的是一个**连续的索引区间**,区间内的块本就都该在选中范围里。
  // 之前这里取的是 BFS 的一条「路径」,而路径 ≠ 区间:BFS 一找到通路就停,
  // 路径之外的块整块被丢掉。
  //
  // 2026-08-16 实测(料理师 part1 第 26 页):这本书**每一行单独成块**
  // (blk 17/18/19/20…),用户选中 5 行的一段,画出来只剩零散几截,
  // 「已选」预览也因为中间的字被过滤掉而串成「食心配中が毒ありの」。
  //
  // 连通判定仍然有用 —— 它挡的是「视觉上夹在中间、其实属于另一个气泡/栏」
  // 的块。所以保留它,但只用来**排除**:凡是与区间内其它块连通的都留下,
  // 只有孤立的才剔除。这样同段多行照常全选,旁边的气泡仍然进不来。
  const allowed = _charSpanBlocks(blocks, sb, eb);
  return (c) => allowed.has(_charBlockId(c)) || (_charBlockId(c) < 0 && !!c.sp);
}

// chars[s..e] 拼成文本（含 X gap 智能空格 + 跨行换行；跟 _selByCharRange 同逻辑）
function _charsRangeToText(chars, sIdx, eIdx) {
  if (sIdx < 0 || eIdx >= chars.length || sIdx > eIdx) return '';
  const _inBlk = _charRangeBlockFilter(chars, sIdx, eIdx);
  let text = '', lastChar = null;
  const _cjk = s => /[぀-ヿ㐀-鿿　-〿＀-￯]/.test(s || '');
  for (let i = sIdx; i <= eIdx; i++) {
    const c = chars[i];
    if (!_inBlk(c)) continue;   // 别块(另一栏/题号)不计入
    if (lastChar) {
      // 两边都是 CJK(日/中,无词间空格)→ 跨行/间隙都直接拼,不插换行或空格(否则 公表する 跨行被拆成「公 表する」无法识别)
      const cjkPair = _cjk(c.c) && _cjk(lastChar.c);
      const dy = Math.abs(c.top - lastChar.top);
      if (dy > c.height * 0.5) { if (!cjkPair) text += '\n'; }
      else {
        const gap = c.left - (lastChar.left + lastChar.width);
        const ref = Math.min(c.height, lastChar.height);
        if (!cjkPair && gap > ref * ((/[A-Za-z]/.test(c.c) && /[A-Za-z]/.test(lastChar.c)) ? 1.3 : 0.6) && !lastChar.sp && !c.sp) text += ' ';   // 0.6 防 justified 词内字距拉伸误拆(如 between→be tween)
      }
    }
    text += c.sp ? ' ' : c.c;
    lastChar = c;
  }
  return text.replace(/[ \t]+/g, ' ').replace(/ ?\n ?/g, '\n').trim();
}

// 找选中范围所在的句子（左/右扩到 . ! ? 。！？ 之后 / 段落起止）
function _expandSentenceFromRange(chars, sIdx, eIdx) {
  // 句子边界 = 句末标点(。！？.!?)优先。
  // ⚠ rawdict 块(bk)只在「跨块 **且** 跨视觉行」时才断:
  //   - 日语 justified 排版会把同一视觉行拆成多个块(如同行的「…解き,」「それら…」),
  //     这种同行拆块绝不能断,否则句子在逗号处截断、到不了句号(用户报的 bug)
  //   - 标题/邻段在不同视觉行+不同块 → 断(不把标题并进句子)
  const isSentEnd = (c) => /[.!?。！？]/.test(c);
  const _bk = (a, b) => a && b && a.bk != null && b.bk != null && a.bk >= 0 && b.bk >= 0 && a.bk !== b.bk;
  const _lineChanged = (a, b) => Math.abs(a.top - b.top) > Math.max(a.height, b.height) * 0.5;
  const _paraGap = (a, b) => Math.abs(a.top - b.top) > Math.max(a.height, b.height) * 1.5;
  const _stop = (a, b) => (_bk(a, b) && _lineChanged(a, b)) || _paraGap(a, b);
  let s = sIdx;
  while (s > 0) {
    if (isSentEnd(chars[s - 1].c)) break;
    if (_stop(chars[s - 1], chars[s])) break;
    s--;
  }
  let e = eIdx;
  while (e < chars.length - 1) {
    if (isSentEnd(chars[e].c)) break;
    if (_stop(chars[e], chars[e + 1])) break;
    e++;
  }
  return {start: s, end: e};
}

function _expandToWordStart(chars, idx) {
  if (idx < 0 || idx >= chars.length) return idx;
  const isWord = (c) => /[A-Za-z0-9_]/.test(c);
  const isCJK = (c) => /[　-〿぀-ゟ゠-ヿ㐀-䶿一-鿿＀-￯]/.test(c);   // 汉字+平/片假名+全角+CJK标点
  // 优先用 PyMuPDF 词边界：同一个词 id 直接扩到词首（根治连字/紧排/装饰编号粘连）
  if (chars[idx].w !== -1) {
    const wid = chars[idx].w;
    while (idx > 0 && chars[idx - 1].w === wid && !'/|／'.includes(chars[idx - 1].c)) idx--;
    // 跳过词首标点（PyMuPDF 把 “conditional 的弯引号并入同 word）
    while (idx < chars.length - 1 && chars[idx + 1].w === wid && !isWord(chars[idx].c) && !isCJK(chars[idx].c)) idx++;
    return idx;
  }
  if (!isWord(chars[idx].c)) return idx;
  while (idx > 0 && isWord(chars[idx - 1].c) &&
         Math.abs(chars[idx - 1].top - chars[idx].top) <= chars[idx].height * 0.5 &&
         (chars[idx].left - (chars[idx - 1].left + chars[idx - 1].width)) <= chars[idx].height * 0.8) {
    idx--;   // 词信息缺失兜底：大水平间隙(>0.8字高)=跨排版块，不并入同一词
  }
  return idx;
}
function _expandToWordEnd(chars, idx) {
  if (idx < 0 || idx >= chars.length) return idx;
  const isWord = (c) => /[A-Za-z0-9_]/.test(c);
  const isCJK = (c) => /[　-〿぀-ゟ゠-ヿ㐀-䶿一-鿿＀-￯]/.test(c);   // 汉字+平/片假名+全角+CJK标点
  if (chars[idx].w !== -1) {
    const wid = chars[idx].w;
    while (idx < chars.length - 1 && chars[idx + 1].w === wid && !'/|／'.includes(chars[idx + 1].c)) idx++;
    // 跳过词尾标点（more. 的句号、conditional” 的弯引号）
    while (idx > 0 && chars[idx - 1].w === wid && !isWord(chars[idx].c) && !isCJK(chars[idx].c)) idx--;
    return idx;
  }
  if (!isWord(chars[idx].c)) return idx;
  while (idx < chars.length - 1 && isWord(chars[idx + 1].c) &&
         Math.abs(chars[idx + 1].top - chars[idx].top) <= chars[idx].height * 0.5 &&
         (chars[idx + 1].left - (chars[idx].left + chars[idx].width)) <= chars[idx].height * 0.8) {
    idx++;
  }
  return idx;
}

function _selByCharRange(pw, sIdx, eIdx) {
  if (!pw || !pw.__charBoxes) return;
  if (sIdx > eIdx) { const t = sIdx; sIdx = eIdx; eIdx = t; }
  const chars = pw.__charBoxes;
  if (sIdx < 0 || eIdx >= chars.length) return;
  // 拖选两端自动对齐词边界（英文 \w 词；CJK 字符不动 - isWord 不匹配自动跳过）
  sIdx = _expandToWordStart(chars, sIdx);
  eIdx = _expandToWordEnd(chars, eIdx);
  // 同块严格限在该 bk；跨块只接受两端之间的几何连通路径。
  // bk 缺失才回退 w//1e6，无任何块信息时保持旧行为。
  const _inBlk = _charRangeBlockFilter(chars, sIdx, eIdx);
  // 拼出选中文本：跨行加 \n；同行按物理 X gap 智能补空格（应对 PDF 数轴等
  // TJ 间隔但无空格 char 的情况，如 '0 1 2 3 4' 在 PyMuPDF rawdict 里没空格 char）
  let text = '';
  let lastChar = null;
  const _cjk = s => /[぀-ヿ㐀-鿿　-〿＀-￯]/.test(s || '');
  for (let i = sIdx; i <= eIdx; i++) {
    const c = chars[i];
    if (!_inBlk(c)) continue;   // 跨块过滤：别块(题号/另一栏)字符不计入选中文本
    if (lastChar) {
      // 两边都是 CJK(日/中,无词间空格)→ 跨行/间隙都直接拼,不插换行或空格(公表する 跨行不再被拆「公 表する」)
      const cjkPair = _cjk(c.c) && _cjk(lastChar.c);
      const dy = Math.abs(c.top - lastChar.top);
      if (dy > c.height * 0.5) {
        if (!cjkPair) text += '\n';
      } else {
        // 同行：按 X gap 判断要不要加空格
        const gap = c.left - (lastChar.left + lastChar.width);
        const ref = Math.min(c.height, lastChar.height);
        if (!cjkPair && gap > ref * ((/[A-Za-z]/.test(c.c) && /[A-Za-z]/.test(lastChar.c)) ? 1.3 : 0.6) && !lastChar.sp && !c.sp) text += ' ';   // 0.6 防 justified 词内字距拉伸误拆(如 between→be tween)
      }
    }
    // 真实空格 char 直接保留；其它 char 写入
    text += c.sp ? ' ' : c.c;
    lastChar = c;
  }
  // 压缩多余空格 / trim
  text = text.replace(/[ \t]+/g, ' ').replace(/ ?\n ?/g, '\n').trim();
  lastSelText = text;
  _updateSelPreview(lastSelText);
  if (typeof _updateGrammarBtnVisibility === 'function') _updateGrammarBtnVisibility();
  // 给侧栏助手记下选中「所在句」(左右扩到句末标点/段落边界):助手不必每次都 read_page 才有上下文,
  // 直接拿这句判读音/义项(日语同字多音、含义随语境)。整句已被选中就不另存(避免与 selection 重复)。
  try {
    const _sr = _expandSentenceFromRange(chars, sIdx, eIdx);
    if (_sr) {
      const _sent = _charsRangeToText(chars, _sr.start, _sr.end).slice(0, 600);
      const _norm = s => (s || '').replace(/\s+/g, '');
      window.__lastSelSentence = (_sent && _norm(_sent) !== _norm(lastSelText)) ? _sent : '';
    }
  } catch (_) {}
  _charSel = {pw, startIdx: sIdx, endIdx: eIdx, dragging: _charSel?.dragging || false};
  try { window.__lastSelMeta = { page: (typeof _selPageNum === 'function' ? _selPageNum() : (typeof currentPage !== 'undefined' ? currentPage : 0)), t: Date.now() }; } catch (_) {}   // char 层选中也记 meta(页+时间);否则 __voiceContext 新鲜度校验(meta.page===curP && <10min)失败 → 助手拿到空选中
  // 高亮：合并同行 chars 成连续矩形（空格按行高估算占位，让单词间高亮连贯）
  const ov = pw.querySelector('.sel-overlay');
  if (ov) {
    ov.innerHTML = '';
    let cur = null;
    for (let i = sIdx; i <= eIdx; i++) {
      const c = chars[i];
      if (!_inBlk(c)) continue;   // 跨块过滤：别块字符不画选中高亮
      // 空格如果 bbox 缺失，用前一字符的位置 + 估算 width
      let cleft = c.left, ctop = c.top, cw = c.width, ch = c.height;
      if (c.sp && (!cw || cw < 0.5) && i > sIdx) {
        const prev = chars[i - 1];
        cleft = prev.left + prev.width;
        ctop = prev.top;
        ch = prev.height;
        cw = ch * 0.3;   // 估算空格宽度
      }
      if (cur && Math.abs(ctop - cur.top) <= ch * 0.4 && cleft <= cur.left + cur.width + ch * 0.5) {
        cur.width = Math.max(cur.left + cur.width, cleft + cw) - cur.left;
        cur.height = Math.max(cur.height, ch);
      } else {
        if (cur) {
          const div = document.createElement('div');
          div.className = 'hl';
          div.style.left = cur.left + 'px';
          div.style.top = cur.top + 'px';
          div.style.width = cur.width + 'px';
          div.style.height = cur.height + 'px';
          ov.appendChild(div);
        }
        cur = {left: cleft, top: ctop, width: cw, height: ch};
      }
    }
    if (cur) {
      const div = document.createElement('div');
      div.className = 'hl';
      div.style.left = cur.left + 'px';
      div.style.top = cur.top + 'px';
      div.style.width = cur.width + 'px';
      div.style.height = cur.height + 'px';
      ov.appendChild(div);
    }
  }
  // 工具栏位置：选区底部
  const pwRect = pw.getBoundingClientRect();
  const mainEl = document.getElementById('main');
  const mainRect = mainEl.getBoundingClientRect();
  const endChar = chars[eIdx];
  toolbar.style.left = Math.max(8, pwRect.left - mainRect.left + mainEl.scrollLeft + chars[sIdx].left) + 'px';
  toolbar.style.top  = (pwRect.top - mainRect.top + mainEl.scrollTop + endChar.top + endChar.height + 6) + 'px';
  toolbar.classList.add('open');
  // 防溢出屏：选区靠右/靠下时工具栏(max-width 480)会跑出可见区被裁 → 夹回 #main 可见区
  _clampToolbarIntoView(mainEl, pwRect.top - mainRect.top + mainEl.scrollTop + chars[sIdx].top);
}
// 把选择工具栏夹进 #main 可见区(absolute 定位在可滚动的 #main 内;可见区=滚动偏移+clientW/H)。
// selTopY=选区顶部内容 Y;底部放不下就翻到选区上方。
function _clampToolbarIntoView(mainEl, selTopY) {
  const tb = toolbar;
  const tbW = tb.offsetWidth, tbH = tb.offsetHeight;   // 读 offset 触发 reflow，open 后尺寸已确定
  if (!tbW || !tbH) return;
  const visL = mainEl.scrollLeft, visT = mainEl.scrollTop;
  const visR = visL + mainEl.clientWidth, visB = visT + mainEl.clientHeight;
  let left = parseFloat(tb.style.left) || 0;
  let top  = parseFloat(tb.style.top) || 0;
  if (left + tbW > visR - 8) left = visR - 8 - tbW;   // 右溢 → 左移
  if (left < visL + 8) left = visL + 8;               // 仍左溢 → 贴左
  if (top + tbH > visB - 8) {                         // 底溢 → 翻到选区上方
    const above = (selTopY != null ? selTopY : top) - tbH - 6;
    top = (above >= visT + 8) ? above : Math.max(visT + 8, visB - 8 - tbH);
  }
  tb.style.left = left + 'px';
  tb.style.top  = top + 'px';
}

// 按 char 扩展到词边界（英文 \w / CJK 逐字）。空格视作非词字符
function _wordExpandFromChar(chars, idx) {
  if (idx < 0 || idx >= chars.length) return null;
  const isWord = (c) => /[A-Za-z0-9_]/.test(c);
  const isCJK  = (c) => /[　-〿぀-ゟ゠-ヿ㐀-䶿一-鿿＀-￯]/.test(c);
  const c = chars[idx].c;
  // 优先用词边界(英语 PyMuPDF + 日语 fugashi)：同一个 w 扩成整词；
  // 之前 CJK 在这里 hardcoded 返回 single,阻止了 fugashi 分词生效。
  if (chars[idx].w !== -1) {
    const wid = chars[idx].w;
    let s = idx, e = idx;
    while (s > 0 && chars[s - 1].w === wid && !'/|／'.includes(chars[s - 1].c)) s--;
    while (e < chars.length - 1 && chars[e + 1].w === wid && !'/|／'.includes(chars[e + 1].c)) e++;
    while (s < e && !isWord(chars[s].c) && !isCJK(chars[s].c)) s++;   // 去词首标点
    while (e > s && !isWord(chars[e].c) && !isCJK(chars[e].c)) e--;   // 去词尾标点(如 often. 的句号)
    return {start: s, end: e};
  }
  // 没词 id 时:CJK 字符无分词信息 → 只选 1 字(避免整页扩),英文按 isWord 扩
  if (isCJK(c)) return {start: idx, end: idx};
  if (!isWord(c)) return {start: idx, end: idx};
  let s = idx;
  while (s > 0 && isWord(chars[s - 1].c) &&
         Math.abs(chars[s - 1].top - chars[idx].top) <= chars[idx].height * 0.5 &&
         (chars[s].left - (chars[s - 1].left + chars[s - 1].width)) <= chars[idx].height * 0.8) {
    s--;   // 大水平间隙=跨排版块(如 Unit 编号↔标题)，不并入同一词
  }
  let e = idx;
  while (e < chars.length - 1 && isWord(chars[e + 1].c) &&
         Math.abs(chars[e + 1].top - chars[idx].top) <= chars[idx].height * 0.5 &&
         (chars[e + 1].left - (chars[e].left + chars[e].width)) <= chars[idx].height * 0.8) {
    e++;   // 大水平间隙=跨排版块，不并入同一词
  }
  return {start: s, end: e};
}

// 同行扩展（双击）—— 含空格，但不跨块/明显水平空白。
function _lineExpandFromChar(chars, idx) {
  if (idx < 0 || idx >= chars.length) return null;
  const refTop = chars[idx].top;
  const refH = chars[idx].height;
  const refBk = _charBlockId(chars[idx]);
  const _sameLineNeighbor = (a, b) => {
    if (Math.abs(b.top - refTop) > refH * 0.4) return false;
    const bBk = _charBlockId(b);
    if (refBk >= 0 && bBk >= 0 && bBk !== refBk) return false;
    const left = a.left <= b.left ? a : b;
    const right = left === a ? b : a;
    const gap = right.left - (left.left + left.width);
    const h = Math.max(1, refH, a.height || 0, b.height || 0);
    return gap <= h * 1.5;
  };
  let s = idx, e = idx;
  while (s > 0 && _sameLineNeighbor(chars[s - 1], chars[s])) s--;
  while (e < chars.length - 1 && _sameLineNeighbor(chars[e], chars[e + 1])) e++;
  // 去掉两端空格
  while (s < e && chars[s].sp) s++;
  while (e > s && chars[e].sp) e--;
  return {start: s, end: e};
}

// 段扩展（三击）：连续行 + 行间距 < 2.2× 行高
function _paragraphExpandFromChar(chars, idx) {
  if (idx < 0 || idx >= chars.length) return null;
  const refH = chars[idx].height;
  const _bkOf = (c) => (c.bk != null && c.bk >= 0) ? c.bk : -1;
  const _bk0 = _bkOf(chars[idx]);   // 限本块(所在段落)：不跨栏/跨段/跨页，避免上下文吃到整页
  let s = idx, e = idx;
  let curTop = chars[idx].top;
  // 向左/上
  while (s > 0) {
    if (_bk0 >= 0 && _bkOf(chars[s - 1]) >= 0 && _bkOf(chars[s - 1]) !== _bk0) break;
    const t = chars[s - 1].top;
    if (Math.abs(t - curTop) > refH * 2.2) break;
    s--;
    if (Math.abs(t - curTop) > refH * 0.4) curTop = t;
  }
  curTop = chars[idx].top;
  while (e < chars.length - 1) {
    if (_bk0 >= 0 && _bkOf(chars[e + 1]) >= 0 && _bkOf(chars[e + 1]) !== _bk0) break;
    const t = chars[e + 1].top;
    if (Math.abs(t - curTop) > refH * 2.2) break;
    e++;
    if (Math.abs(t - curTop) > refH * 0.4) curTop = t;
  }
  return {start: s, end: e};
}

// 公式注入字符:整条选中(同一 w,不被 / | 等截断)。公式字符是连续追加的,按 w 左右扩到底。
function _formulaBounds(chars, idx) {
  const wid = chars[idx].w;
  let s = idx, e = idx;
  while (s > 0 && chars[s - 1].w === wid && chars[s - 1].fml) s--;
  while (e < chars.length - 1 && chars[e + 1].w === wid && chars[e + 1].fml) e++;
  return { start: s, end: e };
}
// 公式渲染浮层:MathJax 渲染该公式 + 复制LaTeX / 问AI / 制卡。点公式区即弹,点别处消失。
function _formulaRawLatex(chars, b) {
  for (let i = b.start; i <= b.end; i++) if (chars[i].flx) return chars[i].flx;
  // 兜底:从字符 c 拼回并去掉首尾 $ / $$
  let s = chars.slice(b.start, b.end + 1).map(c => c.c).join('');
  s = s.replace(/^\$\$?/, '').replace(/\$\$?$/, '');
  return s;
}
function _ensureFmlPopCss() {
  if (document.getElementById('fml-pop-css')) return;
  const st = document.createElement('style'); st.id = 'fml-pop-css';
  st.textContent =
    '#fml-pop{position:absolute;z-index:150;background:#0f1830;border:1px solid #2f4a7d;border-radius:12px;' +
    'box-shadow:0 10px 30px rgba(0,0,0,.55);padding:10px 12px;max-width:min(92vw,560px);color:#e6eeff}' +
    '#fml-pop .fp-render{overflow-x:auto;overflow-y:hidden;text-align:center;padding:4px 2px 8px;color:#eaf2ff;font-size:18px}' +
    '#fml-pop .fp-tex{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:12px;color:#9fb4e0;background:#0a1120;' +
    'border:1px solid #22325a;border-radius:7px;padding:5px 7px;white-space:pre-wrap;word-break:break-all;max-height:78px;overflow:auto;margin-bottom:8px}' +
    '#fml-pop .fp-btns{display:flex;gap:7px;flex-wrap:wrap}' +
    '#fml-pop .fp-btns button{flex:1 1 auto;min-width:66px;background:#16213e;border:1px solid #2f4a7d;color:#cfe0ff;' +
    'border-radius:8px;padding:7px 6px;font-size:13px;cursor:pointer;-webkit-tap-highlight-color:transparent}' +
    '#fml-pop .fp-btns button:active{background:#22325a}';
  document.head.appendChild(st);
}
function _hideFmlPop() { const p = document.getElementById('fml-pop'); if (p) p.remove(); }
window._hideFmlPop = _hideFmlPop;
function showFormulaPopover(pw, b) {
  _ensureFmlPopCss(); _hideFmlPop();
  const chars = pw.__charBoxes;
  const latex = _formulaRawLatex(chars, b);
  const isBlock = /\\begin\{|\\\\/.test(latex);
  const wrapped = isBlock ? ('$$' + latex + '$$') : ('$' + latex + '$');
  const copyStr = isBlock ? ('$$' + latex + '$$') : ('$' + latex + '$');
  const pop = document.createElement('div'); pop.id = 'fml-pop';
  const render = document.createElement('div'); render.className = 'fp-render';
  render.textContent = isBlock ? ('$$' + latex + '$$') : ('\\(' + latex + '\\)');
  const tex = document.createElement('div'); tex.className = 'fp-tex'; tex.textContent = copyStr;
  const btns = document.createElement('div'); btns.className = 'fp-btns';
  const mk = (label, fn) => { const x = document.createElement('button'); x.textContent = label; x.addEventListener('click', (ev) => { ev.stopPropagation(); fn(); }); return x; };
  btns.appendChild(mk('📋 复制', () => {
    (navigator.clipboard ? navigator.clipboard.writeText(copyStr) : Promise.reject()).then(() => { if (window._toast) window._toast('已复制 LaTeX'); }).catch(() => {
      try { const ta = document.createElement('textarea'); ta.value = copyStr; document.body.appendChild(ta); ta.select(); document.execCommand('copy'); ta.remove(); if (window._toast) window._toast('已复制 LaTeX'); } catch (_) {}
    });
  }));
  btns.appendChild(mk('💡 问 AI', () => { _hideFmlPop(); try { window.onExplain && window.onExplain(); } catch (_) {} }));
  btns.appendChild(mk('🃏 制卡', () => {
    _hideFmlPop();
    try { window.addDraftText && window.addDraftText(copyStr, '公式', FILE_REL + (typeof currentPage !== 'undefined' ? ('#p' + currentPage) : '')); } catch (_) {}
    try { window.openDraftModal && window.openDraftModal(); } catch (_) {}
  }));
  pop.appendChild(render); pop.appendChild(tex); pop.appendChild(btns);
  const mainEl = document.getElementById('viewer') || document.querySelector('.viewer') || document.body;
  mainEl.appendChild(pop);
  // 定位:公式上方(放不下则下方)。用选区起止字符的页内坐标。
  try {
    const pwRect = pw.getBoundingClientRect(), mainRect = mainEl.getBoundingClientRect();
    const c0 = chars[b.start], cN = chars[b.end];
    const leftPx = pwRect.left - mainRect.left + mainEl.scrollLeft + Math.min(c0.left, cN.left);
    const topAbove = pwRect.top - mainRect.top + mainEl.scrollTop + c0.top - pop.offsetHeight - 8;
    const topBelow = pwRect.top - mainRect.top + mainEl.scrollTop + Math.max(c0.top + c0.height, cN.top + cN.height) + 8;
    pop.style.left = Math.max(8, Math.min(leftPx, mainEl.scrollWidth - pop.offsetWidth - 8)) + 'px';
    pop.style.top = (topAbove > mainEl.scrollTop + 4 ? topAbove : topBelow) + 'px';
  } catch (_) {}
  if (window.MathJax && MathJax.typesetPromise) MathJax.typesetPromise([render]).catch(() => {});
}
window.showFormulaPopover = showFormulaPopover;
// 公式浮层外部点击关闭(char-layer 点击已 stopPropagation 不冒泡到这里 → 只处理页边/UI 等外部点)
document.addEventListener('pointerdown', (e) => {
  const p = document.getElementById('fml-pop');
  if (p && !(e.target && e.target.closest && e.target.closest('#fml-pop'))) _hideFmlPop();
});

let _dragStartCharIdx = null, _dragMoved = false, _dragStartXY = null, _fromLBtn = false;
let _hlTapPending = null;   // {pw,hit,x,y}:本次按下落在查询高亮上 → 松手(未拖动)时派发该高亮动作,不查词
let _dragDir = null;   // 触摸拖动首次动够时锁定:'scroll'(竖直为主→翻页) / 'select'(水平为主→选字)
let _swipeStart = null;   // 单页模式：起点在空白处的横滑 → 翻页（起点在字上仍走拖选）
let _lastClickCharIdx = -1, _lastClickTime = 0, _clickCount = 0;

// document 级 mousemove/mouseup 只在模块顶层注册一次:原先在 _bindCharLayer 内注册且从不移除,
// 每次页面重渲/缩放重绑都泄漏 +2 个监听,且旧闭包捕获过期 cl(缩放后 rect 失真致选区错位)。
// 经 __charDrag 分发:每次重绑都覆盖为指向最新 connected cl(见 _bindCharLayer 尾),天然路由到最新绑定。
document.addEventListener('mousemove', (e) => {
  if (_dragStartCharIdx == null || !_charSel) return;   // _dragStartCharIdx 为主守卫(touchcancel 只清它)
  const d = _charSel.pw && _charSel.pw.__charDrag; if (!d) return;
  const p = d.ptToLocal(e.clientX, e.clientY);
  d.onMove(p.x, p.y, null);
});
document.addEventListener('mouseup', (e) => {
  if (_dragStartCharIdx == null || !_charSel) return;
  const d = _charSel.pw && _charSel.pw.__charDrag; if (!d) return;
  const p = d.ptToLocal(e.clientX, e.clientY);
  d.onEnd(p.x, p.y);
});
// 覆盖层高亮点击派发:onStart 几何命中后记 _hlTapPending;这里在**松手**(pointerup,触摸+鼠标通用)派发该高亮动作,
//   移动超阈值(pointermove)则取消(视作拖动,不派发也不选字)。独立于 onEnd 的跨页守卫,对两端都可靠。
document.addEventListener('pointermove', (e) => {
  if (_hlTapPending && Math.abs(e.clientX - _hlTapPending.cx) + Math.abs(e.clientY - _hlTapPending.cy) >= 10) _hlTapPending = null;
}, true);
document.addEventListener('pointerup', () => {
  if (!_hlTapPending) return;
  const hit = _hlTapPending.hit, pw = _hlTapPending.pw; _hlTapPending = null;
  try { window.__readerHlTap && window.__readerHlTap(pw, hit); } catch (_) {}
}, true);
document.addEventListener('pointercancel', () => { _hlTapPending = null; }, true);
// 安全网:任何指针松开/取消 → 全局恢复呼吸高亮 .hl 可点。onStart 拖选时给它们置了 inline pointer-events:none,
// 但 onEnd 只恢复"起点页"那份(_charSel.pw!==pw 提前 return);跨页松手/中断手势会让别页 solid 词组高亮残留 none →
// 点它穿透到 char-layer =「点了不弹」。这里兜底清掉所有页的残留 none(只在手势结束时跑,不影响进行中的拖选)。
['pointerup', 'pointercancel', 'touchend', 'touchcancel'].forEach(ev => document.addEventListener(ev, () => {
  document.querySelectorAll(_OVL_HL_SEL).forEach(el => { if (el.style.pointerEvents === 'none') el.style.pointerEvents = ''; });
}, true));
// document 级 touchstart 无条件记 _clLastTouchAt:char-layer 的「touch 后 700ms 忽略 iOS 合成 mousedown」守卫
// 原本只在 touch 落在 char-layer 自身时才更新时间戳;touch 落在**覆盖层**(词组/查词高亮 .hl 等,char-layer 的兄弟,
// 冒泡不到 char-layer)时守卫失效 → 点词组高亮后 handler 删掉 .hl,~300ms 后合成 mousedown 穿到 char-layer →
// 误查手指下的词(A/B 缓存秒弹)覆盖词组框。这里任何 touch 都记时间戳,守卫就覆盖到覆盖层上的点击。
document.addEventListener('touchstart', () => { try { window._clLastTouchAt = Date.now(); } catch (_) {} }, true);

function _bindCharLayer(cl, pw) {
  const ptToLocal = (clientX, clientY) => {
    const r = cl.getBoundingClientRect();
    // 视觉坐标 → charBoxes 布局坐标:用 BCR 与 layout 尺寸的**比值**补偿全链路缩放——
    // wrap 自身的过渡 zoom、page-container/祖先的 pinch zoom、transform scale 一并覆盖。
    // 旧实现只除 pw.style.zoom:祖先有 zoom 时(实测双页态 ≈0.84)整页点击横向偏 ~16%,
    // 页中部胖字能蒙对、右缘/页底整片点不中(点視覺上的 議 → 命中右页的 内部)。
    const kx = r.width  ? cl.clientWidth  / r.width  : 1;
    const ky = r.height ? cl.clientHeight / r.height : 1;
    return {x: (clientX - r.left) * kx, y: (clientY - r.top) * ky};
  };

  // 严格 bbox 命中 + 同行最近 char fallback。
  // 日语扫描书走 visual segmentation 定位字符,char bbox 宽 ≈ 0.78 × spacing →
  // 字符之间有 ~22% 空隙；严格命中空隙时回落"y 在行内 + x 离 char center 最近"防止点了没反应。
  const _findCharStrict = (x, y) => {
    for (let i = 0; i < pw.__charBoxes.length; i++) {
      const c = pw.__charBoxes[i];
      if (c.sp) continue;
      if (x >= c.left && x <= c.left + c.width &&
          y >= c.top  && y <= c.top  + c.height) return i;
    }
    // fallback:y 严格在行高内,取 x 距 char center 最近的(距离阈值 < 1× char height,
    // 避免远处空白也错命中)
    let best = -1, bestDist = Infinity;
    for (let i = 0; i < pw.__charBoxes.length; i++) {
      const c = pw.__charBoxes[i];
      if (c.sp) continue;
      if (y < c.top || y > c.top + c.height) continue;
      const cx = c.left + c.width / 2;
      const d = Math.abs(x - cx);
      if (d < bestDist) { bestDist = d; best = i; }
    }
    if (best >= 0 && bestDist <= (pw.__charBoxes[best].height || 30) * 1.0) return best;
    // 第三段:振假名带/行间缝/行尾余白容差。ruby 画在字行**上方 ~0.5 行高**(pointer-events:none),
    // 点在假名或行缝上时 y 不落在任何 char bbox 行内 → 此前直接 MISS 被当「点空白」清选区
    // (实测 応用情報 p37「議事」上方 furigana 区即死区)。给竖直偏差 ≤0.7×行高、水平贴近的字兜底;
    // dy 权重 ×2 → 行缝处优先归属更近的那一行。
    let best3 = -1, bd3 = Infinity;
    for (let i = 0; i < pw.__charBoxes.length; i++) {
      const c = pw.__charBoxes[i];
      if (c.sp) continue;
      const h = c.height || 30;
      const dy = y < c.top ? (c.top - y) : (y > c.top + c.height ? y - c.top - c.height : 0);
      if (dy > h * 0.7) continue;
      const cx = c.left + c.width / 2;
      const dx = Math.abs(x - cx);
      if (dx > h * 1.2) continue;
      const d = dx + dy * 2;
      if (d < bd3) { bd3 = d; best3 = i; }
    }
    if (best3 >= 0) return best3;
    return -1;
  };
  const onStart = (x, y, cx, cy) => {
    if (window._ink && (_ink.mode || _ink.drawing)) return false;   // 手写模式/正在画 → 不选字(防御:各入口都兜住)
    // 根治:先几何判断本次按下是否落在查询高亮上。是 → 记 pending,松手派发该高亮动作,**不选字/不查词**。
    //   与高亮 pointer-events 状态完全无关 → 无论 .hl 是否残留 none、事件是否穿透,都不会误查手指下的字。
    if (cx != null) {
      const _hit = _overlayHlHitAtClient(pw, cx, cy);
      if (_hit && _hit.kind) { _hlTapPending = { pw, hit: _hit, cx, cy }; _dragStartCharIdx = null; return false; }
    }
    _syncCharBoxScale(pw);   // 命中前先把 charBoxes 对齐到当前显示尺寸(烘焙 scale 可能已过期)
    _hideFmlPop();           // 任何新按下先关掉旧公式浮层(若新点中公式,onEnd 会重新弹)
    _fromLBtn = false;   // 普通 char-layer 起点（非 L 按钮转发）
    // 诊断：每次按下输出 char-layer rect + 点击位置
    if (!window._loggedRect) {
      const r = cl.getBoundingClientRect();
      window.dlog?.(`cl rect: l=${r.left.toFixed(0)} t=${r.top.toFixed(0)} w=${r.width.toFixed(0)} h=${r.height.toFixed(0)}`);
      window._loggedRect = true;
    }
    window.dlog?.(`tap local: x=${x.toFixed(0)} y=${y.toFixed(0)}`);
    const idx = _findCharStrict(x, y);
    if (idx < 0) {
      _dragStartCharIdx = null;
      // 点空白 → 关 toolbar + 清选区（用户期望取消选中状态）
      toolbar.classList.remove('open');
      lastSelText = '';
      _updateSelPreview('');
      document.querySelectorAll('.sel-overlay').forEach(ov => ov.innerHTML = '');
      return false;
    }
    _dragStartCharIdx = idx;
    _dragStartXY = {x, y};
    _dragMoved = false;
    _dragDir = null;   // 方向未定;首次动够时锁定
    _charSel = {pw, startIdx: idx, endIdx: idx, dragging: true};
    // 拖选期间禁用 vocab-layer 拦截：否则拖到/松手在 L 按钮(pointer-events:auto)上会丢 move/up → 卡住/全选
    const vl = pw.querySelector('.vocab-layer'); if (vl) vl.style.pointerEvents = 'none';
    // 同理禁用 查词/词组/解释 呼吸高亮的 .hl 点击：否则拖选经过它们会被截获 → 丢 move/up(选区乱涨成多词
    // → 误弹词组按钮)+ 误触发其 click(弹出别词结果)。松手(onEnd)/转滚动(scroll)时恢复。
    pw.querySelectorAll(_OVL_HL_SEL).forEach(el => el.style.pointerEvents = 'none');
    return true;
  };

  const onMove = (x, y, ev) => {
    if (_dragStartCharIdx == null) return;
    if (_charSel && _charSel.pw !== pw) return;   // 多页/翻页累积的 document 监听：只处理拖选起点页
    if (!_dragStartXY) return;
    const dx = Math.abs(x - _dragStartXY.x), dy = Math.abs(y - _dragStartXY.y);
    if (dx + dy < 8) return;
    // 触摸:首次动够时锁定方向。竖直为主(dy>dx) → 当作翻页/滚动:放弃选字、不拦默认滚动。
    // 鼠标(ev=null)不受此限,竖直拖仍可选。多行选择只要**起手横向**就锁成 select、后续往下拉照常选。
    const isTouch = !!(ev && ev.touches);
    if (isTouch && _dragDir === null) {
      _dragDir = (dy > dx) ? 'scroll' : 'select';
      if (_dragDir === 'scroll') {
        _dragStartCharIdx = null;   // 放弃这次拖选 → 后续 move/end 直接 return,页面正常上下滚
        _charSel = null;
        pw.querySelector('.sel-overlay')?.replaceChildren();
        const vl = pw.querySelector('.vocab-layer'); if (vl) vl.style.pointerEvents = '';
        pw.querySelectorAll(_OVL_HL_SEL).forEach(el => el.style.pointerEvents = '');
        return;   // 不 preventDefault
      }
    }
    _dragMoved = true;
    if (ev && ev.cancelable) ev.preventDefault();
    const idx = _findCharAt(pw.__charBoxes, x, y);
    if (idx < 0) return;
    _selByCharRange(pw, _dragStartCharIdx, idx);
  };

  const onEnd = (x, y) => {
    if (_dragStartCharIdx == null) return;
    if (_charSel && _charSel.pw !== pw) return;   // 多页/翻页累积的 document 监听：只处理拖选起点页
    const vl = pw.querySelector('.vocab-layer'); if (vl) vl.style.pointerEvents = '';  // 恢复 L 按钮可点
    pw.querySelectorAll(_OVL_HL_SEL).forEach(el => el.style.pointerEvents = '');       // 恢复呼吸高亮可点
    const startIdx = _dragStartCharIdx;
    _dragStartCharIdx = null;
    if (_dragMoved) {
      const idx = _findCharAt(pw.__charBoxes, x, y);
      if (idx >= 0) _selByCharRange(pw, startIdx, idx);
      try { window.__setFocusSel && window.__setFocusSel((lastSelText || '').trim(), 'text'); } catch (_) {}   // 拖选段落 → 右侧焦点显示
      // 选中=普通选中(点别处照常消失)。持久呼吸高亮只在点「词组」按钮查询期间出现(showPhrasePopover)
    } else if (_fromLBtn) {
      // 从 L 按钮起点且没拖动 = 单击 L 按钮 → 交给 L 按钮 click 处理整句翻译，这里不查词
      _fromLBtn = false;
    } else {
      // 公式注入字符:点公式区 → 整条公式选中 + MathJax 渲染浮层(不走单/双/三击词典)
      const _h0 = pw.__charBoxes[startIdx];
      if (_h0 && _h0.fml) {
        const fb = _formulaBounds(pw.__charBoxes, startIdx);
        _selByCharRange(pw, fb.start, fb.end);
        toolbar.classList.remove('open');
        try { showFormulaPopover(pw, fb); } catch (_) {}
        try { window.__setFocusSel && window.__setFocusSel(_formulaRawLatex(pw.__charBoxes, fb), 'formula'); } catch (_) {}
        _lastClickCharIdx = -1; _clickCount = 0;
        return;
      }
      // 单/双/三击
      const now = Date.now();
      if (_lastClickCharIdx === startIdx && now - _lastClickTime < 380) {
        _clickCount = (_clickCount % 3) + 1;
      } else {
        _clickCount = 1;
        _lastClickCharIdx = startIdx;
      }
      _lastClickTime = now;
      let bounds = null;
      if (_clickCount === 1) bounds = _wordExpandFromChar(pw.__charBoxes, startIdx);
      else if (_clickCount === 2) bounds = _lineExpandFromChar(pw.__charBoxes, startIdx);
      else bounds = _paragraphExpandFromChar(pw.__charBoxes, startIdx);
      if (bounds) {
        _selByCharRange(pw, bounds.start, bounds.end);
        // 单击单词 → 弹单词小框查词
        if (_clickCount === 1) {
          const _t = (lastSelText || '').trim();
          const _cr = _expandSentenceFromRange(pw.__charBoxes, bounds.start, bounds.end);
          const _ctx = _cr ? _charsRangeToText(pw.__charBoxes, _cr.start, _cr.end).slice(0, 400) : '';
          const hasKana  = s => /[぀-ゟ゠-ヿ]/.test(s);      // 平/片假名 = 铁定日语
          const hasKanji = s => /[一-鿿㐀-䶿]/.test(s);      // CJK 汉字(日中共用,光看词分不出)
          const isEng = /^[A-Za-z][A-Za-z'’\-]*$/.test(_t);
          const declared = BOOK_LANGS.length > 0;
          // 日语判定:优先用本书语言声明;没声明则回退启发(假名铁定/纯汉字看上下文假名)
          let isJa;
          if (declared) {
            isJa = BOOK_LANGS.includes('ja') && (hasKana(_t) || hasKanji(_t));
          } else {
            isJa = hasKana(_t) || (hasKanji(_t) && hasKana(_ctx));
          }
          // 英文:沿用「点击翻译」开关;若声明了语言且没勾英语则不弹
          const engOk = isEng && _clickTranslateEnabled() && (!declared || BOOK_LANGS.includes('en'));
          // 母语(不需要翻译的语言)单击选词 = 毫无意义 → 单击中文汉字词(纯汉字无假名、本书非日语)不弹任何东西、清掉选中。
          // 拖选/双击行/三击段仍照常弹(走别的分支)。
          const isNativeHan = hasKanji(_t) && !hasKana(_t) && !(declared && BOOK_LANGS.includes('ja'));
          if (_t && _t.length <= 30 && (isJa || engOk)) {
            // 同步关掉刚被 _selByCharRange 打开的工具栏:同一事件 tick 内移除 → 浏览器根本不画它。
            // 此前靠 30ms 后的 showWordPopover 去关 → 工具栏闪一帧再消失(慢词时=「弹框闪烁后消失」)。
            toolbar.classList.remove('open');
            // 点的词是不是已知生词(有下划线=以前查过、服务器有缓存)→ 跳过呼吸,直接弹占位框秒填结果(用户反馈:已查过的词不该再呼吸等待)
            let _isKnown = false;
            try {
              const _vm = (_charSel && _charSel.pw && _charSel.pw.__vocabMarks) || [];
              const _tl = _t.toLowerCase();
              _isKnown = _vm.some(m => (m.word && String(m.word).toLowerCase() === _tl) || (m.lemma && String(m.lemma).toLowerCase() === _tl));
            } catch (_) {}
            if (window.__uiShared && window.PdfAdapter) {
              PdfAdapter.lookupWord({ word: _t, context: _ctx, page: _selPageNum(), file: FILE_REL, langs: BOOK_LANGS, anchorRect: _charSel, noBreathe: _isKnown, fallback: (w, c) => showWordPopover(w, c) });
            } else {
              setTimeout(() => { try { showWordPopover(_t, _ctx); } catch(_){} }, 30);
            }
          } else if (isNativeHan) {
            toolbar.classList.remove('open');
            lastSelText = '';
            _updateSelPreview('');
            pw.querySelectorAll('.sel-overlay').forEach(ov => ov.innerHTML = '');   // 清掉单击中文词的高亮 → 单击=无操作
          }
        } else {
          // 双击选行 / 三击选段 → 右侧焦点显示这段
          try { window.__setFocusSel && window.__setFocusSel((lastSelText || '').trim(), 'text'); } catch (_) {}
        }
      }
    }
  };

  cl.addEventListener('mousedown', (e) => {
    if (e.button !== 0) return;
    if ((window._ink && (_ink.mode || _ink.drawing)) || (Date.now() < (window.__inkGuardUntil || 0))) return;   // 手写模式/正在画/刚写完 1s 内 → 不选字查词(palm rejection)
    if (Date.now() - (window._clLastTouchAt || 0) < 700) return;   // 忽略 touch 后 iOS 合成的 mousedown（否则 onStart 双触发→假双击→刚弹的小框被冲掉）
    e.preventDefault(); e.stopPropagation();   // 阻止旧 document.mousedown 清 toolbar
    const p = ptToLocal(e.clientX, e.clientY);
    onStart(p.x, p.y, e.clientX, e.clientY);
  });
  // document 级 mousemove/mouseup 移到模块顶层单 dispatcher(经 pw.__charDrag 分发),不再每次绑定泄漏
  cl.addEventListener('touchstart', (e) => {
    if (e.touches.length !== 1) { _dragStartCharIdx = null; _swipeStart = null; return; }
    // Apple Pencil(touchType='stylus')或手写模式/正在画 → 让墨迹层处理,**不选字**
    // (墨迹绘制在 wrap 的 pointerdown,跟这条 touchstart 是不同类事件,pointerdown 的 stopPropagation 挡不住它)
    if ((e.touches[0] && e.touches[0].touchType === 'stylus') || (window._ink && (_ink.mode || _ink.drawing)) || (Date.now() < (window.__inkGuardUntil || 0))) {   // 手写中/刚写完 1s 内:手掌触摸不选字查词(palm rejection)
      _dragStartCharIdx = null; _swipeStart = null; return;
    }
    window._clLastTouchAt = Date.now();   // 标记触摸：后续 iOS 合成 mousedown 忽略
    e.stopPropagation();   // 阻止旧 document.touchstart 清 toolbar
    const t = e.touches[0];
    const p = ptToLocal(t.clientX, t.clientY);
    onStart(p.x, p.y, t.clientX, t.clientY);
    // 单页模式：整页任意处都可横滑翻页（不限空白）。tap=选词 / 横滑=翻页 / 竖滑=滚动；单页不做拖选。
    _swipeStart = (readMode === 'single')
      ? {x: t.clientX, y: t.clientY, lastX: t.clientX, decided: false, h: false}
      : null;
  }, {passive: true});
  cl.addEventListener('touchmove', (e) => {
    if (e.touches.length !== 1) return;
    if (_swipeStart) {   // 单页模式：横滑翻页 / 竖滑滚动 / 移动即放弃选词
      const t = e.touches[0];
      const dx = t.clientX - _swipeStart.x, dy = t.clientY - _swipeStart.y;
      _swipeStart.lastX = t.clientX;
      if (!_swipeStart.decided && (Math.abs(dx) + Math.abs(dy)) > 8) {
        _swipeStart.decided = true;
        _swipeStart.h = Math.abs(dx) > Math.abs(dy);   // 横向主导=翻页，否则=滚动
        // 一旦移动 → 放弃 tap 选词（拖动不在单页里选字）
        _dragStartCharIdx = null; _charSel = null;
        pw.querySelector('.sel-overlay')?.replaceChildren();
        const vl = pw.querySelector('.vocab-layer'); if (vl) vl.style.pointerEvents = '';
        pw.querySelectorAll(_OVL_HL_SEL).forEach(el => el.style.pointerEvents = '');
        if (!_swipeStart.h) _swipeStart = null;   // 竖滑 → 交回原生滚动，不再拦
      }
      // 横滑：每次 move 都 preventDefault 抢下手势，防浏览器判滚动→touchcancel→翻不了页（F2 根因）
      if (_swipeStart && _swipeStart.h && e.cancelable) e.preventDefault();
      return;
    }
    if (_dragStartCharIdx == null) return;
    e.stopPropagation();
    const t = e.touches[0];
    const p = ptToLocal(t.clientX, t.clientY);
    onMove(p.x, p.y, e);
  }, {passive: false});
  cl.addEventListener('touchend', (e) => {
    // 单页横滑翻页：右滑→上一页，左滑→下一页（阈值 40px）；没构成横滑则落到下面 tap 选词
    if (_swipeStart) {
      const sw = _swipeStart; _swipeStart = null;
      if (sw.h) {
        const t0 = e.changedTouches[0];
        const dx = (t0 ? t0.clientX : sw.lastX) - sw.x;
        if (Math.abs(dx) >= 40) { e.preventDefault(); window.changePage(dx > 0 ? -1 : 1); return; }
        return;   // 横向移动了但不够阈值 → 不翻也不选
      }
    }
    if (_dragStartCharIdx == null) return;
    e.preventDefault(); e.stopPropagation();
    const t = e.changedTouches[0];
    if (!t) return;
    const p = ptToLocal(t.clientX, t.clientY);
    onEnd(p.x, p.y);
  });
  cl.addEventListener('touchcancel', () => { _dragStartCharIdx = null; _swipeStart = null;
    const vl = pw.querySelector('.vocab-layer'); if (vl) vl.style.pointerEvents = '';
    pw.querySelectorAll(_OVL_HL_SEL).forEach(el => el.style.pointerEvents = ''); });
  // 暴露给 vocab L 按钮：从 L 按钮上也能转发拖选（既能点翻译，也能从其上拖选）
  pw.__charDrag = { onStart, onMove, onEnd, ptToLocal };
}
