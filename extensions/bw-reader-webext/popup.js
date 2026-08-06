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
const voiceButton = document.getElementById("voice-open");
const voiceStatus = document.getElementById("voice-status");

if (voiceButton && voiceStatus) {
  voiceButton.addEventListener("click", async () => {
    voiceButton.disabled = true;
    try {
      // Captured here rather than in the call page because only the popup can
      // see which tab is active; once the call page opens, the popup is gone
      // and the active tab is the call page itself.
      const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });

      // The content script has the page body; the tab record only has its title.
      // A failure here is not fatal -- the call still works, the assistant just
      // knows which page it is rather than what it says -- so it degrades
      // instead of blocking the call.
      // Injected for this one call, into the tab the user just opened the popup
      // over. Replaces the previous approach of a permanent listener in every
      // page's content script, which broke the popup on ordinary sites.
      let page = null;
      if (tab?.id != null && chrome.scripting?.executeScript) {
        try {
          const [result] = await chrome.scripting.executeScript({
            target: { tabId: tab.id },
            func: () => {
              // innerText, not textContent: it honours display:none and reads in
              // visual order, keeping nav chrome and inline scripts out.
              const raw = String(document.body?.innerText || "");
              const text = raw.replace(/[ \t]+/g, " ").replace(/\n{3,}/g, "\n\n").trim();
              let selection = "";
              try {
                selection = String(window.getSelection() || "").trim();
              } catch (_) {}
              return {
                // Bounded so a long page cannot exceed the bridge's message ceiling.
                text: text.slice(0, 12000),
                selection: selection.slice(0, 2000),
              };
            },
          });
          page = result?.result || null;
        } catch {
          page = null;
        }
      }

      await chrome.storage.local.set({
        bwCallContext: {
          url: page?.url || tab?.url || "",
          title: page?.title || tab?.title || "",
          text: page?.text || "",
          selection: page?.selection || "",
          // Stamped so the call page can refuse context old enough to be about
          // some other page entirely.
          capturedAt: Date.now(),
        },
      });

      await chrome.tabs.create({ url: chrome.runtime.getURL("call.html") });
      window.close();
    } catch (error) {
      voiceButton.disabled = false;
      voiceStatus.textContent = "✗ " + (error?.message || String(error));
    }
  });
}

loadStatus();
