from __future__ import annotations

import base64
import binascii
import hashlib
import json
from pathlib import Path
from urllib import parse

from privlink import config, fetcher, network, storage
from privlink.htmlparse import IconCandidate


def choose_extension(icon_url: str, content_type: str) -> str:
    ext = Path(parse.urlsplit(icon_url).path).suffix.lower()
    if content_type in config.CONTENT_TYPE_TO_EXT:
        return config.CONTENT_TYPE_TO_EXT[content_type]
    guessed = config.CONTENT_TYPE_TO_EXT.get(content_type.split(";")[0].strip().lower(), "")
    if guessed:
        return guessed
    if ext in config.ALLOWED_ICON_EXTENSIONS:
        return ext
    return ".ico"


def icon_filename(normalized_url: str, extension: str) -> str:
    digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:24]
    return f"{digest}{extension}"


def icon_upload_filename(content: bytes, original_name: str) -> str:
    ext = Path(original_name).suffix.lower() or ".ico"
    digest = hashlib.sha256(content).hexdigest()[:24]
    return f"upload-{digest}{ext}"


def icon_extension_from_payload(source_url: str, filename: str, content_type: str) -> str:
    clean_content_type = (content_type or "").split(";", 1)[0].strip().lower()
    payload_ext = ""
    for value in (filename, parse.urlsplit(source_url or "").path):
        ext = Path(value).suffix.lower()
        if ext in config.ALLOWED_ICON_EXTENSIONS:
            payload_ext = ext
            break

    if clean_content_type:
        mapped = config.CONTENT_TYPE_TO_EXT.get(clean_content_type)
        if mapped:
            return mapped
        if not clean_content_type.startswith("image/"):
            if payload_ext:
                return payload_ext
            raise ValueError(f"Invalid icon content-type: {clean_content_type}")

    if payload_ext:
        return payload_ext

    if clean_content_type.startswith("image/"):
        return ".ico"
    raise ValueError("Icon content-type or filename is required")


def decode_base64_icon(raw_data: str) -> bytes:
    data = (raw_data or "").strip()
    if not data:
        raise ValueError("Icon data is empty")
    if data.startswith("data:"):
        _, _, data = data.partition(",")
    try:
        content = base64.b64decode("".join(data.split()), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Invalid base64 icon data") from exc
    if not content:
        raise ValueError("Icon content is empty")
    if len(content) > config.ICON_UPLOAD_MAX_BYTES:
        raise ValueError("Icon size cannot exceed 1MB")
    return content


async def write_browser_icon(
    *,
    normalized_url: str,
    source_url: str,
    filename: str,
    content_type: str,
    data_base64: str,
) -> tuple[str, str]:
    content = decode_base64_icon(data_base64)
    extension = icon_extension_from_payload(source_url, filename, content_type)
    filename_to_store = icon_filename(normalized_url, extension)
    relative_path = Path("ICON") / filename_to_store
    await storage.put(storage.ICONS, filename_to_store, content)
    return relative_path.as_posix(), (source_url or f"browser-upload://{filename_to_store}").strip()


def icon_url_for_slug(slug: str) -> str:
    """返回 Simple Icons CDN URL。"""
    return f"https://cdn.simpleicons.org/{slug}"


# Simple Icons 数据文件：位于当前工作目录（本地源码 / Docker 部署的仓库根）；
# 读不到时回落包内 simple_icons_data.py 内嵌模块，见 list_simple_icons()。
SIMPLE_ICONS_FILE = "simple-icons.json"


# 内置 Simple Icons 数据（兜底）；完整列表由 scripts/fetch-simple-icons.py 生成
# 到仓库根 simple-icons.json，启动时优先加载该文件。
_SIMPLE_ICONS_FALLBACK: list[dict[str, str]] = [
    {"name": "GitHub", "slug": "github", "url": "https://cdn.simpleicons.org/github"},
    {"name": "GitLab", "slug": "gitlab", "url": "https://cdn.simpleicons.org/gitlab"},
    {"name": "Figma", "slug": "figma", "url": "https://cdn.simpleicons.org/figma"},
    {"name": "Docker", "slug": "docker", "url": "https://cdn.simpleicons.org/docker"},
    {"name": "Telegram", "slug": "telegram", "url": "https://cdn.simpleicons.org/telegram"},
    {"name": "Discord", "slug": "discord", "url": "https://cdn.simpleicons.org/discord"},
    {"name": "YouTube", "slug": "youtube", "url": "https://cdn.simpleicons.org/youtube"},
    {"name": "Twitter", "slug": "twitter", "url": "https://cdn.simpleicons.org/twitter"},
    {"name": "Instagram", "slug": "instagram", "url": "https://cdn.simpleicons.org/instagram"},
    {"name": "LinkedIn", "slug": "linkedin", "url": "https://cdn.simpleicons.org/linkedin"},
    {"name": "Reddit", "slug": "reddit", "url": "https://cdn.simpleicons.org/reddit"},
    {"name": "Slack", "slug": "slack", "url": "https://cdn.simpleicons.org/slack"},
    {"name": "Notion", "slug": "notion", "url": "https://cdn.simpleicons.org/notion"},
    {"name": "Vercel", "slug": "vercel", "url": "https://cdn.simpleicons.org/vercel"},
    {"name": "Next.js", "slug": "nextdotjs", "url": "https://cdn.simpleicons.org/nextdotjs"},
    {"name": "React", "slug": "react", "url": "https://cdn.simpleicons.org/react"},
    {"name": "Vue.js", "slug": "vue-dot-js", "url": "https://cdn.simpleicons.org/vue-dot-js"},
    {"name": "Angular", "slug": "angular", "url": "https://cdn.simpleicons.org/angular"},
    {"name": "Svelte", "slug": "svelte", "url": "https://cdn.simpleicons.org/svelte"},
    {"name": "Node.js", "slug": "node-dot-js", "url": "https://cdn.simpleicons.org/node-dot-js"},
    {"name": "Python", "slug": "python", "url": "https://cdn.simpleicons.org/python"},
    {"name": "TypeScript", "slug": "typescript", "url": "https://cdn.simpleicons.org/typescript"},
    {"name": "JavaScript", "slug": "javascript", "url": "https://cdn.simpleicons.org/javascript"},
    {"name": "Go", "slug": "go", "url": "https://cdn.simpleicons.org/go"},
    {"name": "Rust", "slug": "rust", "url": "https://cdn.simpleicons.org/rust"},
    {"name": "Kubernetes", "slug": "kubernetes", "url": "https://cdn.simpleicons.org/kubernetes"},
    {"name": "AWS", "slug": "amazonaws", "url": "https://cdn.simpleicons.org/amazonaws"},
    {"name": "Google Cloud", "slug": "googlecloud", "url": "https://cdn.simpleicons.org/googlecloud"},
    {"name": "Azure", "slug": "azure", "url": "https://cdn.simpleicons.org/azure"},
    {"name": "FastAPI", "slug": "fastapi", "url": "https://cdn.simpleicons.org/fastapi"},
    {"name": "Hono", "slug": "hono", "url": "https://cdn.simpleicons.org/hono"},
    {"name": "Cloudflare", "slug": "cloudflare", "url": "https://cdn.simpleicons.org/cloudflare"},
    {"name": "Tailwind CSS", "slug": "tailwindcss", "url": "https://cdn.simpleicons.org/tailwindcss"},
    {"name": "Stripe", "slug": "stripe", "url": "https://cdn.simpleicons.org/stripe"},
    {"name": "PayPal", "slug": "paypal", "url": "https://cdn.simpleicons.org/paypal"},
    {"name": "Spotify", "slug": "spotify", "url": "https://cdn.simpleicons.org/spotify"},
    {"name": "Netflix", "slug": "netflix", "url": "https://cdn.simpleicons.org/netflix"},
    {"name": "TikTok", "slug": "tiktok", "url": "https://cdn.simpleicons.org/tiktok"},
    {"name": "Stack Overflow", "slug": "stackoverflow", "url": "https://cdn.simpleicons.org/stackoverflow"},
    {"name": "Dev.to", "slug": "dev", "url": "https://cdn.simpleicons.org/dev"},
    {"name": "Medium", "slug": "medium", "url": "https://cdn.simpleicons.org/medium"},
    {"name": "Substack", "slug": "substack", "url": "https://cdn.simpleicons.org/substack"},
    {"name": "Mastodon", "slug": "mastodon", "url": "https://cdn.simpleicons.org/mastodon"},
    {"name": "Threads", "slug": "threads", "url": "https://cdn.simpleicons.org/threads"},
    {"name": "Bluesky", "slug": "bluesky", "url": "https://cdn.simpleicons.org/bluesky"},
    {"name": "OpenAI", "slug": "openai", "url": "https://cdn.simpleicons.org/openai"},
    {"name": "ChatGPT", "slug": "chatgpt", "url": "https://cdn.simpleicons.org/chatgpt"},
    {"name": "Claude", "slug": "anthropic", "url": "https://cdn.simpleicons.org/anthropic"},
    {"name": "Perplexity", "slug": "perplexity", "url": "https://cdn.simpleicons.org/perplexity"},
    {"name": "Zhihu", "slug": "zhihu", "url": "https://cdn.simpleicons.org/zhihu"},
    {"name": "Bilibili", "slug": "bilibili", "url": "https://cdn.simpleicons.org/bilibili"},
    {"name": "Weibo", "slug": "weibo", "url": "https://cdn.simpleicons.org/weibo"},
    {"name": "Baidu", "slug": "baidu", "url": "https://cdn.simpleicons.org/baidu"},
    {"name": "Google", "slug": "google", "url": "https://cdn.simpleicons.org/google"},
    {"name": "Microsoft", "slug": "microsoft", "url": "https://cdn.simpleicons.org/microsoft"},
    {"name": "Apple", "slug": "apple", "url": "https://cdn.simpleicons.org/apple"},
    {"name": "Linux", "slug": "linux", "url": "https://cdn.simpleicons.org/linux"},
    {"name": "Windows", "slug": "windows", "url": "https://cdn.simpleicons.org/windows"},
    {"name": "Android", "slug": "android", "url": "https://cdn.simpleicons.org/android"},
    {"name": "Adobe", "slug": "adobe", "url": "https://cdn.simpleicons.org/adobe"},
]


def _icons_from_raw(raw: object) -> list[dict[str, str]] | None:
    if not isinstance(raw, list):
        return None
    return [
        {
            "name": item["title"],
            "slug": item["slug"],
            "url": icon_url_for_slug(item["slug"]),
        }
        for item in raw
        if isinstance(item, dict) and "title" in item and "slug" in item
    ]


def list_simple_icons() -> list[dict[str, str]]:
    """返回 Simple Icons 图标列表。

    依次尝试：工作目录 simple-icons.json（本地源码 / Docker 部署）→ 包内
    simple_icons_data.py（Workers bundle 只收 .py 不收 JSON；wheel 安装同样可用）
    → 内置兜底列表。
    """
    file_path = Path(SIMPLE_ICONS_FILE)
    try:
        if file_path.is_file():
            icons = _icons_from_raw(json.loads(file_path.read_text(encoding="utf-8")))
            if icons:
                return icons
    except (OSError, ValueError, KeyError):
        config.logger.exception("simple-icons 数据加载失败: %s", file_path)
    try:
        from privlink.data.simple_icons_data import ICONS

        return [
            {"name": title, "slug": slug, "url": icon_url_for_slug(slug)}
            for title, slug in ICONS
        ]
    except ImportError:
        config.logger.warning("simple_icons_data 内嵌模块不可用，使用内置兜底列表")
    return list(_SIMPLE_ICONS_FALLBACK)


async def download_icon(
    candidates: list[IconCandidate],
    normalized_url: str,
    *,
    referer: str | None = None,
) -> tuple[str, str, str]:
    last_error = ""
    for candidate in candidates:
        try:
            network.validate_remote_url(candidate.url)
            final_icon_url, body, content_type = await fetcher.fetch_url(
                candidate.url,
                max_bytes=config.ICON_MAX_BYTES,
                accept="image/*,*/*;q=0.5",
                referer=referer,
            )
            if not content_type.startswith("image/"):
                raise ValueError(f"Invalid content-type: {content_type or 'unknown'}")
            if not body:
                raise ValueError("Empty icon content")

            extension = choose_extension(final_icon_url, content_type)
            filename = icon_filename(normalized_url, extension)
            relative_path = Path("ICON") / filename
            await storage.put(storage.ICONS, filename, body)
            config.logger.info("图标下载成功: %s -> %s", candidate.url, relative_path)
            return relative_path.as_posix(), final_icon_url, ""
        except Exception as exc:  # noqa: BLE001
            last_error = str(exc)
            config.logger.debug("图标候选 %s 下载失败: %s", candidate.url, last_error)
            continue
    config.logger.warning("所有图标候选均失败 (%s): %s", normalized_url, last_error)
    return "", "", last_error
