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

// 一次性诊断:确认扩展自身页面(safari-web-extension:// 源)能否取得麦克风。
// content script 拿不到 getUserMedia 是确定的,但扩展页面不是 content script。
// 若这里能拿到,Safari Realtime 就能整个留在扩展内完成,不必经 App Group +
// deep link 那条多跳桥 —— 那条桥用户实测几乎每次失败。
// 结论出来后这段应当移除。
const micButton = document.getElementById("mic-test");
const micStatus = document.getElementById("mic-status");
if (micButton && micStatus) {
  micButton.addEventListener("click", async () => {
    micButton.disabled = true;
    micStatus.textContent = "请求中…";
    let stream = null;
    try {
      if (!navigator.mediaDevices?.getUserMedia) {
        micStatus.textContent = "✗ 该环境没有 mediaDevices.getUserMedia";
        return;
      }
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const track = stream.getAudioTracks()[0];
      micStatus.textContent = track
        ? `✓ 拿到麦克风:${track.label || "(无标签)"} state=${track.readyState}`
        : "✗ 授权通过但没有音轨";
    } catch (error) {
      // name 比 message 更能区分:NotAllowedError=被拒,NotFoundError=无设备,
      // NotSupportedError/TypeError=该上下文根本不提供。
      micStatus.textContent =
        `✗ ${error?.name || "Error"}: ${error?.message || String(error)}`;
    } finally {
      // 立刻释放,避免探测按钮占着麦克风。
      stream?.getTracks().forEach((t) => t.stop());
      micButton.disabled = false;
    }
  });
}

loadStatus();

// 第二个一次性诊断:扩展页面能否直接连到 Windows 桥接器的 WSS。
// 端点主机名(bwicarus-2)与 manifest 里放行的 Pi(bwicarus)不同,而扩展页面的
// 网络限制又与 content script 不同 —— 到底要不要改 host_permissions/CSP,
// 靠猜不如直接连一次。区分三种结局:握手成功、被策略拒(CSP/权限)、连不上(网络)。
// 结论出来后这段应当移除。
const wssButton = document.getElementById("wss-test");
const wssStatus = document.getElementById("wss-status");
if (wssButton && wssStatus) {
  const ENDPOINT =
    "wss://bwicarus-2.taile44d0c.ts.net/reader-computer-voice/v1";
  wssButton.addEventListener("click", () => {
    wssButton.disabled = true;
    wssStatus.textContent = "连接中…";
    let socket = null;
    let settled = false;
    const finish = (text) => {
      if (settled) return;
      settled = true;
      wssStatus.textContent = text;
      wssButton.disabled = false;
      try { socket?.close(); } catch (e) {}
    };
    // 只测能否握手,不发 HELLO、不发 START,不会启动任何语音。
    const timer = setTimeout(
      () => finish("✗ 8 秒未建立,也未报错(可能被静默拦截或网络不通)"),
      8000
    );
    try {
      socket = new WebSocket(ENDPOINT);
    } catch (error) {
      clearTimeout(timer);
      finish(`✗ 构造失败 ${error?.name || "Error"}: ${error?.message || error}`);
      return;
    }
    socket.onopen = () => {
      clearTimeout(timer);
      finish("✓ 握手成功,扩展页面可直连 Windows");
    };
    // WebSocket 的 error 事件不带原因(规范如此,防跨源探测),
    // 所以 close 的 code 才是唯一线索:1006=异常关闭(多为策略或网络)。
    socket.onclose = (event) => {
      clearTimeout(timer);
      finish(`✗ 关闭 code=${event.code} clean=${event.wasClean}`);
    };
    socket.onerror = () => {
      clearTimeout(timer);
      finish("✗ error 事件(无详情,看上面的 close code)");
    };
  });
}

