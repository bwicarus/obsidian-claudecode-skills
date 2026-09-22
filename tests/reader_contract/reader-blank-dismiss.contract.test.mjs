import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const SIDEDRAWER = readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-sidedrawer.js", import.meta.url),
  "utf8",
);
const CARET = readFileSync(
  new URL(
    "../../_server_deploy/static/pdf/reader.src/16-caret-select.js",
    import.meta.url,
  ),
  "utf8",
);
const VOICECALL = readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-voicecall.js", import.meta.url),
  "utf8",
);
const STICKY = readFileSync(
  new URL("../../_server_deploy/static/pdf/rc-stickynote.js", import.meta.url),
  "utf8",
);

function sourceFunction(source, name, nextName) {
  const start = source.indexOf(`function ${name}`);
  const end = source.indexOf(`function ${nextName}`, start);
  assert.ok(start >= 0 && end > start, `${name} source must exist`);
  return vm.runInNewContext(`(${source.slice(start, end).trim()})`);
}

function sourceBlock(source, firstName, nextName) {
  const start = source.indexOf(`function ${firstName}`);
  const end = source.indexOf(`function ${nextName}`, start);
  assert.ok(start >= 0 && end > start, `${firstName} source block must exist`);
  return source.slice(start, end).trim();
}

function fakeTarget(matches = []) {
  return {
    nodeType: 1,
    closest(selector) {
      return matches.some((candidate) => selector.includes(candidate))
        ? this
        : null;
    },
  };
}

test("passive sidebar content dismisses reader transients without stealing controls", () => {
  const start = SIDEDRAWER.indexOf("function _sideBlankDismissTarget");
  const end = SIDEDRAWER.indexOf("function buildChrome", start);
  const contract = SIDEDRAWER.slice(start, end);

  assert.ok(start >= 0 && end > start);
  assert.match(contract, /event\.composedPath/,
    "shadow-hosted controls must be classified by their real composed target");
  assert.match(
    contract,
    /button,a,input,textarea,select,option,label,summary,details/,
  );
  assert.match(
    contract,
    /\.vc-card,[\s\S]*?\.rv-improve-panel,[\s\S]*?#ep-side-settings/,
    "cards, the improvement sheet and settings keep their own clicks",
  );
  assert.match(contract, /path\.indexOf\(side\) >= 0/,
    "composed paths must prove that the event passed through the sidebar");
  assert.match(contract, /side\.contains\(event\.target\)/,
    "ordinary and legacy events retain a DOM containment fallback");
  const interactiveStart = contract.indexOf("var interactive");
  const interactiveEnd = contract.indexOf("try { if (target.closest", interactiveStart);
  const interactiveContract = contract.slice(interactiveStart, interactiveEnd);
  assert.doesNotMatch(
    interactiveContract,
    /\[data-pane\]|\.asst-msg|\.rc-turn|\.asst-tool|#asst-turnrail/,
    "pane and message containers are passive content, not controls",
  );
  assert.match(contract, /adapter\.clearSelection\(\)/);
  assert.match(contract, /CustomEvent\('rc:dismiss-transients'/);
  // buildChrome 现在收 force 参数（2026-09-22 无壳模式：App 里默认不建这套
  // 网页外壳，真要看旧界面时才补建）。约定本身没变：**只要把外壳建了出来，
  // 就必须绑上点空白关闭**（模板自带的和兑底新建的都算）。
  assert.match(
    SIDEDRAWER,
    /function buildChrome\(force\)[\s\S]*?_bindSideBlankDismiss\(side\)/,
    "the blank-dismiss contract must be bound for both existing and fallback drawers",
  );
  // 无壳分支跳过绑定是故意的：那时根本没有可点的外壳。
  // 但它必须在绑定**之前**返回，否则就又变成"建了再藏"。
  const shelllessAt = SIDEDRAWER.indexOf("shelllessDrawer() && force !== true");
  assert.ok(shelllessAt > 0 &&
            shelllessAt < SIDEDRAWER.indexOf("_bindSideBlankDismiss(side);"),
            "shell-less branch must return before the chrome is bound");
});

test("sidebar pane and message bodies dismiss, while real controls remain interactive", () => {
  const shouldDismiss = sourceFunction(
    SIDEDRAWER,
    "_sideBlankDismissTarget",
    "_bindSideBlankDismiss",
  );
  const shadowHost = fakeTarget();
  const paneBody = fakeTarget(["[data-pane]"]);
  const messageBody = fakeTarget([".asst-msg", ".rc-turn"]);
  const button = fakeTarget(["button"]);
  const side = {
    contains(target) {
      return target === shadowHost || target === messageBody || target === button;
    },
  };

  assert.equal(shouldDismiss({
    target: shadowHost,
    composedPath: () => [paneBody, side, shadowHost],
  }, side), true, "blank pane content must dismiss through a shadow boundary");
  assert.equal(shouldDismiss({
    target: messageBody,
    composedPath: () => [messageBody, side],
  }, side), true, "conversation text is passive sidebar content");
  assert.equal(shouldDismiss({
    target: button,
    composedPath: () => [button, side],
  }, side), false, "buttons keep their own interaction");
});

test("sidebar binding clears and broadcasts once for passive content only", () => {
  let clearCount = 0;
  const events = [];
  const listeners = new Map();
  const context = {
    RC: {
      adapter() {
        return {
          clearSelection() {
            clearCount += 1;
            return Promise.resolve();
          },
        };
      },
    },
    window: {
      dispatchEvent(event) {
        events.push(event);
      },
    },
    CustomEvent: class CustomEvent {
      constructor(type, init) {
        this.type = type;
        this.detail = init && init.detail;
      }
    },
  };
  vm.runInNewContext(
    `${sourceBlock(SIDEDRAWER, "_sideBlankDismissTarget", "buildChrome")};\n` +
      "this.bindSideBlankDismiss = _bindSideBlankDismiss;",
    context,
  );

  const messageText = fakeTarget([".asst-msg", ".rc-turn", "[data-pane]"]);
  const button = fakeTarget(["button", ".asst-msg", "[data-pane]"]);
  const shadowHost = fakeTarget();
  const shadowButton = fakeTarget(["button"]);
  const side = {
    contains(target) {
      return target === messageText || target === button || target === shadowHost;
    },
    addEventListener(type, listener, capture) {
      listeners.set(type, { listener, capture });
    },
  };
  context.bindSideBlankDismiss(side);
  const binding = listeners.get("pointerdown");
  assert.ok(binding, "sidebar must bind pointerdown");
  assert.equal(binding.capture, true, "dismissal must run before child bubbling handlers");

  let prevented = 0;
  let stopped = 0;
  binding.listener({
    target: messageText,
    composedPath: () => [messageText, side],
    preventDefault: () => { prevented += 1; },
    stopPropagation: () => { stopped += 1; },
  });
  assert.equal(clearCount, 1);
  assert.equal(events.length, 1);
  assert.equal(events[0].type, "rc:dismiss-transients");
  assert.equal(events[0].detail.source, "sidebar-blank");

  binding.listener({
    target: button,
    composedPath: () => [button, side],
    preventDefault: () => { prevented += 1; },
    stopPropagation: () => { stopped += 1; },
  });
  binding.listener({
    target: shadowHost,
    composedPath: () => [shadowButton, {}, shadowHost, side],
    preventDefault: () => { prevented += 1; },
    stopPropagation: () => { stopped += 1; },
  });
  assert.equal(clearCount, 1, "real controls must not trigger selection cleanup");
  assert.equal(events.length, 1, "real controls must not broadcast dismissal");
  assert.equal(prevented, 0, "sidebar clicks must never be swallowed");
  assert.equal(stopped, 0, "sidebar clicks must keep their normal propagation");
});

test("reader blank and sidebar blank share one complete selection cleanup", () => {
  const start = CARET.indexOf("function _shouldCloseToolbar");
  const end = CARET.indexOf("// 手写起笔时调", start);
  const contract = CARET.slice(start, end);

  assert.ok(start >= 0 && end > start);
  assert.match(contract, /target\?\.closest\?\.\('\.char-layer'\)/,
    "character selection owns clicks inside the PDF text layer");
  assert.match(
    contract,
    /button,a,input,textarea,select,option,label,summary,details/,
  );
  assert.match(contract, /function _dismissSelectionTransients\(\)/);
  assert.match(contract, /window\.getSelection\(\)\.removeAllRanges\(\)/);
  assert.match(contract, /lastSelText = ''/);
  assert.match(contract, /_charSel = null/);
  assert.match(
    contract,
    /window\.addEventListener\('rc:dismiss-transients', _dismissSelectionTransients\)/,
    "sidebar blanks and reader blanks must clear the same transient state",
  );
});

test("sidebar blank collapses persistent cards and closes only transient floats", () => {
  assert.match(
    VOICECALL,
    /addEventListener\('rc:dismiss-transients',[\s\S]*?classList\.contains\('vc-hasdot'\)[\s\S]*?_cardForm\(card\.el, 'dot'\)[\s\S]*?_cardClose\(card\)/,
  );
  assert.match(
    STICKY,
    /window\.addEventListener\('rc:dismiss-transients', onDocDown\)/,
    "pinned cards must reuse the same outside-click lowering path",
  );
});
