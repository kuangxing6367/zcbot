# WebUI 管理后台

Web 管理后台插件（官方插件）。提供 Web 管理面板和 REST API。

## 元信息

- 优先级：0
- 进程归属：core
- 默认开关：启用（`web.enabled` 设为 `false` 可禁用）
- 提供的服务：`web_server`（禁用时注册 `WebServerStub` 假节点，调用方判空安全）

## 前端构建产物位置

前端源码位于 `webui/`（Vue 3 + Vite + Element Plus），构建产物输出到
`core_plugins/webui/web/`，由框架静态路由 `framework/api/static_routes.py` 直接托管。
即前端与插件自包含在一起，不再散落在项目根目录的 `web/`。

构建：

```bash
cd webui
npm install
npm run build   # 产物输出到 ../core_plugins/webui/web/
```

## 接入方式

启用时延迟导入 `flask` / `waitress` 等重依赖（依赖由本插件 `requirements.txt` 声明，
缺失时加载器自动补装），创建 `WebServer` 并 `start()`；禁用时仅注册假节点不创建 Flask 应用。

## 可配置项（config.yaml 段 `web`）

| 键 | 说明 | 默认值 |
|---|---|---|
| `enabled` | 是否启用 | 启用 |
| `host` | 监听地址 | `127.0.0.1` |
| `port` | 监听端口 | `8080` |
| `official_sidebar` | 官方侧边栏显隐 | 由配置文件决定 |

启用 `config['ssl']` 后 Web 走 https（waitress 不支持 TLS，改用 werkzeug 的
`make_server(ssl_context=...)`）。

## 相关文件

- `webui/src` 前端源码
- `webui/vite.config.js` 构建配置（`outDir: '../core_plugins/webui/web'`）
- `webui/requirements.txt` 后端依赖（flask / flask-cors / waitress）
- `core_plugins/webui/web/` 构建产物（随插件自包含）

## 说明

管理后台的菜单、侧边栏、仪表盘卡片等能力由各个插件通过 `ctx.webui(...)` /
`@dashboard_card` / `@api` 等接口动态注册，本插件只负责承载 Web 服务与静态资源。
