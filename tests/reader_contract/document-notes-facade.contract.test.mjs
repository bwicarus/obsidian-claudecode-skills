import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(HERE, "../..");
const FACADE_PATH = path.join(
  ROOT,
  "extensions/bw-reader-webext/src/facade.js",
);
const SOURCE = fs.readFileSync(FACADE_PATH, "utf8");
const FACTORY_START = SOURCE.indexOf(
  "  function createDocumentNotesTransport(environment) {",
);
const FACTORY_END = SOURCE.indexOf(
  "\n  window.__bwDocumentNotes = createDocumentNotesTransport({",
  FACTORY_START,
);
assert.ok(FACTORY_START >= 0 && FACTORY_END > FACTORY_START);
const FACTORY_SOURCE = SOURCE.slice(FACTORY_START, FACTORY_END);
const SCOPE = `document-notes-scope-v1-${"a".repeat(64)}`;

function loadFactory() {
  const sandbox = {
    URL,
    Error,
    JSON,
    Object,
    Promise,
    Reflect,
    Set,
    Map,
    String,
  };
  vm.runInNewContext(
    `${FACTORY_SOURCE}\nglobalThis.factory = createDocumentNotesTransport;`,
    sandbox,
    { filename: "facade-document-notes-transport.js" },
  );
  return sandbox.factory;
}

function makePort() {
  const messageListeners = [];
  const disconnectListeners = [];
  return {
    messages: [],
    disconnected: false,
    onMessage: {
      addListener(listener) {
        messageListeners.push(listener);
      },
    },
    onDisconnect: {
      addListener(listener) {
        disconnectListeners.push(listener);
      },
    },
    postMessage(message) {
      this.messages.push(message);
    },
    disconnect() {
      if (this.disconnected) return;
      this.disconnected = true;
      for (const listener of [...disconnectListeners]) listener();
    },
    receive(message) {
      for (const listener of [...messageListeners]) listener(message);
    },
  };
}

function makeWindow(href = "https://example.com/article?chapter=1#intro") {
  const listeners = new Map();
  const targetWindow = {
    location: { href },
    structuredClone,
    setTimeout,
    clearTimeout,
    setInterval,
    clearInterval,
    addEventListener(type, listener) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(listener);
    },
    dispatch(type) {
      for (const listener of [...(listeners.get(type) || [])]) {
        listener({ type });
      }
    },
  };
  targetWindow.history = {
    pushState(_state, _title, next) {
      if (next != null) {
        targetWindow.location.href = new URL(
          String(next),
          targetWindow.location.href,
        ).href;
      }
    },
    replaceState(_state, _title, next) {
      if (next != null) {
        targetWindow.location.href = new URL(
          String(next),
          targetWindow.location.href,
        ).href;
      }
    },
  };
  return targetWindow;
}

function harness(href, options = {}) {
  const ports = [];
  const targetWindow = makeWindow(href);
  const chrome = {
    runtime: {
      lastError: null,
      connect(options) {
        assert.equal(options?.name, "bw-document-notes");
        const port = makePort();
        ports.push(port);
        return port;
      },
    },
  };
  const transport = loadFactory()({
    window: targetWindow,
    chrome,
    ...options,
  });
  return { chrome, ports, targetWindow, transport };
}

const protocolMessage = (message) => ({
  protocol: "bw-document-notes/1",
  ...message,
});

const clean = (value) => JSON.parse(JSON.stringify(value));

async function waitFor(predicate, timeoutMs = 1000) {
  const started = Date.now();
  while (!predicate()) {
    if (Date.now() - started > timeoutMs) {
      throw new Error("condition timed out");
    }
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
}

async function ready(h, expectedHref = h.targetWindow.location.href) {
  const identityPromise = h.transport.identity();
  assert.equal(h.ports.length, 1);
  const url = new URL(expectedHref);
  url.hash = "";
  const documentId = `web:${url.href}`;
  h.ports[0].receive(protocolMessage({
    type: "READY",
    documentId,
    scope: SCOPE,
  }));
  assert.deepEqual(clean(await identityPromise), { documentId, scope: SCOPE });
  return documentId;
}

async function lastCall(port, count) {
  await waitFor(() => port.messages.length >= count);
  return port.messages[count - 1];
}

test("provider-only guard precedes every extension transport side effect", () => {
  const guard = SOURCE.indexOf("if (window.__bwPwaProviderOnly) return;");
  const localStore = SOURCE.indexOf("const localStoreCall");
  const documentNotes = SOURCE.indexOf("createDocumentNotesTransport");
  assert.ok(guard > 0);
  assert.ok(guard < localStore);
  assert.ok(guard < documentNotes);
  const providerWindow = { __bwPwaProviderOnly: true };
  vm.runInNewContext(SOURCE, { window: providerWindow }, {
    filename: "facade-provider-only.js",
  });
  assert.equal(providerWindow.__bwDocumentNotes, undefined);
});

test("maps public API calls without accepting a caller-selected document identity", async () => {
  const h = harness();
  const documentId = await ready(h);
  const port = h.ports[0];

  const newIdPromise = h.transport.newId();
  const newIdCall = await lastCall(port, 1);
  assert.deepEqual(clean(newIdCall), {
    protocol: "bw-document-notes/1",
    type: "CALL",
    id: newIdCall.id,
    operation: "NEW_ID",
    payload: {},
  });
  port.receive(protocolMessage({
    type: "RESULT",
    id: newIdCall.id,
    ok: true,
    data: "c_0123456789abcdef0123456789abcdef",
  }));
  assert.equal(
    await newIdPromise,
    "c_0123456789abcdef0123456789abcdef",
  );

  const listPromise = h.transport.list({ includeDeleted: true });
  const listCall = await lastCall(port, 2);
  assert.deepEqual(clean(listCall.payload), {
    query: { includeDeleted: true },
  });
  port.receive(protocolMessage({
    type: "RESULT",
    id: listCall.id,
    ok: true,
    data: [],
  }));
  assert.deepEqual(await listPromise, []);

  const getPromise = h.transport.get("note-1", { includeDeleted: false });
  const getCall = await lastCall(port, 3);
  assert.deepEqual(clean(getCall.payload), {
    noteId: "note-1",
    query: { includeDeleted: false },
  });
  port.receive(protocolMessage({
    type: "RESULT",
    id: getCall.id,
    ok: true,
    data: { documentId, noteId: "note-1", rev: 1 },
  }));
  assert.equal((await getPromise).documentId, documentId);

  await assert.rejects(
    h.transport.list({ documentId }),
    (cause) => cause.code === "BW_DOCUMENT_NOTES_IDENTITY",
  );
  assert.equal(port.messages.length, 3);
});

test("validates the shared anchor envelope and strips only its trusted identity copy", async () => {
  const h = harness();
  const documentId = await ready(h);
  const port = h.ports[0];
  const input = {
    documentId,
    anchor: {
      documentId,
      kind: "web-dom",
      revision: 1,
      data: { selector: "#main > p:nth-child(2)" },
    },
    fields: { text: "hello" },
  };
  const createPromise = h.transport.create(input, {
    mutationId: "create:note-1",
  });
  const createCall = await lastCall(port, 1);
  assert.equal(input.anchor.documentId, documentId);
  assert.equal(input.documentId, documentId);
  assert.equal(
    Object.hasOwn(createCall.payload.input, "documentId"),
    false,
  );
  assert.equal(
    Object.hasOwn(createCall.payload.input.anchor, "documentId"),
    false,
  );
  assert.deepEqual(clean(createCall.payload.input.anchor), {
    kind: "web-dom",
    revision: 1,
    data: { selector: "#main > p:nth-child(2)" },
  });
  port.receive(protocolMessage({
    type: "RESULT",
    id: createCall.id,
    ok: true,
    data: { documentId, noteId: "note-1", rev: 1 },
  }));
  await createPromise;

  const changes = {
    anchor: {
      documentId,
      kind: "web-dom",
      revision: 1,
      data: { selector: "#main > p:nth-child(3)" },
    },
    fields: { text: "updated" },
  };
  const patchPromise = h.transport.patch("note-1", changes, {
    ifRev: 1,
    mutationId: "patch:note-1:1",
  });
  const patchCall = await lastCall(port, 2);
  assert.equal(changes.anchor.documentId, documentId);
  assert.equal(
    Object.hasOwn(patchCall.payload.changes.anchor, "documentId"),
    false,
  );
  port.receive(protocolMessage({
    type: "RESULT",
    id: patchCall.id,
    ok: true,
    data: { documentId, noteId: "note-1", rev: 2 },
  }));
  await patchPromise;

  await assert.rejects(
    h.transport.create({
      documentId,
      anchor: {
        documentId: "web:https://attacker.invalid/",
        kind: "web-dom",
        revision: 1,
        data: {},
      },
    }, { mutationId: "bad-create" }),
    (cause) => cause.code === "BW_DOCUMENT_NOTES_IDENTITY",
  );
  await assert.rejects(
    h.transport.create({
      documentId: "web:https://attacker.invalid/",
      anchor: {
        documentId,
        kind: "web-dom",
        revision: 1,
        data: {},
      },
    }, { mutationId: "bad-create-top-level" }),
    (cause) => cause.code === "BW_DOCUMENT_NOTES_IDENTITY",
  );
  await assert.rejects(
    h.transport.patch("note-1", {
      documentId,
      fields: { text: "bad" },
    }, { ifRev: 2, mutationId: "bad-patch" }),
    (cause) => cause.code === "BW_DOCUMENT_NOTES_IDENTITY",
  );
  assert.equal(port.messages.length, 2);
});

test("rejects a stale READY and retries briefly against the same page generation", async () => {
  const h = harness("https://example.com/old#part");
  const identityPromise = h.transport.identity();
  h.ports[0].receive(protocolMessage({
    type: "READY",
    documentId: "web:https://example.com/stale",
    scope: SCOPE,
  }));
  assert.equal(h.ports[0].disconnected, true);
  await waitFor(() => h.ports.length === 2);
  h.ports[1].receive(protocolMessage({
    type: "READY",
    documentId: "web:https://example.com/old",
    scope: SCOPE,
  }));
  assert.deepEqual(clean(await identityPromise), {
    documentId: "web:https://example.com/old",
    scope: SCOPE,
  });
});

test("SPA identity change immediately rejects old calls once and reconnects", async () => {
  const h = harness("https://example.com/chapter/1#top");
  const oldDocumentId = await ready(h);
  const oldPort = h.ports[0];
  const invalidations = [];
  const unsubscribeInvalidation = h.transport.onInvalidate((event) => {
    invalidations.push(event);
  });

  const pendingList = h.transport.list({});
  const oldCall = await lastCall(oldPort, 1);
  h.targetWindow.history.pushState({}, "", "/chapter/2#section");
  await assert.rejects(
    pendingList,
    (cause) => cause.code === "BW_DOCUMENT_NOTES_STALE",
  );
  assert.equal(oldPort.disconnected, true);
  assert.equal(invalidations.length, 1);
  assert.equal(
    invalidations[0].previousIdentity.documentId,
    oldDocumentId,
  );
  assert.equal(invalidations[0].reason, "history.pushState");

  // A late result from the old port cannot settle or leak into the new page.
  oldPort.receive(protocolMessage({
    type: "RESULT",
    id: oldCall.id,
    ok: true,
    data: [{ documentId: oldDocumentId, noteId: "stale", rev: 1 }],
  }));

  await waitFor(() => h.ports.length === 2);
  h.ports[1].receive(protocolMessage({
    type: "READY",
    documentId: "web:https://example.com/chapter/2",
    scope: SCOPE,
  }));
  assert.deepEqual(clean(await h.transport.identity()), {
    documentId: "web:https://example.com/chapter/2",
    scope: SCOPE,
  });

  h.targetWindow.history.replaceState({}, "", "#other-hash");
  h.targetWindow.dispatch("hashchange");
  assert.equal(invalidations.length, 1);
  assert.equal(h.ports.length, 2);
  unsubscribeInvalidation();
});

test("location-at-call fences a main-world SPA change without relying on history wrappers", async () => {
  const h = harness("https://example.com/main-world/old#top");
  const oldDocumentId = await ready(h);
  const oldPort = h.ports[0];

  const oldPending = h.transport.get("old-note", {});
  const oldCall = await lastCall(oldPort, 1);

  // Simulate pushState in the page's main world. Content scripts run in an
  // isolated world, so their monkeypatch of history is not called. Chromium's
  // background port sender also remains immutable for this existing port.
  h.targetWindow.location.href =
    "https://example.com/main-world/new?chapter=2#section";

  const newCallPromise = h.transport.newId();
  await assert.rejects(
    oldPending,
    (cause) => cause.code === "BW_DOCUMENT_NOTES_STALE",
  );
  assert.equal(oldPort.disconnected, true);
  assert.equal(oldPort.messages.length, 1);

  // A late RESULT from the immutable old connection cannot revive the request.
  oldPort.receive(protocolMessage({
    type: "RESULT",
    id: oldCall.id,
    ok: true,
    data: { documentId: oldDocumentId, noteId: "old-note", rev: 1 },
  }));

  await waitFor(() => h.ports.length === 2);
  const newPort = h.ports[1];
  newPort.receive(protocolMessage({
    type: "READY",
    documentId:
      "web:https://example.com/main-world/new?chapter=2",
    scope: SCOPE,
  }));
  const newCall = await lastCall(newPort, 1);
  assert.equal(newCall.operation, "NEW_ID");
  assert.equal(oldPort.messages.length, 1);
  newPort.receive(protocolMessage({
    type: "RESULT",
    id: newCall.id,
    ok: true,
    data: "c_0123456789abcdef0123456789abcdef",
  }));
  assert.equal(
    await newCallPromise,
    "c_0123456789abcdef0123456789abcdef",
  );
});

test("low-frequency location fence catches a silent main-world SPA route", async () => {
  const h = harness("https://example.com/silent/old#top", {
    locationPollMs: 1,
  });
  const oldDocumentId = await ready(h);
  const oldPort = h.ports[0];
  const invalidations = [];
  h.transport.onInvalidate((event) => invalidations.push(event));

  // No history wrapper, Navigation API event, popstate, hashchange or DOM
  // mutation: this is the Safari/older Firefox blind spot.
  h.targetWindow.location.href =
    "https://example.com/silent/new?chapter=2#ignored";

  await waitFor(() => oldPort.disconnected);
  assert.equal(invalidations.length, 1);
  assert.equal(invalidations[0].reason, "location-poll");
  assert.equal(
    invalidations[0].previousIdentity.documentId,
    oldDocumentId,
  );

  await waitFor(() => h.ports.length === 2);
  h.ports[1].receive(protocolMessage({
    type: "READY",
    documentId: "web:https://example.com/silent/new?chapter=2",
    scope: SCOPE,
  }));
  assert.deepEqual(clean(await h.transport.identity()), {
    documentId: "web:https://example.com/silent/new?chapter=2",
    scope: SCOPE,
  });
});

test("location is rechecked after connect and before an old-port CALL", async () => {
  const h = harness("https://example.com/race/old");
  await ready(h);
  const oldPort = h.ports[0];

  // The call's first synchronous location check sees /old. Change Location
  // before the resolved READY promise resumes on its next microtask.
  const pending = h.transport.newId();
  h.targetWindow.location.href = "https://example.com/race/new#part";

  await assert.rejects(
    pending,
    (cause) => cause.code === "BW_DOCUMENT_NOTES_STALE",
  );
  assert.equal(oldPort.disconnected, true);
  assert.equal(oldPort.messages.length, 0);
  await waitFor(() => h.ports.length === 2);
  h.ports[1].receive(protocolMessage({
    type: "READY",
    documentId: "web:https://example.com/race/new",
    scope: SCOPE,
  }));
  assert.deepEqual(clean(await h.transport.identity()), {
    documentId: "web:https://example.com/race/new",
    scope: SCOPE,
  });
});

test("identity rechecks location after connect in an isolated-world SPA race", async () => {
  const h = harness("https://example.com/identity/old");
  await ready(h);
  const oldPort = h.ports[0];

  const identityPromise = h.transport.identity();
  h.targetWindow.location.href =
    "https://example.com/identity/new?view=notes#ignored";

  await assert.rejects(
    identityPromise,
    (cause) => cause.code === "BW_DOCUMENT_NOTES_STALE",
  );
  assert.equal(oldPort.disconnected, true);
  assert.equal(oldPort.messages.length, 0);
  await waitFor(() => h.ports.length === 2);
  h.ports[1].receive(protocolMessage({
    type: "READY",
    documentId:
      "web:https://example.com/identity/new?view=notes",
    scope: SCOPE,
  }));
  assert.deepEqual(clean(await h.transport.identity()), {
    documentId:
      "web:https://example.com/identity/new?view=notes",
    scope: SCOPE,
  });
});

test("a physical disconnect makes every in-flight generation stale", async () => {
  const h = harness("https://example.com/disconnect");
  await ready(h);
  const oldPort = h.ports[0];
  const pendingGet = h.transport.get("note-1", {});
  await lastCall(oldPort, 1);
  oldPort.disconnect();
  await assert.rejects(
    pendingGet,
    (cause) => cause.code === "BW_DOCUMENT_NOTES_STALE",
  );

  await waitFor(() => h.ports.length === 2);
  h.ports[1].receive(protocolMessage({
    type: "READY",
    documentId: "web:https://example.com/disconnect",
    scope: SCOPE,
  }));
  assert.deepEqual(clean(await h.transport.identity()), {
    documentId: "web:https://example.com/disconnect",
    scope: SCOPE,
  });
});

test("broadcasts an early CHANGE unchanged and never treats it as the RESULT", async () => {
  const h = harness();
  const documentId = await ready(h);
  const port = h.ports[0];
  const received = [];
  const unsubscribe = h.transport.subscribe((event) => received.push(event));
  const input = {
    documentId,
    anchor: {
      documentId,
      kind: "web-dom",
      revision: 1,
      data: { selector: "#story" },
    },
  };
  let settled = false;
  const createPromise = h.transport.create(input, {
    mutationId: "create:early-change",
  }).then((value) => {
    settled = true;
    return value;
  });
  const call = await lastCall(port, 1);
  const change = {
    contract: "document-note-repository/1",
    operation: "put",
    cursor: 7,
    mutationId: "create:early-change",
    remote: false,
    note: { documentId, noteId: "note-early", rev: 1 },
    error: null,
  };
  port.receive(protocolMessage({ type: "CHANGE", data: change }));
  assert.equal(received.length, 1);
  assert.equal(received[0], change);
  assert.equal(settled, false);

  port.receive(protocolMessage({
    type: "RESULT",
    id: call.id,
    ok: true,
    data: change.note,
  }));
  assert.deepEqual(await createPromise, change.note);
  unsubscribe();
  port.receive(protocolMessage({
    type: "CHANGE",
    data: { ...change, note: { ...change.note, rev: 2 } },
  }));
  assert.equal(received.length, 1);
});

test("preserves background invalidation details for listeners and mutations", async () => {
  const h = harness("https://example.com/invalidate-details");
  await ready(h);
  const port = h.ports[0];
  const invalidations = [];
  h.transport.onInvalidate((event) => invalidations.push(event));

  const removePromise = h.transport.remove("note-1", {
    ifRev: 2,
    mutationId: "remove:note-1:2",
  });
  await lastCall(port, 1);
  port.receive(protocolMessage({
    type: "INVALIDATED",
    reason: "account-changed",
    code: "BW_DOCUMENT_NOTES_ACCOUNT_CHANGED",
    error: "账户已切换",
    details: {
      outcomeUnknown: true,
      mutationIds: ["remove:note-1:2"],
    },
  }));

  await assert.rejects(removePromise, (cause) => (
    cause.code === "BW_DOCUMENT_NOTES_STALE" &&
    cause.details?.outcomeUnknown === true &&
    cause.details?.mutationId === "remove:note-1:2" &&
    clean(cause.details?.mutationIds)[0] === "remove:note-1:2"
  ));
  assert.equal(invalidations.length, 1);
  assert.equal(
    invalidations[0].error.code,
    "BW_DOCUMENT_NOTES_ACCOUNT_CHANGED",
  );
  assert.deepEqual(clean(invalidations[0].error.details), {
    outcomeUnknown: true,
    mutationIds: ["remove:note-1:2"],
  });

  await waitFor(() => h.ports.length === 2);
  h.ports[1].receive(protocolMessage({
    type: "READY",
    documentId: "web:https://example.com/invalidate-details",
    scope: SCOPE,
  }));
  await h.transport.identity();
});
