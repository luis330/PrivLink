const DEFAULT_API_BASE = "http://127.0.0.1:8000";

const apiBaseInput = document.getElementById("api-base");
const tokenInput = document.getElementById("token");
const saveButton = document.getElementById("save");
const statusEl = document.getElementById("status");

async function loadOptions() {
  const options = await chrome.storage.sync.get({
    apiBase: DEFAULT_API_BASE,
    token: "",
  });
  apiBaseInput.value = options.apiBase || DEFAULT_API_BASE;
  tokenInput.value = options.token || "";
}

async function saveOptions() {
  const apiBase = apiBaseInput.value.trim().replace(/\/+$/, "");
  const token = tokenInput.value.trim();
  if (!/^https?:\/\//i.test(apiBase)) {
    statusEl.style.color = "#9f1c1c";
    statusEl.textContent = "API 地址必须以 http:// 或 https:// 开头";
    return;
  }
  await chrome.storage.sync.set({ apiBase, token });
  statusEl.style.color = "#156f3d";
  statusEl.textContent = "已保存";
}

saveButton.addEventListener("click", saveOptions);
loadOptions();
