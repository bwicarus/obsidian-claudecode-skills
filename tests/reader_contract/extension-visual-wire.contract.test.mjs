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
    /event\.source === window\.parent[\s\S]*LOCAL_VISUAL_CONTRACT[\s\S]*visualCapturePending\.get/,
  );
  assert.match(
    CALL,
    /sendRequest\("reader-visual", visualChunkFields\(request, \{[\s\S]*status: "chunk"[\s\S]*chunkIndex:[\s\S]*chunkCount,[\s\S]*totalBytes:/,
  );
  assert.match(
    CALL,
    /sendRequest\("reader-visual", visualChunkFields\(request, \{[\s\S]*status: "unavailable"[\s\S]*chunkCount: 0/,
  );
  assert.doesNotMatch(CALL, /reader-visual[\s\S]{0,160}setInterval/);
});

test("content capture is bound to its own call frame and routes scopes to shared capture", () => {
  assert.match(
    CONTENT,
    /function ownCallFrameForSource\(source\)[\s\S]*source !== sourceInstanceId[\s\S]*runtime\.getURL\("call\.html"\)/,
  );
  assert.match(
    CONTENT,
    /var frame = ownCallFrameForSource\(request\.sourceInstanceId\);[\s\S]*event\.source !== frame\.contentWindow/,
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
    /type: "capture-response"[\s\S]*status: ready \? "ready" : "unavailable"[\s\S]*mimeType: ready \? "image\/jpeg" : ""/,
  );
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
    /request\.sourceInstanceId === String\(visualPage\?\.document\?\.sourceInstanceId \|\| ""\)[\s\S]*request\.file === String\(visualPage\?\.url \|\| ""\)[\s\S]*request\.page === 0/,
  );
  assert.match(
    CALL,
    /window\.parent\.postMessage\(local, "\*"\)/,
  );
  assert.match(
    CALL,
    /sendRequest\("reader-browser-control", \{[\s\S]*sessionId:[\s\S]*correlation:[\s\S]*snapshotRevision:[\s\S]*action:[\s\S]*status,[\s\S]*scrollX:[\s\S]*scrollY:[\s\S]*url:[\s\S]*title:/,
  );
  assert.match(
    CONTENT,
    /window\.__bwBrowserControl\.install\(\{[\s\S]*frame: frame,[\s\S]*sourceInstanceId: sourceInstanceId/,
  );
  assert.doesNotMatch(CALL, /\beval\s*\(|\bnew\s+Function\b|location\.(?:assign|replace)/);
});
