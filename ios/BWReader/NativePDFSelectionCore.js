// Packaged after the original pure PDF character/selection functions. This is
// a data adapter for native gestures; it neither creates nor reads DOM nodes.
globalThis.BWNativePDFSelection = function (page) {
  if (!page || !Array.isArray(page.chars) || page.chars.length > 100000 ||
      !(page.page_w > 0) || !(page.page_h > 0)) throw new Error('Invalid character page');
  const chars = _mapCharBoxes(page.chars, 1, page.source, page.engine_revision, page.character_geometry);
  chars.__layout = page.layout || null;
  const positions = new Map(chars.map((c, i) => [c._oi, i]));
  function selected(start, end, keep) {
    if (start > end) [start, end] = [end, start];
    if (start < 0 || end >= chars.length) throw new Error('Selection outside character page');
    const accepted = _charRangeBlockFilter(chars, start, end);
    const exact = new Set();
    for (let i = start; i <= end; i++) {
      if (!chars[i].sp && accepted(chars[i]) && (!keep || keep.has(i))) exact.add(i);
    }
    if (!exact.size) return null;
    const text = _charsRangeToText(chars, start, end, exact);
    const sentenceRange = _expandSentenceFromRange(chars, start, end);
    const sentence = _charsRangeToText(chars, sentenceRange.start, sentenceRange.end).slice(0, 600);
    return {
      indexes: Array.from(exact, i => chars[i]._oi), text,
      sentence: sentence.replace(/\s/g, '') === text.replace(/\s/g, '') ? '' : sentence,
      rects: _charRangeToVisualRects(chars, start, end, 'point', exact),
      regionMode: accepted.regionMode,
    };
  }
  return Object.freeze({
    hit(x, y, anchor, exactOnly) {
      if (![x, y].every(Number.isFinite)) return -1;
      const i = exactOnly
        ? chars.findIndex(c => !c.sp && x >= c.left && x <= c.left + c.width && y >= c.top && y <= c.top + c.height)
        : _findCharAt(chars, x, y, positions.get(anchor));
      return i >= 0 ? chars[i]._oi : -1;
    },
    range(anchor, endpoint) {
      let start = positions.get(anchor), end = positions.get(endpoint);
      if (start == null || end == null) throw new Error('Unknown selection endpoint');
      if (start > end) [start, end] = [end, start];
      return selected(_expandToWordStart(chars, start), _expandToWordEnd(chars, end));
    },
    exact(indexes) {
      if (!Array.isArray(indexes) || indexes.length > chars.length) throw new Error('Invalid selection indexes');
      const values = indexes.map(i => positions.get(i));
      if (values.some(i => i == null)) throw new Error('Unknown selection index');
      if (!values.length) return null;
      values.sort((a, b) => a - b);
      return selected(values[0], values[values.length - 1], new Set(values));
    },
  });
};
