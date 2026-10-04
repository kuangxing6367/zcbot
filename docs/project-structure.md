# 项目结构

## 目录结构

```
.
├── main.py                 # 启动入口：python main.py [自定义配置路径]
├── pyproject.toml          # 项目元数据/依赖（PEP 621，与 requirements.txt 同步）
├── requirements.txt        # 依赖安装入口（main.py 启动自检读取，兼容保留）
├── config.yaml             # 全局配置（首次启动生成）
├── core_plugins.yaml       # 官方扩展配置中心（启动自动扫描同步、回写、合并）
├── framework/              # 平台内核（不含任何协议接入实现；微内核设计见 zernus）
│   ├── core/               # 内核包：base(Framework) · dispatch · runtime · event_buffer
│   ├── ctx/                # 插件上下文包：base · messaging · events · webui · db
│   ├── loader/             # 加载器包：base(PluginLoader) · config · lifecycle · runtime · ui
│   ├── deps/               # 依赖包：PluginDepsMixin + pip 镜像安装
│   ├── perm/               # 权限包：core · admin · groups · tracks（懒加载）
│   ├── config.py           # 配置加载 + core_plugins.yaml 配置中心
│   ├── hooks.py            # 扩展点注册表（HookRegistry）—— 内核契约
│   ├── log_broker.py       # 日志总线
│   ├── dual_auth.py        # 双令牌（会话 token + API Key）
│   ├── scheduler.py        # 定时任务调度
│   ├── runtime.py          # 中立运行时上下文（current_source_var）
│   ├── database/           # 数据库包：storage · db · db_conn · dialect · schema · init_db · sql_sim
│   ├── messaging/          # 消息事件包：event · event_bus · router · protocol 等
│   ├── terminal/           # 终端包：builtins 编排 + cmd_{core,plugin,msg,info,ops} + panel
│   ├── api/                # 后台 REST：webapp · webserver · 插件/市场/框架运维等
│   └── ipc/                # core/host 双进程 JSON-RPC（dual_process 门控）
├── core_plugins/           # 官方扩展（在 core_plugins.yaml 开关，默认全关）
├── plugins/                # 用户扩展（每个一个目录，含 main.py）
├── webui/                  # 后台前端源码（Vue 3 + Vite + Element Plus）
├── core_plugins/webui/web/ # 前端构建产物（由 webui/ 构建，随 webui 插件自包含）
├── sql/                    # 建表 SQL（init.sql / init_mysql55.sql）
├── data/                   # 运行数据：zcbot.db、logs/、plugins_dat/
└── docs/                   # 开发文档（全部平铺在 docs/ 一层，入口 index.md）
```

## 权限开发接口速览

- **Event 上**（handler 内最常用）：`ev.has_perm(node)`、`ev.check_perm(node)`（三态）、`ev.perms`（快照）、`ev.perm_groups`、`ev.primary_group`，上下文自动从事件构造。
- **ctx 上**（非事件场景，如定时任务）：`ctx.has_perm(uid, node, context=..., role=...)`、`ctx.check_perm(...)`、`ctx.user_groups(...)`。
- **命令级**：`@ctx.command(..., require_perm="x.y", require_level="admin")`，两者任一满足即放行；解析顺序：用户直接节点 → 所属组（weight 降序）→ 精确度（精确 > 段通配 > `*`）→ 同精度否决优先。
- **缓存**：权限解析 60s TTL、组快照 10s，Web 端改动后自动失效。

完整机制见 [权限系统](permission.md)。

## 前端构建

后台前端源码在 `webui/`，产物输出到 `core_plugins/webui/web/`：

```bash
cd webui
npm install
npm run build      # 产物输出到 ../core_plugins/webui/web/
```

## 数据库建表

表结构见 `sql/init.sql`（MySQL 方言 DDL，运行时自动翻译给 SQLite/MySQL 双方使用）与 `sql/init_mysql55.sql`（MySQL 5.5 兼容），启动时自动建表补缺。详见 [数据库](database.md)。
