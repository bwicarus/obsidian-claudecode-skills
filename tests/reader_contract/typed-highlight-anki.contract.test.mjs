import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";

const ROOT = new URL("../../", import.meta.url);
const read = (path) => fs.readFileSync(new URL(path, ROOT), "utf8");
const PDF = read("_server_deploy/static/pdf/reader.src/17-highlight.js");
const EPUB = read("_server_deploy/static/pdf/epub-html.js");
const VOICE = read("_server_deploy/static/pdf/rc-voicecall.js");
const FLASH = read("_server_deploy/static/pdf/rc-flashcard.js");
const COMPUTER = read("_server_deploy/static/pdf/rc-computer-voice.js");
const WINDOWS_OUTPUT = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderRealtimeOutput.cs",
);
const WINDOWS_OUTPUT_RPC = read(
  "extensions/bw-reader-webext/windows/ComputerVoiceAudio/ReaderRealtimeOutputRpc.cs",
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

test("EPUB exact-text bridge highlight shows the existing offset-action card after persistence", async () => {
  const start = EPUB.indexOf("window.__bwReaderHighlightExactText = function (request) {");
  const end = EPUB.indexOf("\n\n  // ── 「第N章」", start);
  assert.ok(start >= 0 && end > start);
  const shown = [];
  const applied = [];
  const highlight = {
    id: "c_1234567890abcdef",
    anchor: { section: 3, start: 4, end: 12 },
    text: "橋接高亮",
    color: "#fff59d",
    time: 123,
  };
  const action = {
    id: "direct-highlight:" + highlight.id,
    kind: "epub_highlight",
    title: "高亮:1处",
    detail: "· 橋接高亮",
    undo: { op: "hl_delete", file: "book.epub", ids: [highlight.id] },
    redo: { op: "hl_create", file: "book.epub", items: [highlight] },
    state: "done",
    ts: 123,
  };
  const context = vm.createContext({
    window: null,
    Promise,
    String,
    Number,
    Math,
    Date,
    FREL: "book.epub",
    _hls: {},
    _epubExactSource: () => Promise.resolve({
      section: 3,
      offset: { start: 4, end: 12 },
      element: { id: "section-3" },
    }),
    reqJson: (_method, _url, _body, ok) => ok({ highlight, action }),
    applyHl: (_element, value) => { applied.push(value.id); },
    _epShowAction: (value) => { shown.push(JSON.parse(JSON.stringify(value))); },
    out: null,
  });
  context.window = context;
  vm.runInContext(EPUB.slice(start, end), context, { filename: "epub-exact-highlight.js" });
  context.out = context.window.__bwReaderHighlightExactText({
    file: "book.epub",
    target: { kind: "epub", section: 3 },
    text: "橋接高亮",
    color: "yellow",
    note: "",
    mutationId: highlight.id,
  });
  const result = await context.out;
  assert.equal(result.status, "highlight_saved");
  assert.deepEqual(applied, [highlight.id]);
  assert.deepEqual(shown, [action]);
  assert.equal(shown[0].kind, "epub_highlight");
  assert.deepEqual(shown[0].redo.items[0].anchor, highlight.anchor);
  assert.equal(JSON.stringify(shown[0]).includes("rects"), false);
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
  const brokerSeconds = Number(
    WINDOWS_OUTPUT.match(/DeliveryTimeout = TimeSpan\.FromSeconds\((\d+)\)/)?.[1],
  );
  const rpcSeconds = Number(
    WINDOWS_OUTPUT_RPC.match(/ExchangeTimeout = TimeSpan\.FromSeconds\((\d+)\)/)?.[1],
  );
  assert.ok(Number.isFinite(brokerSeconds) && Number.isFinite(rpcSeconds));
  assert.ok(
    rpcSeconds > brokerSeconds,
    "the named-pipe caller must not abandon a still-valid broker operation",
  );
});

test("Realtime output waits for exact highlight and rendered Anki draft", () => {
  const start = VOICE.indexOf("function _applyReaderRealtimeOutput(delivery)");
  const end = VOICE.indexOf("function _acceptReaderRealtimeOutput(delivery)", start);
  assert.ok(start >= 0 && end > start);
  const receiver = VOICE.slice(start, end);
  assert.match(receiver, /delivery\.kind === 'highlight-text'/);
  assert.match(receiver, /__bwReaderHighlightExactText\(p\)/);
  assert.match(receiver, /delivery\.kind === 'anki-draft'/);
  assert.match(receiver, /_readerDraftSourceMode\(p\)/);
  assert.match(receiver, /_draftSourceMode === 'exact'/);
  assert.match(receiver, /__bwReaderValidateExactSource\(p\)/);
  assert.match(receiver, /Promise\.resolve\(\{ ok: true, generic: true \}\)/);
  assert.match(receiver, /_readerDraftGid\(p\.draftId\)/);
  assert.match(VOICE, /crypto\.subtle\.digest\(\s*'SHA-256'/);
  // source 现在只构造一次，浮层与对话流两个宿主共用同一份 —— 分别构造就可能
  // 悄悄产生差异，而差异在仓库里表现为两个实体。
  assert.match(receiver, /var draftSource = _readerDraftSource\(/);
  assert.match(receiver, /repositorySource: draftSource/);
  assert.match(receiver, /entityRegistered:\s*false/);
  assert.match(receiver, /var draftLocal = \{/);
  assert.match(receiver, /localDraft: draftLocal/);
  assert.match(receiver, /sourceInstanceId:\s*delivery\.sourceInstanceId/);
  assert.match(receiver, /RC\.flashcard\.presentDraft\(\s*p\.cards,\s*gid,/);
  assert.match(receiver, /repository:\s*'local'/);
  assert.doesNotMatch(receiver, /fetch\('\/pdf\/api\/anki-draft'/);
  assert.doesNotMatch(receiver, /addLocalAnkiCard/);
  assert.ok(receiver.indexOf("_rememberReaderOutput") > receiver.indexOf("Promise.resolve(work)"));
});

test("Anki MCP delivery registers exact or generic drafts before rendering confirmation UI", () => {
  const start = FLASH.indexOf("function presentDraft(cards, gid, options)");
  const end = FLASH.indexOf("RC.flashcard =", start);
  const present = FLASH.slice(start, end);
  assert.match(present, /repo\.registerDraft\(\{/);
  assert.match(present, /var source = options\.repositorySource/);
  assert.match(present, /kind:\s*'reader-generated-card-draft'/);
  assert.match(present, /source:\s*source/);
  assert.match(present, /requireDraftIdForReplay:\s*true/);
  assert.match(present, /mode:\s*'draft'/);
  assert.match(present, /surface:\s*options\.host \? 'inflow' : 'float'/);
  assert.match(present, /querySelector\('\.fc-card'\)/);
  assert.doesNotMatch(present, /anki-add-cards/);
});

test("local repository is authoritative while the Pi entity remains a legacy fallback", () => {
  assert.match(
    FLASH,
    /repo\.load\(st\.gid\)[\s\S]*if \(record\) return applyRepositoryRecord/,
    "state restore must consult the local repository first",
  );
  assert.match(
    FLASH,
    /if \(!st \|\| st\.opts\.entityRegistered === false\) return Promise\.resolve\(false\)/,
    "a local-only entity must never be patched into the legacy Pi registry",
  );
  assert.match(FLASH, /saveConfirmedCard\(request, \{ mutationId: mutationId \}\)/);
  assert.match(FLASH, /id:\s*st\.gid,[\s\S]*cid:\s*st\.gid,[\s\S]*gid:\s*st\.gid/);
  assert.match(FLASH, /recordAnkiReceipt\(/);
});

test("draft confirmation is local-first and ReaderPC is an optional projection", () => {
  const add = FLASH.slice(
    FLASH.indexOf("function addToAnki"),
    FLASH.indexOf("function _recordExternalReceipt"),
  );
  assert.match(add, /var request\s*=\s*\{/);
  assert.match(add, /repo\.saveConfirmedCard\(request, \{ mutationId: mutationId \}\)/);
  assert.match(add, /cardIndex:\s*i/);
  assert.match(add, /cards:\s*repositoryCards\(st\.cards\)/);
  assert.match(FLASH, /✓ 已保存到 Reader 本地卡库/);
  assert.doesNotMatch(add, /addLocalAnkiCard|anki-add-cards/);

  const optionalExport = FLASH.slice(
    FLASH.indexOf("function exportToComputerAnki"),
    FLASH.indexOf("function exportToMobileAnki"),
  );
  const durablePending = optionalExport.indexOf("_recordExternalReceipt(st, i, 'readerpc', pending");
  const externalWrite = optionalExport.indexOf("RC.computerVoice.addLocalAnkiCard({");
  assert.ok(durablePending >= 0 && externalWrite > durablePending);
  assert.match(optionalExport, /draftId:\s*draft\.draftId/);
  assert.match(optionalExport, /sourceInstanceId:\s*draft\.sourceInstanceId/);
  assert.match(optionalExport, /cardIndex:\s*i/);
  assert.match(optionalExport, /repositoryCard\(c\)/);
  assert.match(optionalExport, /c\._pcExportStatus === 'unknown'/);
  assert.match(optionalExport, /电脑 Anki 接收结果未知，已阻止重复发送/);

  const local = COMPUTER.slice(
    COMPUTER.indexOf("function normalizeLocalAnkiCard"),
    COMPUTER.indexOf("function offlineAvailability"),
  );
  const channel = COMPUTER.slice(
    COMPUTER.indexOf("function acquireFreshAnkiChannel"),
    COMPUTER.indexOf("function lookupJapaneseFallback"),
  );
  assert.match(local, /acquireFreshAnkiChannel\(\)/);
  assert.match(channel, /channel\.request\("context-open"/);
  assert.match(channel, /value\.state !== "context-only"/);
  assert.match(local, /sessionId:\s*acquired\.sessionId/);
  assert.match(local, /BW_READER_LOCAL_ANKI_CHANNEL_UNAVAILABLE/);
  assert.match(local, /"anki-add-cards-local"/);
  assert.match(local, /LOCAL_ANKI_ADD_TIMEOUT_MS/);
  assert.doesNotMatch(local, /\/pdf\/api\/anki-add-cards/);
  assert.match(COMPUTER, /addLocalAnkiCard:\s*addLocalAnkiCard/);
});

test("generic make_anki does not expose a saveable card before local draft persistence", () => {
  const immediate = VOICE.slice(
    VOICE.indexOf("if (_sc) {"),
    VOICE.indexOf("// 后台任务", VOICE.indexOf("if (_sc) {")),
  );
  const immediatePersist = immediate.indexOf("RC.flashcard.presentDraft(_sc, _gid");
  const immediatePart = immediate.indexOf("RC.turnCard.addPart(_stid", immediatePersist);
  assert.ok(immediatePersist >= 0 && immediatePart > immediatePersist);
  assert.match(immediate, /卡片草稿未写入本地仓库，未显示可保存卡片/);

  const background = VOICE.slice(
    VOICE.indexOf("if (stt === 'done'"),
    VOICE.indexOf("} else if (stt === 'error')"),
  );
  const backgroundPersist = background.indexOf("RC.flashcard.presentDraft(_cds, _gid2");
  const backgroundPart = background.indexOf("RC.turnCard.addPart(_turnTid", backgroundPersist);
  assert.ok(backgroundPersist >= 0 && backgroundPart > backgroundPersist);
  assert.match(background, /卡片草稿未写入本地仓库，未显示可保存卡片/);
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
