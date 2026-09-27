from __future__ import annotations

from urllib import parse

from privlink import config, network, storage
from privlink.db import fetch_existing_icon, upsert_site_record
from privlink.fetcher import fetch_url
from privlink.htmlparse import (
    SiteHTMLParser,
    build_icon_candidates,
    choose_site_name,
    decode_html,
)
from privlink.icons import download_icon, write_browser_icon
from privlink.models import BrowserIngestRequest


async def maybe_remove_old_icon(old_icon_path: str, new_icon_path: str) -> None:
    """图标替换后删除旧图标文件。

    只处理站内上传/抓取的图标（ICON/<单层文件名>，兼容无前缀的旧记录）；
    图标库 CDN 外链、带子目录或可疑字符的路径一律跳过，防止误删与路径穿越。
    """
    old_clean = (old_icon_path or "").strip()
    new_clean = (new_icon_path or "").strip()
    if not old_clean or old_clean == new_clean:
        return
    name = old_clean.removeprefix("ICON/")
    if not name or any(ch in name for ch in '/\\:\0') or ".." in name:
        return
    await storage.delete(storage.ICONS, name)


def _record_status(site_name: str, icon_rel_path: str) -> str:
    """名称与图标齐全为 success，缺一为 partial，都没有为 failed。"""
    if site_name and icon_rel_path:
        return "success"
    if site_name or icon_rel_path:
        return "partial"
    return "failed"


async def process_site_url(raw_url: str) -> dict[str, str]:
    config.logger.info("开始处理网站: %s", raw_url)
    result = {
        "url": (raw_url or "").strip(),
        "final_url": "",
        "site_name": "",
        "icon_rel_path": "",
        "icon_source_url": "",
        "status": "failed",
        "error": "",
        "warning": "",
    }
    errors: list[str] = []

    try:
        normalized_url = network.normalize_url(raw_url)
        network.validate_remote_url(normalized_url)
    except Exception as exc:  # noqa: BLE001
        result["status"] = "invalid"
        result["error"] = str(exc)
        result["warning"] = ""
        config.logger.warning("URL 验证失败 (%s): %s", raw_url, exc)
        return result

    final_url = normalized_url
    parser: SiteHTMLParser | None = None
    try:
        final_url, html_body, content_type = await fetch_url(
            normalized_url,
            max_bytes=config.ICON_MAX_BYTES,
            accept="text/html,application/xhtml+xml,*/*;q=0.5",
        )
        if content_type and "html" not in content_type:
            errors.append(f"Unexpected HTML content-type: {content_type}")
        parser = SiteHTMLParser()
        parser.feed(decode_html(html_body))
    except Exception as exc:  # noqa: BLE001
        errors.append(str(exc))
        config.logger.warning("抓取页面失败 (%s): %s", normalized_url, exc)

    site_name = choose_site_name(parser, final_url)
    icon_candidates = build_icon_candidates(parser, final_url)
    icon_rel_path, icon_source_url, icon_error = await download_icon(
        icon_candidates,
        normalized_url,
        referer=final_url,
    )
    if icon_error:
        errors.append(icon_error)

    status = _record_status(site_name, icon_rel_path)

    warning_text = "; ".join(part for part in errors if part)
    error_text = warning_text
    if status == "success":
        error_text = ""
    old_icon = await upsert_site_record(
        url=normalized_url,
        site_name=site_name,
        icon_rel_path=icon_rel_path,
        icon_source_url=icon_source_url,
        status=status,
        error_text=error_text,
    )
    await maybe_remove_old_icon(old_icon, icon_rel_path)
    config.logger.info("网站处理完成: %s [%s] 名称=%s 图标=%s", normalized_url, status, site_name, icon_rel_path or "无")

    result.update(
        {
            "url": normalized_url,
            "final_url": final_url,
            "site_name": site_name,
            "icon_rel_path": icon_rel_path,
            "icon_source_url": icon_source_url,
            "status": status,
            "error": error_text,
            "warning": warning_text if status == "success" else "",
        }
    )
    return result


async def process_browser_ingest(payload: BrowserIngestRequest) -> dict[str, str]:
    config.logger.info("开始处理浏览器上报网站: %s", payload.url)
    result = {
        "url": (payload.url or "").strip(),
        "final_url": "",
        "site_name": "",
        "icon_rel_path": "",
        "icon_source_url": "",
        "status": "failed",
        "error": "",
        "warning": "",
    }
    errors: list[str] = []

    try:
        normalized_url = network.normalize_url(payload.url)
        network.validate_remote_url(normalized_url)
        final_url = network.normalize_url(payload.final_url) if payload.final_url.strip() else normalized_url
        network.validate_remote_url(final_url)
    except Exception as exc:  # noqa: BLE001
        result["status"] = "invalid"
        result["error"] = str(exc)
        config.logger.warning("浏览器上报 URL 验证失败 (%s): %s", payload.url, exc)
        return result

    site_name = " ".join((payload.site_name or "").split()).strip()
    if not site_name:
        site_name = (parse.urlsplit(final_url).hostname or parse.urlsplit(normalized_url).hostname or "").strip()

    icon_rel_path = ""
    icon_source_url = ""
    if payload.icon and payload.icon.data_base64.strip():
        try:
            icon_rel_path, icon_source_url = await write_browser_icon(
                normalized_url=normalized_url,
                source_url=payload.icon.source_url.strip(),
                filename=payload.icon.filename.strip(),
                content_type=payload.icon.content_type.strip(),
                data_base64=payload.icon.data_base64,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
            config.logger.warning("浏览器上报图标写入失败 (%s): %s", normalized_url, exc)

    old_icon_rel_path = ""
    if not icon_rel_path:
        old_icon_rel_path, old_icon_source_url = await fetch_existing_icon(normalized_url)
        icon_rel_path = old_icon_rel_path
        icon_source_url = old_icon_source_url

    status = _record_status(site_name, icon_rel_path)

    warning_text = "; ".join(part for part in errors if part)
    error_text = warning_text
    if status == "success" and not warning_text:
        error_text = ""

    old_icon = await upsert_site_record(
        url=normalized_url,
        site_name=site_name,
        icon_rel_path=icon_rel_path,
        icon_source_url=icon_source_url,
        status=status,
        error_text=error_text,
    )
    if icon_rel_path and icon_rel_path != old_icon_rel_path:
        await maybe_remove_old_icon(old_icon, icon_rel_path)

    result.update(
        {
            "url": normalized_url,
            "final_url": final_url,
            "site_name": site_name,
            "icon_rel_path": icon_rel_path,
            "icon_source_url": icon_source_url,
            "status": status,
            "error": error_text,
            "warning": warning_text if status == "success" else "",
        }
    )
    return result
