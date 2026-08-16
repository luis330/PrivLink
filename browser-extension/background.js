const DEFAULT_API_BASE = "http://127.0.0.1:8000";
const ICON_MAX_BYTES = 1024 * 1024;

chrome.action.onClicked.addListener(async (tab) => {
  if (!tab.id || !/^https?:\/\//i.test(tab.url || "")) {
    await showMessage(tab.id, "当前页面不是 http/https 页面，无法保存。");
    return;
  }

  const options = await chrome.storage.sync.get({
    apiBase: DEFAULT_API_BASE,
    token: "",
  });
  const apiBase = String(options.apiBase || DEFAULT_API_BASE).replace(/\/+$/, "");
  const token = String(options.token || "").trim();
  if (!token) {
    await chrome.runtime.openOptionsPage();
    await showMessage(tab.id, "请先在插件选项里配置 PrivLink Token。");
    return;
  }

  try {
    await setBadge(tab.id, "...");
    const metadata = await collectPageMetadata(tab.id);
    const icon = await fetchBestIcon(metadata.icon_candidates || []);
    const payload = {
      url: metadata.url,
      final_url: metadata.final_url,
      site_name: metadata.site_name,
      icon,
    };
    const result = await postIngest(apiBase, token, payload);
    await setBadge(tab.id, "OK");
    await showMessage(tab.id, "已保存到 PrivLink：" + (result.site_name || result.url || metadata.url));
  } catch (error) {
    await setBadge(tab.id, "ERR");
    await showMessage(tab.id, "保存失败：" + (error && error.message ? error.message : String(error)));
  } finally {
    setTimeout(() => {
      if (tab.id) chrome.action.setBadgeText({ tabId: tab.id, text: "" });
    }, 1800);
  }
});

async function collectPageMetadata(tabId) {
  const results = await chrome.scripting.executeScript({
    target: { tabId },
    files: ["content.js"],
  });
  const metadata = results && results[0] ? results[0].result : null;
  if (!metadata || !metadata.url) {
    throw new Error("无法读取当前页面信息");
  }
  return metadata;
}

async function fetchBestIcon(candidates) {
  for (const candidate of candidates) {
    try {
      const response = await fetch(candidate.source_url, {
        method: "GET",
        credentials: "include",
        cache: "no-store",
        headers: { Accept: "image/*,*/*;q=0.5" },
      });
      if (!response.ok) continue;
      const blob = await response.blob();
      if (!blob || blob.size <= 0 || blob.size > ICON_MAX_BYTES) continue;
      const contentType = response.headers.get("content-type") || blob.type || candidate.content_type || "";
      if (
        contentType &&
        !contentType.toLowerCase().startsWith("image/") &&
        !hasAllowedIconExt(candidate.filename) &&
        !hasAllowedIconExt(candidate.source_url)
      ) {
        continue;
      }
      return {
        source_url: candidate.source_url,
        content_type: contentType,
        filename: candidate.filename || filenameFromUrl(candidate.source_url),
        data_base64: arrayBufferToBase64(await blob.arrayBuffer()),
      };
    } catch (_) {
      // Try the next candidate.
    }
  }
  return null;
}

function hasAllowedIconExt(value) {
  return /\.(ico|png|jpe?g|svg|webp|gif|bmp|avif)(?:$|[?#])/i.test(String(value || ""));
}

async function postIngest(apiBase, token, payload) {
  const response = await fetch(apiBase + "/api/site/ingest", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Nav-Token": token,
    },
    body: JSON.stringify(payload),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.error || "HTTP " + response.status);
  }
  return data;
}

function filenameFromUrl(url) {
  try {
    const path = new URL(url).pathname;
    const name = path.split("/").filter(Boolean).pop();
    return name || "favicon.ico";
  } catch (_) {
    return "favicon.ico";
  }
}

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    const chunk = bytes.subarray(i, i + chunkSize);
    binary += String.fromCharCode.apply(null, chunk);
  }
  return btoa(binary);
}

async function setBadge(tabId, text) {
  if (!tabId) return;
  await chrome.action.setBadgeText({ tabId, text });
  await chrome.action.setBadgeBackgroundColor({ tabId, color: text === "ERR" ? "#c92f2f" : "#156f3d" });
}

async function showMessage(tabId, message) {
  if (!tabId) return;
  try {
    await chrome.scripting.executeScript({
      target: { tabId },
      func: (text) => alert(text),
      args: [message],
    });
  } catch (_) {
    // Some browser pages reject script injection; the badge still shows status.
  }
}
