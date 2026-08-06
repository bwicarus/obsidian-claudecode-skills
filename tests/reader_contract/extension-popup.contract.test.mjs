import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import vm from "node:vm";

const SOURCE = readFileSync(
  new URL("../../extensions/bw-reader-webext/popup.js", import.meta.url),
  "utf8",
);
const HTML = readFileSync(
  new URL("../../extensions/bw-reader-webext/popup.html", import.meta.url),
  "utf8",
);
const tick = () => new Promise((resolve) => setImmediate(resolve));

function harness({
  syncStatus = {
    contract: "sync-conflict-control/1",
    owner: "extension-background",
    state: "ready",
    at: 1,
    conflictCount: 0,
    truncated: false,
    conflicts: [],
  },
} = {}) {
  const elements = new Map();
  const messages = [];
  const tabQueries = [];
  const element = (id) => {
    const listeners = new Map();
    return {
      id,
      value: "",
      textContent: "",
      placeholder: "",
      disabled: false,
      hidden: false,
      children: [],
      addEventListener(type, listener) {
        listeners.set(type, listener);
      },
      appendChild(child) {
        this.children.push(child);
        return child;
      },
      replaceChildren(...children) {
        this.children = [...children];
      },
      async emit(type) {
        return listeners.get(type)?.();
      },
    };
  };
  for (const id of [
    "token",
    "status",
    "save",
    "test",
    "sync-status",
    "sync-conflicts",
  ]) {
    elements.set(`#${id}`, element(id));
  }
  const document = {
    querySelector(selector) {
      return elements.get(selector) || null;
    },
    getElementById(id) {
      return elements.get(`#${id}`) || null;
    },
    createElement(tagName) {
      return element(String(tagName || "").toLowerCase());
    },
  };
  const responseData = {
    namespace: `acct-v1-${"a".repeat(64)}`,
    credential: {
      configured: true,
      candidateCount: 1,
      inactiveCandidateCount: 0,
    },
    legacyQuarantine: {
      apiToken: { quarantined: true, present: false, bytes: 0 },
    },
    sync: structuredClone(syncStatus),
  };
  const chrome = {
    tabs: {
      async query(query) {
        tabQueries.push(structuredClone(query));
        return [{ id: 77 }];
      },
    },
    runtime: {
      async sendMessage(message) {
        messages.push(structuredClone(message));
        return { ok: true, data: structuredClone(responseData) };
      },
    },
  };
  vm.runInContext(SOURCE, vm.createContext({
    document,
    chrome,
    console,
    Number,
    Object,
    String,
    Error,
  }), { filename: "popup.js" });
  return { elements, messages, tabQueries };
}

test("popup 账户逻辑不读 storage，只有通话上下文暂存可写入", () => {
  const voiceEntryOffset = SOURCE.indexOf("// Both one-off probes");
  assert.ok(voiceEntryOffset > 0);
  assert.equal(SOURCE.slice(0, voiceEntryOffset).includes("chrome.storage"), false);
  assert.equal(SOURCE.includes("chrome.storage.local.get"), false);
  assert.equal(
    (SOURCE.match(/chrome\.storage\.local\.set/g) || []).length,
    1,
  );
  assert.equal(SOURCE.includes("bwCallContext"), true);
  assert.equal(SOURCE.includes("apiToken"), false);
  for (const type of [
    "BW_ACCOUNT_STATUS",
    "BW_ACCOUNT_TOKEN_SAVE",
    "BW_ACCOUNT_TOKEN_TEST",
  ]) {
    assert.equal(SOURCE.includes(type), true);
  }
  assert.equal(SOURCE.includes("BW_SYNC_RETRY_AFTER_RESOLUTION"), false);
  assert.equal(SOURCE.includes("conflictSetId"), false);
  assert.equal(HTML.includes("sync-retry"), false);
});

test("popup 不再展示或发送旧电脑语音配对入口", () => {
  for (const obsolete of [
    "BW_COMPUTER_VOICE_PAIR",
    "computer-voice-pair-id",
    "computer-voice-code",
    "一次性配对码",
    "Reader 配对 ID",
  ]) {
    assert.equal(SOURCE.includes(obsolete), false);
    assert.equal(HTML.includes(obsolete), false);
  }
});

test("popup 始终把操作绑定当前活动 tab，保存后清空输入且不显示明文", async () => {
  const h = harness();
  await tick();
  assert.deepEqual(h.tabQueries[0], { active: true, currentWindow: true });
  const initialStatus = h.messages.find(
    (message) => message.type === "BW_ACCOUNT_STATUS",
  );
  assert.deepEqual(initialStatus.target, { tabId: 77, frameId: 0 });

  const token = h.elements.get("#token");
  token.value = "popup-secret-token";
  await h.elements.get("#save").emit("click");
  const save = h.messages.find((message) => message.type === "BW_ACCOUNT_TOKEN_SAVE");
  assert.deepEqual(save.target, { tabId: 77, frameId: 0 });
  assert.equal(save.payload.token, "popup-secret-token");
  assert.equal(token.value, "");
  assert.equal(
    h.elements.get("#status").textContent.includes("popup-secret-token"),
    false,
  );

  await h.elements.get("#test").emit("click");
  const testMessage = h.messages.find(
    (message) => message.type === "BW_ACCOUNT_TOKEN_TEST",
  );
  assert.deepEqual(testMessage.target, { tabId: 77, frameId: 0 });
});

test("popup 只显示冲突白名单摘要，并明确保持只读安全暂停", async () => {
  const conflictSetId = `conflict-set-v1-${"d".repeat(32)}`;
  const rawRecord = "raw-record-must-not-render";
  const upstreamText = "upstream database exception must not render";
  const namespace = `acct-v1-${"e".repeat(64)}`;
  const h = harness({
    syncStatus: {
      contract: "sync-conflict-control/1",
      owner: "extension-background",
      state: "blocked",
      at: 55,
      conflictSetId,
      conflictCount: 1,
      truncated: false,
      conflicts: [{
        lane: "server",
        collection: "user-settings",
        id: "theme",
        reason: "revision-conflict",
        incomingRev: 2,
        currentRev: 1,
        rawRecord,
        upstreamText,
      }],
      namespace,
      token: "popup-sync-private-token",
      rawRecord,
      upstreamText,
    },
  });
  await tick();
  await tick();

  const summary = [
    h.elements.get("#sync-status").textContent,
    ...h.elements.get("#sync-conflicts").children.map(
      (item) => item.textContent,
    ),
  ].join("\n");
  assert.ok(summary.length > 0);
  assert.equal(summary.includes(rawRecord), false);
  assert.equal(summary.includes(upstreamText), false);
  assert.equal(summary.includes(namespace), false);
  assert.equal(summary.includes("popup-sync-private-token"), false);
  assert.equal(summary.includes(conflictSetId), false);
  assert.match(
    summary,
    /同步已安全暂停，完整裁决器尚未启用，不自动选择本地\/服务器版本。/,
  );
  assert.equal(h.elements.has("#sync-retry"), false);
  assert.equal(
    h.messages.some(
      (message) => message.type === "BW_SYNC_RETRY_AFTER_RESOLUTION",
    ),
    false,
  );
});

test("popup error 状态只显示安全错误码与重试提示，不泄露原始错误", async () => {
  const rawMessage = "upstream authentication response must not render";
  const nestedMessage = "database connection detail must not render";
  const h = harness({
    syncStatus: {
      contract: "sync-conflict-control/1",
      owner: "extension-background",
      state: "error",
      at: 88,
      errorCode: "BW_SYNC_AUTH",
      retryable: true,
      conflictCount: 0,
      truncated: false,
      conflicts: [],
      error: rawMessage,
      lastError: {
        code: "BW_SYNC_AUTH",
        message: nestedMessage,
      },
    },
  });
  await tick();
  await tick();

  const rendered = [
    h.elements.get("#sync-status").textContent,
    ...h.elements.get("#sync-conflicts").children.map(
      (item) => item.textContent,
    ),
  ].join("\n");
  assert.match(rendered, /同步失败/);
  assert.match(rendered, /错误码：BW_SYNC_AUTH/);
  assert.match(rendered, /将自动重试/);
  assert.equal(rendered.includes(rawMessage), false);
  assert.equal(rendered.includes(nestedMessage), false);
});
