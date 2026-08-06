import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { webcrypto } from "node:crypto";

const ROOT = new URL("../../", import.meta.url);
const CALL = readFileSync(
  new URL("extensions/bw-reader-webext/call.js", ROOT),
  "utf8",
);
const CONTENT = readFileSync(
  new URL("extensions/bw-reader-webext/content.js", ROOT),
  "utf8",
);
const BACKGROUND = readFileSync(
  new URL("extensions/bw-reader-webext/background.js", ROOT),
  "utf8",
);
const FACADE = readFileSync(
  new URL("extensions/bw-reader-webext/src/facade.js", ROOT),
  "utf8",
);

test("context-only link registers its bound visual source after every handshake", async () => {
  const previous = {
    WebSocket: globalThis.WebSocket,
    btoa: globalThis.btoa,
  };
  const addedCrypto = !globalThis.crypto;
  const requests = [];
  const events = [];
  const sockets = [];

  class FakeWebSocket {
    constructor(url) {
      this.url = url;
      this.readyState = 0;
      sockets.push(this);
      queueMicrotask(() => {
        this.readyState = 1;
        this.onopen?.();
      });
    }

    send(raw) {
      const request = JSON.parse(raw);
      requests.push(request);
      queueMicrotask(() => this.onmessage?.({
        data: JSON.stringify({
          contract: "reader-computer-voice-direct/1",
          type: "response",
          requestId: request.requestId,
          ok: true,
          payload: {},
        }),
      }));
    }

    close() {
      this.readyState = 3;
      this.onclose?.();
    }
  }

  try {
    globalThis.WebSocket = FakeWebSocket;
    if (addedCrypto) {
      Object.defineProperty(globalThis, "crypto", {
        value: webcrypto,
        configurable: true,
      });
    }
    if (!globalThis.btoa) {
      globalThis.btoa = (value) => Buffer.from(value, "binary").toString("base64");
    }
    const { ContextLink } = await import(
      new URL(`../../extensions/bw-reader-webext/ctxlink.js?visual=${Date.now()}`, import.meta.url)
    );
    const link = new ContextLink(() => {}, (message) => events.push(message));
    await link.bindVisualSource("AAAAAAAAAAAAAAAAAAAAAA");
    link.connect();
    await new Promise((resolve) => setTimeout(resolve, 20));

    assert.deepEqual(
      requests.slice(0, 3).map((request) => request.type),
      ["hello", "context-open", "visual-register"],
    );
    assert.deepEqual(
      Object.keys(requests[2]).sort(),
      ["contract", "requestId", "sessionId", "sourceInstanceId", "type"].sort(),
    );
    assert.equal(requests[2].sourceInstanceId, "AAAAAAAAAAAAAAAAAAAAAA");
    assert.equal(link.ready, true);

    const unsolicited = {
      contract: "reader-computer-voice-direct/1",
      type: "event",
      event: "reader-visual-request",
      payload: {},
    };
    sockets[0].onmessage({ data: JSON.stringify(unsolicited) });
    const browserUnsolicited = {
      contract: "reader-computer-voice-direct/1",
      type: "event",
      event: "reader-browser-control-request",
      payload: {},
    };
    sockets[0].onmessage({ data: JSON.stringify(browserUnsolicited) });
    assert.deepEqual(events, [unsolicited, browserUnsolicited]);
    link.close();
  } finally {
    globalThis.WebSocket = previous.WebSocket;
    if (addedCrypto) delete globalThis.crypto;
    if (previous.btoa === undefined) delete globalThis.btoa;
    else globalThis.btoa = previous.btoa;
  }
});

test("call frame accepts only current-source visual pulls and returns strict chunks", () => {
  assert.match(CALL, /VISUAL_DELIVERY_CONTRACT = "reader-visual-delivery\/2"/);
  assert.match(
    CALL,
    /exactKeys\(message, \["contract", "type", "event", "payload"\]\)/,
  );
  assert.match(
    CALL,
    /request\.sourceInstanceId !== visualSourceInstanceId[\s\S]*request\.file !== String\(visualPage\?\.url \|\| ""\)/,
  );
  assert.match(
    CALL,
    /request\.snapshotRevision !== visualCommitted\.snapshotRevision[\s\S]*String\(visualPage\?\.viewKey \|\| ""\) !== visualCommitted\.viewKey/,
  );
  assert.match(
    CALL,
    /const capture = await localVisualCapture\(request\);[\s\S]*request\.file !== String\(visualPage\?\.url \|\| ""\)[\s\S]*request\.file !== visualCommitted\.file/,
    "capture must not return a SPA page that replaced the committed URL while rendering",
  );
  assert.match(
    CALL,
    /function localVisualCapture\(request\)[\s\S]*type: "BW_READER_VISUAL_CAPTURE"[\s\S]*expectedViewKey: visualCommitted\?\.viewKey \|\| ""[\s\S]*20000/,
  );
  assert.match(
    CALL,
    /exactKeys\(data, \[[\s\S]*"correlation", "sourceInstanceId"[\s\S]*data\.correlation !== request\.correlation[\s\S]*data\.sourceInstanceId !== request\.sourceInstanceId/,
  );
  assert.match(
    BACKGROUND,
    /function readerCommittedRequest\(binding, request, expectedViewKey\)[\s\S]*request\.snapshotRevision === binding\.snapshotRevision[\s\S]*expectedViewKey === binding\.viewKey[\s\S]*request\.drawingRevision/,
  );
  assert.match(
    BACKGROUND,
    /message\.type === "BW_READER_VISUAL_CAPTURE"[\s\S]*readerValidateVisualRequest\(message, committed\)[\s\S]*readerTabsMessage\(binding\.tabId, message, 20000\)[\s\S]*readerValidateVisualReply/,
  );
  assert.match(
    BACKGROUND,
    /function readerValidateVisualReply\(result, request\)[\s\S]*result\.correlation !== request\.correlation[\s\S]*result\.sourceInstanceId !== request\.sourceInstanceId[\s\S]*result\.mimeType !== "image\/jpeg"[\s\S]*charCodeAt\(0\) !== 0xff/,
  );
  assert.match(
    CALL,
    /sendRequest\("reader-visual", visualChunkFields\(request, \{[\s\S]*status: "chunk"[\s\S]*chunkIndex:[\s\S]*chunkCount,[\s\S]*totalBytes:/,
  );
  assert.match(
    CALL,
    /sendRequest\("reader-visual", visualChunkFields\(request, \{[\s\S]*status: "unavailable"[\s\S]*chunkCount: 0/,
  );
  assert.doesNotMatch(CALL, /visualCapturePending|type: "capture-request"/);
  assert.doesNotMatch(CALL, /reader-visual[\s\S]{0,160}setInterval/);
});

test("content capture is bound to its own call frame and routes scopes to shared capture", () => {
  assert.match(
    CONTENT,
    /function normalizeLocalVisualRequest\(message\)[\s\S]*exactVisualKeys\(message, \["type", "request", "expectedViewKey"\]\)[\s\S]*request\.sourceInstanceId !== sourceInstanceId[\s\S]*request\.file !== String\(location\.href \|\| ""\)/,
  );
  assert.match(
    CONTENT,
    /function trustedInternalRuntimeSender\(sender\)[\s\S]*if \(!sender \|\| sender\.tab\) return false[\s\S]*sender\.url\.startsWith\(runtime\.getURL\(""\)\)/,
  );
  assert.match(
    CONTENT,
    /runtime\.onMessage\.addListener\(function \(message, sender, sendResponse\)[\s\S]*trustedInternalRuntimeSender\(sender\)[\s\S]*captureVisualForRuntime\(visual\.request, visual\.expectedViewKey\)[\s\S]*sendResponse\(localVisualResponse/,
  );
  assert.match(
    CONTENT,
    /request\.scope === "viewport-context"[\s\S]*RC\.capturePageComposite\(target\)/,
  );
  assert.match(
    CONTENT,
    /request\.scope === "drawing-nearby" \|\| request\.scope === "selection-near"[\s\S]*RC\.captureInkRegion\(target\)/,
  );
  assert.match(
    CONTENT,
    /if \(!visualRequestMatchesLive\(request, expectedViewKey\)\) return null[\s\S]*capturePageComposite[\s\S]*return visualRequestMatchesLive\(request, expectedViewKey\) \? capture : null/,
    "capture must reject a viewport or drawing that changed before or during rendering",
  );
  assert.match(
    CONTENT,
    /function visualRequestMatchesLive\(request, expectedViewKey\)[\s\S]*request\.sourceInstanceId !== sourceInstanceId[\s\S]*request\.file !== String\(location\.href \|\| ""\)/,
    "capture must remain bound to the live source and URL on SPA navigation",
  );
  assert.match(
    CONTENT,
    /function webDrawingState\(\)[\s\S]*"dr_"[\s\S]*drawingRevision: revision/,
    "web ink must publish a stable drawing revision for drawing-nearby",
  );
  assert.match(
    CONTENT,
    /type: "capture-response"[\s\S]*status: ready \? "ready" : "unavailable"[\s\S]*mimeType: ready \? "image\/jpeg" : ""/,
  );
  assert.match(
    CONTENT,
    /function runtimeRequest\(message, timeoutMs\)[\s\S]*runtime\.sendMessage 超时[\s\S]*runtime\.sendMessage\(message, done\)/,
  );
  assert.doesNotMatch(CONTENT, /type: "capture-request"|ownCallFrameForSource/);
});

test("browser control uses only the fixed local executor and returns the strict bridge shape", () => {
  assert.match(CALL, /BROWSER_CONTROL_CONTRACT = "reader-browser-control\/1"/);
  assert.match(
    CALL,
    /message\.event !== "reader-browser-control-request"[\s\S]*exactKeys\(p, \[[\s\S]*"action", "target", "selectionId"/,
  );
  assert.match(
    CALL,
    /const actions = \[[\s\S]*"next-viewport"[\s\S]*"previous-viewport"[\s\S]*"scroll-to-text"[\s\S]*"scroll-to-heading"[\s\S]*"scroll-to-selection"/,
  );
  assert.match(
    CALL,
    /function localBrowserControl\(request, committedViewKey\)[\s\S]*type: "BW_READER_BROWSER_CONTROL"[\s\S]*snapshotRevision: request\.snapshotRevision[\s\S]*expectedViewKey: committedViewKey[\s\S]*10000/,
  );
  assert.match(
    CALL,
    /data\.requestId !== local\.requestId[\s\S]*data\.sourceInstanceId !== local\.sourceInstanceId[\s\S]*data\.action !== local\.action[\s\S]*typeof data\.ok !== "boolean"/,
  );
  assert.match(
    CALL,
    /sendRequest\("reader-browser-control", \{[\s\S]*sessionId:[\s\S]*correlation:[\s\S]*snapshotRevision:[\s\S]*action:[\s\S]*status,[\s\S]*scrollX:[\s\S]*scrollY:[\s\S]*url:[\s\S]*title:/,
  );
  assert.match(
    BACKGROUND,
    /readerCallSender\(sender\)[\s\S]*readerValidateControlRequest\(message, committed\)[\s\S]*readerTabsMessage\(binding\.tabId, message, 10000\)[\s\S]*readerValidateControlReply/,
  );
  assert.match(
    BACKGROUND,
    /function readerValidateControlReply\(result, request\)[\s\S]*result\.requestId !== request\.requestId[\s\S]*result\.sourceInstanceId !== request\.sourceInstanceId[\s\S]*result\.action !== request\.action/,
  );
  assert.match(
    CONTENT,
    /function normalizeLocalBrowserControlRequest\(message\)[\s\S]*"snapshotRevision", "file", "page", "expectedViewKey"[\s\S]*message\.type !== "BW_READER_BROWSER_CONTROL"[\s\S]*request\.sourceInstanceId !== sourceInstanceId/,
  );
  assert.match(
    CONTENT,
    /var executor = window\.__bwBrowserControl;[\s\S]*typeof executor\.execute !== "function"[\s\S]*sendResponse\(executor\.execute\(request\)\)/,
  );
  assert.doesNotMatch(CALL, /window\.parent\.postMessage\(local/);
  assert.doesNotMatch(CALL, /\beval\s*\(|\bnew\s+Function\b|location\.(?:assign|replace)/);
});

test("visual sources are isolated by claimed tab and cannot be inherited by another iframe", () => {
  assert.match(CONTENT, /__bwInlineComputerVoiceSurface[\s\S]*surface\.frameForClaim\(\)[\s\S]*BW_READER_CALL_CLAIM_CREATE[\s\S]*sourceInstanceId: sourceInstanceId[\s\S]*appKind: appKind/);
  assert.doesNotMatch(CONTENT, /querySelector\([^\n]*call\.html/);
  assert.match(FACADE, /frameForClaim: function \(\) \{[\s\S]*frame\.isConnected \? frame : null/);
  assert.match(
    CONTENT,
    /new Uint8Array\(32\)[\s\S]*BW_READER_CALL_CLAIM_CREATE[\s\S]*sourceInstanceId: sourceInstanceId[\s\S]*frame\.contentWindow\.postMessage\(\{[\s\S]*bw-reader-call-claim\/1/,
  );
  assert.match(
    CALL,
    /event\.source !== window\.parent[\s\S]*bw-reader-call-claim\/1[\s\S]*BW_READER_CALL_CLAIM_BIND[\s\S]*refreshVisualContext\(\)/,
  );
  assert.match(CALL, /reply\.data\?\.appKind[\s\S]*frameAppKind = reply\.data\.appKind/);
  assert.doesNotMatch(CALL, /d\.type !== "configure"|host through configure/);
  assert.doesNotMatch(FACADE, /type: 'configure'|const configure =/);
  assert.match(BACKGROUND, /readerPendingCallClaims = new Map\(\)/);
  assert.match(BACKGROUND, /readerBoundCallFrames = new Map\(\)/);
  assert.match(
    BACKGROUND,
    /bound\.frameId !== sender\.frameId[\s\S]*bound\.documentId !== String\(sender\.documentId \|\| ""\)[\s\S]*bound\.tabUrl !== tabUrl/,
  );
  assert.match(BACKGROUND, /sourceInstanceId: pending\.sourceInstanceId/);
  assert.match(
    BACKGROUND,
    /message\.type === "BW_READER_CALL_CLAIM_BIND"[\s\S]*pending\.expiresAt < Date\.now\(\)[\s\S]*message\.capability !== pending\.capability[\s\S]*readerPendingCallClaims\.delete\(binding\.tabId\)[\s\S]*appKind: pending\.appKind/,
  );
  assert.match(
    BACKGROUND,
    /function readerCallSender\(sender, requireClaim = true\)[\s\S]*sender\.url !== base \+ "\?compact=1"[\s\S]*readerBoundCallFrames\.get\(sender\.tab\.id\)/,
  );
  assert.match(
    BACKGROUND,
    /READER_CONTEXT_VISUAL_KEY = "bwReaderVisualBindingsV1"[\s\S]*readerStoredVisualBinding\([\s\S]*binding\.sourceInstanceId/,
  );
  assert.match(
    BACKGROUND,
    /async function readerPersistVisualBinding\(tabId, visualContext\)[\s\S]*next\[String\(tabId\)\] = visualContext/,
  );
  assert.match(
    BACKGROUND,
    /function readerTabsMessage\(tabId, message, timeoutMs\)[\s\S]*顶层网页响应超时[\s\S]*chrome\.tabs\.sendMessage\(tabId, message, \{ frameId: 0 \}, done\)/,
  );
  assert.match(
    CALL,
    /function runtimeRequest\(message, timeoutMs = 15000\)[\s\S]*runtime\.sendMessage 超时[\s\S]*chrome\.runtime\.sendMessage\(message, done\)/,
  );
});

test("a late tab URL event preserves a new document claim before its first snapshot commits", async () => {
  const start = BACKGROUND.indexOf("function readerDropTabState(tabId, removeAny)");
  const end = BACKGROUND.indexOf("if (chrome.tabs?.onRemoved?.addListener)", start);
  assert.ok(start >= 0 && end > start, "tab cleanup helpers must remain extractable");

  const deleted = [];
  const harness = new Function(
    "readerSafeUrl",
    "readerVisualContext",
    "readerDeleteVisualBinding",
    `
      const readerContextStateByTab = new Map();
      const readerPendingCallClaims = new Map();
      const readerBoundCallFrames = new Map();
      let readerContextPostQueue = Promise.resolve();
      ${BACKGROUND.slice(start, end)}
      return {
        readerContextStateByTab,
        readerPendingCallClaims,
        readerBoundCallFrames,
        readerHandleTabUrlUpdate,
        waitForCleanup: () => readerContextPostQueue,
      };
    `,
  )(
    (value) => {
      try {
        const parsed = new URL(String(value || ""));
        return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : null;
      } catch (_) {
        return null;
      }
    },
    (value) => value || null,
    async (tabId, expectedUrl, removeAny) => deleted.push({ tabId, expectedUrl, removeAny }),
  );

  const tabId = 17;
  const oldUrl = "https://example.test/old";
  const nextUrl = "https://example.test/new";
  const nextSource = "BBBBBBBBBBBBBBBBBBBBBB";
  harness.readerContextStateByTab.set(tabId, {
    visualContext: { identity: { file: oldUrl, sourceInstanceId: "AAAAAAAAAAAAAAAAAAAAAA" } },
  });
  harness.readerBoundCallFrames.set(tabId, {
    frameId: 2,
    documentId: "new-document",
    tabUrl: nextUrl,
    sourceInstanceId: nextSource,
  });
  harness.readerPendingCallClaims.set(tabId, {
    capability: "claim",
    tabUrl: nextUrl,
    sourceInstanceId: nextSource,
  });

  harness.readerHandleTabUrlUpdate(tabId, nextUrl);
  await harness.waitForCleanup();

  assert.equal(harness.readerContextStateByTab.has(tabId), false, "old visual state must retire");
  assert.equal(harness.readerBoundCallFrames.get(tabId)?.sourceInstanceId, nextSource);
  assert.equal(harness.readerPendingCallClaims.get(tabId)?.sourceInstanceId, nextSource);
  assert.deepEqual(deleted, [{ tabId, expectedUrl: oldUrl, removeAny: false }]);
});

test("selection region receiver accepts stable increasing ordinals with deletion gaps", () => {
  const start = BACKGROUND.indexOf("function readerValidateRegions(raw)");
  const end = BACKGROUND.indexOf("function readerValidateVisual(raw, url)", start);
  assert.ok(start >= 0 && end > start, "selection region validator must remain extractable");
  const validate = new Function(
    "readerExactKeys",
    "readerSafeId",
    "readerRelayError",
    `${BACKGROUND.slice(start, end)}; return readerValidateRegions;`,
  )(
    (value, keys) => value && typeof value === "object" && !Array.isArray(value) &&
      Object.keys(value).sort().join("\0") === keys.slice().sort().join("\0"),
    (value) => typeof value === "string" && /^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(value),
    (code, message) => Object.assign(new Error(message), { code }),
  );
  const stable = {
    contract: "reader-selection-regions/1",
    total: 2,
    truncated: false,
    items: [
      { selectionId: "region-b", label: "#2 12:01", ordinal: 2, createdAtEpochMs: 2000 },
      { selectionId: "region-c", label: "#3 12:02", ordinal: 3, createdAtEpochMs: 3000 },
    ],
  };
  assert.deepEqual(
    validate(stable).items.map((item) => item.ordinal),
    [2, 3],
  );
  assert.throws(
    () => validate({
      ...stable,
      items: [stable.items[1], stable.items[0]],
    }),
    /页面选区条目无效/,
    "ordinals must still be strictly increasing",
  );
});

test("Safari callback and Promise storage APIs settle once without duplicate writes", async () => {
  const start = BACKGROUND.indexOf("function readerStorageGet(key)");
  const end = BACKGROUND.indexOf("async function readerStoredVisualBinding", start);
  assert.ok(start >= 0 && end > start, "storage compatibility helpers must remain extractable");

  const chrome = {
    runtime: { lastError: null },
    storage: {
      local: {
        getCalls: 0,
        setCalls: 0,
        writes: [],
        get(key, done) {
          this.getCalls += 1;
          done({ [key]: "callback-wins" });
          return Promise.resolve({ [key]: "promise-loses" });
        },
        set(value, done) {
          this.setCalls += 1;
          this.writes.push(value);
          done();
          return Promise.resolve();
        },
      },
    },
  };
  const readerRelayError = (code, message) => Object.assign(new Error(message), { code });
  const helpers = new Function(
    "chrome",
    "readerRelayError",
    `${BACKGROUND.slice(start, end)}; return { readerStorageGet, readerStorageSet };`,
  )(chrome, readerRelayError);

  const read = await helpers.readerStorageGet("probe");
  await helpers.readerStorageSet({ probe: "value" });
  await new Promise((resolve) => setTimeout(resolve, 0));

  assert.deepEqual(read, { probe: "callback-wins" });
  assert.equal(chrome.storage.local.getCalls, 1);
  assert.equal(chrome.storage.local.setCalls, 1);
  assert.deepEqual(chrome.storage.local.writes, [{ probe: "value" }]);
});
