import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";

const SOURCE = fs.readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-voicecall.js", import.meta.url),
  "utf8",
);
const EXTENSION_FACADE = fs.readFileSync(
  new URL("../../extensions/bw-reader-webext/src/facade.js", import.meta.url),
  "utf8",
);
const EXTENSION_BACKGROUND = fs.readFileSync(
  new URL("../../extensions/bw-reader-webext/background.js", import.meta.url),
  "utf8",
);
const APP_RUNTIME = fs.readFileSync(
  new URL("../../ios/BWReader/App/ReaderLocalRuntimeServer.swift", import.meta.url),
  "utf8",
);

function mediaHelpers(windowOverride = {}) {
  const start = SOURCE.indexOf("function _cardHttpsURL(value)");
  const end = SOURCE.indexOf("function _infoText(card)", start);
  assert.ok(start >= 0 && end > start);
  const factory = new Function(
    "window",
    "URL",
    "encodeURIComponent",
    "esc",
    "document",
    `${SOURCE.slice(start, end)}
     return { _cardHttpsURL, _cardMediaURL, _videoCardRef,
       _videoCardThumb, _videoCardThumbSource, _videoButtonRef,
       _openVideoRef, _infoHtml };`,
  );
  const browserWindow = {
    location: { href: "https://reader.example/pdf/view" },
    open() {},
    ...windowOverride,
  };
  return factory(
    browserWindow,
    URL,
    encodeURIComponent,
    (value) => String(value)
      .replaceAll("&", "&amp;")
      .replaceAll('"', "&quot;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;"),
    { addEventListener() {} },
  );
}

test("structured image cards use the same-origin bounded image proxy", () => {
  const helpers = mediaHelpers();
  const remote = "https://images.unsplash.com/photo.jpg?fit=crop&w=800";
  const proxied = `/pdf/api/img-proxy?url=${encodeURIComponent(remote)}`;
  assert.equal(helpers._cardMediaURL(remote), proxied);
  assert.equal(helpers._cardMediaURL("http://images.example/photo.jpg"), "");
  assert.equal(
    helpers._cardMediaURL("/pdf/api/page-image?file=book.pdf&page=1"),
    "/pdf/api/page-image?file=book.pdf&page=1",
  );
  assert.equal(
    helpers._cardMediaURL(proxied),
    proxied,
    "a replayed card must not wrap the controlled proxy a second time",
  );

  const html = helpers._infoHtml({
    kind: "images",
    data: { items: [{ url: remote, title: "Tokyo" }] },
  });
  assert.match(html, new RegExp(`src="${proxied.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}"`));
  assert.match(html, /data-source-url="https:\/\/images\.unsplash\.com/);
  assert.doesNotMatch(html, /src="https:\/\/images\.unsplash\.com/);
});

test("ordinary-page extension images cross its fetch bridge instead of the host site", () => {
  assert.match(
    EXTENSION_FACADE,
    /const ORIGIN = "https:\/\/bwicarus\.taile44d0c\.ts\.net"/,
  );
  assert.match(
    EXTENSION_FACADE,
    /privateImagePath = \/\^\\\/pdf\\\/api\\\/(?:[\s\S]*?)img-proxy/,
  );
  assert.match(
    EXTENSION_FACADE,
    /if \(u\.startsWith\("\/"\)\) u = ORIGIN \+ u/,
  );
  assert.match(
    EXTENSION_FACADE,
    /loadPrivateImage[\s\S]*window\.__bwReaderFetch\(requestPath\)/,
  );
  assert.match(EXTENSION_BACKGROUND, /"\/pdf\/api\/img-proxy"/);
});

test("dragged card images persist a stable source instead of loopback or blob display URLs", () => {
  const start = SOURCE.indexOf("// ── 单张图片拖出=图片便签");
  const end = SOURCE.indexOf("window.__vcDragToDock", start);
  assert.ok(start >= 0 && end > start);
  const drag = SOURCE.slice(start, end);
  assert.match(drag, /dataset\.sourceUrl/);
  assert.match(drag, /_cardMediaURL\(_persistentSource\)/);
  assert.match(drag, /_dImg = \{ url: _persistentSource \|\| _asrcRaw/);
  assert.doesNotMatch(drag, /_dImg = \{ url:[^\n]*was\.img\.src/);
  assert.match(drag, /图片来源无效，未粘贴/);
  assert.doesNotMatch(
    drag,
    /var _asrc = .*was\.img\.src/,
    "a resolved App loopback or extension blob URL must never become durable note HTML",
  );
});

test("URL-only YouTube cards derive the player id and a proxied thumbnail", () => {
  const helpers = mediaHelpers();
  const item = {
    title: "Tokyo Tower",
    url: "https://www.youtube.com/watch?v=YXb87xE5PVk",
    src: "YouTube",
  };
  const ref = helpers._videoCardRef(item);
  assert.deepEqual(ref, {
    id: "YXb87xE5PVk",
    src: "yt",
    url: item.url,
  });
  assert.equal(
    helpers._videoCardThumbSource(item, ref),
    "https://i.ytimg.com/vi/YXb87xE5PVk/mqdefault.jpg",
  );
  assert.equal(
    helpers._videoCardThumb(item, ref),
    "/pdf/api/img-proxy?url=" + encodeURIComponent(
      "https://i.ytimg.com/vi/YXb87xE5PVk/mqdefault.jpg",
    ),
  );
  const html = helpers._infoHtml({ kind: "videos", data: { items: [item] } });
  assert.match(html, /YouTube/);
  assert.match(html, /i\.ytimg\.com%2Fvi%2FYXb87xE5PVk%2Fmqdefault\.jpg/);
  assert.match(html, /data-video-id="YXb87xE5PVk"/);
  assert.match(html, /data-video-src="yt"/);
  assert.match(html, /data-video-url="https:\/\/www\.youtube\.com\/watch\?v=YXb87xE5PVk"/);
});

test("video play prefers the shared internal player on every Reader host", () => {
  const start = SOURCE.indexOf("function _igWire(root, card)");
  const end = SOURCE.indexOf("function renderInfo(card)", start);
  assert.ok(start >= 0 && end > start);
  const wire = SOURCE.slice(start, end);
  assert.match(wire, /_openVideoRef\(_videoButtonRef\(pb\), vt\.title \|\| ''\)/);
  assert.doesNotMatch(wire, /id:\s*vt\.id/);
  assert.doesNotMatch(wire, /__BW_NATIVE_LOCAL_READER__[\s\S]*window\.open/);

  const playerCalls = [];
  let externalCalls = 0;
  const helpers = mediaHelpers({
    RC: { videoPlayer: { open(value) { playerCalls.push(value); } } },
    open() { externalCalls += 1; },
  });
  assert.equal(helpers._openVideoRef({
    id: "YXb87xE5PVk",
    src: "yt",
    url: "https://www.youtube.com/watch?v=YXb87xE5PVk",
  }, "Tokyo Tower"), true);
  assert.deepEqual(playerCalls, [{
    id: "YXb87xE5PVk",
    src: "yt",
    title: "Tokyo Tower",
  }]);
  assert.equal(externalCalls, 0);
});

test("pinned video cards keep a delegated play route and recover legacy YouTube ids", () => {
  assert.match(SOURCE, /closest\('\.rc-note \.vc-vg-play'\)/);
  assert.match(SOURCE, /_videoButtonRef\(button\)/);
  assert.match(SOURCE, /host === 'i\.ytimg\.com' \|\| host === 'img\.youtube\.com'/);
  assert.match(SOURCE, /outer\.pathname === '\/pdf\/api\/img-proxy'/);
});

test("the App CSP permits only the two official embedded-player origins", () => {
  assert.match(
    APP_RUNTIME,
    /frame-src https:\/\/www\.youtube-nocookie\.com https:\/\/player\.bilibili\.com;/,
  );
  assert.doesNotMatch(APP_RUNTIME, /frame-src 'none'/);
});
