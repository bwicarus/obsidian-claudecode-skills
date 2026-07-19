const tokenInput = document.querySelector("#token");
const status = document.querySelector("#status");

chrome.storage.local.get("apiToken").then((s) => { tokenInput.value = s.apiToken || ""; });

document.querySelector("#save").addEventListener("click", async () => {
  await chrome.storage.local.set({ apiToken: tokenInput.value.trim() });
  status.textContent = "已保存";
});

document.querySelector("#test").addEventListener("click", async () => {
  status.textContent = "测试中……";
  try {
    const resp = await chrome.runtime.sendMessage({ type: "PING" });
    if (!resp?.ok) throw new Error(resp?.error || "连接失败");
    status.textContent = "✓ 连接正常";
  } catch (e) {
    status.textContent = "✗ " + (e instanceof Error ? e.message : String(e));
  }
});
