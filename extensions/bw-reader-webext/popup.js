const tokenInput = document.querySelector("#token");
const status = document.querySelector("#status");
const saveButton = document.querySelector("#save");
const testButton = document.querySelector("#test");
const syncStatus = document.querySelector("#sync-status");
const syncConflicts = document.querySelector("#sync-conflicts");

async function activeTarget() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  const tab = Array.isArray(tabs) ? tabs[0] : null;
  if (!Number.isInteger(tab?.id)) throw new Error("找不到当前活动页面");
  return { tabId: tab.id, frameId: 0 };
}

async function accountMessage(type, payload = null) {
  const target = await activeTarget();
  const response = await chrome.runtime.sendMessage({ type, target, payload });
  if (!response?.ok) {
    const error = new Error(response?.error || "扩展账户服务不可用");
    error.code = response?.code || "BW_ACCOUNT_POPUP";
    throw error;
  }
  return response.data || {};
}

function renderStatus(data, prefix = "") {
  const credential = data?.credential || {};
  const namespace = String(data?.namespace || "");
  const accountLabel = namespace
    ? namespace.slice(0, 12) + "…" + namespace.slice(-6)
    : "未知账户";
  const quarantined = Object.values(data?.legacyQuarantine || {})
    .filter((item) => item?.present).length;
  const parts = [
    prefix,
    `当前账户：${accountLabel}`,
    credential.configured
      ? `已验证设备令牌（历史候选 ${credential.candidateCount || 1} 个）`
      : "尚未保存当前账户的设备令牌"
  ].filter(Boolean);
  if (quarantined) {
    parts.push(`发现 ${quarantined} 项旧版裸数据，已隔离且未采用`);
  }
  status.textContent = parts.join("\n");
  tokenInput.placeholder = credential.configured
    ? "已保存；如需更换，请粘贴新令牌"
    : "从 /profile/ 创建并粘贴";
  renderSyncStatus(data?.sync);
}

function renderSyncStatus(value) {
  const sync = value && value.contract === "sync-conflict-control/1"
    ? value
    : null;
  syncConflicts.replaceChildren();
  syncConflicts.hidden = true;
  if (!sync) {
    syncStatus.textContent = "同步状态暂不可用";
    return;
  }
  const labels = {
    ready: "未发现阻断冲突",
    syncing: "正在同步",
    blocked: "发现需要人工确认的冲突",
    "conflict-observed": "已观察到非阻断冲突",
    error: "同步失败",
    paused: "同步已暂停",
    resolving: "正在重新检查",
    destroyed: "同步服务已停止"
  };
  const count = Math.max(0, Number(sync.conflictCount) || 0);
  const errorCode = /^[A-Z][A-Z0-9_]{0,79}$/.test(
    String(sync.errorCode || "")
  ) ? String(sync.errorCode) : "";
  syncStatus.textContent = [
    labels[sync.state] || "同步状态未知",
    sync.state === "error" && errorCode
      ? `错误码：${errorCode}${sync.retryable === true ? "（将自动重试）" : "（需要检查账户或配置）"}`
      : "",
    count ? `冲突 ${count} 项${sync.truncated ? "（仅显示前 50 项）" : ""}` : "",
    sync.state === "blocked"
      ? "同步已安全暂停，完整裁决器尚未启用，不自动选择本地/服务器版本。"
      : ""
  ].filter(Boolean).join("\n");
  const conflicts = Array.isArray(sync.conflicts)
    ? sync.conflicts.slice(0, 50)
    : [];
  if (!conflicts.length) return;
  for (const conflict of conflicts) {
    const item = document.createElement("li");
    const identity = [conflict.collection, conflict.id]
      .map((part) => String(part || "").trim())
      .filter(Boolean)
      .join(" · ");
    const revisions = `r${Math.max(0, Number(conflict.incomingRev) || 0)}` +
      ` → r${Math.max(0, Number(conflict.currentRev) || 0)}`;
    item.textContent = [
      String(conflict.lane || ""),
      identity,
      String(conflict.reason || "conflict"),
      revisions
    ].filter(Boolean).join(" · ");
    syncConflicts.appendChild(item);
  }
  syncConflicts.hidden = false;
}

async function loadStatus() {
  status.textContent = "正在读取扩展当前账户……";
  try {
    renderStatus(await accountMessage("BW_ACCOUNT_STATUS"));
  } catch (error) {
    status.textContent = "✗ " + (error?.message || String(error));
    renderSyncStatus(null);
  }
}

saveButton.addEventListener("click", async () => {
  const token = tokenInput.value.trim();
  if (!token) {
    status.textContent = "✗ 请先粘贴设备令牌";
    return;
  }
  saveButton.disabled = true;
  testButton.disabled = true;
  status.textContent = "正在核对令牌所属扩展账户……";
  try {
    const data = await accountMessage("BW_ACCOUNT_TOKEN_SAVE", { token });
    tokenInput.value = "";
    renderStatus(data, "✓ 已保存到当前账户");
  } catch (error) {
    status.textContent = "✗ " + (error?.message || String(error));
  } finally {
    saveButton.disabled = false;
    testButton.disabled = false;
  }
});

testButton.addEventListener("click", async () => {
  saveButton.disabled = true;
  testButton.disabled = true;
  status.textContent = "正在验证当前账户令牌……";
  try {
    const data = await accountMessage("BW_ACCOUNT_TOKEN_TEST");
    renderStatus(data, "✓ 连接正常");
  } catch (error) {
    status.textContent = "✗ " + (error?.message || String(error));
  } finally {
    saveButton.disabled = false;
    testButton.disabled = false;
  }
});

// Both one-off probes that stood here have served their purpose: extension
// pages can obtain the microphone, and they can open a WebSocket to the bridge
// host without any manifest change. Those two results are what justify running
// the call in the extension instead of handing it to the App, so the probes are
// gone and the real entry point takes their place.
const voiceStatus = document.getElementById("voice-status");

// The page reports itself now, so there is nothing here to start. This only
// says whether it managed to reach Windows -- a page whose own CSP blocks the
// connection would otherwise look identical to one that is working.
if (voiceStatus) {
  (async () => {
    try {
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
      if (tab?.id == null) { voiceStatus.textContent = "无法读取当前标签"; return; }
      const reply = await chrome.tabs.sendMessage(tab.id, { type: "BW_CTX_STATUS" });
      if (reply?.ready) voiceStatus.textContent = "✓ 已连接,本页内容正在同步";
      else if (reply) voiceStatus.textContent = "○ 本页未连接";
      else voiceStatus.textContent = "本页未运行扩展内容脚本";
    } catch {
      voiceStatus.textContent = "本页未运行扩展内容脚本";
    }
  })();
}

// Temporary diagnostic readout. The voice failure produces no record on Windows,
// cannot be shown in a 42px frame, and long-press does not surface titles
// reliably on iPad -- so the frame writes its progress to storage and it is read
// back here. Remove together with trace() in inline-computer-voice.js.
const traceBox = document.getElementById("voice-trace");
const traceClear = document.getElementById("trace-clear");

async function renderTrace() {
  if (!traceBox) return;
  try {
    const bag = await chrome.storage.local.get("bwVoiceTrace");
    const list = bag?.bwVoiceTrace || [];
    if (!list.length) {
      // An empty trail is itself a finding: the frame never ran.
      traceBox.textContent =
        "（无记录）\n按过电脑按钮后仍为空，说明 iframe 从未加载。";
      return;
    }
    traceBox.textContent = list
      .map((e) => `${e.at}  ${e.stage}` + (e.detail ? "\n      " + e.detail : ""))
      .join("\n");
  } catch (error) {
    traceBox.textContent = "读取失败: " + (error?.message || String(error));
  }
}

if (traceClear) {
  traceClear.addEventListener("click", async () => {
    try { await chrome.storage.local.remove("bwVoiceTrace"); } catch (_) {}
    renderTrace();
  });
}
renderTrace();
