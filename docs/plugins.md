# PrivLink 插件规范与安全合规

PrivLink 支持通过**插件库**扩展导航页功能。插件以"内置精选"模式分发：插件文件随仓库发布，插件库即浏览内置清单。想新增插件，往 `plugins/` 目录加文件并在 `registry.json` 登记，通过测试后随版本发布。

## 一、信任模型（为什么这样设计）

主页面 CSP 允许 `script-src 'unsafe-inline'`（单文件前端 + Workers Assets 无法注入 nonce 的务实选择），因此**插件绝不能以注入脚本的方式进入主页面**——否则插件代码将获得与主站同等的权限（可读 `authToken`、全部书签）。

插件系统采用 **iframe 沙箱 + postMessage 能力桥**：

```
导航页（主文档，持 token）
 ├─ 插件宿主：沙箱生命周期 + 桥 + 插件库模态框 + inline/float 槽位
 │    └─ <iframe sandbox="allow-scripts" src="/plugins/<id>/index.html">
 │        （无 allow-same-origin → 不透明源，拿不到 DOM/localStorage/token/cookie）
 ├─ 后端 /api/plugins/*（TokenGuard 门禁）+ plugin_data 表 + 窗口布局注册表
 └─ 静态路由 /plugins/{id}/{path}（专用严格 CSP，本地与 Workers 两端一致）
```

即使插件代码完全恶意，沙箱内也接触不到任何真实数据；桥按清单声明的权限下发能力，未声明一律拒绝。

## 二、插件构成

每个插件一个目录，单 HTML 自包含（内联 CSS/JS），**禁止任何外部引用**（外链脚本/样式/图片均不允许）——保证离线可用、审查一眼看全、CSP 天然满足。不引入任何构建工具。

```
plugins/
├─ registry.json          # 唯一清单源（元数据都在这里）
├─ sticky-notes/
│  └─ index.html          # 插件本体
└─ poly-gradient/
   └─ index.html          # 插件本体
```

### registry.json

```json
{
  "bridgeVersion": 1,
  "plugins": [
    {
      "id": "sticky-notes",
      "name": "便签",
      "version": "1.2.3",
      "description": "在导航页添加便签，支持基础 Markdown（行内代码、列表、任务列表、块引用）",
      "author": "luis",
      "slot": "float",
      "entry": "index.html",
      "permissions": ["storage"]
    }
  ]
}
```

| 字段 | 约束 |
|---|---|
| `id` | `^[a-z0-9][a-z0-9-]{0,63}$`，禁止点；同时用作 URL 段与数据命名空间 |
| `version` | semver（`x.y.z`） |
| `slot` | `inline`（导航网格下方可折叠插件区）/ `float`（浮动窗口；默认每实例一窗，`singleton` 时至多一窗。宿主即"插件库"，卡片按钮文案由 `launchLabel` 决定） |
| `entry` | 入口 HTML 文件名（相对插件目录） |
| `permissions` | 子集 of `["storage", "export"]`；未声明的能力桥会拒绝 |
| `defaultSize` | 可选，`{w, h}`（浮窗首次创建的默认尺寸，宿主按视口与上下限钳制） |
| `singleton` | 可选布尔。`true` 时该插件至多一个窗口，启动入口在已打开时只会把现有窗口置顶聚焦 |
| `autoOpen` | 可选布尔，默认 `true`。`false` 时启用插件不自动浮出首窗，只能经插件库卡片 `launchLabel` 按钮或背景设置 `backgroundLabel` 入口启动 |
| `launchLabel` | 可选字符串（仅 float）。插件库卡片启动按钮文案，如"创建图片"；缺省时多实例插件为"新建窗口"，`singleton` 为"打开窗口" |
| `backgroundLabel` | 可选字符串（仅 float）。启用后在背景设置的"默认/纯色/图片"同级加入启动按钮，如"自定义渐变"；未启用或未声明则不显示 |
| `bridgeVersion` | 清单级桥协议版本，必须与宿主 `PLUGIN_BRIDGE_VERSION` 一致（有测试把关） |

### 内置插件

| id | 槽位 / 权限 | 说明 |
|---|---|---|
| `sticky-notes` | float / `storage` | 多实例便签：每张便签一个浮窗，支持基础 Markdown、拖拽移动与缩放，数据经桥持久化 |
| `poly-gradient` | float / `export` | 多点光源低多边形网格渐变：PC/移动尺寸预设与自定义尺寸、PNG 下载、设为背景；`singleton: true` + `autoOpen: false`，启用后不自动弹窗。入口有两处：插件库卡片的"创建图片"，以及背景设置顶部与"默认/纯色/图片"同级的"自定义渐变"。窗口内"关闭"直接丢弃本次编辑（插件未申请 storage），手动多边形绘制已移除，仅保留低多边形网格 |

## 二点五、浮动窗口模型（slot=float）

float 插件默认是**多实例**的：一个插件 = 任意多个独立窗口（如每张便签一个窗口）；`singleton: true` 的插件例外（至多一窗）。职责划分：

- **宿主管**：窗口挂载/销毁、位置与尺寸（`position:fixed` 视口坐标，拖拽/缩放后持久化）、点击置顶、视口钳制。窗口容器挂在 `<body>` 下（`.panel` 的 `backdrop-filter` 会成为 fixed 后代的包含块，嵌进去会定位错乱）。
- **插件管**：窗口内容与自己的标题栏视觉（+ 新建 / × 删除 / 拖拽把手 / 缩放把手；`singleton` / `autoOpen: false` 插件可只保留关闭等必要控件）。
- **实例注册表**（`app_settings` 键 `plugin_windows:<id>`，宿主经 `GET` + `POST .../windows/add`、`PATCH/DELETE .../windows/{instId}` 增量读写，全量 `PUT` 仅为兼容保留，插件经桥**不可达**）：`[{id, x, y, w, h}]`，≤100 实例。键不存在（GET 404）= 从未有过实例 → 宿主自动浮出首张空白窗口（`autoOpen: false` 除外）；`[]` = 用户刻意全部关闭 → 不自动重建（启动型插件用卡片 `launchLabel` 按钮兜底）。
- **手动启动 / 单实例**：`autoOpen: false` 的插件启用时不自动建窗，卡片按 `launchLabel` 显示启动按钮，`backgroundLabel` 会额外在背景设置的"默认/纯色/图片"同级生成入口；`singleton: true` 保证只存在一个窗口，重复启动只聚焦现有窗口；从背景入口启动会先关闭背景设置弹窗。停用 `autoOpen: false` 的插件会关闭其全部窗口并清除布局，避免再次启用时自动弹出；未关闭的窗口在刷新后照旧恢复。
- **实例数据**：桥对 float 实例的 storage 键做透明前缀 `<实例id>:<key>`，插件代码无感；关闭实例按前缀清理（`DELETE /api/plugins/{id}/data?prefix=<实例id>:`）。卸载 purge 清空整个命名空间 + 布局注册表。
- **生命周期顺序**（关闭实例）：解绑桥 → 移除窗口元素 → 更新注册表 → 清数据。

## 三、桥协议（v1）

宿主与插件通过 `window.postMessage` 通信，消息格式：

```js
{ v: 1, id: "p1", type: "storage.set", payload: { key: "notes", value: "[...]" } }
// 宿主回包
{ v: 1, id: "p1", ok: true, data: { value: null } }
```

- **安全校验**：宿主只处理 `event.source` 能在桥映射中命中的消息（iframe 创建时绑定 contentWindow↔(pluginId, 实例id)）；插件只信 `event.source === window.parent`。沙箱 iframe 的 origin 是 `"null"`，不能靠 origin 校验。
- **通用能力**：`hello`（握手，宿主回 bridgeVersion 与 permissions）、`storage.get/set/del`（宿主代持 token 调 `/api/plugins/{id}/data`，**插件永不接触 token**；float 实例自动加键前缀）、`resize`（iframe 高度自适应，仅 inline 槽位有效，float 窗口尺寸归宿主管）、`image.export`（需 `export` 权限，见下）。
- **图片导出能力**（`image.export`，需声明 `export` 权限）：

```js
// 插件 → 宿主；bytes 用 transfer list 传递
{ v: 1, id: "p1", type: "image.export",
  payload: { action: "download" | "background", bytes: arrayBuffer } }
// 宿主 → 插件
{ v: 1, id: "p1", ok: true, data: { width: 1920, height: 1080, action: "download" } }
```

  - 仅接受 PNG；宿主校验签名与 IHDR，宽高 ≤4096、总像素 ≤4096×4096。
  - `download`：宿主在页面文档内触发下载（≤16MB），插件无需（也无法）在沙箱内直接下载。
  - `background`：宿主以持有 token 的 `apiFetch` 调用 `POST /api/appearance/background/images`，成功后立即应用并刷新背景库；单图 ≤5MB（`BACKGROUND_UPLOAD_MAX_BYTES`）。**该设置是服务端全局状态，对所有访客生效**，插件应在 UI 中二次确认。
  - 插件侧不应自行拼接文件名/宽高；宿主从 PNG 头解析并生成文件名。
  - 下载在移动端可能被浏览器接管，插件应同时提供本地预览（`blob:` 图片由 CSP 放行）作为长按保存兜底。

- **float 专属能力**（inline 插件发来一律静默忽略）：

| 消息 | payload | 说明 |
|---|---|---|
| `window.drag` | start: `{phase: "start", mode: "move"\|"resize", x, y, pointerId}`；兜底 end: `{phase: "end", mode, x, y}` | 移动/缩放。`x/y` 为指针的 iframe 局部坐标，`pointerId` 供宿主过滤指针。**宿主在 start 时接管指针**：把 iframe 置 `pointer-events:none`，在 window 上监听 move/up 用页面绝对坐标驱动，结束后恢复命中、持久化位置，并向插件回推 `window.dragEnd` 通知。旧版的 move/end 坐标中继（`x/y/rx/ry` 参照系约定）仍被兼容处理，但新插件不应再使用——沙箱 iframe 收不到范围外指针事件，插件侧自行跟踪必然丢事件 |
| `window.create` | `{}` | 新建一个实例（宿主级联 +24px 定位），应答 `{id}`；`singleton` 插件由宿主兜底为聚焦现有窗口 |
| `window.close` | `{}` | 关闭当前实例（按上面顺序清理） |
| `window.focus` | `{}` | 当前窗口置顶 |

- `notify`/`openUrl` 为预留能力，未实现。

**拖拽的实现要点**（写 float 插件的拖拽时必读）：沙箱 iframe 只能收到指针位于其范围内的事件，`setPointerCapture` 不跨文档边界——插件侧自行跟踪指针，快速拖拽时必然丢 `move`/`up`（表现为抖动、松手后窗口停在旧位置、僵局会话吞掉下一次拖拽）。正确做法：`pointerdown` 时向宿主发 `window.drag` start（带局部坐标与 `pointerId`）后即不再跟踪，指针事件由宿主接管；插件监听宿主回推的 `window.dragEnd` 复位本地状态，并保留本地 `pointerup`/`pointercancel` 作为宿主未接管的兜底。

- **宿主 → 插件通知**：`{v: 1, type: "window.dragEnd", payload: {mode}}`（无 `id`，不走应答表）。宿主结束一次接管的拖拽/缩放后回推，插件据此复位本地拖拽状态；安全性同回包——插件只信 `event.source === window.parent`。

### 插件侧胶水代码

插件复制以下骨架使用（完整示例见 `plugins/sticky-notes/index.html`）：

```js
var seq = 0, pending = new Map();
function callBridge(type, payload) {
  return new Promise(function (resolve, reject) {
    var id = "p" + (++seq);
    pending.set(id, { resolve: resolve, reject: reject });
    window.parent.postMessage({ v: 1, id: id, type: type, payload: payload || {} }, "*");
    window.setTimeout(function () {
      if (pending.has(id)) { pending.delete(id); reject(new Error("桥响应超时")); }
    }, 8000);
  });
}
window.addEventListener("message", function (event) {
  if (event.source !== window.parent) return;
  var msg = event.data;
  if (!msg || typeof msg !== "object" || msg.v !== 1) return;
  if (msg.type === "window.dragEnd") { /* 复位本地拖拽状态 */ return; }
  if (!pending.has(msg.id)) return;
  var task = pending.get(msg.id); pending.delete(msg.id);
  if (msg.ok) task.resolve(msg.data); else task.reject(new Error(msg.error || "桥调用失败"));
});
// 初始化：hello 握手（重试数次避开竞态）→ storage.get 取数据
// 另：window.top === window.self 时是被直接导航打开，应提示"请在导航页中使用"
```

注意事项：

- 沙箱 iframe 内 `localStorage`/`document.cookie` 访问会抛 SecurityError，**一律走桥的 storage 能力**。
- 渲染用 `createElement` + `textContent`，不要拼 HTML。
- 高度变化后调 `resize`，宿主负责设置 iframe 高度。

## 四、上线安全合规（审查清单）

新增插件随仓库 PR 审查，逐项确认：

- [ ] registry.json 条目符合 schema（有测试校验：`tests/test_plugins.py::test_builtin_registry_schema`）
- [ ] 手动启动 / 单实例插件声明 `autoOpen: false` / `singleton: true`，并提供 `launchLabel`；需要背景入口时再声明 `backgroundLabel`
- [ ] 入口 HTML 通过静态审查——以下模式一律不得出现（测试 `test_builtin_plugin_html_static_review` 把关）：
  - 外部脚本/资源引用（`<script src=`、`src="//…"`、`href="//…"`）
  - 网络 API（`fetch(`、`XMLHttpRequest`、`WebSocket(`、`EventSource(`、动态 `import(`、`sendBeacon`）
  - 嵌套浏览上下文与表单（`<iframe`、`<form`、`target="_blank"`）
- [ ] 数据经 storage 桥存储，不尝试 localStorage/cookie
- [ ] 图片导出只走 `image.export` 桥：不在沙箱内直接下载/上传，不自行拼接文件名
- [ ] `background` 动作有二次确认（全局生效）；PNG 尺寸 ≤4096、背景图 ≤5MB 已在插件侧预检
- [ ] 渲染卫生：`createElement`/`textContent`，不拼 HTML 字符串
- [ ] 功能克制：权限只申请真正需要的

静态审查是**准入关**，不是安全边界——真正的硬边界是沙箱与服务时 CSP，审查失误不会升级为页面被攻击。

### 服务时收口（纵深防御，自动生效）

| 层 | 机制 |
|---|---|
| 主防线 | iframe `sandbox="allow-scripts"`（无 allow-same-origin）→ 不透明源，拿不到 token/DOM/cookie |
| 文档 CSP | `CSP_PLUGIN`（config.py）：断网、img 仅 data:/blob:（堵死外传信道）、无 frame/popups；本地由静态路由显式下发，Workers 由 `_headers` 的 `/plugins/*` 段下发 |
| 加载门禁 | 静态路由校验 registry 成员——未收录目录不可加载 |
| 端点门禁 | `/api/plugins/*` 全部经 TokenGuard，不在公开只读白名单 |
| 数据配额 | 每插件 200 键 / 256KB（config.py `PLUGIN_DATA_MAX_*`），超限写入拒绝 |
| 卸载清理 | 卸载（`purge=1`）清空该插件命名空间的全部数据 |

## 五、数据与部署

- 插件数据存 `plugin_data` 表（`plugin_id, key, value` 命名空间隔离），本地 SQLite 与 Cloudflare D1 双后端；新表 DDL 在 `init_storage()` 与 `D1_SCHEMA_STATEMENTS` 中，存量部署升级即自动迁移。
- 安装状态存 `app_settings` 键 `plugins_state`（JSON），多设备一致。
- 仅持 token 的 owner 渲染插件区；访客视图不加载插件。
- 插件静态文件：本地由 `/plugins/{id}/{path}` 路由直读仓库根 `plugins/`（Docker 镜像含 `COPY plugins/`）；Workers 由 Assets 服务 `assets/plugins/`（`scripts/sync-frontend.py` 目录镜像）。

## 六、新增插件步骤

1. 建 `plugins/<id>/index.html`（桥胶水参考 sticky-notes；图片导出与单实例/手动启动参考 poly-gradient）；
2. 在 `plugins/registry.json` 登记条目（id 与目录名一致）；
3. `python scripts/sync-frontend.py`（镜像进 `assets/plugins/`，Workers 部署用）；
4. 对照第四节审查清单自查；
5. `uv run python -m pytest tests/test_plugins.py -q` 全绿后随版本提交。

