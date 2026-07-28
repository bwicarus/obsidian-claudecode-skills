const tokenInput = document.querySelector("#token");
const status = document.querySelector("#status");
const saveButton = document.querySelector("#save");
const testButton = document.querySelector("#test");
const syncStatus = document.querySelector("#sync-status");
const syncConflicts = document.querySelector("#sync-conflicts");
const computerVoiceStatus = document.querySelector("#computer-voice-status");
const computerVoicePairId = document.querySelector("#computer-voice-pair-id");
const computerVoiceCode = document.querySelector("#computer-voice-code");
const computerVoicePairButton = document.querySelector("#computer-voice-pair");
const computerVoiceRefresh = document.querySelector("#computer-voice-refresh");
const computerVoiceExtensionId = document.querySelector(
  "#computer-voice-extension-id"
);

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

async function computerVoiceMessage(type, payload = null) {
  const response = await chrome.runtime.sendMessage({ type, payload });
  if (!response?.ok) {
    const error = new Error(response?.error || "电脑客户端桥接器不可用");
    error.code = response?.code || "BW_COMPUTER_VOICE_POPUP";
    throw error;
  }
  return response.data || {};
}

function renderComputerVoice(value) {
  const labels = {
    active: "● 通话中",
    starting: "◐ 正在建立媒体链路",
    ready: "● 已就绪；只会在 Reader 点击电话按钮后启动",
    "not-ready": "○ 已连接扩展，但本机桥接器尚未全部就绪"
  };
  computerVoiceStatus.textContent = value?.paired
    ? (labels[value.state] || "○ 电脑客户端状态未知")
    : "尚未配对；请先在 Reader 设置中生成一次性配对信息。";
  if (value?.paired && !value?.localOptIn) {
    computerVoiceStatus.textContent += "\nWindows 本机明确启用尚未完成。";
  }
  if (value?.paired && !value?.appReady) {
    computerVoiceStatus.textContent += "\nCodex 桌面应用尚未通过就绪检测。";
  }
  computerVoiceExtensionId.textContent =
    `本扩展 ID：${value?.extensionId || chrome.runtime.id}`;
  computerVoicePairButton.disabled = !!value?.paired;
  computerVoicePairId.disabled = !!value?.paired;
  computerVoiceCode.disabled = !!value?.paired;
}

async function loadComputerVoiceStatus() {
  computerVoiceStatus.textContent = "正在读取桥接状态……";
  try {
    renderComputerVoice(await computerVoiceMessage("BW_COMPUTER_VOICE_STATUS"));
  } catch (error) {
    computerVoiceStatus.textContent = "✗ " + (error?.message || String(error));
    computerVoiceExtensionId.textContent = `本扩展 ID：${chrome.runtime.id}`;
  }
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

computerVoicePairButton.addEventListener("click", async () => {
  computerVoicePairButton.disabled = true;
  computerVoiceRefresh.disabled = true;
  computerVoiceStatus.textContent = "正在完成一次性配对……";
  try {
    const value = await computerVoiceMessage("BW_COMPUTER_VOICE_PAIR", {
      pairId: computerVoicePairId.value.trim(),
      pairingCode: computerVoiceCode.value.trim()
    });
    computerVoicePairId.value = "";
    computerVoiceCode.value = "";
    renderComputerVoice(value);
  } catch (error) {
    computerVoiceStatus.textContent = "✗ " + (error?.message || String(error));
    computerVoicePairButton.disabled = false;
  } finally {
    computerVoiceRefresh.disabled = false;
  }
});

computerVoiceRefresh.addEventListener("click", () => {
  void loadComputerVoiceStatus();
});

loadStatus();
loadComputerVoiceStatus();
