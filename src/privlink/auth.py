from __future__ import annotations

import secrets

from fastapi import Request
from fastapi.responses import JSONResponse

from privlink import config
from privlink.models import error_payload

# 公开只读接口：无 token 也放行到路由，由路由内按身份过滤（仅返回公开站点）；
# auth/status 供前端探测门禁状态，不泄露敏感信息
PUBLIC_READONLY_API_PATHS = {"/api/sites", "/api/tags", "/api/auth/status", "/api/appearance/background"}
# Workers 端 public-ip 返回访客自己的 IP，对其本人不构成泄露，故放开；
# 本地返回服务端出口 IP，对匿名访客敏感，仍需 token
WORKERS_PUBLIC_READONLY_API_PATHS = {"/api/network/public-ip"}


def is_public_readonly_path(path: str) -> bool:
    if path in PUBLIC_READONLY_API_PATHS:
        return True
    return config.IS_WORKERS and path in WORKERS_PUBLIC_READONLY_API_PATHS


def token_header_matches(value: str | None) -> bool:
    """X-Nav-Token 请求头是否匹配当前配置的管理 token。"""
    if not config.NAV_TOKEN:
        return False
    provided = (value or "").strip()
    return bool(provided) and secrets.compare_digest(provided, config.NAV_TOKEN)


def resolve_request_identity(request: Request) -> str | None:
    """校验 X-Nav-Token 并返回请求身份；多用户体系下将改为按 token 查询用户。"""
    if token_header_matches(request.headers.get("X-Nav-Token")):
        return "owner"
    return None


def can_view_private(request: Request) -> bool:
    """开放模式（未配置 token）全可见；门禁模式下仅持有效 token 的请求可见私有站点。"""
    if not config.NAV_TOKEN:
        return True
    return resolve_request_identity(request) is not None


def validate_ingest_token(request: Request) -> JSONResponse | None:
    if not config.NAV_TOKEN:
        return JSONResponse(status_code=403, content=error_payload("浏览器采集接口未启用"))
    if resolve_request_identity(request) is None:
        return JSONResponse(status_code=401, content=error_payload("浏览器采集 token 无效"))
    return None
