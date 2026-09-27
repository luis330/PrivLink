from __future__ import annotations

from typing import Any
from urllib import parse

from pydantic import BaseModel, Field

from privlink import config


class ParseRequest(BaseModel):
    url: str = Field(min_length=1)


class BrowserIconPayload(BaseModel):
    source_url: str = ""
    content_type: str = ""
    filename: str = ""
    data_base64: str = Field(default="", max_length=config.ICON_UPLOAD_MAX_BYTES * 2)


class BrowserIngestRequest(BaseModel):
    url: str = Field(min_length=1)
    final_url: str = ""
    site_name: str = ""
    icon: BrowserIconPayload | None = None


class ParseResponse(BaseModel):
    url: str
    final_url: str
    site_name: str
    icon_rel_path: str
    icon_source_url: str
    status: str
    error: str
    warning: str


class SiteItem(BaseModel):
    id: int
    url: str
    site_name: str
    icon_rel_path: str
    updated_at: str
    sort_order: int
    is_public: bool = True
    tags: list[str] = []


class SiteUpdateRequest(BaseModel):
    site_name: str = Field(min_length=1)
    url: str = Field(min_length=1)
    icon_file: str | None = None
    tags: list[str] | None = None
    is_public: bool | None = None


class PublicIPv4Response(BaseModel):
    ip: str
    # 语义标记：本地部署为 server（服务端出口 IP）；Workers 部署为 client
    # （访问者自己的 IP）——Workers 出口是 CF 任播边缘节点，探测出口 IP 无意义。
    kind: str = "server"


class MessageResponse(BaseModel):
    message: str


class AuthStatusResponse(BaseModel):
    token_required: bool
    authorized: bool


class BackgroundSettingResponse(BaseModel):
    type: str
    color: str
    image: str
    image_url: str


class BackgroundSettingRequest(BaseModel):
    type: str = Field(min_length=1)
    color: str = ""
    image: str = ""


class BackgroundImageItem(BaseModel):
    file: str
    size: int
    url: str


class TagItem(BaseModel):
    name: str
    count: int


class ReorderRequest(BaseModel):
    site_ids: list[int] = Field(min_length=1)


def error_payload(message: str) -> dict[str, str]:
    return {
        "url": "",
        "final_url": "",
        "site_name": "",
        "icon_rel_path": "",
        "icon_source_url": "",
        "status": "failed",
        "error": message,
        "warning": "",
    }


def to_site_item(
    row: dict[str, Any],
    tags: list[str] | None = None,
) -> dict[str, Any]:
    url = (row.get("url") or "").strip()
    site_name = (row.get("site_name") or "").strip()
    if not site_name:
        site_name = (parse.urlsplit(url).hostname or url).strip()
    return {
        "id": int(row["id"]),
        "url": url,
        "site_name": site_name,
        "icon_rel_path": (row.get("icon_rel_path") or "").strip(),
        "updated_at": (row.get("updated_at") or "").strip(),
        "sort_order": int(row["sort_order"]),
        "is_public": bool(row["is_public"]) if "is_public" in row else True,
        "tags": list(tags) if tags else [],
    }
