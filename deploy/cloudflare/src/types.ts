/**
 * 全部公共 TypeScript 类型定义。
 * 与 main.py 中 Pydantic 模型一一对应，作为两端 API 契约的单一事实来源。
 */

export {};

// ── 鉴权 ──────────────────────────────────────────────

export interface AuthStatusResponse {
  token_required: boolean;
  authorized: boolean;
}

// ── 站点 ───────────────────────────────────────────────

export interface SiteItem {
  id: number;
  url: string;
  site_name: string;
  icon_rel_path: string;
  updated_at: string;
  sort_order: number;
  is_public: boolean;
  tags: string[];
}

export interface ParseResponse {
  url: string;
  final_url: string;
  site_name: string;
  icon_rel_path: string;
  icon_source_url: string;
  status: "success" | "partial" | "failed" | "invalid";
  error: string;
  warning: string;
}

export interface ParseRequest {
  url: string;
}

export interface BrowserIconPayload {
  source_url: string;
  content_type: string;
  filename: string;
  data_base64: string;
}

export interface BrowserIngestRequest {
  url: string;
  final_url: string;
  site_name: string;
  icon: BrowserIconPayload | null;
}

export interface SiteUpdateRequest {
  site_name: string;
  url: string;
  icon_file: string | null;
  tags: string[] | null;
  is_public: boolean | null;
}

export interface ReorderRequest {
  site_ids: number[];
}

// ── 标签 ───────────────────────────────────────────────

export interface TagItem {
  name: string;
  count: number;
}

// ── 图标库 ─────────────────────────────────────────────

export interface SimpleIconItem {
  name: string;
  slug: string;
  url: string;
}

// ── 背景设置 ───────────────────────────────────────────

export interface BackgroundSettingResponse {
  type: "default" | "color" | "image";
  color: string;
  image: string;
  image_url: string;
}

export interface BackgroundSettingRequest {
  type: string;
  color: string;
  image: string;
}

export interface BackgroundImageItem {
  file: string;
  size: number;
  url: string;
}

// ── 通用 ───────────────────────────────────────────────

export interface MessageResponse {
  message: string;
}

export interface PublicIpResponse {
  ip: string;
  /**
   * 返回的是谁的 IP：server=服务端出口 IP（Python 端）；
   * client=访问者自己的 IP（TS 端，取自 CF-Connecting-IP）。
   * 不叫 PublicIPv4Response 是因为访客可能走 IPv6，TS 端原样返回。
   */
  kind: "server" | "client";
}
