import { createRequire } from "node:module";
import assert from "node:assert/strict";

const require = createRequire(import.meta.url);

export const DocumentHost = require("../../_server_deploy/static/reader-runtime/document-host.js");
export const DataStore = require("../../_server_deploy/static/reader-runtime/data-store.js");
export const DataRegistry = require("../../_server_deploy/static/reader-runtime/data-registry.js");
export const SyncGateway = require("../../_server_deploy/static/reader-runtime/sync-gateway.js");
export const StorageRouter = require("../../_server_deploy/static/reader-runtime/storage-router.js");
export const RuntimeSelector = require("../../_server_deploy/static/reader-runtime/runtime-selector.js");

export function makeRegistry(scopes) {
  const snapshot = structuredClone(scopes || {});
  for (const entry of Object.values(snapshot)) {
    if (entry?.sync === true && !Number.isInteger(entry.recordSchema)) {
      entry.recordSchema = 1;
    }
  }
  const clone = (value) => value == null ? value : structuredClone(value);
  const providerCollections = () => Object.keys(snapshot).filter((name) => {
    const entry = snapshot[name] || {};
    return entry.scope === "global" &&
      entry.status === "ready" &&
      entry.provider === true;
  }).sort();
  const syncCollections = () => Object.keys(snapshot).filter((name) => {
    const entry = snapshot[name] || {};
    return entry.scope === "global" &&
      entry.status === "ready" &&
      entry.provider === true &&
      entry.sync === true;
  }).sort();
  const syncDescriptor = () => syncCollections().map((name) => {
    const entry = snapshot[name];
    return {
      name,
      conflictPolicy: String(entry.conflictPolicy || ""),
      derived: entry.derived === true,
      recordSchema: Number(entry.recordSchema),
    };
  });
  const syncDigest = () =>
    "sync-v3:record-parent-state/1|" +
    syncDescriptor().map((item) => [
      item.name,
      item.conflictPolicy,
      item.derived ? "1" : "0",
      String(item.recordSchema),
    ].join(":")).join("|");
  return {
    CONTRACT: "data-registry/1",
    SYNC_CONTRACT: "sync-v3",
    SYNC_CHANGE_CONTRACT: "record-parent-state/1",
    scopes: () => clone(snapshot),
    collections: () => clone(snapshot),
    collection: (name) => Object.hasOwn(snapshot, String(name || ""))
      ? clone(snapshot[String(name || "")])
      : null,
    providerCollections,
    isProviderCollection: (name) => providerCollections().includes(String(name || "")),
    syncCollections,
    isSyncCollection: (name) => syncCollections().includes(String(name || "")),
    syncDescriptor,
    syncDigest,
  };
}

export function makeStore(name = "store") {
  return DataStore.createDataStore({
    backend: DataStore.createMemoryBackend(),
    deviceId: name,
    idFactory: (prefix) => `${prefix}_${name}_generated`,
    causalCollections: DataRegistry.syncCollections(),
  });
}

export function makeDocumentFixture(kind) {
  const state = {
    kind,
    index: 0,
    selection: {
      text: `${kind} selected text`,
      context: `${kind} surrounding context`,
      anchor: { kind: `${kind}-anchor`, position: 2 },
      rect: { left: 10, top: 20, right: 110, bottom: 40, width: 100, height: 20 },
    },
    highlights: new Map(),
  };
  const documentId = `doc:${kind}:1`;
  const capabilities = {};
  for (const name of Object.keys(DocumentHost.CAPABILITY_METHODS)) {
    capabilities[name] = { status: "supported", owner: "pwa" };
  }
  const methods = {
    getSelection() {
      return state.selection;
    },
    clearSelection() {
      state.selection = null;
      return true;
    },
    getVisibleContent() {
      return {
        text: `${kind} visible content at ${state.index}`,
        location: { unit: kind === "epub" ? "section" : "page", index: state.index, total: 5 },
      };
    },
    getCurrentLocation() {
      return { unit: kind === "epub" ? "section" : "page", index: state.index, total: 5 };
    },
    createAnchor(source) {
      return source.anchor || { kind: `${kind}-anchor`, position: state.index };
    },
    resolveAnchor(anchor) {
      return { resolved: true, anchor, text: `${kind} resolved text` };
    },
    read() {
      return { text: `${kind} readable body`, data: { index: state.index } };
    },
    navigate(target) {
      const data = target?.data || target || {};
      state.index = Number(data.index ?? data.page ?? data.section ?? 0);
      return { moved: true, index: state.index };
    },
    search(query) {
      return [{ id: `${kind}-hit-1`, query, text: `${query} in ${kind}` }];
    },
    renderHighlight(record) {
      state.highlights.set(record.id, structuredClone(record));
      return { rendered: true, id: record.id };
    },
    removeHighlight(id) {
      return { removed: state.highlights.delete(id), id };
    },
  };
  const host = DocumentHost.createDocumentHost({
    kind,
    documentId,
    anchorKind: `${kind}-anchor`,
    capabilities,
    methods,
  });
  return { host, state, methods, documentId };
}

export async function runDocumentHostContract(fixture) {
  const { host, state } = fixture;
  assert.equal(host.audit().valid, true);
  assert.equal(host.capability("selection").status, "supported");

  const selection = await host.getSelection();
  assert.ok(selection?.text, "selection 必须返回真实文本");
  const anchor = await host.createAnchor(selection);
  assert.equal(anchor.documentId, host.documentId);
  assert.ok(anchor.data, "anchor 必须保留宿主私有 payload");
  const resolved = await host.resolveAnchor(anchor);
  assert.equal(resolved?.resolved, true, "anchor 必须可以解析");
  await host.clearSelection();
  assert.equal(state.selection, null, "clearSelection 必须产生可观察结果");

  const content = await host.getVisibleContent();
  assert.ok(content.text.length > 0, "visibleContent 不得是空 no-op");
  const before = await host.getCurrentLocation();
  await host.navigate({ index: 3 });
  const after = await host.getCurrentLocation();
  assert.notEqual(after.index, before.index, "navigate 必须改变真实位置");

  const read = await host.read({ location: after });
  assert.ok(read.text.length > 0, "read 必须返回真实内容");
  const hits = await host.search("needle");
  assert.ok(Array.isArray(hits) && hits.length > 0, "search 必须返回真实命中");

  await host.renderHighlight({ id: "hl_shared_1", text: "needle" });
  assert.equal(state.highlights.has("hl_shared_1"), true, "renderHighlight 必须渲染指定稳定 ID");
  await host.removeHighlight("hl_shared_1");
  assert.equal(state.highlights.has("hl_shared_1"), false, "removeHighlight 必须移除同一稳定 ID");
}

export function makeUiSpy() {
  return {
    mountCount: 0,
    owner: null,
    mount(input) {
      this.mountCount += 1;
      this.owner = input.owner;
      return { mounted: true };
    },
  };
}

export function makeRelayTransport() {
  const state = {
    cursor: 0,
    changes: [],
    mutations: new Set(),
  };
  return {
    state,
    push(request) {
      const ackedMutationIds = [];
      for (const change of request.changes || []) {
        if (state.mutations.has(change.mutationId)) {
          ackedMutationIds.push(change.mutationId);
          continue;
        }
        state.mutations.add(change.mutationId);
        state.cursor += 1;
        state.changes.push({ ...structuredClone(change), cursor: state.cursor });
        ackedMutationIds.push(change.mutationId);
      }
      return { cursor: state.cursor, ackedMutationIds, changes: [], conflicts: [] };
    },
    pull(request) {
      return {
        cursor: state.cursor,
        ackedMutationIds: [],
        changes: state.changes.filter((change) => change.cursor > request.cursor),
        conflicts: [],
      };
    },
    status() {
      return { state: "ready" };
    },
  };
}
