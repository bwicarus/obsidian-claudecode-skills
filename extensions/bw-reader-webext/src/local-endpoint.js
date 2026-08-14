/* local-endpoint.js — 扩展直连宿主 App 的本机 Reader 服务。
 *
 * 为什么要有它：扩展现在把高亮、便签写去 Pi，而 App 的那份在自己的 IndexedDB
 * 里。同一件东西两份数据 —— 用户在扩展里划的线，App 里看不见。iPad 上扩展是随
 * App 分发的，本来就该读写 App 那一份。
 *
 * 硬失败，不回落 Pi。App 没在跑就明确说「请先打开 BW Reader」，而不是悄悄改打
 * Pi。回落看起来更友好，实际是把割裂藏起来：用户以为存上了，换个入口就不见了，
 * 而且再也想不起是哪一次存到了另一边。宁可当场不可用。
 *
 * 落点来自 native message 的 capabilities（App 写在 App Group 里的端口与
 * capability token）。它会随 App 重启而变，所以每次拿到都要重新校验，
 * 不缓存跨 App 生命周期的值。
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.BWLocalEndpoint = api;
  }
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  "use strict";

  const CONTRACT = "bw-local-endpoint/1";

  // App 侧三种状态，处理方式完全不同，不能折成一个「没有」：
  //   ready           → 直连
  //   app-not-running → 提示打开 App（用户能自己解决）
  //   unavailable     → App Group 配置问题（用户解决不了，要看日志）
  const STATUS = Object.freeze({
    READY: "ready",
    APP_NOT_RUNNING: "app-not-running",
    UNAVAILABLE: "unavailable",
    MISSING: "missing",
  });

  const ERROR_CODES = Object.freeze({
    APP_NOT_RUNNING: "BW_LOCAL_ENDPOINT_APP_NOT_RUNNING",
    UNAVAILABLE: "BW_LOCAL_ENDPOINT_UNAVAILABLE",
    MALFORMED: "BW_LOCAL_ENDPOINT_MALFORMED",
  });

  function endpointError(message, code) {
    const error = new Error(message);
    error.code = code;
    error.contract = CONTRACT;
    return error;
  }

  /** 只接受本机回环上的落点。别的地址一律拒绝 —— 这条 base 会被拼上
   *  capability token 去发请求，指错地方等于把 token 交给别人。 */
  function isLoopbackBase(value) {
    let url;
    try {
      url = new URL(String(value || ""));
    } catch (_) {
      return false;
    }
    if (url.protocol !== "http:") return false;
    if (url.hostname !== "127.0.0.1") return false;
    if (url.username || url.password || url.search || url.hash) return false;
    // /r/<64 位十六进制 token>
    return /^\/r\/[0-9a-f]{64}$/.test(url.pathname);
  }

  /** 从 capabilities 响应里读出落点。返回 {status, base?} —— 不抛，
   *  因为「没有落点」是一种正常状态，调用方要据此决定提示什么。 */
  function parse(capabilities) {
    const value =
      capabilities && typeof capabilities === "object" ? capabilities : {};
    const status = String(value.localEndpointStatus || STATUS.MISSING);

    if (status === STATUS.APP_NOT_RUNNING) {
      return { status: STATUS.APP_NOT_RUNNING };
    }
    if (status === STATUS.UNAVAILABLE) {
      return {
        status: STATUS.UNAVAILABLE,
        detail: String(value.localEndpointError || "").slice(0, 300),
      };
    }
    if (status !== STATUS.READY) {
      // 旧版 App 不带这个字段。当成「没有」，而不是当成「不可用」——
      // 前者提示装新版，后者会让人去查 App Group 配置，方向差得很远。
      return { status: STATUS.MISSING };
    }

    const endpoint =
      value.localEndpoint && typeof value.localEndpoint === "object"
        ? value.localEndpoint
        : null;
    const base = endpoint ? String(endpoint.base || "") : "";
    if (!isLoopbackBase(base)) {
      return { status: STATUS.UNAVAILABLE, detail: "落点不是本机回环地址" };
    }
    return {
      status: STATUS.READY,
      base,
      startedAtEpochSeconds: Number(endpoint.startedAtEpochSeconds) || 0,
    };
  }

  /** 把落点变成可用的 base，拿不到就抛 —— 调用方不该继续往 Pi 打。 */
  function requireBase(resolved) {
    const value = resolved || {};
    if (value.status === STATUS.READY && value.base) return value.base;
    if (value.status === STATUS.APP_NOT_RUNNING) {
      throw endpointError(
        "BW Reader 没在运行。请先打开 App，扩展的高亮与便签要写进 App 的本机数据。",
        ERROR_CODES.APP_NOT_RUNNING
      );
    }
    if (value.status === STATUS.UNAVAILABLE) {
      throw endpointError(
        "无法连接 BW Reader 本机服务" +
          (value.detail ? "：" + value.detail : ""),
        ERROR_CODES.UNAVAILABLE
      );
    }
    throw endpointError(
      "这个版本的 BW Reader 尚未提供本机服务落点，请更新 App。",
      ERROR_CODES.MALFORMED
    );
  }

  /** 把原本发往 Pi 的路径改指向 App。只接受 /pdf/api/... 这种绝对路径，
   *  拼接前再确认一次 base，避免把路径接到空串上变成相对请求。 */
  function localURL(base, path) {
    if (!isLoopbackBase(base)) {
      throw endpointError("本机落点无效", ERROR_CODES.MALFORMED);
    }
    const suffix = String(path || "");
    if (!suffix.startsWith("/")) {
      throw endpointError("路径必须以 / 开头", ERROR_CODES.MALFORMED);
    }
    return base + suffix;
  }

  return Object.freeze({
    CONTRACT,
    STATUS,
    ERROR_CODES,
    isLoopbackBase,
    parse,
    requireBase,
    localURL,
  });
});
