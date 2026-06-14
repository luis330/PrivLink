(() => {
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

  return {
    url: location.href,
    final_url: location.href,
    site_name: pickSiteName(),
    icon_candidates: collectIconCandidates(),
  };
})();
