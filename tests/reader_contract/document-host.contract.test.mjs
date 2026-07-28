import test from "node:test";
import assert from "node:assert/strict";
import {
  DocumentHost,
  makeDocumentFixture,
  runDocumentHostContract,
} from "./helpers.mjs";

for (const kind of ["pdf", "epub", "html", "favorite", "web"]) {
  test(`DocumentHost ${kind} 通过同一行为契约`, async () => {
    await runDocumentHostContract(makeDocumentFixture(kind));
  });
}

test("pending 能力明确报错，不静默降级", async () => {
  const host = DocumentHost.createDocumentHost({
    kind: "web",
    documentId: "doc:web:pending",
    capabilities: {
      selection: { status: "pending", reason: "DOM quote 锚仍待迁移" },
    },
    methods: {},
  });
  await assert.rejects(
    host.getSelection(),
    (error) => error.code === "BW_CAPABILITY_PENDING" && /DOM quote/.test(error.message),
  );
});

test("声明 supported 却缺实现时，构造阶段即失败", () => {
  assert.throws(
    () => DocumentHost.createDocumentHost({
      kind: "pdf",
      documentId: "doc:broken",
      capabilities: {
        navigation: { status: "supported" },
      },
      methods: {},
    }),
    (error) => error.code === "BW_DOCUMENT_HOST_CONTRACT"
      && error.details.missing.navigation.includes("navigate"),
  );
});

test("函数名存在但行为是 no-op，语义契约仍会失败", async () => {
  const fixture = makeDocumentFixture("web");
  fixture.host = DocumentHost.createDocumentHost({
    kind: "web",
    documentId: fixture.documentId,
    capabilities: fixture.host.capabilities,
    methods: {
      ...fixture.methods,
      navigate() { return true; },
    },
  });
  await assert.rejects(
    runDocumentHostContract(fixture),
    /navigate 必须改变真实位置/,
  );
});

for (const legacy of [
  { adapterKind: "pdf", expectedKind: "pdf", anchorKind: "pdf-char" },
  { adapterKind: "epub-html", expectedKind: "epub", anchorKind: "epub-offset" },
  { adapterKind: "html", expectedKind: "html", anchorKind: "offset" },
  { adapterKind: "favorite", expectedKind: "favorite", anchorKind: "epub-offset" },
]) {
  test(`旧 ${legacy.adapterKind} adapter 可并行暴露 DocumentHost，不替换 RC.use`, async () => {
    let index = 0;
    let selection = {
      text: "legacy selection",
      context: "legacy context",
      anchor: { position: 1 },
    };
    const adapter = {
      kind: legacy.adapterKind,
      config: { anchorKind: legacy.anchorKind },
      fileInfo: () => ({ file: `legacy:${legacy.adapterKind}:1` }),
      captureSelection: () => selection,
      clearSelection: () => { selection = null; },
      getContext: () => ({ visible_text: "legacy visible content" }),
      currentLocation: () => ({ unit: "page", index, total: 4 }),
      navigate: (target) => {
        index = Number(target?.data?.index ?? target?.index ?? 0);
        return true;
      },
    };
    const host = DocumentHost.createLegacyDocumentHost(adapter);
    assert.equal(host.kind, legacy.expectedKind);
    assert.equal(host.capability("selection").status, "supported");
    assert.equal((await host.getSelection()).text, "legacy selection");
    assert.ok((await host.getVisibleContent()).text);
    await host.navigate({ index: 2 });
    assert.equal((await host.getCurrentLocation()).index, 2);
    await assert.rejects(host.search("not-yet"), (error) => error.code === "BW_CAPABILITY_PENDING");
  });
}

test("旧网页 adapter 的 URL navigate 不会被误判为锚点解析", async () => {
  let currentUrl = "https://example.com/one";
  const adapter = {
    kind: "web",
    config: { anchorKind: "web-proxy" },
    fileInfo: () => ({ file: `web:${currentUrl}` }),
    captureSelection: () => ({
      text: "selected web text",
      context: "web context",
      rect: { left: 10, top: 20, right: 30, bottom: 40 },
    }),
    clearSelection() {},
    currentLocation: () => ({ unit: "url", index: 0, total: 1, data: { url: currentUrl } }),
    navigate(target) {
      const url = target?.data?.url ?? target?.url;
      if (!url) return false;
      currentUrl = url;
      return true;
    },
  };

  const host = DocumentHost.createLegacyDocumentHost(adapter);
  assert.equal(host.capability("selection").status, "supported");
  assert.equal(host.capability("navigation").status, "supported");
  assert.equal(host.capability("anchors").status, "pending");
  await host.navigate({ url: "https://example.com/two" });
  assert.equal(currentUrl, "https://example.com/two");
  await assert.rejects(
    host.createAnchor({ anchor: { kind: "web-quote", quote: "selected web text" } }),
    (error) => error.code === "BW_CAPABILITY_PENDING" && error.capability === "anchors",
  );
});

test("旧 adapter 只有提供真实 anchor resolver 才声明 anchors supported", async () => {
  let resolvedPosition = null;
  const adapter = {
    kind: "html",
    config: { anchorKind: "offset" },
    fileInfo: () => ({ file: "legacy:html:anchored" }),
    captureSelection: () => ({
      text: "anchored text",
      context: "context",
      anchor: { start: 4, end: 17 },
    }),
    clearSelection() {},
    jumpToAnchor(anchor) {
      resolvedPosition = anchor.start;
      return true;
    },
  };

  const host = DocumentHost.createLegacyDocumentHost(adapter);
  assert.equal(host.capability("anchors").status, "supported");
  const anchor = await host.createAnchor(await host.getSelection());
  const resolved = await host.resolveAnchor(anchor);
  assert.equal(resolved.resolved, true);
  assert.equal(resolvedPosition, 4);
});
