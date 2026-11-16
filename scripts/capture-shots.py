#!/usr/bin/env python
"""生成 PrivLink 项目展示素材（静态图 / 功能动图）。

设计要点：
- 演示实例跑在独立工作目录（系统临时目录下 privlink-capture），自带 data/ICON/background，
  完全不动仓库里的真实 data/；index.html / plugins 从仓库复制，视觉与线上一致。
- 站名与图标来自演示数据集，图标经 Google favicon 服务下载后落盘到演示 ICON/。
- Playwright 驱动本机 Edge（channel=msedge），无需下载浏览器；ffmpeg 负责 webm -> GIF。
- 截图前拦截 /api/network/public-ip，避免把本机真实出口 IP 写进公开素材。

用法：
  uv pip install --python <装了 playwright 的解释器> playwright
  python scripts/capture-shots.py --only s1
  python scripts/capture-shots.py            # 全部场景
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "docs" / "images"
DEFAULT_WORK = Path(os.environ.get("TEMP", "/tmp")) / "privlink-capture"
PORT = 8137
MASK_IP = "203.0.113.42"  # RFC 5737 文档保留地址
BG_IMAGE = "bg-9a230271f1b97907a23c7c72.png"  # 演示统一背景（仓库 background/ 下）
G1_URL = "https://www.douban.com"  # 收录动图的演示目标（不在演示数据集内）

# 演示数据：(显示名, url, 图标来源, 标签)
# 图标来源：dash:<slug> 取 dashboard-icons 彩色 PNG；si:<slug>:<hex> 取 Simple Icons 并染色
SITES = [
    ("DeepSeek", "https://chat.deepseek.com", "dash:deepseek", "AI"),
    ("GitHub", "https://github.com", "dash:github", "开发"),
    ("Figma", "https://www.figma.com", "dash:figma", "设计"),
    ("Hacker News", "https://news.ycombinator.com", "dash:ycombinator", "社区"),
    ("Notion", "https://www.notion.so", "dash:notion", "工具"),
    ("Claude", "https://claude.ai", "dash:anthropic", "AI"),
    ("GitLab", "https://gitlab.com", "dash:gitlab", "开发"),
    ("Dribbble", "https://dribbble.com", "si:dribbble:EA4C89", "设计"),
    ("知乎", "https://www.zhihu.com", "si:zhihu:0084FF", "社区"),
    ("飞书", "https://www.feishu.cn", "dash:lark", "工具"),
    ("Perplexity", "https://www.perplexity.ai", "dash:perplexity", "AI"),
    ("npm", "https://www.npmjs.com", "dash:npm", "开发"),
    ("Behance", "https://www.behance.net", "dash:behance", "设计"),
    ("哔哩哔哩", "https://www.bilibili.com", "si:bilibili:00A1D6", "社区"),
    ("Excalidraw", "https://excalidraw.com", "dash:excalidraw", "工具"),
    ("Hugging Face", "https://huggingface.co", "si:huggingface:FFD21E", "AI"),
    ("Gitee", "https://gitee.com", "dash:gitee", "开发"),
    ("Unsplash", "https://unsplash.com", "si:unsplash:000000", "设计"),
    ("Reddit", "https://www.reddit.com", "dash:reddit", "社区"),
    ("Linear", "https://linear.app", "dash:linear", "工具"),
    ("Ollama", "https://ollama.com", "dash:ollama", "AI"),
    ("Docker", "https://www.docker.com", "dash:docker", "开发"),
    ("Canva", "https://www.canva.com", "dash:canva", "设计"),
    ("微博", "https://weibo.com", "dash:weibo", "社区"),
    ("Obsidian", "https://obsidian.md", "dash:obsidian", "工具"),
    ("Midjourney", "https://www.midjourney.com", "dash:midjourney", "AI"),
    ("Vercel", "https://vercel.com", "dash:vercel", "开发"),
    ("Framer", "https://www.framer.com", "dash:framer", "设计"),
    ("Product Hunt", "https://www.producthunt.com", "si:producthunt:DA552F", "社区"),
    ("ChatGPT", "https://chatgpt.com", "dash:openai", "AI"),
    ("Cloudflare", "https://www.cloudflare.com", "dash:cloudflare", "开发"),
    ("Iconify", "https://iconify.design", "si:iconify:1769AA", "设计"),
    ("百度", "https://www.baidu.com", "dash:baidu", "工具"),
]
PRIVATE = {"飞书", "Linear"}
TAGS = ["AI", "开发", "设计", "社区", "工具"]

FAKE_CURSOR_JS = """
(() => {
  if (window.top !== window) return;   // 只画在顶层文档，插件 iframe 里不要重复出现
  if (window.__capCursorReady) return;
  window.__capCursorReady = true;
  const style = document.createElement('style');
  style.textContent = [
    'html { scrollbar-width: none; }',
    '.public-ip-display { display: none !important; }',
    'body::-webkit-scrollbar { display: none; }',
    '#cap-cursor { position: fixed; left: 0; top: 0; width: 18px; height: 18px; margin: -9px 0 0 -9px;',
    '  border-radius: 50%; background: rgba(15,124,240,0.35); border: 2px solid #fff;',
    '  box-shadow: 0 0 0 2px rgba(8,22,64,0.22), 0 3px 10px rgba(8,22,64,0.5); z-index: 2147483647;',
    '  pointer-events: none;',
    '  opacity: 0; transition: opacity .18s ease; }',
    '#cap-cursor.on { opacity: 1; }',
    '#cap-cursor::after { content: ""; position: absolute; inset: -9px; border-radius: 50%;',
    '  border: 2px solid rgba(15,124,240,0.65); opacity: 0; }',
    '#cap-cursor.tap::after { animation: capRipple .42s ease-out; }',
    '@keyframes capRipple { from { transform: scale(.55); opacity: .95; } to { transform: scale(1.7); opacity: 0; } }',
  ].join('');
  const move = (x, y) => {
    const el = document.getElementById('cap-cursor');
    if (el) { el.style.left = x + 'px'; el.style.top = y + 'px'; }
  };
  const mount = () => {
    if (document.getElementById('cap-cursor')) return;
    document.head.appendChild(style);
    const dot = document.createElement('div');
    dot.id = 'cap-cursor';
    dot.className = 'on';
    document.body.appendChild(dot);
  };
  window.addEventListener('mousemove', (e) => move(e.clientX, e.clientY), true);
  window.addEventListener('mousedown', () => {
    const el = document.getElementById('cap-cursor');
    if (!el) return;
    el.classList.add('tap');
    setTimeout(() => el.classList.remove('tap'), 460);
  }, true);
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', mount);
  else mount();
})();
"""


def log(msg: str) -> None:
    print(f"[shots] {msg}", flush=True)


def fetch_icon(spec: str, dest: Path) -> str | None:
    """按 dash:<slug> / si:<slug>:<hex> 取图标并落盘，返回相对路径。"""
    kind, _, rest = spec.partition(":")
    if kind == "dash":
        url = f"https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/{rest}.png"
        ext = ".png"
    elif kind == "si":
        slug, _, color = rest.partition(":")
        url = f"https://cdn.jsdelivr.net/npm/simple-icons@13/icons/{slug}.svg"
        ext = ".svg"
    else:
        raise ValueError(f"未知图标来源 {spec}")

    name = hashlib.sha1(spec.encode()).hexdigest()[:24] + ext
    target = dest / name
    if target.exists():
        return f"ICON/{name}"

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read(600_000)
    except Exception as exc:  # noqa: BLE001
        log(f"图标获取失败 {spec}: {exc}")
        return None

    if kind == "si":
        svg = data.decode("utf-8", "ignore")
        color = rest.partition(":")[2] or "111111"
        svg = svg.replace("<svg", f'<svg fill="#{color}"', 1)
        (dest / name).write_text(svg, encoding="utf-8")
    else:
        if not data.startswith(b"\x89PNG"):
            log(f"图标不是 PNG {spec}")
            return None
        (dest / name).write_bytes(data)
    return f"ICON/{name}"


def build_workspace(work: Path) -> None:
    if work.exists():
        shutil.rmtree(work)
    for sub in ("data", "ICON", "background"):
        (work / sub).mkdir(parents=True, exist_ok=True)
    for name in (
        "index.html", "manifest.json", "favicon.ico", "favicon-32x32.png",
        "favicon-16x16.png", "apple-touch-icon.png", "android-chrome-192x192.png",
        "android-chrome-512x512.png",
    ):
        shutil.copy2(REPO / name, work / name)
    shutil.copytree(REPO / "plugins", work / "plugins")
    shutil.copy2(REPO / "data" / "sites.db", work / "data" / "sites.db")


def seed_db(work: Path) -> None:
    conn = sqlite3.connect(work / "data" / "sites.db")
    cur = conn.cursor()
    for table in ("site_tags", "sites", "tags", "plugin_data", "app_settings"):
        cur.execute(f"DELETE FROM {table}")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    tag_ids: dict[str, int] = {}
    for name in TAGS:
        cur.execute("INSERT INTO tags (name, created_at) VALUES (?, ?)", (name, now))
        tag_ids[name] = cur.lastrowid
    for idx, (name, url, icon_spec, tag) in enumerate(SITES, start=1):
        icon_rel = fetch_icon(icon_spec, work / "ICON")
        cur.execute(
            """INSERT INTO sites (url, site_name, icon_rel_path, icon_source_url,
                   created_at, updated_at, last_status, last_error, sort_order, is_public)
               VALUES (?, ?, ?, ?, ?, ?, 'success', NULL, ?, ?)""",
            (url, name, icon_rel, "", now, now, idx,
             0 if name in PRIVATE else 1),
        )
        site_id = cur.lastrowid
        cur.execute(
            "INSERT INTO site_tags (site_id, tag_id) VALUES (?, ?)",
            (site_id, tag_ids[tag]),
        )
    conn.commit()
    conn.close()


def wait_port(port: int, timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket() as sock:
            sock.settimeout(0.4)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.25)
    raise RuntimeError(f"服务未在 {timeout}s 内监听 {port}")


def start_server(work: Path, server_python: Path, port: int = PORT,
                 token: str | None = None) -> subprocess.Popen:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO), str(REPO / "src")])
    env["PYTHONIOENCODING"] = "utf-8"
    if token:
        env["NAV_TOKEN"] = token
    proc = subprocess.Popen(
        [str(server_python), "-m", "uvicorn", "main:app", "--host", "127.0.0.1",
         "--port", str(port), "--workers", "1"],
        cwd=str(work), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    wait_port(port)
    return proc


def webm_to_gif(src: Path, dst: Path, width: int, fps: int, skip: float = 0.0,
                speed: float = 1.0, crop: str = "") -> None:
    """webm -> GIF：裁掉左右空白、按 speed 倍速、调色板量化。"""
    parts = []
    if crop:
        parts.append(f"crop={crop}")
    if speed and speed != 1.0:
        parts.append(f"setpts=PTS/{speed}")
    parts.append(f"fps={fps}")
    parts.append(f"scale={width}:-1:flags=lanczos")
    chain = ",".join(parts)
    vf = (
        f"{chain},split[a][b];"
        f"[a]palettegen=max_colors=96:stats_mode=diff[p];"
        f"[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle"
    )
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-ss", str(skip), "-i", str(src), "-vf", vf, "-loop", "0", str(dst)],
        check=True,
    )


async def wait_ready(page) -> None:
    await page.wait_for_function(
        "() => document.querySelectorAll('.nav-item').length > 0 && "
        "Array.from(document.images).every(i => i.complete)"
    )
    await page.wait_for_timeout(700)


SIMPLE_ICON_CACHE: dict[str, bytes] = {}


def fetch_simple_icon(slug: str) -> bytes:
    """按 slug 取 Simple Icons SVG（带进程内缓存），走 jsDelivr。"""
    cached = SIMPLE_ICON_CACHE.get(slug)
    if cached is not None:
        return cached
    url = f"https://cdn.jsdelivr.net/npm/simple-icons@13/icons/{slug}.svg"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8", "ignore")
    # Simple Icons 的 SVG 没有宽高与填充色：补上尺寸与深色填充，否则 <img> 里渲染为空白
    raw = raw.replace("<svg", '<svg width="32" height="32" fill="#1c1c1e"', 1)
    svg = raw.encode("utf-8")
    SIMPLE_ICON_CACHE[slug] = svg
    return svg


async def new_page(browser, record_dir: Path | None = None, viewport: dict | None = None,
                    port: int = PORT, dpr: int | None = None):
    vp = viewport or {"width": 1440, "height": 760 if record_dir is not None else 900}
    kwargs = dict(
        viewport=vp,
        device_scale_factor=1 if record_dir is not None else (dpr or 2),
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
    )
    if record_dir is not None:
        kwargs["record_video_dir"] = str(record_dir)
        kwargs["record_video_size"] = vp
    ctx = await browser.new_context(**kwargs)
    await ctx.route(
        "**/api/network/public-ip",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"ip":"0.0.0.0","kind":"server"}',
        ),
    )

    async def icon_route(route):
        """本机访问不到 cdn.simpleicons.org：截图时改由 Python 去 jsDelivr 取同名 SVG。"""
        slug = urllib.parse.urlsplit(route.request.url).path.strip("/").split("/")[0]
        try:
            svg = await asyncio.to_thread(fetch_simple_icon, slug)
        except Exception:  # noqa: BLE001
            await route.abort()
            return
        await route.fulfill(status=200, content_type="image/svg+xml", body=svg)

    await ctx.route("**/cdn.simpleicons.org/**", icon_route)

    page = await ctx.new_page()
    await page.add_init_script(FAKE_CURSOR_JS)
    await page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
    await wait_ready(page)
    return ctx, page


async def click_human(page, locator) -> None:
    box = await locator.bounding_box()
    if not box:
        await locator.click()
        return
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    await page.mouse.move(x, y, steps=14)
    await page.wait_for_timeout(120)
    await page.mouse.down()
    await page.wait_for_timeout(90)
    await page.mouse.up()


async def shot_s1(browser, out: Path) -> None:
    ctx, page = await new_page(browser)
    await page.mouse.move(720, 300)
    await page.wait_for_timeout(600)
    await page.screenshot(path=str(out / "home.png"))
    await ctx.close()


async def shot_g1(browser, out: Path, tmp: Path) -> None:
    reset_demo_url(G1_URL)
    record_dir = tmp / "video-g1"
    record_dir.mkdir(parents=True, exist_ok=True)
    ctx, page = await new_page(browser, record_dir)
    await page.mouse.move(660, 430)
    await page.wait_for_timeout(320)

    await click_human(page, page.locator("#add-title"))
    await page.wait_for_timeout(400)
    await click_human(page, page.locator("#url-input"))
    await page.locator("#url-input").type(G1_URL, delay=30)
    await page.wait_for_timeout(260)
    await click_human(page, page.locator("#submit-btn"))
    await page.wait_for_selector("#result:not([hidden])", timeout=25000)
    await page.wait_for_timeout(450)

    tag_input = page.locator("#result-tags-input")
    await click_human(page, tag_input)
    await tag_input.type("社区", delay=45)
    await page.wait_for_timeout(260)
    await page.keyboard.press("Enter")
    await page.wait_for_timeout(260)
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(320)

    save_btn = page.locator("#result-save-btn")
    if await save_btn.is_enabled():
        await click_human(page, save_btn)
        await page.wait_for_timeout(1200)

    panel = page.locator("#add-panel")
    if "collapsed" not in (await panel.get_attribute("class") or ""):
        await click_human(page, page.locator("#add-title"))
        await page.wait_for_timeout(600)

    # 用刚打的标签筛出新收录的卡片，避免 34 张卡片时末行只剩一张
    await click_human(page, page.locator("#tag-bar .tag-btn").filter(has_text="社区"))
    await page.wait_for_timeout(800)
    first = page.locator(".nav-item").first
    box = await first.bounding_box()
    if box:
        await page.mouse.move(box["x"] + 40, box["y"] + 26, steps=16)
    await page.wait_for_timeout(1000)

    video = page.video
    await ctx.close()
    webm = Path(await video.path())
    webm_to_gif(webm, out / "add-site.gif", width=900, fps=11, skip=1.15, speed=1.5,
                crop="1000:760:220:0")
    if not os.environ.get("SHOTS_KEEP_VIDEO"):
        webm.unlink(missing_ok=True)

async def shot_g2(browser, out: Path, tmp: Path) -> None:
    reset_demo_url(G1_URL)
    record_dir = tmp / "video-g2"
    record_dir.mkdir(parents=True, exist_ok=True)
    ctx, page = await new_page(browser, record_dir)
    await page.mouse.move(660, 300)
    await page.wait_for_timeout(500)

    tags = page.locator("#tag-bar .tag-btn")
    await click_human(page, tags.filter(has_text="开发"))
    await page.wait_for_timeout(900)
    await click_human(page, tags.filter(has_text="全部"))
    await page.wait_for_timeout(900)

    items = page.locator(".nav-item")
    src, dst = items.nth(0), items.nth(6)
    sbox, dbox = await src.bounding_box(), await dst.bounding_box()
    sx, sy = sbox["x"] + 40, sbox["y"] + 26
    dx, dy = dbox["x"] + 58, dbox["y"] + 26
    await page.mouse.move(sx, sy, steps=12)
    await page.wait_for_timeout(220)
    await page.mouse.down()
    await page.wait_for_timeout(300)
    for step in range(1, 21):
        await page.mouse.move(sx + (dx - sx) * step / 20, sy + (dy - sy) * step / 20, steps=2)
        await page.wait_for_timeout(70)
    await page.wait_for_timeout(420)
    await page.mouse.up()
    await page.wait_for_timeout(1500)

    await click_human(page, tags.filter(has_text="社区"))
    await page.wait_for_timeout(1100)

    video = page.video
    await ctx.close()
    webm = Path(await video.path())
    webm_to_gif(webm, out / "filter-drag.gif", width=900, fps=11, skip=1.15, speed=1.4,
                crop="1000:430:220:0")
    if not os.environ.get("SHOTS_KEEP_VIDEO"):
        webm.unlink(missing_ok=True)

def reset_demo_url(url: str) -> None:
    """删除上次录制时被收录的演示站点，保证每次从零解析。"""
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/sites", timeout=10) as resp:
        sites = json.loads(resp.read().decode("utf-8"))
    hits = [s["id"] for s in sites if s.get("url", "").rstrip("/") == url.rstrip("/")]
    for site_id in hits:
        req = urllib.request.Request(
            f"http://127.0.0.1:{PORT}/api/sites/{site_id}", method="DELETE"
        )
        urllib.request.urlopen(req, timeout=10).read()
    if hits:
        log(f"已清理演示收录站点 {url}")


async def main_async(args) -> None:
    work: Path = args.work
    if args.rebuild or not (work / "data" / "sites.db").exists():
        log(f"重建工作目录 {work}")
        build_workspace(work)
        seed_db(work)
        bg = REPO / "background" / "bg-9a230271f1b97907a23c7c72.png"
        if bg.exists():
            shutil.copy2(bg, work / "background" / bg.name)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    server_python = Path(args.server_python)
    if not server_python.exists():
        raise SystemExit(f"找不到服务端解释器：{server_python}（用 --server-python 指定已装依赖的 python）")
    server = start_server(work, server_python)
    if args.rebuild or args.background:
        api("PUT", "/api/appearance/background", {"type": "image", "image": BG_IMAGE})
        log(f"背景已设为 {BG_IMAGE}")
    from playwright.async_api import async_playwright

    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(channel="msedge", headless=True)
            try:
                if "s1" in args.only:
                    prepare_plugins(False)
                    await shot_s1(browser, OUT_DIR)
                    log("S1 完成")
                if "s2" in args.only:
                    prepare_plugins(False)
                    await shot_s2(browser, OUT_DIR)
                    log("S2 完成")
                if "s3" in args.only:
                    prepare_plugins(False)
                    await shot_s3(browser, OUT_DIR)
                    log("S3 完成")
                if "s4" in args.only:
                    prepare_plugins(True)
                    await shot_s4(browser, OUT_DIR)
                    log("S4 完成")
                if "s5" in args.only:
                    prepare_plugins(True)
                    seed_float_windows()
                    await shot_s5(browser, OUT_DIR)
                    log("S5 完成")
                if "s6" in args.only:
                    prepare_plugins(False)
                    await shot_s6(browser, OUT_DIR, work, server_python)
                    log("S6 完成")
                if "s7" in args.only:
                    prepare_plugins(False)
                    await shot_s7(browser, OUT_DIR)
                    log("S7 完成")
                if "s8" in args.only:
                    prepare_plugins(False)
                    await shot_s8(browser, OUT_DIR)
                    log("S8 完成")
                if "g1" in args.only:
                    prepare_plugins(False)
                    await shot_g1(browser, OUT_DIR, work)
                    log("G1 完成")
                if "g2" in args.only:
                    prepare_plugins(False)
                    await shot_g2(browser, OUT_DIR, work)
                    log("G2 完成")
                if "g3" in args.only:
                    prepare_plugins(True)
                    await shot_g3(browser, OUT_DIR, work)
                    log("G3 完成")
            finally:
                await browser.close()
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()


def api(method: str, path: str, payload: dict | None = None, port: int = PORT):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data,
        headers={"Content-Type": "application/json"}, method=method,
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def ensure_plugins() -> None:
    for plugin_id in ("sticky-notes", "poly-gradient"):
        api("PUT", f"/api/plugins/{plugin_id}/installed", {"enabled": True})
    log("已启用便签 / 多边形渐变")


def seed_float_windows() -> None:
    api("POST", "/api/plugins/sticky-notes/windows/add",
        {"id": "note-demo", "x": 96, "y": 330, "w": 300, "h": 300})
    api("POST", "/api/plugins/poly-gradient/windows/add",
        {"id": "grad-demo", "x": 500, "y": 104, "w": 900, "h": 620})


def clear_float_windows(plugin_id: str) -> None:
    api("PUT", f"/api/plugins/{plugin_id}/windows", {"windows": []})


async def set_range(frame, selector: str, value: str) -> None:
    await frame.locator(selector).evaluate(
        """(el, v) => {
            el.value = v;
            el.dispatchEvent(new Event('input', { bubbles: true }));
            el.dispatchEvent(new Event('change', { bubbles: true }));
        }""",
        value,
    )


def prepare_plugins(enabled: bool) -> None:
    """按场景开关浮动插件：关闭时不会自动浮出便签窗口，避免污染首屏截图。"""
    for pid in ("sticky-notes", "poly-gradient"):
        api("PUT", f"/api/plugins/{pid}/installed", {"enabled": enabled})
    if enabled:
        for pid in ("sticky-notes", "poly-gradient"):
            api("PUT", f"/api/plugins/{pid}/windows", {"windows": []})


async def shot_s2(browser, out: Path) -> None:
    ctx, page = await new_page(browser)
    box = await page.locator(".nav-item").nth(2).bounding_box()
    await page.mouse.click(box["x"] + 40, box["y"] + 26, button="right")
    await page.wait_for_selector("#context-menu:not([hidden])")
    eb = await page.locator("#menu-edit").bounding_box()
    await page.mouse.move(eb["x"] + 16, eb["y"] + eb["height"] / 2, steps=10)
    await page.wait_for_timeout(600)
    await page.screenshot(path=str(out / "context-menu.png"))
    await ctx.close()


async def shot_s3(browser, out: Path) -> None:
    ctx, page = await new_page(browser)
    box = await page.locator(".nav-item").nth(2).bounding_box()
    await page.mouse.click(box["x"] + 40, box["y"] + 26, button="right")
    await page.wait_for_selector("#context-menu:not([hidden])")
    await click_human(page, page.locator("#menu-edit"))
    await page.wait_for_selector("#edit-modal:not([hidden])")
    await page.wait_for_timeout(400)
    await click_human(page, page.locator("#edit-icon-btn"))
    await page.wait_for_selector("#icon-picker:not([hidden])")
    await page.wait_for_function("() => document.querySelectorAll('#icon-grid img').length > 20")
    # 图标是 loading=lazy：截图前强制加载前几屏，否则只会看到空白方块
    await page.evaluate(
        """() => {
            const imgs = [...document.querySelectorAll('#icon-grid img')].slice(0, 48);
            imgs.forEach((img) => {
                img.loading = 'eager';
                const src = img.src;
                img.src = '';
                img.src = src;
            });
        }"""
    )
    await page.wait_for_function(
        """() => {
            const imgs = [...document.querySelectorAll('#icon-grid img')].slice(0, 40);
            return imgs.length >= 40 && imgs.every((i) => i.complete && i.naturalWidth > 0);
        }""",
        timeout=45000,
    )
    await page.wait_for_timeout(400)
    sb = await page.locator("#icon-search").bounding_box()
    if sb:
        await page.mouse.move(sb["x"] + 60, sb["y"] + sb["height"] / 2, steps=10)
    await page.wait_for_timeout(400)
    await page.screenshot(path=str(out / "edit-modal.png"))
    await ctx.close()


async def shot_s4(browser, out: Path) -> None:
    ctx, page = await new_page(browser)
    await click_human(page, page.locator("#plugins-market-btn"))
    await page.wait_for_selector("#market-list .market-card")
    await page.wait_for_timeout(600)
    btn = page.locator(".market-card button").filter(has_text="创建图片").first
    box = await btn.bounding_box()
    await page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=10)
    await page.wait_for_timeout(500)
    await page.screenshot(path=str(out / "plugin-market.png"))
    await ctx.close()


async def shot_s5(browser, out: Path) -> None:
    api("DELETE", "/api/plugins/sticky-notes/data")
    seed_float_windows()
    ctx, page = await new_page(browser)
    await page.wait_for_timeout(600)
    frame = page.frame_locator("section.plugin-window[data-plugin='sticky-notes'] iframe")
    area = frame.locator("textarea")
    await area.click()
    await page.keyboard.type("周五 18:00 前把新版导航发出去", delay=6)
    await page.keyboard.press("Enter")
    await page.keyboard.type("- 先跑一遍 pytest", delay=6)
    await page.wait_for_timeout(800)
    await page.mouse.click(1200, 640)
    await page.wait_for_timeout(600)
    await page.mouse.move(690, 300, steps=12)
    await page.wait_for_timeout(500)
    await page.screenshot(path=str(out / "plugin-windows.png"))
    await ctx.close()


async def shot_s6(browser, out: Path, work: Path, server_python: Path) -> None:
    proc = start_server(work, server_python, port=PORT + 1, token="demo-gate-token")
    try:
        ctx, page = await new_page(browser, port=PORT + 1)
        # 访客点“添加网站”即被判为管理动作：面板不展开，直接弹 Token 引导
        await click_human(page, page.locator("#add-title"))
        await page.wait_for_selector("#token-modal:not([hidden])", timeout=12000)
        await page.wait_for_timeout(600)
        tb = await page.locator("#token-input").bounding_box()
        if tb:
            await page.mouse.move(tb["x"] + 80, tb["y"] + tb["height"] / 2, steps=12)
        await page.wait_for_timeout(500)
        await page.screenshot(path=str(out / "token-gate.png"))
        await ctx.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()


async def shot_s7(browser, out: Path) -> None:
    ctx, page = await new_page(browser, viewport={"width": 390, "height": 844}, dpr=2)
    box = await page.locator(".nav-item").nth(3).bounding_box()
    await page.mouse.move(box["x"] + 40, box["y"] + 26, steps=10)
    await page.wait_for_timeout(600)
    await page.screenshot(path=str(out / "mobile.png"), clip={"x": 0, "y": 0, "width": 390, "height": 700})
    await ctx.close()


async def shot_s8(browser, out: Path) -> None:
    ctx, page = await new_page(browser)
    await click_human(page, page.locator("#transfer-btn"))
    await page.wait_for_selector("#transfer-modal:not([hidden])")
    await page.wait_for_timeout(600)
    await page.screenshot(path=str(out / "transfer.png"))
    await ctx.close()


def wait_background_applied(timeout: float = 20.0) -> bool:
    """等宿主把插件导出的图片设为背景（换成了新的 bg-<hash> 文件）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        setting = api("GET", "/api/appearance/background")
        if setting.get("type") == "image" and setting.get("image") != BG_IMAGE:
            return True
        time.sleep(0.5)
    log("等待背景切换超时")
    return False


async def shot_g3(browser, out: Path, tmp: Path) -> None:
    clear_float_windows("poly-gradient")
    record_dir = tmp / "video-g3"
    record_dir.mkdir(parents=True, exist_ok=True)
    # 渐变窗口默认 1000×720：录制视口用 900 高，否则底部“设为背景”会被裁到屏幕外
    ctx, page = await new_page(browser, record_dir, viewport={"width": 1440, "height": 900})
    await page.mouse.move(660, 430)
    await page.wait_for_timeout(450)

    await click_human(page, page.locator("#plugins-market-btn"))
    await page.wait_for_selector("#market-list .market-card")
    await click_human(page, page.locator(".market-card button").filter(has_text="创建图片").first)
    frame = page.frame_locator("section.plugin-window[data-plugin='poly-gradient'] iframe")
    await frame.locator("#canvas").wait_for()
    await page.wait_for_timeout(1200)

    await set_range(frame, "#meshCols", "46")
    await page.wait_for_timeout(700)
    await set_range(frame, "#meshJitter", "26")
    await page.wait_for_timeout(700)
    await click_human(page, frame.locator("#meshSeed"))
    await page.wait_for_timeout(900)

    await frame.locator("#backgroundBtn").click()
    await page.wait_for_timeout(700)
    await frame.locator("#confirmOk").click()
    await asyncio.to_thread(wait_background_applied)
    await page.wait_for_timeout(1200)
    await click_human(page, frame.locator("#closeBtn"))
    await page.wait_for_timeout(1200)

    await page.mouse.move(720, 520, steps=12)
    await page.wait_for_timeout(900)

    video = page.video
    await ctx.close()
    webm = Path(await video.path())
    webm_to_gif(webm, out / "gradient-bg.gif", width=900, fps=11, skip=1.15, speed=1.5)
    if not os.environ.get("SHOTS_KEEP_VIDEO"):
        webm.unlink(missing_ok=True)
    api("PUT", "/api/appearance/background", {"type": "image", "image": BG_IMAGE})


def main() -> None:
    parser = argparse.ArgumentParser(description="生成 PrivLink 展示素材")
    parser.add_argument(
        "--only", nargs="*",
        default=["s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "g1", "g2", "g3"],
        choices=["s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "g1", "g2", "g3"],
    )
    parser.add_argument("--work", type=Path, default=DEFAULT_WORK)
    parser.add_argument("--rebuild", action="store_true", help="重建演示工作目录（含重新下载图标）")
    parser.add_argument("--background", action="store_true", help="把演示背景设为 BG_IMAGE")
    parser.add_argument(
        "--server-python",
        default=str(REPO / ".venv" / "Scripts" / "python.exe"),
        help="启动演示服务用的解释器（需装好项目依赖）",
    )
    asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
