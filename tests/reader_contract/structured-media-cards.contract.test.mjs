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
const VIDEO_PLAYER = fs.readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-videoplayer.js", import.meta.url),
  "utf8",
);
const VIDEO_CARDS = fs.readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-video.js", import.meta.url),
  "utf8",
);
const STICKY_NOTES = fs.readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-stickynote.js", import.meta.url),
  "utf8",
);
const APP_WEB_VIEW = fs.readFileSync(
  new URL("../../ios/BWReader/App/ReaderWebView.swift", import.meta.url),
  "utf8",
);

function mediaHelpers(windowOverride = {}, documentOverride = {}) {
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
     return { _cardHttpsURL, _cardMediaURL, _cardAssetID, _cardAssetURL,
       _cardImageURL, _videoCardRef,
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
    { addEventListener() {}, ...documentOverride },
  );
}

function legacyVideoHelpers() {
  const start = VIDEO_CARDS.indexOf("function _httpsURL(value)");
  const end = VIDEO_CARDS.indexOf("function _openPlayer(v)", start);
  assert.ok(start >= 0 && end > start);
  return new Function(
    "URL",
    "encodeURIComponent",
    `${VIDEO_CARDS.slice(start, end)}; return { _thumbSource, _thumbURL };`,
  )(URL, encodeURIComponent);
}

function playerURLHelpers(windowOverride = {}) {
  const start = VIDEO_PLAYER.indexOf("function _isBili(v)");
  const end = VIDEO_PLAYER.indexOf("function _hook()", start);
  assert.ok(start >= 0 && end > start);
  return new Function(
    "window",
    "encodeURIComponent",
    `${VIDEO_PLAYER.slice(start, end)}; return { vEmbedSrc, _externalURL };`,
  )({
    location: {
      protocol: "http:",
      origin: "http://127.0.0.1:43129",
    },
    ...windowOverride,
  }, encodeURIComponent);
}

function stickyVideoHelpers() {
  const start = STICKY_NOTES.indexOf("function _isBili(v)");
  const end = STICKY_NOTES.indexOf("function vEmbedSrc(v)", start);
  assert.ok(start >= 0 && end > start);
  return new Function(
    "URL",
    "encodeURIComponent",
    `${STICKY_NOTES.slice(start, end)}; return { _videoThumbURL };`,
  )(URL, encodeURIComponent);
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

test("registered image cards persist the account asset and fall back through img-proxy once", () => {
  let delegatedError = null;
  let hostDocumentListener = false;
  const helpers = mediaHelpers({}, {
    body: {
      addEventListener(type, listener, options) {
        if (type === "error" && options === true) delegatedError = listener;
      },
    },
    addEventListener(type) {
      if (type === "error") hostDocumentListener = true;
    },
  });
  const remote = "https://images.example/photo.jpg?width=900";
  const aid = "img_a1b2c3d4";
  const primary = `/pdf/api/asset/${aid}?proxy=1`;
  const fallback = `/pdf/api/img-proxy?url=${encodeURIComponent(remote)}`;
  assert.equal(helpers._cardAssetID(aid), aid);
  assert.equal(helpers._cardAssetID("img_../../secret"), "");
  assert.equal(helpers._cardAssetURL(aid), primary);
  assert.equal(helpers._cardImageURL({ aid, url: remote }), primary);

  const html = helpers._infoHtml({
    kind: "images",
    data: { items: [{ aid, url: remote, title: "durable" }] },
  });
  assert.match(html, new RegExp(`src="${primary.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}"`));
  assert.match(html, new RegExp(`data-aid="${aid}"`));
  assert.match(html, /data-source-url="https:\/\/images\.example\/photo\.jpg\?width=900"/);
  assert.equal(typeof delegatedError, "function");
  assert.equal(hostDocumentListener, false,
    "the non-composed image error must be captured inside the Reader ShadowRoot");

  const attrs = new Map([
    ["data-aid", aid],
    ["data-source-url", remote],
    ["src", primary],
  ]);
  const image = {
    tagName: "IMG",
    getAttribute(name) { return attrs.get(name) || ""; },
    setAttribute(name, value) { attrs.set(name, String(value)); },
  };
  delegatedError({ target: image });
  assert.equal(attrs.get("src"), fallback);
  assert.equal(attrs.get("data-asset-fallback-done"), "1");
  attrs.set("src", primary);
  delegatedError({ target: image });
  assert.equal(attrs.get("src"), primary, "the same image never retries its fallback");

  const internalAttrs = new Map([
    ["data-aid", aid],
    ["data-source-url", "/pdf/api/page-image?file=book.pdf&page=1"],
    ["src", primary],
  ]);
  delegatedError({ target: {
    tagName: "IMG",
    getAttribute(name) { return internalAttrs.get(name) || ""; },
    setAttribute(name, value) { internalAttrs.set(name, String(value)); },
  } });
  assert.equal(internalAttrs.get("src"), primary,
    "only the original bounded HTTPS source may become an asset fallback");
  assert.equal(internalAttrs.has("data-asset-fallback-done"), false);
});

test("sticky image cards normalize durable asset URLs before render and persistence", () => {
  const normalizer = STICKY_NOTES.slice(
    STICKY_NOTES.indexOf("function normalizeHtmlCardImageAssets("),
    STICKY_NOTES.indexOf("function bindHtmlCardSelection("),
  );
  assert.match(normalizer, /\^\[a-z\]\{2,4\}_\[a-f0-9\]\{4,12\}\$/);
  assert.match(normalizer, /\/pdf\/api\/asset\/.*\?proxy=1/);
  assert.match(normalizer, /data-source-url/);
  assert.match(normalizer, /removeAttribute\('data-asset-fallback-done'\)/);
  assert.match(
    STICKY_NOTES,
    /var durableContent = normalizeHtmlCardImageAssets\(h\.content\)/,
  );
  assert.match(
    STICKY_NOTES,
    /var rawContent = normalizeHtmlCardImageAssets\(htmlObj\.content\)/,
  );
});

test("ordinary-page extension images cross its fetch bridge instead of the host site", () => {
  assert.match(
    EXTENSION_FACADE,
    // 2026-09-06 起服务器是 Windows 桥（bwicarus-2）；Pi 的 webapp 已停。
    /const ORIGIN = "https:\/\/bwicarus-2\.taile44d0c\.ts\.net"/,
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
  assert.match(html, /referrerpolicy="same-origin"/);
  assert.doesNotMatch(html, /referrerpolicy="no-referrer"/);
});

test("legacy YouTube and Bilibili result cards use the same local thumbnail route", () => {
  const helpers = legacyVideoHelpers();
  const youtube = { id: "YXb87xE5PVk", src: "yt", thumb: "" };
  assert.equal(
    helpers._thumbURL(youtube),
    "/pdf/api/img-proxy?url=" + encodeURIComponent(
      "https://i.ytimg.com/vi/YXb87xE5PVk/mqdefault.jpg",
    ),
  );
  const bili = {
    id: "BV1xx411c7mD",
    src: "bili",
    thumb: "https://i0.hdslb.com/bfs/archive/example.jpg",
  };
  assert.equal(helpers._thumbSource(bili), bili.thumb);
  assert.equal(
    helpers._thumbURL(bili),
    "/pdf/api/img-proxy?url=" + encodeURIComponent(bili.thumb),
  );
  assert.match(VIDEO_CARDS, /referrerpolicy="same-origin"/);
  assert.doesNotMatch(VIDEO_CARDS, /referrerpolicy="no-referrer"/);
});

test("pinned video notes keep their durable source but display through the local proxy", () => {
  const helpers = stickyVideoHelpers();
  const youtube = helpers._videoThumbURL({ id: "YXb87xE5PVk", src: "yt" });
  assert.equal(
    youtube,
    "/pdf/api/img-proxy?url=" + encodeURIComponent(
      "https://i.ytimg.com/vi/YXb87xE5PVk/mqdefault.jpg",
    ),
  );
  const biliSource = "https://i0.hdslb.com/bfs/archive/example.jpg";
  assert.equal(
    helpers._videoThumbURL({ id: "BV1xx411c7mD", src: "bili", thumb: biliSource }),
    "/pdf/api/img-proxy?url=" + encodeURIComponent(biliSource),
  );
  assert.match(STICKY_NOTES, /referrerpolicy="same-origin"/);
  assert.doesNotMatch(STICKY_NOTES, /referrerpolicy="no-referrer"/);
});

test("video play prefers the shared internal player on every Reader host", () => {
  const start = SOURCE.indexOf("function _igWire(root, card)");
  const end = SOURCE.indexOf("async function renderInfo(card, options)", start);
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

test("pinned video cards dispatch from both Reader notes and extension page pins", () => {
  let delegatedClick = null;
  const playerCalls = [];
  let prevented = 0;
  let stopped = 0;
  mediaHelpers({
    RC: { videoPlayer: { open(value) { playerCalls.push(value); } } },
  }, {
    addEventListener(type, listener, options) {
      if (type === "click" && options === true) delegatedClick = listener;
    },
  });
  assert.equal(typeof delegatedClick, "function");

  const attrs = {
    "data-video-id": "YXb87xE5PVk",
    "data-video-src": "yt",
    "data-video-url": "https://www.youtube.com/watch?v=YXb87xE5PVk",
    "data-video-title": "Tokyo Tower",
  };
  const button = {
    getAttribute(name) { return attrs[name] || ""; },
    closest() { return null; },
  };
  delegatedClick({
    target: {
      closest(selector) {
        assert.equal(selector, ".rc-note .vc-vg-play,.bw-page-pin .vc-vg-play");
        return button;
      },
    },
    preventDefault() { prevented += 1; },
    stopImmediatePropagation() { stopped += 1; },
  });

  assert.deepEqual(playerCalls, [{
    id: "YXb87xE5PVk",
    src: "yt",
    title: "Tokyo Tower",
  }]);
  assert.equal(prevented, 1);
  assert.equal(stopped, 1);
});

test("pinned video cards recover legacy YouTube ids", () => {
  assert.match(SOURCE, /_videoButtonRef\(button\)/);
  assert.match(SOURCE, /host === 'i\.ytimg\.com' \|\| host === 'img\.youtube\.com'/);
  assert.match(SOURCE, /outer\.pathname === '\/pdf\/api\/img-proxy'/);
});

test("the shared video player restores hit testing inside the extension overlay", () => {
  assert.match(VIDEO_PLAYER, /#rc-vplayer\{[^}]*pointer-events:auto/);
});

test("the shared player identifies its embedder without leaking the App capability path", () => {
  const helpers = playerURLHelpers();
  const embed = new URL(helpers.vEmbedSrc({ id: "YXb87xE5PVk", src: "yt" }));
  assert.equal(embed.origin, "https://www.youtube-nocookie.com");
  assert.equal(embed.pathname, "/embed/YXb87xE5PVk");
  assert.equal(embed.searchParams.get("origin"), "http://127.0.0.1:43129");
  assert.doesNotMatch(embed.href, /\/r\//);
  assert.match(VIDEO_PLAYER, /iframe\.referrerPolicy = 'origin'/);
  assert.match(VIDEO_PLAYER, /setAttribute\('referrerpolicy', 'origin'\)/);
});

test("the App uses Bilibili's actual mobile player and always exposes external fallback", () => {
  const helpers = playerURLHelpers({ __BW_NATIVE_LOCAL_READER__: true });
  const embed = new URL(helpers.vEmbedSrc({ id: "BV1xx411c7mD", src: "bili" }));
  assert.equal(embed.origin, "https://www.bilibili.com");
  assert.equal(embed.pathname, "/blackboard/webplayer/mbplayer.html");
  assert.equal(
    helpers._externalURL({ id: "BV1xx411c7mD", src: "bili" }),
    "https://www.bilibili.com/video/BV1xx411c7mD",
  );
  assert.match(VIDEO_PLAYER, /class="rcvp-fallback"/);
  assert.match(VIDEO_PLAYER, /class="rcvp-external"/);
  assert.match(VIDEO_PLAYER, /window\.open\(_externalURL\(cur\.v\), '_blank', 'noopener'\)/);
  assert.match(VIDEO_PLAYER, /_markFailed\('内置播放器加载失败'\)/);
});

test("the App CSP and navigation delegate allow only exact player documents", () => {
  assert.match(
    APP_RUNTIME,
    /frame-src https:\/\/www\.youtube-nocookie\.com\/embed\/ https:\/\/player\.bilibili\.com\/player\.html https:\/\/www\.bilibili\.com\/blackboard\/webplayer\/mbplayer\.html;/,
  );
  assert.doesNotMatch(APP_RUNTIME, /frame-src 'none'/);
  assert.match(APP_WEB_VIEW, /private func isAllowedEmbeddedVideoURL/);
  assert.match(APP_WEB_VIEW, /host == "www\.youtube-nocookie\.com"[\s\S]*parts\[0\] == "embed"/);
  assert.match(APP_WEB_VIEW, /host == "player\.bilibili\.com"[\s\S]*path == "\/player\.html"/);
  assert.match(APP_WEB_VIEW, /host == "www\.bilibili\.com"[\s\S]*path == "\/blackboard\/webplayer\/mbplayer\.html"/);
  assert.match(
    APP_WEB_VIEW,
    /navigationAction\.targetFrame\?\.isMainFrame == false,[\s\S]*isAllowedEmbeddedVideoURL\(url\),[\s\S]*isTrustedReaderURL\(sourceURL\)[\s\S]*isAllowedEmbeddedVideoURL\(sourceURL\)/,
  );
});
