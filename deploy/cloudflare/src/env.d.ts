// 本地开发时通过 miniflare 模拟 D1 / R2 绑定的类型声明
declare global {
  const DB: unknown;
  const ICON_BUCKET: unknown;
  const BACKGROUND_BUCKET: unknown;
  const NAV_TOKEN: string;
  const NAV_MODE: string;
}

export {};
