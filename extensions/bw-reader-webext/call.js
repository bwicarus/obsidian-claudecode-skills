"use strict";

// Drives the in-extension call. rc-computer-voice.js already implements the
// whole bridge protocol -- handshake, PCM framing, sequence guard, reconnect --
// and has no DOM dependency at all, so nothing about it needs reimplementing
// here. This file only supplies the page around it: context, a button, and a
// readable account of what happened.
//
// Note what is deliberately absent: __bwReaderFetch. In an injected page that
// global replaces fetch with a proxy through the background worker, which is
// exactly what iOS keeps killing mid-request ("翻译中" forever). Leaving it
// unset makes rc-computer-voice fall back to the page's own fetch, so the
// fragile hop is not merely avoided -- it does not exist on this path.

// Three builds have now died on "TypeError: Load failed" -- Safari's wording for
// a failed fetch -- and each time I removed the request I believed was to blame
// and hit it again. The request is not where I keep guessing, so stop guessing:
// record every fetch this page makes and show it next to the failure.
//
// Cheap and self-limiting: it captures URL and outcome only, keeps the last few,
// and is removed once the culprit is known.
const fetchLog = [];
(function instrumentFetch() {
  if (typeof window.fetch !== "function") return;
  const original = window.fetch.bind(window);
  window.fetch = function (input, init) {
    const url = String((input && input.url) || input || "").slice(0, 120);

    // ctxSync's upload, short-circuited rather than attempted.
    //
    // It feeds the Pi→Windows snapshot path, which this page does not use: the
    // context here is written straight into the local snapshot and travels over
    // the bridge socket. And it could never succeed anyway -- the request
    // carries credentials:'include', while the extension authenticates with a
    // device token and holds no Pi cookie, so the best case was always a 401.
    //
    // Setting a base was the tidier fix and did not take; with three builds
    // already spent on this one request, blocking it outright is what actually
    // stops it from taking the call down. A synthetic JSON response lets
    // ctxSync's .then(r => r.json()) finish normally instead of rejecting.
    if (url.indexOf("/pdf/api/context-sync") !== -1) {
      // The caller verifies the reply against what it asked for -- ok, plus the
      // same enabled and deliveryMode it sent -- so the request body is echoed
      // back. A flat refusal fails that check (BW_READER_CONTEXT_MODE_ACK).
      //
      // The acknowledgement is honest at the level that matters here: the mode
      // being negotiated is how the Pi should relay context, and this page does
      // not route context through the Pi at all. It writes the snapshot locally
      // and the bridge collects it over the socket. So whatever mode is asked
      // for is, trivially, in effect.
      let echo = { ok: true };
      try {
        const sent = init && init.body ? JSON.parse(String(init.body)) : {};
        echo = { ok: true, enabled: sent.enabled, deliveryMode: sent.deliveryMode };
      } catch (_) {}
      if (fetchLog.length < 12) fetchLog.push(`(已拦截,回显) ${url}`);
      return Promise.resolve(
        new Response(JSON.stringify(echo), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        })
      );
    }

    // Serve the outgoing journal locally.
    //
    // Page text does not travel on active-reading -- that carries only
    // kind/file/title/page/selection. It arrives as a page.context event, which
    // the bridge pulls from the Pi's journal: the Reader writes events there,
    // the bridge reads them and forwards them to Windows. An extension page is
    // not in that loop and never will be, which is why the snapshot kept
    // reporting 正文可用: false however the local snapshot was filled.
    //
    // So the journal is answered here instead, in the same shape the Pi returns.
    // Everything downstream is untouched: the module validates, forwards, and
    // acknowledges exactly as it does for a real one.
    if (url.indexOf("/pdf/api/outgoing/journal") !== -1) {
      return Promise.resolve(serveJournal(url));
    }

    return original(input, init).then(
      (res) => {
        if (fetchLog.length < 12) fetchLog.push(`${res.status} ${url}`);
        return res;
      },
      (err) => {
        if (fetchLog.length < 12) fetchLog.push(`✗ ${err && err.name} ${url}`);
        throw err;
      }
    );
  };
})();

// --- local outgoing journal --------------------------------------------------
// Holds the page as one page.context event and hands it over once. The shape
// mirrors reader-specs/fixtures/outgoing-events.jsonl, whose fields are exactly
// the ones the Windows snapshot renders (stable / text_available / text_source /
// fallback_reason / truncated).
const journal = { events: [], head: 0 };

function publishPageContext(ctx) {
  journal.head += 1;
  journal.events.push({
    v: 1,
    seq: journal.head,
    type: "page.context",
    event: "page.context",
    ts: Math.floor(Date.now() / 1000),
    // 12 hex, as the fixture has it.
    id: Array.from({ length: 12 }, () => "0123456789abcdef"[(Math.random() * 16) | 0]).join(""),
    stable: true,
    book_id: ctx.url,
    file: ctx.url,
    page: null,
    title: ctx.title || "",
    kind: "web",
    text_available: !!ctx.text,
    page_context: {
      text: ctx.text || "",
      text_available: !!ctx.text,
      text_source: ctx.text ? "web:innerText" : null,
      // Stated, not hidden: the assistant should know the page was cut rather
      // than treat a truncated page as the whole of it.
      fallback_reason: ctx.text ? null : "扩展未取得正文",
      truncated: (ctx.text || "").length >= 12000,
      reason: "call",
      visual: null,
      embeds: { highlights: 0, blocks: 0, unanchored: [] },
    },
  });
}

function serveJournal(url) {
  let since = 0;
  try {
    const m = /[?&]since=([^&]*)/.exec(url);
    if (m) since = parseInt(decodeURIComponent(m[1]), 10) || 0;
  } catch (_) {}

  // Sequence numbers must run unbroken from since+1 or the module declares a
  // gap and stops the context bridge outright.
  const pending = journal.events.filter((e) => e.seq > since);

  return new Response(
    JSON.stringify({
      ok: true,
      contract: "reader-outgoing-context/1",
      cursor: since,
      head: journal.head,
      events: pending,
      gap: false,
      note: "",
      waited: 0,
      // Says "no long-poll here" rather than leaving the caller to wait out a
      // timeout that will never arrive.
      waitDenied: pending.length === 0,
    }),
    { status: 200, headers: { "Content-Type": "application/json" } }
  );
}

const els = {
  btn: document.getElementById("asst-computer"),
  status: document.getElementById("status"),
  detail: document.getElementById("detail"),
  ctxTitle: document.getElementById("ctxTitle"),
  ctxUrl: document.getElementById("ctxUrl"),
};

// Point ctxSync at the Pi immediately, before anything can use it.
//
// The instrumented log named the culprit: two requests to a bare
// "/pdf/api/context-sync". Relative, so in this page they resolve against
// safari-web-extension://<uuid>/ -- an address that does not exist, which is
// what Safari reports as "TypeError: Load failed". They fired even with no
// context loaded and publishContext never called, so they are ctxSync's own
// doing, not something any call of mine triggered. That is why three builds of
// removing my own calls changed nothing.
//
// With a base set, the same requests address the Pi. They may well be rejected
// there for lack of a session -- but a 401 is an answer, and an answer does not
// reject the promise or take the call down with it.
(function anchorContextSyncBase() {
  try {
    const RC = window.RC;
    if (RC && RC.ctxSync && typeof RC.ctxSync.setBase === "function") {
      RC.ctxSync.setBase("https://bwicarus.taile44d0c.ts.net");
    }
  } catch (_) {}
})();

function say(text, cls) {
  els.status.textContent = text;
  els.status.className = cls || "";
}

function detail(lines) {
  els.detail.style.display = "block";
  els.detail.textContent = lines.join("\n");
}

// Errors reach the user through the device screen or not at all -- there is no
// console to open on an iPad. Anything thrown gets flattened rather than shown
// as the useless "[object Object]".
function describe(err) {
  if (!err) return "(no error object)";
  const parts = [];
  if (err.code) parts.push("code=" + err.code);
  if (err.name && err.name !== "Error") parts.push("name=" + err.name);
  if (err.message) parts.push(err.message);
  if (!parts.length) {
    try { return JSON.stringify(err); } catch { return String(err); }
  }
  return parts.join(" | ");
}

// --- self-check -------------------------------------------------------------
// Runs before any user action, because a missing dependency and a refused
// connection are different problems and must not present identically.
function selfCheck() {
  const notes = [];
  const RC = window.RC;

  if (!RC) {
    say("✗ rc-core.js 未加载", "err");
    detail(["window.RC 不存在 —— vendor/rc-core.js 没有被打包进扩展。"]);
    return false;
  }
  notes.push("RC ✓");
  notes.push("RC.ctxSync " + (RC.ctxSync ? "✓" : "缺失(降级)"));
  notes.push("RC.toast " + (RC.toast ? "✓" : "缺失(降级)"));
  notes.push("RC.outgoing " + (RC.outgoing ? "✓" : "缺失(降级)"));

  if (!RC.computerVoice || typeof RC.computerVoice.startFromUserGesture !== "function") {
    say("✗ rc-computer-voice.js 未加载", "err");
    detail(notes.concat(["RC.computerVoice.startFromUserGesture 不可用。"]));
    return false;
  }
  notes.push("RC.computerVoice ✓");

  // The gesture capture consults this before preparing anything; if the module
  // is absent it returns early and no audio lease is ever taken, which later
  // surfaces as GESTURE_REQUIRED at the point of dialling rather than here.
  let gestureAllowed = null;
  try {
    gestureAllowed = RC.voicecall
      && typeof RC.voicecall.canCaptureComputerVoiceGesture === "function"
      ? RC.voicecall.canCaptureComputerVoiceGesture()
      : null;
  } catch (err) {
    gestureAllowed = "抛错: " + describe(err);
  }
  notes.push("手势许可 " + (gestureAllowed === true ? "✓"
    : gestureAllowed === null ? "✗ rc-voicecall.js 未加载" : "✗ " + gestureAllowed));

  // Registration is what makes this button a trusted approval surface. It is
  // rejected silently on any mismatch -- wrong id, wrong type, detached node --
  // so the boolean is checked rather than assumed.
  const registered = RC.computerVoice.registerComputerButton(els.btn);
  notes.push("按钮注册 " + (registered ? "✓" : "✗ 被拒(id/type/挂载不合要求)"));

  if (!registered || gestureAllowed !== true) {
    say("✗ 无法接受点击", "err");
    detail(notes);
    return false;
  }

  try {
    const target = RC.computerVoice.getTargetApp && RC.computerVoice.getTargetApp();
    notes.push("目标 App: " + (target || "(默认)"));
  } catch (err) {
    notes.push("目标 App 读取失败: " + describe(err));
  }

  detail(notes);
  return true;
}

// --- page context -----------------------------------------------------------
// The popup captures the active tab and leaves it here. This page has no access
// to the page being read: it is a separate origin, and by the time it opens the
// tab may not even be frontmost.
async function loadContext() {
  try {
    const bag = await chrome.storage.local.get("bwCallContext");
    const ctx = bag && bag.bwCallContext;
    if (!ctx || !ctx.url) return null;

    // Stale context is worse than none: answering about the wrong page is a
    // silent error, whereas an empty card is visibly empty.
    if (ctx.capturedAt && Date.now() - ctx.capturedAt > 5 * 60 * 1000) return null;

    els.ctxTitle.textContent = ctx.title || "(无标题)";
    els.ctxUrl.textContent = ctx.url
      + (ctx.text ? `　·　正文 ${ctx.text.length} 字` : "　·　无正文");
    // Appended to the self-check block: a refused report is silent otherwise,
    // and would show up only as an assistant that answers about nothing.
    els.detail.textContent += "\n上下文 " + publishContext(ctx);
    return ctx;
  } catch {
    return null;
  }
}

// Hand the page to RC.ctxSync, which is where rc-computer-voice reads the
// active-reading snapshot from -- there is no other inlet. kind:"web" is a
// first-class case there, so nothing needs inventing; it just has to be filled.
//
// Reported as "web" and never as a book: the assistant must talk about the page
// in front of the user, and inheriting a Reader book here would be the silent
// kind of wrong -- confident answers about the wrong document.
const READER_ORIGIN = "https://bwicarus.taile44d0c.ts.net";

function publishContext(ctx) {
  const RC = window.RC;
  if (!RC || !RC.ctxSync) return "✗ RC.ctxSync 不可用";
  try {
    // Required for kind:"web": report() refuses it outright when no base is set,
    // because an extension runs on other people's sites and the sync target
    // cannot be inferred from the current origin.
    if (typeof RC.ctxSync.setBase === "function") RC.ctxSync.setBase(READER_ORIGIN);

    // Neither setEnabled() nor report() is used, and both were tried: each ends
    // in a request to the Pi that this origin cannot make. setEnabled POSTs the
    // preference; report() finishes with _ctxSchedule(), queueing the upload
    // that feeds the Pi→Windows snapshot path. Both surfaced as
    // "TypeError: Load failed" and took the call down with them (1.0.9, 1.0.10).
    //
    // That upload path is not how this context reaches Windows. The bridge polls
    // localActiveReadingSnapshot(), which reads _state().pend directly and sends
    // it over the socket as active-reading -- no network of its own, and it does
    // not consult the enable flag either. So the snapshot is written straight
    // in, and the Pi is left out of a conversation it is not part of.
    const state = typeof RC.ctxSync._state === "function" ? RC.ctxSync._state() : null;
    if (!state) return "✗ 无法访问 ctxSync 状态";

    state.pend = {
      kind: "web",
      url: ctx.url,
      // Windows pairs active-reading with the page.context event by
      // (file, page). The event declares page 0, so the snapshot must say the
      // same or the pairing never completes and context stays pending -- which
      // is precisely what the snapshot showed: active-reading only, page null.
      pos: 0,
      title: ctx.title || "",
      text: ctx.text || "",
      selection: ctx.selection || "",
    };
    // Stale canonical belongs to whatever document was described before; leaving
    // it attached is how one page's title ends up on another's content.
    state.canonical = null;

    // The snapshot carries position and selection; the text rides the journal.
    publishPageContext(ctx);
    return ctx.text
      ? `✓ 已就绪(快照 + 正文 ${ctx.text.length} 字)`
      : "✓ 已就绪(仅位置,无正文)";
  } catch (err) {
    return "✗ " + describe(err);
  }
}

// --- call lifecycle ---------------------------------------------------------
let active = false;
// Kept so the call can hand the page to Windows once the session is up.
let pageContext = null;

async function start() {
  els.btn.disabled = true;
  say("正在连接…");

  try {
    // Must be awaited inside the click handler: iOS grants microphone access
    // only while a user gesture is being processed, and rc-computer-voice
    // requests capture as part of starting.
    await window.RC.computerVoice.startFromUserGesture({});
    active = true;

    // Sent after the call is up, because the action requires an authenticated
    // active session. This is the one path by which page text reaches Windows;
    // a failure here costs the assistant the page contents but not the call, so
    // it is reported rather than thrown.
    if (pageContext) {
      window.RC.computerVoice
        .sendWebPageContext({
          url: pageContext.url,
          title: pageContext.title,
          text: pageContext.text,
          selection: pageContext.selection,
          observedAtEpochMs: pageContext.capturedAt,
        })
        .then((ack) => {
          // The older context ACK reports outcome and seq, not revision.
          const outcome = ack?.outcome ?? "?";
          const seq = ack?.seq ?? ack?.cursor ?? "?";
          els.detail.textContent += `\n网页上下文 ✓ ${outcome} (seq ${seq})`;
          // Only after the first send lands: following sends deltas against it,
          // so starting before it is acknowledged would leave the bridge with a
          // later page and no baseline.
          startFollowing();
        })
        .catch((err) => {
          els.detail.textContent += "\n网页上下文 ✗ " + describe(err);
        });
    }
    els.btn.disabled = false;
    els.btn.textContent = "结束通话";
    els.btn.classList.add("stop");
    say("● 通话中", "ok");
  } catch (err) {
    els.btn.disabled = false;
    say("✗ 连接失败", "err");
    detail([
      describe(err),
      "",
      fetchLog.length ? "本页发出的请求:" : "本页未发出任何请求",
      ...fetchLog.map((line) => "  " + line),
    ]);
  }
}

async function stop() {
  els.btn.disabled = true;
  try {
    await window.RC.computerVoice.stop();
  } catch (err) {
    detail(["停止时报错(通话可能已断): " + describe(err)]);
  }
  active = false;
  // Otherwise the interval keeps scripting the tab after the call is over.
  stopFollowing();
  els.btn.disabled = false;
  els.btn.textContent = "开始通话";
  els.btn.classList.remove("stop");
  say("已结束");
}

// --- keeping the page current ------------------------------------------------
// The first send is a photograph; this keeps it a view. While the call is up the
// tab is re-read on an interval and a fresh page.context goes out whenever it
// actually changed -- scrolling into new text, selecting something, or the page
// itself updating.
//
// Polling rather than listening in the page: a listener would mean permanent
// code in every site, which is what broke the popup outright once already. This
// costs one scripted read every few seconds and leaves nothing behind.
//
// Scope is the tab the call started from. Following the user across tabs would
// need host access to every site, and package_safari.py deliberately forbids
// that ("host permission must remain restricted to the active Pi"). That line is
// a decision, not an oversight, so it stands until its owner says otherwise.
const FOLLOW_INTERVAL_MS = 4000;
let followTimer = null;
let lastSignature = "";

async function readTab(tabId) {
  if (tabId == null || !chrome.scripting?.executeScript) return null;
  try {
    const [res] = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => {
        const raw = String(document.body?.innerText || "");
        const text = raw.replace(/[ \t]+/g, " ").replace(/\n{3,}/g, "\n\n").trim();
        let selection = "";
        try {
          selection = String(window.getSelection() || "").trim();
        } catch (_) {}
        return {
          url: String(location.href || ""),
          title: String(document.title || ""),
          text: text.slice(0, 12000),
          selection: selection.slice(0, 2000),
        };
      },
    });
    return res?.result || null;
  } catch {
    // Expected once the tab navigates away: activeTab lapses with it. Not an
    // error worth surfacing -- the call carries on with the last known page.
    return null;
  }
}

function startFollowing() {
  if (followTimer || !pageContext?.tabId) return;
  lastSignature = `${pageContext.url}|${(pageContext.text || "").length}|${pageContext.selection || ""}`;

  followTimer = setInterval(async () => {
    if (!active) return;
    const fresh = await readTab(pageContext.tabId);
    if (!fresh) return;

    // Compared before sending: the bridge would otherwise receive an identical
    // page every few seconds, and each one costs a sequence number and a round
    // trip for nothing.
    const signature = `${fresh.url}|${fresh.text.length}|${fresh.selection}`;
    if (signature === lastSignature) return;
    lastSignature = signature;

    try {
      await window.RC.computerVoice.sendWebPageContext({
        url: fresh.url,
        title: fresh.title,
        text: fresh.text,
        selection: fresh.selection,
        observedAtEpochMs: Date.now(),
      });
      els.ctxUrl.textContent =
        `${fresh.url}　·　正文 ${fresh.text.length} 字` +
        (fresh.selection ? `　·　选中 ${fresh.selection.length} 字` : "");
    } catch (err) {
      els.detail.textContent += "\n跟随更新 ✗ " + describe(err);
    }
  }, FOLLOW_INTERVAL_MS);
}

function stopFollowing() {
  if (followTimer) clearInterval(followTimer);
  followTimer = null;
}

els.btn.addEventListener("click", () => (active ? stop() : start()));

// The bridge can drop the call on its own -- Windows side stopping, network
// loss, sequence guard tripping. Without this the button would keep claiming
// the call is live.
if (window.RC && window.RC.computerVoice && window.RC.computerVoice.onStatus) {
  window.RC.computerVoice.onStatus((s) => {
    if (!s) return;
    if (s.error) detail(["桥接状态: " + describe(s.error)]);
    if (active && s.active === false) {
      active = false;
      els.btn.textContent = "开始通话";
      els.btn.classList.remove("stop");
      // The bridge can end the call without the button being touched, so the
      // interval has to be released here too.
      stopFollowing();
      say("通话已断开", "err");
    }
  });
}

// Switch the bridge to the delivery mode this page can actually feed.
//
// Under snapshot-mcp, Windows pulls context from the Pi itself and nothing the
// extension does can reach it -- which is why 1.0.18's journal was never even
// requested, and the snapshot kept showing only active-reading events. Under
// legacy-inject the module pulls the outgoing journal instead, and that request
// is one this page answers.
//
// Done at load rather than on the click: it awaits a round trip, and awaiting
// inside the gesture would risk the audio lease taken during it.
async function useLegacyDelivery() {
  const RC = window.RC;
  if (!RC?.computerVoice?.setContextDeliveryMode) return "✗ 接口不可用";
  try {
    await RC.computerVoice.setContextDeliveryMode("legacy-inject");
    return "✓ legacy-inject";
  } catch (err) {
    return "✗ " + describe(err);
  }
}

(async function init() {
  if (!selfCheck()) {
    els.btn.disabled = true;
    return;
  }
  // Deliberately left on snapshot-mcp. Switching to legacy-inject did make the
  // module pull the journal this page can answer, but the two modes are
  // different delivery mechanisms, not better and worse ones: the Windows
  // snapshot view exists only under snapshot-mcp. Legacy traded a working
  // feature (position, selection, freshness -- all visible and correct) for one
  // that did not arrive anyway. Keeping the mode the bridge was configured for.
  const ctx = await loadContext();
  // Kept for start(), which cannot reach this scope's local. Missing this
  // assignment made the guard in start() permanently false, so the page was
  // never sent at all -- 1.0.22 could not have worked whatever else was right.
  pageContext = ctx;
  if (!ctx) {
    els.ctxTitle.textContent = "（未取得网页上下文）";
    els.ctxUrl.textContent = "AI 将听不到当前网页内容";
  }
  say("就绪");
})();
