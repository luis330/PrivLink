# 环境约定

## 系统环境
- 简体中文 Windows 11
- 工作目录：`D:\Studyspace\Gitea\Nav_Loacl`
- Shell：PowerShell 7+（pwsh），不是 bash
- 包管理：uv（注意 PATH 上可能是 hermes 的版本）

## 路径处理（必须遵守）
- 一律使用 Windows 格式：盘符 + 反斜杠，如 `D:\Studyspace\Gitea\Nav_Loacl\src`
- 盘符必须正确：D 盘就是 `D:\`，不丢盘符、不补成 `C:`
- 禁止 POSIX 风格路径（`/home/...`、无盘符 `/Wanglf/...`）
- 涉及 Python 解释器路径时，交叉校验三处一致：
  1. `pyvenv.cfg` 的 `home`
  2. trampoline / `sys.executable` 自报值
  3. `UV_PYTHON_INSTALL_DIR` 与实际文件位置
- 发现自报路径无盘符或盘符错误时，按实际文件位置修正，不信任自报值

## 交互语言
- 使用简体中文
- 用 Windows 术语：虚拟环境、工作目录、注册表、环境变量、服务
- 不混用 Linux 术语（如"主目录"→"用户目录"、"可执行权限"→"ACL"）

## 命令约定
- 用 PowerShell cmdlet：`Get-ChildItem`、`Set-Content`、`Test-Path`
- 不用 bash 别名：`ls`、`cat`、`grep`、`&&`（PowerShell 用 `;`）
- 调用 exe 用 `& "路径\to\exe.exe"`，路径含空格必须引号
- 路径参数用 `-LiteralPath`，避免通配符被解析

## 已知环境坑（勿重复踩）
- uv trampoline 可能内嵌错误盘符路径（C:/D: 不符），报 `entity not found` 时查二进制资源区与 `pyvenv.cfg`
- Pyodide 解释器自报 `sys.executable` 会丢盘符（POSIX 风格），不可直接信任
- uv probe 失败会静默回落到项目 `.venv`，导致包装错位置——用 `--target` + `--python-platform` 绕过
- npx 缓存的 `workerd.exe` 可能截断，报 `spawn EFTYPE` 时用 npm 重装 `@cloudflare/workerd-windows-64`

## ZCode 工具链事实（会话内验证）
- Bash 工具实际运行 cmd.exe 而非 pwsh：PowerShell 命令需显式包装 `pwsh -NoProfile -Command "..."`；cmd 不支持 HEREDOC——`git commit -m "$(cat <<'EOF'…)"` 会把字面量当消息，多行提交消息用 `git commit -F <消息文件>`；cmd 也不识别 `|` 管道内的 pwsh 片段与 `tail` 等Unix 工具，复杂文本处理改用 `.venv\Scripts\python.exe -c`（参数整体用双引号、内部用单引号）
- 计划模式（plan mode）下 Bash 与 SendMessage 一律被禁，只读核查用 Explore 子代理
- 浏览器自动化（IAB）怪癖：截图常比 evaluate 交互滞后一帧——交互与截图分两个 JS 调用、间隔 ≥1s 可避开；`tab.reload()` 不一定刷新 iframe 子资源缓存（必要时给 src 加 `?cb=` 参数穿透）；合成拖拽事件带子像素坐标，断言元素位置留 ±1px 容差

## 项目结构（PrivLink）
- 纯 Python 项目（pyproject name=`privlink`）：**无 package.json、无前端构建步骤**，前端改动不需要任何打包
- 后端：`src\privlink\`（FastAPI），入口 `main.py`；配置集中在 `src\privlink\config.py`（默认端口 8000；`NAV_TOKEN` 为空 = 开放模式，`.env` 不存在即开放）
- 本地起服务：`& "D:\Studyspace\Gitea\Nav_Loacl\.venv\Scripts\python.exe" -m uvicorn main:app --host 127.0.0.1 --port <端口>`（须在仓库根目录运行，路径全是相对 CWD）
- 前端：**根目录 `index.html` 是唯一样式源码**（约 203KB 单文件，CSS/JS 全内联，CRLF 行尾——脚本读写用 `newline=""` 保真，勿产生整文件行尾抖动）
- `assets\` 是部署镜像（Cloudflare Workers 用，`wrangler.jsonc` + `src\worker.py`），由 `scripts\sync-frontend.py` 从根目录同步（index.html、manifest.json、图标、plugins/）。**改任何前端/插件文件后必须重跑 sync**；本地部署由 FastAPI 直接读根目录原件
- 测试：`& ".\.venv\Scripts\python.exe" -m pytest tests\ -q`（约 110 项，改前端后跑一遍；含 token 执法检查）
- 内置插件在 `plugins\<id>\index.html`，清单 `plugins\registry.json`；桥协议文档 `docs\plugins.md`
- 改前端的标准流程：改根目录原件 → `scripts\sync-frontend.py` → `pytest` → 浏览器实测（对照改版前后截图）

## 前端样式约束（设计 token，2026-10 收拢定型）
风格基调：玻璃光晕——径向光斑×2（--glow-a/--glow-b）+ 线性渐变 `--bg-a→--bg-b` + backdrop-blur 半透明白面板。主色 `#0f7cf0`（不是 #165DFF，该值在仓库中不存在）。**先收尺度再谈风格：不要往样式里引入新的裸值。**

- `:root` 双层 token（改值改这里，不要散改规则）：
  - 颜色/语义层：`--button-bg/--button-hover`（主色对）、`--bg-a/--bg-b`（画布渐变对）、`--focus/--focus-soft/--focus-soft-2`（焦点族）、`--ok/--warn/--bad` 及状态底 `--ok-bg/--warn-bg/--bad-bg`、`--danger/--danger-ink`（危险族）、`--brand-line/soft/soft-2/ink`（品牌派生）、`--surface-1/2/3`（亮面阶）、`--chip-ink/--chip-x-bg/-hover`、`--glass-*`、`--fallback-*` 等 56 个
  - 尺度层：圆角 3 档+full `--radius-s 10 / --radius-m 14 / --radius-l 18 / --radius-full 999`（等差 4）；字号 5 级+display `--fs-xs 12 / --fs-s 13 / --fs-m 14 / --fs-l 16 / --fs-xl 19` + `--fs-display clamp(24px,3.4vw,34px)`；间距 4 基数 6 档 `--sp-1 4 … --sp-6 24`；阴影 3 级 `--shadow-1/2/3`（统一 rgba(8,22,64,α) 色相，focus 环独立不算阴影）；玻璃滤镜 3 档 `--filter-glass blur(10px)+saturate(140%) / --filter-menu blur(18px) / --filter-modal blur(22px)`（模糊深度=浮层高度，blur 必配 saturate 防发灰，借鉴 Station 配方）+ 玻璃面内高光 `--glass-highlight`（inset 1px 白，叠在阴影 token 之后）
- 硬约束：`font-size`、`border-radius`、`padding/margin/gap`、`box-shadow`、`backdrop-filter` 一律引用 token 变量；颜色字面量只允许出现在三处——`:root` 定义、`<meta theme-color>`/manifest、色板 `data-color`（用户可选值）。**执法已脚本化**：`scripts\check-tokens.py` 扫描上述 7 属性裸值（白名单 = `:root` 块、行内 `/* token-allow: 理由 */` 标记、无单位 0），`tests\test_frontend_tokens.py` 挂进 pytest，CI 的 pytest 步骤自动携带。**作用域为宿主 index.html**：沙箱插件文档（plugins\*）没有 `:root` 可引用，豁免本约束，沿用插件内既有约定
- 有意保留的例外（均已带 `token-allow` 标记，勿当漏洞"修复"）：`3px/4px`（:focus-visible 描边贴合直角控件）、`50%`（圆形）、`2px`（发丝间距，低于 --sp-1 的微调）、`0 0 0 3px` focus 环与 tag-suggest 双环内环 `1px`（选中态描边）、拖拽落点指示条 `±3px`（硬边方向语言，`.nav-item.drag-over-*`）、`190px/210px`（body 底部给停靠面板 `.add-panel` 预留的滚动空隙，桌面/移动两档，改面板高度时同步）、`0.8em`（▾ 箭头伪元素）；另 nav-grid 列间距恒为 0（80px 定宽项自带 28px 视觉空隙，有意为之）
- 字号层级语义：11px 时代的辅助小字已统一升到 `--fs-xs 12`；15px 标题归 `--fs-l 16` 拉开与正文的层级

## 插件协议约束（float 拖拽，2026-10 修复定型）
- 沙箱 `sandbox="allow-scripts"` 无 same-origin（opaque origin）：**指针事件不跨 iframe 文档边界，`setPointerCapture` 不跨 frame**——插件内严禁自行跟踪拖拽指针（曾因此产生快速拖拽抖动、松手失位、僵局会话吞掉下一次拖拽，已修复）
- 正确模型：插件 pointerdown 只上报一次 `window.drag` start（iframe 局部坐标 + pointerId），宿主置 iframe `pointer-events:none` 后在 window 上用页面绝对坐标接管 move/up；结束时宿主恢复命中、持久化位置，并回推 `window.dragEnd` 通知复位插件状态
- 宿主→插件通知形如 `{v, type, payload}`（无 id，不走应答表）；旧版 move/end 坐标中继（rx/ry 参照系）仍被兼容但不要再写
- 拖拽期间的防御已内建：窗外松手借重入首拍（`buttons===0`）收敛；插件侧 pointerdown 无条件重启会话兜底
- 窗口注册表（windows 布局）**前端写路径一律走增量端点**（`POST .../windows/add` 幂等 upsert、`PATCH/DELETE .../windows/{instId}`，服务端每插件进程内写锁串行化读-改-写），全量 PUT 仅为兼容保留——增量写消除跨标签页过期快照整键覆盖的竞态；布局 GET 失败时前端置未知态并禁止注册表写；注册表/布局写失败经"重试一次"仍败会弹页面顶部持久化警告条，不许再静默吞错
