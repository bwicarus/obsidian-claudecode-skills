import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => fs.readFileSync(new URL(path, ROOT), "utf8");
const PDF = read("_server_deploy/static/pdf/reader.src/17-highlight.js");
const EPUB = read("_server_deploy/static/pdf/epub-html.js");
const VOICE = read("_server_deploy/static/pdf/rc-voicecall.js");
const FLASH = read("_server_deploy/static/pdf/rc-flashcard.js");
const WINDOWS_OUTPUT = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderRealtimeOutput.cs",
);
const MANIFEST = JSON.parse(read("ios/BWReader/native_reader_interface_manifest.json"));

test("PDF and EPUB exact-text helpers reject wrong or ambiguous source", () => {
  for (const source of [PDF, EPUB]) {
    assert.match(source, /window\.__bwReaderHighlightExactText/);
    assert.match(source, /window\.__bwReaderValidateExactSource/);
    assert.match(source, /BW_READER_SOURCE_(?:TEXT_)?AMBIGUOUS|BW_READER_HIGHLIGHT_TEXT_AMBIGUOUS/);
    assert.match(source, /BW_READER_(?:SOURCE|HIGHLIGHT)_WRONG_BOOK/);
  }
  assert.match(PDF, /id:\s*request\.mutationId/);
  assert.match(EPUB, /id:\s*request\.mutationId/);
});

test("PDF exact-text highlight reuses a ready page and bounds a stalled navigation", async () => {
  const start = PDF.indexOf("async function _pdfExactTextPage(targetPage)");
  const end = PDF.indexOf("window.__bwReaderHighlightExactText", start);
  assert.ok(start >= 0 && end > start);
  const factory = new Function(
    "window",
    "document",
    "pdfDoc",
    "currentPage",
    "setTimeout",
    `${PDF.slice(start, end)}; return _pdfExactTextPage;`,
  );

  const ready = { dataset: { loaded: "1" }, __charBoxes: [{ c: "A" }] };
  let navigationCalls = 0;
  const currentPage = factory(
    { goToPage() { navigationCalls += 1; return new Promise(() => {}); } },
    { querySelector() { return ready; } },
    { numPages: 3 },
    1,
    globalThis.setTimeout,
  );
  assert.equal(await currentPage(1), ready);
  assert.equal(navigationCalls, 0, "a ready current page must not be re-rendered");

  let probes = 0;
  const delayedPage = factory(
    { goToPage() { navigationCalls += 1; return new Promise(() => {}); } },
    { querySelector() { probes += 1; return probes >= 3 ? ready : null; } },
    { numPages: 3 },
    1,
    (callback) => { callback(); },
  );
  assert.equal(await delayedPage(2), ready);
  assert.equal(navigationCalls, 1);
  assert.ok(probes >= 3, "DOM readiness, not a hanging navigation promise, decides success");
});

test("PDF exact-text success waits until its saved rectangle is rendered", () => {
  const start = PDF.indexOf("async function _pdfWaitForHighlightVisible");
  const end = PDF.indexOf("window.__bwReaderHighlightExactText", start);
  assert.ok(start >= 0 && end > start);
  const body = PDF.slice(start, end);
  assert.match(body, /renderHighlightsOnPage\(pw, page\)/);
  assert.match(body, /querySelectorAll\('\.hl-saved'\)/);
  assert.match(body, /node\.dataset\.id === id/);
  assert.match(body, /BW_READER_HIGHLIGHT_NOT_RENDERED/);
  assert.match(PDF, /await _pdfWaitForHighlightVisible\(/);
  assert.match(
    WINDOWS_OUTPUT,
    /DeliveryTimeout = TimeSpan\.FromSeconds\(20\)/,
    "the broker deadline must cover the bounded page and render waits",
  );
});

test("Realtime output waits for exact highlight and rendered Anki draft", () => {
  const start = VOICE.indexOf("function _acceptReaderRealtimeOutput");
  const end = VOICE.indexOf("RC.voicecall =", start);
  const receiver = VOICE.slice(start, end);
  assert.match(receiver, /delivery\.kind === 'highlight-text'/);
  assert.match(receiver, /__bwReaderHighlightExactText\(p\)/);
  assert.match(receiver, /delivery\.kind === 'anki-draft'/);
  assert.match(receiver, /__bwReaderValidateExactSource\(p\)/);
  assert.match(receiver, /fetch\('\/pdf\/api\/anki-draft'/);
  assert.match(receiver, /data\.anki_written !== false/);
  assert.match(receiver, /RC\.flashcard\.presentDraft\(p\.cards, data\.gid\)/);
  assert.ok(receiver.indexOf("_rememberReaderOutput") > receiver.indexOf("Promise.resolve(work)"));
});

test("Anki MCP delivery reuses the existing confirmation-only UI", () => {
  const start = FLASH.indexOf("function presentDraft(cards, gid)");
  const end = FLASH.indexOf("RC.flashcard =", start);
  const present = FLASH.slice(start, end);
  assert.match(present, /mode:\s*'draft'/);
  assert.match(present, /surface:\s*'float'/);
  assert.match(present, /querySelector\('\.fc-add'\)/);
  assert.doesNotMatch(present, /anki-add-cards/);
});

test("native App proxies only the current verified book for draft registration", () => {
  const route = MANIFEST.routes.find((entry) => entry.path === "/pdf/api/anki-draft");
  assert.ok(route);
  assert.equal(route.owner, "pi");
  assert.deepEqual(route.methods, ["POST"]);
  assert.deepEqual(route.surfaces, ["epub", "pdf"]);
  assert.deepEqual(route.remoteBook, {
    mode: "required",
    scope: "current",
    requiredMethods: ["POST"],
    identities: [{
      methods: ["POST"],
      location: "json",
      pointer: "/file",
      transform: "exact",
    }],
    continuation: null,
  });
});
