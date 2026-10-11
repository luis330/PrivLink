from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

from privlink import config, storage
from privlink.db import get_app_setting, set_app_setting


def default_background_setting() -> dict[str, str]:
    return {"type": "default", "color": "", "image": "", "image_url": ""}


def background_image_url(filename: str) -> str:
    return f"/background/{filename}"


async def normalize_background_setting(raw: dict[str, Any]) -> dict[str, str]:
    setting_type = str(raw.get("type") or "").strip().lower()
    color = str(raw.get("color") or "").strip()
    image = str(raw.get("image") or "").strip()
    if setting_type == "default":
        return default_background_setting()
    if setting_type == "color":
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise ValueError("纯色背景需要形如 #RRGGBB 的颜色值")
        return {"type": "color", "color": color, "image": "", "image_url": ""}
    if setting_type == "image":
        if not config.BACKGROUND_FILENAME_RE.fullmatch(image):
            raise ValueError("背景图文件名不合法")
        if not await storage.exists(storage.BACKGROUNDS, image):
            raise ValueError("背景图文件不存在")
        return {"type": "image", "color": "", "image": image, "image_url": background_image_url(image)}
    raise ValueError("背景类型必须是 default、color 或 image")


async def load_background_setting() -> dict[str, str]:
    """读取当前背景设置（公开 GET 使用）。

    脏数据或背景图文件缺失时降级为默认值，但**不回写**：公开读接口不应修改数据，
    否则存储短暂不可用（或 binding 配置错误）就会永久抹掉用户的设置。
    """
    raw = await get_app_setting(config.BACKGROUND_SETTING_KEY)
    if not raw:
        return default_background_setting()
    try:
        data = json.loads(raw)
        return await normalize_background_setting(data if isinstance(data, dict) else {})
    except ValueError as exc:
        config.logger.warning("背景设置无效，按默认值返回（未改写存储）: %s", exc)
        return default_background_setting()


async def stored_background_image() -> str:
    """数据库中记录的背景图文件名（不校验文件是否存在）；非图片背景返回空串。"""
    raw = await get_app_setting(config.BACKGROUND_SETTING_KEY)
    try:
        data = json.loads(raw) if raw else {}
    except ValueError:
        return ""
    if not isinstance(data, dict) or data.get("type") != "image":
        return ""
    return str(data.get("image") or "")


async def save_background_setting(payload: dict[str, Any]) -> dict[str, str]:
    setting = await normalize_background_setting(payload)
    await set_app_setting(config.BACKGROUND_SETTING_KEY, json.dumps(setting))
    return setting


def background_upload_filename(content: bytes, original_name: str) -> str:
    ext = Path(original_name).suffix.lower()
    if ext not in config.ALLOWED_BACKGROUND_EXTENSIONS:
        raise ValueError("仅支持 jpg、png、webp 格式背景图")
    digest = hashlib.sha256(content).hexdigest()[:24]
    return f"bg-{digest}{ext}"


def ensure_image_intact(content: bytes, original_name: str) -> None:
    """拒绝传输中被截断的图片（PNG 缺尾部 IEND、JPEG 缺 FFD9 尾、WEBP RIFF 长度不符）。

    上传链路对内容不做转换、按原字节存储，若客户端上传中断，服务端收到的半截文件
    会以 200 正常入库并被设为背景——用户只看到"图片显示不全"。在入库前按格式尾部
    特征校验，把这种文件挡在 400 并给出明确文案。误拒风险：PNG 规范禁止 IEND 之后
    再有数据块，JPEG 尾部 FFD9 之后允许少量填充——校验窗口放宽到末尾 256 字节。
    """
    lower = original_name.lower()
    if lower.endswith(".png"):
        if len(content) >= 12 and content[:8] == b"\x89PNG\r\n\x1a\n":
            if b"IEND" not in content[-256:]:
                raise ValueError("图片文件不完整或已损坏（PNG 缺少结束块），请重新导出后再上传")
        return
    if lower.endswith((".jpg", ".jpeg")):
        if len(content) >= 4 and content[:3] == b"\xff\xd8\xff":
            if b"\xff\xd9" not in content[-256:]:
                raise ValueError("图片文件不完整或已损坏（JPEG 缺少结束标记），请重新导出后再上传")
        return
    if lower.endswith(".webp"):
        if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
            riff_size = int.from_bytes(content[4:8], "little") + 8
            if riff_size != len(content):
                raise ValueError("图片文件不完整或已损坏（WEBP 长度不符），请重新导出后再上传")


async def list_background_images() -> list[dict[str, Any]]:
    entries = await storage.list_entries(storage.BACKGROUNDS)
    items: list[dict[str, Any]] = []
    for name, size, _sort_ts in entries:
        if not config.BACKGROUND_FILENAME_RE.fullmatch(name):
            continue
        items.append({"file": name, "size": size, "url": background_image_url(name)})
    return items


async def delete_background_image_file(filename: str) -> None:
    if not config.BACKGROUND_FILENAME_RE.fullmatch(filename):
        raise ValueError("文件名不合法")
    await storage.delete(storage.BACKGROUNDS, filename)
