import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => fs.readFileSync(new URL(path, ROOT), "utf8");
const PDF = read("_server_deploy/static/pdf/reader.src/17-highlight.js");
const EPUB = read("_server_deploy/static/pdf/epub-html.js");

function rangeRef(source, start, end) {
  let offset = 0;
  const byOffset = new Map();
  for (const item of source.markers) {
    byOffset.set(offset, item.marker);
    offset += item.text.length;
  }
  assert.ok(byOffset.has(start), `missing marker at start offset ${start}`);
  assert.ok(byOffset.has(end), `missing marker at end offset ${end}`);
  return {
    contract: "reader-source-range/1",
    snapshotId: source.snapshotId,
    documentId: source.documentId,
    target: source.target,
    sourceDigest: source.sourceDigest,
    revision: source.revision,
    startMarker: byOffset.get(start),
    endMarker: byOffset.get(end),
  };
}

function assertSourceShape(source, expectedText) {
  assert.equal(source.contract, "reader-highlight-source/1");
  assert.match(source.snapshotId, /^hrs_[0-9a-f]{24}$/);
  assert.match(source.sourceDigest, /^rsd1_[0-9a-f]{8}_[0-9a-f]{16}$/);
  assert.equal(typeof source.revision, "string");
  assert.ok(source.revision.length > 0 && source.revision.length <= 160);
  assert.ok(Number.isInteger(source.expiresAt));
  assert.ok(source.expiresAt > Date.now());
  assert.ok(source.expiresAt <= Date.now() + 300_000);
  assert.ok(source.markers.length >= 2 && source.markers.length <= 2048);
  assert.equal(source.markers.at(-1).text, "");
  assert.equal(source.markers.map((item) => item.text).join(""), expectedText);
  const ids = source.markers.map((item) => item.marker);
  assert.equal(new Set(ids).size, ids.length);
  for (const item of source.markers) {
    assert.deepEqual(Object.keys(item).sort(), ["marker", "text"]);
    assert.match(item.marker, /^m_[0-9a-z]{1,4}$/);
    assert.ok(item.text.length <= 512);
  }
}

function pdfHarness() {
  const start = PDF.indexOf("function _pdfExactTextProjection(chars)");
  const end = PDF.indexOf("window.__bwReaderHighlightExactText", start);
  assert.ok(start >= 0 && end > start);

  const wanted = "長文の文字層断片を越えても範囲は失われない。".repeat(12);
  const raw = "先頭。" + wanted + "区切り。" + wanted + "末尾。";
  const second = raw.indexOf(wanted, raw.indexOf(wanted) + 1);
  const rowBreak = second + Math.floor(wanted.length / 2);
  const chars = Array.from(raw, (c, index) => {
    const row = index < rowBreak ? 0 : 1;
    const left = row ? (index - rowBreak) * 12 : index * 12;
    return {
      c,
      sp: false,
      top: row * 24,
      left,
      width: 10,
      height: 18,
      _x0: left,
      _x1: left + 10,
      _y0: row * 24,
      _y1: row * 24 + 18,
      bk: row,
    };
  });
  let nonce = 0;
  let lastId = "";
  const saves = [];
  const pw = {
    dataset: { loaded: "1", pageNum: "2" },
    __charBoxes: chars,
    __pageTextRevision: "pdf-rev-1",
    querySelectorAll() {
      return lastId ? [{ dataset: { id: lastId }, style: { width: "12", height: "8" } }] : [];
    },
  };
  const context = vm.createContext({
    window: null,
    FILE_REL: "book.pdf",
    pdfDoc: { numPages: 9 },
    currentPage: 2,
    document: { querySelector: () => pw },
    renderHighlightsOnPage() {},
    saveHighlight: async ({ sIdx, eIdx, id }) => {
      lastId = id;
      const text = chars.slice(sIdx, eIdx + 1).map((item) => item.c).join("");
      saves.push({ sIdx, eIdx, id, text });
      return { id, text };
    },
    setTimeout: globalThis.setTimeout,
    crypto: {
      getRandomValues(bytes) {
        nonce += 1;
        for (let i = 0; i < bytes.length; i++) bytes[i] = (nonce + i) & 255;
        return bytes;
      },
    },
  });
  context.window = context;
  context.window.goToPage = () => Promise.resolve();
  context.window.crypto = context.crypto;
  vm.runInContext(PDF.slice(start, end), context, { filename: "pdf-highlight-source-range.js" });
  return { context, pw, chars, raw, wanted, saves };
}

test("PDF marker ranges select a long second occurrence across rows without quote search", async () => {
  const { context, raw, wanted, saves } = pdfHarness();
  const source = await context.window.__bwReaderHighlightSource({
    file: "book.pdf",
    target: { kind: "pdf", page: 2 },
  });
  assertSourceShape(source, raw);

  assert.ok(wanted.length > 180, "the selected span must exceed both legacy truncation windows");
  const first = raw.indexOf(wanted);
  const start = raw.indexOf(wanted, first + 1);
  assert.ok(start > first, "the fixture must contain the same long span twice");
  const ref = rangeRef(source, start, start + wanted.length);
  const result = await context.window.__bwReaderHighlightRange({
    rangeRef: ref,
    color: "yellow",
    note: "",
  });

  assert.equal(result.ok, true);
  assert.equal(result.text, wanted);
  assert.equal(saves.length, 1);
  assert.deepEqual(
    { start: saves[0].sIdx, end: saves[0].eIdx, text: saves[0].text },
    { start, end: start + wanted.length - 1, text: wanted },
  );
  assert.match(saves[0].id, /^c_[a-f0-9]{16}$/,
    "a missing mutation id is derived stably inside the host");

  await context.window.__bwReaderHighlightRange({ rangeRef: ref, color: "yellow", note: "" });
  assert.equal(saves[1].id, saves[0].id, "the same range retry must be idempotent");

  const rangeBody = PDF.slice(
    PDF.indexOf("window.__bwReaderHighlightRange = async function"),
    PDF.indexOf("window.__bwReaderHighlightExactText"),
  );
  assert.doesNotMatch(rangeBody, /\.indexOf\(/,
    "the range path must consume marker offsets, never search the selected quote");
});

test("PDF synthetic layout-space boundaries never include the next real character", async () => {
  const { context, chars, saves } = pdfHarness();
  const ascii = Array.from("foobar", (c, index) => ({
    c,
    sp: false,
    top: 0,
    left: index < 3 ? index * 10 : 100 + (index - 3) * 10,
    width: 8,
    height: 18,
    _x0: index < 3 ? index * 10 : 100 + (index - 3) * 10,
    _x1: (index < 3 ? index * 10 : 100 + (index - 3) * 10) + 8,
    _y0: 0,
    _y1: 18,
    bk: 0,
  }));
  chars.splice(0, chars.length, ...ascii);

  const source = await context.window.__bwReaderHighlightSource({
    file: "book.pdf",
    target: { kind: "pdf", page: 2 },
  });
  assertSourceShape(source, "foo bar");
  const ref = rangeRef(source, 0, 4);
  const result = await context.window.__bwReaderHighlightRange({
    rangeRef: ref,
    color: "yellow",
    note: "",
  });

  assert.equal(result.text, "foo");
  assert.deepEqual(
    { start: saves[0].sIdx, end: saves[0].eIdx, text: saves[0].text },
    { start: 0, end: 2, text: "foo" },
  );
});

test("PDF source snapshots are reused, revision-bound, and fail closed", async () => {
  const { context, pw, chars, raw, saves } = pdfHarness();
  const input = { file: "book.pdf", target: { kind: "pdf", page: 2 } };
  const source = await context.window.__bwReaderHighlightSource(input);
  const again = await context.window.__bwReaderHighlightSource(input);
  assert.equal(again.snapshotId, source.snapshotId);
  assert.equal(again.expiresAt, source.expiresAt);

  const ref = rangeRef(source, 0, source.markers[0].text.length);
  await assert.rejects(
    context.window.__bwReaderHighlightRange({
      rangeRef: { ...ref, startMarker: "m_nope" }, color: "blue",
    }),
    /BW_READER_RANGE_MARKER_INVALID/,
  );
  await assert.rejects(
    context.window.__bwReaderHighlightRange({
      rangeRef: { ...ref, startMarker: ref.endMarker, endMarker: ref.startMarker }, color: "blue",
    }),
    /BW_READER_RANGE_INVALID/,
  );
  await assert.rejects(
    context.window.__bwReaderHighlightRange({
      rangeRef: { ...ref, offset: 3 }, color: "blue",
    }),
    /BW_READER_RANGE_CONTRACT_INVALID/,
  );
  assert.equal(saves.length, 0, "invalid boundaries must not reach persistence");

  const original = chars[3].c;
  chars[3].c = "改";
  await assert.rejects(
    context.window.__bwReaderHighlightRange({ rangeRef: ref, color: "blue" }),
    /BW_READER_RANGE_SOURCE_STALE/,
  );
  assert.equal(saves.length, 0);
  chars[3].c = original;

  vm.runInContext(
    `_pdfReaderSourceSnapshots.get(${JSON.stringify(source.snapshotId)}).expiresAt = Date.now() + 20000`,
    context,
  );
  const renewed = await context.window.__bwReaderHighlightSource(input);
  assert.notEqual(renewed.snapshotId, source.snapshotId,
    "a source inside the final 30 seconds must be renewed before it is shown to a model");

  pw.__pageTextRevision = "pdf-rev-2";
  const revised = await context.window.__bwReaderHighlightSource(input);
  assert.notEqual(revised.snapshotId, source.snapshotId);
  assert.equal(revised.sourceDigest, source.sourceDigest,
    "same text may still require a new snapshot when the native revision changes");
  assertSourceShape(revised, raw);

  const revisedRef = rangeRef(revised, 0, revised.markers[0].text.length);
  vm.runInContext(
    `_pdfReaderSourceSnapshots.get(${JSON.stringify(revised.snapshotId)}).expiresAt = 0`,
    context,
  );
  await assert.rejects(
    context.window.__bwReaderHighlightRange({ rangeRef: revisedRef, color: "blue" }),
    /BW_READER_RANGE_SNAPSHOT_STALE/,
  );
});

function classList(...names) {
  const values = new Set(names);
  return { contains: (name) => values.has(name) };
}

function epubHarness(options = {}) {
  const countableStart = EPUB.indexOf("function _countable(textNode)");
  const countableEnd = EPUB.indexOf("// offsetOf 的反向", countableStart);
  const helpersStart = EPUB.indexOf("var _READER_HIGHLIGHT_SOURCE_CONTRACT");
  const helpersEnd = EPUB.indexOf("// 确保某 section 已渲染", helpersStart);
  const apiStart = EPUB.indexOf("window.__bwReaderHighlightSource = function");
  const apiEnd = EPUB.indexOf("function _epubExactSource", apiStart);
  assert.ok(countableStart >= 0 && countableEnd > countableStart);
  assert.ok(helpersStart >= 0 && helpersEnd > helpersStart);
  assert.ok(apiStart >= 0 && apiEnd > apiStart);

  const col = { classList: classList() };
  const section = { classList: classList("ep-sec"), parentElement: col, nodes: [] };
  const normalParent = section;
  const decorationParent = (kind) => {
    if (kind === "rt") return { tagName: "RT", dataset: { eph: "1" }, classList: classList(), parentElement: section };
    if (kind === "translation") return { tagName: "DIV", dataset: {}, classList: classList("ep-tr-rt"), parentElement: section };
    return { tagName: "DIV", dataset: {}, classList: classList("rc-note"), parentElement: section };
  };
  const node = (value, parentElement = normalParent) => ({ nodeType: 3, nodeValue: value, parentElement });
  if (typeof options.sectionText === "string") {
    section.nodes.push(node(options.sectionText));
  } else {
    section.nodes.push(
      node("前重複句"),
      node("かな", decorationParent("rt")),
      node("中"),
      node("translated", decorationParent("translation")),
      node("重複句終跨"),
      node("sticky", decorationParent("note")),
      node("行"),
    );
  }
  const canonical = typeof options.sectionText === "string"
    ? options.sectionText : "前重複句中重複句終跨行";
  let viewportOffset = Number.isInteger(options.viewportOffset) ? options.viewportOffset : 0;
  section.contains = (candidate) => {
    let current = candidate?.nodeType === 3 ? candidate.parentElement : candidate;
    while (current) {
      if (current === section) return true;
      current = current.parentElement;
    }
    return false;
  };
  const countableInHarness = (item) => {
    let current = item.parentElement;
    while (current && current !== col) {
      if (current.tagName === "RT" && current.dataset?.eph === "1") return false;
      if (current.classList?.contains("ep-tr-rt") || current.classList?.contains("rc-note")) return false;
      current = current.parentElement;
    }
    return true;
  };
  const caretAtViewport = () => {
    let remaining = Math.max(0, viewportOffset);
    let last = section.nodes[0] || null;
    for (const item of section.nodes) {
      if (!countableInHarness(item)) continue;
      last = item;
      if (remaining <= item.nodeValue.length) {
        return { startContainer: item, startOffset: remaining };
      }
      remaining -= item.nodeValue.length;
    }
    return { startContainer: last, startOffset: last?.nodeValue.length || 0 };
  };
  const content = {
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }),
  };
  let nonce = 0;
  const saves = [];
  const applied = [];
  const shown = [];
  const context = vm.createContext({
    window: null,
    Promise,
    String,
    Number,
    Math,
    Date,
    Map,
    Uint8Array,
    Array,
    Object,
    Error,
    FREL: "book.epub",
    COUNT: 1,
    _secGen: 11,
    col,
    content,
    secEls: [section],
    NodeFilter: { SHOW_TEXT: 4 },
    document: {
      createTreeWalker(el) {
        let index = 0;
        return { nextNode: () => el.nodes[index++] || null };
      },
      caretRangeFromPoint() {
        return caretAtViewport();
      },
    },
    _ensureLoaded: () => Promise.resolve(true),
    _hls: {},
    reqJson(_method, _url, body, ok) {
      saves.push(JSON.parse(JSON.stringify(body)));
      if (options.hangSave) return;
      ok({
        highlight: {
          id: body.id,
          anchor: body.anchor,
          text: body.text,
          color: body.color,
          time: 123,
        },
      });
    },
    setTimeout: options.setTimeout || globalThis.setTimeout,
    clearTimeout: options.clearTimeout || globalThis.clearTimeout,
    applyHl: (_el, value) => applied.push(value),
    _epShowAction: (value) => shown.push(value),
    crypto: {
      getRandomValues(bytes) {
        nonce += 1;
        for (let i = 0; i < bytes.length; i++) bytes[i] = (32 + nonce + i) & 255;
        return bytes;
      },
    },
  });
  context.window = context;
  context.window.crypto = context.crypto;
  vm.runInContext(
    EPUB.slice(countableStart, countableEnd) + "\n" +
      EPUB.slice(helpersStart, helpersEnd) + "\n" +
      EPUB.slice(apiStart, apiEnd),
    context,
    { filename: "epub-highlight-source-range.js" },
  );
  return {
    context, section, canonical, saves, applied, shown, decorationParent, node,
    setViewportOffset(value) { viewportOffset = value; },
  };
}

function longEpubFixture() {
  const wanted = "repeatable_target_phrase";
  const opening = "a".repeat(1024) + "|" + wanted + "|";
  const middle = "b".repeat(22_000) + "|";
  const lateStart = opening.length + middle.length;
  const text = opening + middle + wanted + "|" + "c".repeat(6000);
  return { text, wanted, lateStart };
}

test("EPUB range offsets ignore injected decoration nodes and save locally extracted text", async () => {
  const { context, section, canonical, saves, applied, shown, decorationParent, node } = epubHarness();
  const beforeValues = section.nodes.map((item) => item.nodeValue);
  const source = await context.window.__bwReaderHighlightSource({
    file: "book.epub",
    target: { kind: "epub", section: 0 },
  });
  assertSourceShape(source, canonical);
  assert.deepEqual(section.nodes.map((item) => item.nodeValue), beforeValues,
    "building marker source must not insert anything into the book DOM");

  const again = await context.window.__bwReaderHighlightSource({
    file: "book.epub", target: { kind: "epub", section: 0 },
  });
  assert.equal(again.snapshotId, source.snapshotId);

  const wanted = "重複句終跨行";
  const first = canonical.indexOf("重複句");
  const start = canonical.indexOf(wanted, first + 1);
  const ref = rangeRef(source, start, start + wanted.length);

  // Decorations may appear after the snapshot; because they are not book
  // content they must neither stale the source nor shift the persisted anchor.
  section.nodes.splice(5, 0, node("new ruby", decorationParent("rt")));
  const result = await context.window.__bwReaderHighlightRange({
    rangeRef: ref,
    color: "green",
    note: "local note",
  });
  assert.equal(result.ok, true);
  assert.equal(result.text, wanted);
  assert.equal(saves.length, 1);
  assert.equal(saves[0].text, wanted);
  assert.deepEqual(saves[0].anchor, { section: 0, start, end: start + wanted.length });
  assert.match(saves[0].id, /^c_[a-f0-9]{16}$/);
  assert.equal(applied.length, 1);
  assert.equal(shown.length, 1);

  const rangeBody = EPUB.slice(
    EPUB.indexOf("window.__bwReaderHighlightRange = function"),
    EPUB.indexOf("function _epubExactSource"),
  );
  assert.doesNotMatch(rangeBody, /_sectionRawText|\.indexOf\(/);
  assert.match(
    EPUB.slice(
      EPUB.indexOf("function _epubSourceState"),
      EPUB.indexOf("function _epubExactSource"),
    ),
    /_countableText\(el\)/,
  );
});

test("EPUB long chapters expose a viewport window and map its markers to absolute offsets", async () => {
  const { text, wanted, lateStart } = longEpubFixture();
  const { context, saves } = epubHarness({ sectionText: text, viewportOffset: lateStart + 4 });
  const source = await context.window.__bwReaderHighlightSource({
    file: "book.epub", target: { kind: "epub", section: 0 },
  });
  const projected = source.markers.map((item) => item.text).join("");
  assertSourceShape(source, projected);
  assert.ok(text.length > 16_384);
  assert.ok(projected.length <= 16_384);
  assert.equal(projected.split(wanted).length - 1, 1,
    "the late viewport window excludes the identical quote near the chapter start");

  const relativeStart = projected.indexOf(wanted);
  assert.ok(relativeStart >= 0, "the visible late occurrence must be present in the source window");
  const result = await context.window.__bwReaderHighlightRange({
    rangeRef: rangeRef(source, relativeStart, relativeStart + wanted.length),
    color: "blue",
    note: "late chapter",
  });

  assert.equal(result.text, wanted);
  assert.equal(saves.length, 1);
  assert.deepEqual(saves[0].anchor, {
    section: 0,
    start: lateStart,
    end: lateStart + wanted.length,
  });
  assert.equal(saves[0].text, wanted);
  const rangeBody = EPUB.slice(
    EPUB.indexOf("window.__bwReaderHighlightRange = function"),
    EPUB.indexOf("function _epubExactSource"),
  );
  assert.doesNotMatch(rangeBody, /\.indexOf\(/,
    "absolute recovery must add the private base offset, not search for the quote");
});

test("EPUB long-chapter snapshots stale when the viewport or off-window body changes", async () => {
  const { text, wanted, lateStart } = longEpubFixture();
  const { context, section, saves, setViewportOffset } = epubHarness({
    sectionText: text,
    viewportOffset: lateStart + 4,
  });
  const input = { file: "book.epub", target: { kind: "epub", section: 0 } };
  const source = await context.window.__bwReaderHighlightSource(input);
  const projected = source.markers.map((item) => item.text).join("");
  const relativeStart = projected.indexOf(wanted);
  const ref = rangeRef(source, relativeStart, relativeStart + wanted.length);

  setViewportOffset(512);
  const moved = await context.window.__bwReaderHighlightSource(input);
  assert.notEqual(moved.snapshotId, source.snapshotId);
  assert.notEqual(moved.sourceDigest, source.sourceDigest);
  await assert.rejects(
    context.window.__bwReaderHighlightRange({ rangeRef: ref, color: "green" }),
    /BW_READER_RANGE_SOURCE_STALE/,
  );

  setViewportOffset(lateStart + 4);
  const restoredView = await context.window.__bwReaderHighlightSource(input);
  const restoredText = restoredView.markers.map((item) => item.text).join("");
  const restoredStart = restoredText.indexOf(wanted);
  const restoredRef = rangeRef(restoredView, restoredStart, restoredStart + wanted.length);
  section.nodes[0].nodeValue = "z" + text.slice(1);
  await assert.rejects(
    context.window.__bwReaderHighlightRange({ rangeRef: restoredRef, color: "green" }),
    /BW_READER_RANGE_SOURCE_STALE/,
  );
  assert.equal(saves.length, 0, "stale windows must never reach persistence");
});

test("EPUB changed content or revision invalidates the opaque marker snapshot", async () => {
  const { context, section, canonical, saves } = epubHarness();
  const input = { file: "book.epub", target: { kind: "epub", section: 0 } };
  const source = await context.window.__bwReaderHighlightSource(input);
  const ref = rangeRef(source, 0, source.markers[0].text.length);
  await assert.rejects(
    context.window.__bwReaderHighlightRange({
      rangeRef: { ...ref, target: { ...ref.target, page: 1 } },
      color: "pink",
    }),
    /BW_READER_RANGE_SOURCE_STALE/,
  );

  const original = section.nodes[0].nodeValue;
  section.nodes[0].nodeValue = "改" + original.slice(1);
  await assert.rejects(
    context.window.__bwReaderHighlightRange({ rangeRef: ref, color: "pink" }),
    /BW_READER_RANGE_SOURCE_STALE/,
  );
  assert.equal(saves.length, 0);
  section.nodes[0].nodeValue = original;

  vm.runInContext(
    `_epubReaderSourceSnapshots.get(${JSON.stringify(source.snapshotId)}).expiresAt = Date.now() + 20000`,
    context,
  );
  const renewed = await context.window.__bwReaderHighlightSource(input);
  assert.notEqual(renewed.snapshotId, source.snapshotId);
  const renewedRef = rangeRef(renewed, 0, renewed.markers[0].text.length);

  context._secGen = 12;
  const revised = await context.window.__bwReaderHighlightSource(input);
  assert.notEqual(revised.snapshotId, source.snapshotId);
  assert.notEqual(revised.sourceDigest, source.sourceDigest,
    "the public digest is bound to the section generation as well as its text window");
  await assert.rejects(
    context.window.__bwReaderHighlightRange({ rangeRef: renewedRef, color: "pink" }),
    /BW_READER_RANGE_SOURCE_STALE/,
  );
});

test("EPUB range persistence has a bounded unknown-outcome failure", async () => {
  let timeoutCallback = null;
  let timeoutMs = 0;
  const { context, canonical, saves } = epubHarness({
    hangSave: true,
    setTimeout(callback, milliseconds) {
      timeoutCallback = callback;
      timeoutMs = milliseconds;
      return 9;
    },
    clearTimeout() {},
  });
  const source = await context.window.__bwReaderHighlightSource({
    file: "book.epub", target: { kind: "epub", section: 0 },
  });
  const pending = context.window.__bwReaderHighlightRange({
    rangeRef: rangeRef(source, 0, canonical.length),
    color: "yellow",
    note: "",
  });
  for (let i = 0; i < 4 && !timeoutCallback; i++) await Promise.resolve();
  assert.equal(timeoutMs, 6000);
  assert.equal(typeof timeoutCallback, "function");
  timeoutCallback();
  await assert.rejects(pending, /BW_READER_HIGHLIGHT_WRITE_TIMEOUT_UNKNOWN/);
  assert.equal(saves.length, 1, "the bounded path sends exactly one mutation and never retries it");
});
