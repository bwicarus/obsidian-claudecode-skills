/* route-destination.js — 每条 Reader 路由该打给谁。
 *
 * iPad 上扩展应当读写宿主 App 的数据，而不是绕去 Pi。现在同一份高亮，App 存在
 * 自己的 IndexedDB，扩展却写去 Pi —— 用户在扩展里划的线，App 里看不见。
 *
 * 分流依据是能力归属，不是「哪个更快」：
 *   · App 已有本地实现（manifest owner=local/native）→ 打 App
 *   · 需要服务端资源（词典库、翻译 API、Anki、OCR、建图）→ 打 Pi
 *
 * 名单是显式的白名单，不做前缀匹配。前缀匹配的问题是新增路由会**默默**落进
 * 某一边：落错边的后果是数据写进另一份存储，而这种错误要等用户发现「东西不见了」
 * 才暴露。显式列举意味着新路由必须有人做一次决定。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.BWRouteDestination = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const CONTRACT = "bw-route-destination/1";

  const DESTINATION = Object.freeze({
    APP: "app",
    PI: "pi",
  });

  /* App 已有本地实现的路由（manifest owner=local / native）。
   * 这些打 Pi 就会产生第二份数据，是当前割裂的来源。 */
  const APP_OWNED_PATHS = Object.freeze(new Set([
    "/pdf/api/highlights",
    "/pdf/api/epub-highlights",
    "/pdf/api/notes",
    "/pdf/api/note-composite",
    "/pdf/api/reading-pos",
    "/pdf/api/userpages",
    "/pdf/api/video-player-prefs",
    // owner=native：App 原生实现（PDFKit 目录、本机笔记目录、图片代理）
    "/pdf/api/toc",
    "/pdf/api/to-note",
    "/pdf/api/img-proxy",
  ]));

  /* 明确留在 Pi 的：需要服务端才有的东西。
   * 列出来不是为了拦截（默认就走 Pi），是为了让「为什么它不在 App」有据可查 ——
   * 否则每隔一段时间就会有人重新问一遍这些能不能也搬过去。 */
  const SERVER_ONLY_REASONS = Object.freeze({
    "/pdf/api/dict": "ECDICT 词库在服务端（日语已本地化，英语待办）",
    "/pdf/api/dict-quick": "同上",
    "/pdf/api/dict-jp": "同上",
    "/pdf/api/dict-jp-ai": "同上",
    "/pdf/api/dict-jp-zh": "同上",
    "/pdf/api/translate": "翻译 API 密钥只在服务端",
    "/pdf/api/web-translate": "同上",
    "/pdf/api/epub-translate-section": "同上",
    "/pdf/api/explain": "调 AI",
    "/pdf/api/epub-assistant": "调 AI",
    "/pdf/api/epub-convo": "调 AI",
    "/pdf/api/epub-furigana": "需要 unidic",
    "/pdf/api/grammar-books": "需要 spacy 与语法库",
    "/pdf/api/grammar-history": "同上",
    "/pdf/api/page-nodes": "知识图谱在服务端",
    "/pdf/api/vocab-list": "生词库在服务端（待本地化）",
    "/pdf/api/vocab-mastery-map": "同上",
  });

  /** 这条路径该打给谁。未列举的一律走 Pi —— 保守的默认：
   *  猜成 App 会让请求打到一个可能没实现该路由的本机服务上，
   *  而猜成 Pi 至多是维持现状。 */
  function destinationFor(pathname) {
    const path = String(pathname || "");
    return APP_OWNED_PATHS.has(path) ? DESTINATION.APP : DESTINATION.PI;
  }

  function isAppOwned(pathname) {
    return destinationFor(pathname) === DESTINATION.APP;
  }

  /** 某条路由留在服务端的原因；不是明确列举的返回空串。 */
  function serverOnlyReason(pathname) {
    return SERVER_ONLY_REASONS[String(pathname || "")] || "";
  }

  return Object.freeze({
    CONTRACT,
    DESTINATION,
    APP_OWNED_PATHS,
    SERVER_ONLY_REASONS,
    destinationFor,
    isAppOwned,
    serverOnlyReason,
  });
});
