// ==UserScript==
// @name         PrivLink Collector
// @namespace    privlink
// @version      0.1.1
// @description  Save the current browser page to PrivLink.
// @match        http://*/*
// @match        https://*/*
// @noframes
// @grant        GM_getValue
// @grant        GM_setValue
// @grant        GM_registerMenuCommand
// @grant        GM_xmlhttpRequest
// @connect      *
// ==/UserScript==

(function () {
  "use strict";

  if (window.top !== window.self) {
    return;
  }

  const DEFAULT_API_BASE = "http://127.0.0.1:8000";
  const API_BASE_KEY = "privlinkApiBase";
  const TOKEN_KEY = "privlinkToken";
  const ICON_MAX_BYTES = 1024 * 1024;

  function getApiBase() {
    return String(GM_getValue(API_BASE_KEY, DEFAULT_API_BASE) || DEFAULT_API_BASE).replace(/\/+$/, "");
  }

  function getToken() {
    return String(GM_getValue(TOKEN_KEY, "") || "").trim();
  }

  function setApiBase() {
    const next = prompt("PrivLink API 地址", getApiBase());
    if (next === null) return;
    const clean = next.trim().replace(/\/+$/, "");
    if (!/^https?:\/\//i.test(clean)) {
      alert("API 地址必须以 http:// 或 https:// 开头");
      return;
    }
    GM_setValue(API_BASE_KEY, clean);
    alert("已保存 API 地址");
  }

  function setToken() {
    const next = prompt("PrivLink X-Nav-Token", getToken());
    if (next === null) return;
    GM_setValue(TOKEN_KEY, next.trim());
    alert("已保存 Token");
  }

  function normalizeSpaces(value) {
    return String(value || "").trim().split(/\s+/).join(" ");
  }

  function getMetaContent(selector) {
    const node = document.querySelector(selector);
    return normalizeSpaces(node ? node.getAttribute("content") : "");
  }

  function pickSiteName() {
    return (
      getMetaContent('meta[property="og:site_name"]') ||
      getMetaContent('meta[name="og:site_name"]') ||
      normalizeSpaces(document.title) ||
      location.hostname
    );
  }

  function parseSizes(value) {
    const sizes = String(value || "").toLowerCase();
    if (sizes.includes("any")) return 100000000;
    return sizes.split(/\s+/).reduce((best, token) => {
      const match = token.match(/^(\d+)x(\d+)$/);
      if (!match) return best;
      return Math.max(best, Number(match[1]) * Number(match[2]));
    }, 0);
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

  function hasAllowedIconExt(value) {
    return /\.(ico|png|jpe?g|svg|webp|gif|bmp|avif)(?:$|[?#])/i.test(String(value || ""));
  }

  function collectIconCandidates() {
    const candidates = Array.from(document.querySelectorAll("link[rel]"))
      .map((node, index) => {
        const rel = String(node.getAttribute("rel") || "").toLowerCase();
        const href = String(node.getAttribute("href") || "").trim();
        if (!href || !rel.includes("icon")) return null;
        const sourceUrl = new URL(href, document.baseURI).href;
        const contentType = String(node.getAttribute("type") || "").trim();
        return {
          source_url: sourceUrl,
          content_type: contentType,
          filename: filenameFromUrl(sourceUrl),
          is_svg: /\.svg(?:$|[?#])/i.test(sourceUrl) || /svg/i.test(contentType),
          size_score: parseSizes(node.getAttribute("sizes")),
          rank: index,
        };
      })
      .filter(Boolean);

    const fallbackUrl = new URL("/favicon.ico", location.origin).href;
    candidates.push({
      source_url: fallbackUrl,
      content_type: "image/x-icon",
      filename: "favicon.ico",
      is_svg: false,
      size_score: 0,
      rank: candidates.length + 1,
    });

    const seen = new Set();
    return candidates
      .sort((a, b) => Number(b.is_svg) - Number(a.is_svg) || b.size_score - a.size_score || a.rank - b.rank)
      .filter((item) => {
        if (seen.has(item.source_url)) return false;
        seen.add(item.source_url);
        return true;
      })
      .slice(0, 10);
  }

  function headerValue(headers, name) {
    const target = name.toLowerCase();
    return String(headers || "")
      .split(/\r?\n/)
      .map((line) => line.trim())
      .filter(Boolean)
      .reduce((found, line) => {
        if (found) return found;
        const idx = line.indexOf(":");
        if (idx < 0) return "";
        const key = line.slice(0, idx).trim().toLowerCase();
        return key === target ? line.slice(idx + 1).trim() : "";
      }, "");
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

  function requestArrayBuffer(url) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: "GET",
        url,
        responseType: "arraybuffer",
        headers: { Accept: "image/*,*/*;q=0.5" },
        timeout: 10000,
        onload(response) {
          if (response.status < 200 || response.status >= 400) {
            reject(new Error("HTTP " + response.status));
            return;
          }
          resolve(response);
        },
        ontimeout() {
          reject(new Error("Icon request timed out"));
        },
        onerror() {
          reject(new Error("Icon request failed"));
        },
      });
    });
  }

  async function fetchBestIcon(candidates) {
    for (const candidate of candidates) {
      try {
        const response = await requestArrayBuffer(candidate.source_url);
        const buffer = response.response;
        if (!buffer || buffer.byteLength <= 0 || buffer.byteLength > ICON_MAX_BYTES) continue;
        const contentType = headerValue(response.responseHeaders, "content-type") || candidate.content_type;
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
          content_type: contentType || candidate.content_type,
          filename: candidate.filename,
          data_base64: arrayBufferToBase64(buffer),
        };
      } catch (_) {
        // Try the next candidate.
      }
    }
    return null;
  }

  function postJson(url, token, payload) {
    return new Promise((resolve, reject) => {
      GM_xmlhttpRequest({
        method: "POST",
        url,
        headers: {
          "Content-Type": "application/json",
          "X-Nav-Token": token,
        },
        data: JSON.stringify(payload),
        timeout: 15000,
        onload(response) {
          let data = {};
          try {
            data = JSON.parse(response.responseText || "{}");
          } catch (_) {
            data = {};
          }
          if (response.status < 200 || response.status >= 300) {
            reject(new Error(data.error || "HTTP " + response.status));
            return;
          }
          resolve(data);
        },
        ontimeout() {
          reject(new Error("PrivLink request timed out"));
        },
        onerror() {
          reject(new Error("PrivLink request failed"));
        },
      });
    });
  }

  async function saveCurrentPage() {
    const token = getToken();
    if (!token) {
      alert("请先设置 PrivLink Token");
      setToken();
      return;
    }

    const payload = {
      url: location.href,
      final_url: location.href,
      site_name: pickSiteName(),
      icon: await fetchBestIcon(collectIconCandidates()),
    };

    try {
      const data = await postJson(getApiBase() + "/api/site/ingest", token, payload);
      alert("已保存到 PrivLink：" + (data.site_name || data.url || location.href));
    } catch (error) {
      alert("保存失败：" + (error && error.message ? error.message : String(error)));
    }
  }

  GM_registerMenuCommand("保存当前页到 PrivLink", saveCurrentPage);
  GM_registerMenuCommand("设置 PrivLink API 地址", setApiBase);
  GM_registerMenuCommand("设置 PrivLink Token", setToken);
})();
