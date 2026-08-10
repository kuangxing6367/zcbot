# ZCBOT 更新日志

> 版本号规则：`0.0.1-alpha.1-build.N`，N 逐次 +1 不跳号。
> 框架在线更新走 GitHub Release（tag 即版本号），详见 README「框架更新」。

---

## v0.0.1-alpha.1-build.22

### 插件页面路由修复

- fix(webui): 修复插件页面切换插件时路由 key 解析失败回退主页的问题——路由解析先截断 `?` 查询参数再取路由段，`#/plugin_webui?name=xxx&entry=...` 正确解析为 `plugin_webui`
- 优化：侧边栏进入插件页面时自动带上第一个插件的 `name`，hash 始终有参数，避免残留旧参数指向不存在的插件

> 纯前端 JS 改动，刷新页面（Ctrl+F5 强刷）即可生效，无需重启框架。

---

## v0.0.1-alpha.1-build.21

### 框架改进（6 项）

- **event.py**：`_extract_text` 支持 `share` 段提取 `data.url`、`json` 段（小程序/链接卡片）正则提取 http(s) 链接；新增 `_has_text_segment` 区分"纯富媒体消息"与"文本消息"
- **event.py**：新增 `Event.continue_route()` / `is_continue_route()`——插件命中命令后声明"允许系统关键词自动回复继续尝试"
- **event_bus.py**：`aemit()` 返回 `bool`，任一订阅 handler 返回 `True` 视为"已处理"（向后兼容，None/False 不影响）
- **router.py**：新增 `message` 事件广播（文本消息统一监听通道）——路由顺序：插件命令 → `message` 事件 → 系统关键词自动回复；`continue_route()` 时仍尝试关键词回复
- **router.py**：`dynamic_commands` 关键词支持 `handler` 回调（`plugin:func`）动态生成回复内容，失败自动回退静态 `response`；旧表无 `handler` 列自动回退基础查询
- **db.py**：新增 `_mysql_prefix_indexes()`——MySQL DDL 中 `TEXT` / `VARCHAR`>191 的被索引列自动改写为前缀索引 `` `col`(191) ``，避免错误 1170 / 1064（插件写长列 + 索引不再建表失败）；新增 `dynamic_commands.handler` 列自动迁移
- **ctx.py**：新增 `create_table(ddl)` 插件建表统一入口，自动适配方言（SQLite 翻译 / MySQL 前缀索引）
- **sql**：`dynamic_commands` 表新增 `handler` 列（新装环境直接建好）
- **Web/API**：关键词回复管理新增 `handler` 字段（新增/编辑/列表展示）

> 更新后重启生效。

---

## v0.0.1-alpha.1-build.20

### 系统级动态命令（关键词自动回复）落地

- 补全 `dynamic_commands` 表匹配引擎：插件命令均未命中时按关键词自动回复
- 四种匹配方式：`exact`（完全相等）/ `prefix`（前缀）/ `contains`（包含，新增）/ `regex`（正则），正则预编译进内存路由表，热路径零 DB；命中计数批量落库
- 关键词规则随路由表后台刷新（5s）+ 增删改即时重建；无插件环境同样可用
- 新增 `/api/dynamic-commands` 增删改查/启停（写操作需超管 + 审计日志）
- Web 命令管理页新增「关键词回复」卡片（新增/编辑/启停/删除/命中数）
- MySQL 旧库自动迁移 `match_type` ENUM 增加 `contains`；SQLite 无约束免迁移
- 文档：PLUGIN_DEV.md 新增「系统级动态命令」章节，README 特性列表补充

---

## v0.0.1-alpha.1-build.19

### 非文本消息（分享卡片等）事件广播

- 无文本消息（纯分享卡片/图片/视频等）广播 `message.<段类型>`（如 `message.share`）与通用 `message.media` 事件，插件通过 `ctx.on()` 订阅处理
- `Event` 新增 `has_share` / `share` 属性（分享卡片 url/title/desc）
- 文档：PLUGIN_DEV.md 非文本消息事件订阅说明 + README 插件开发示例

---

## 更早版本

- `v0.0.1-alpha.1-build.18`：插件页 401 免重登 + 文件浏览器右键菜单（复制/删除）
- `v0.0.1-alpha.1-build.17`：深色/浅色主题切换 + 文件管理升级（宝塔风）
- `v0.0.1-alpha.1-build.15`：插件 WebUI iframe 401 修复（token 同步 HttpOnly Cookie）
- `v0.0.1-alpha.1-build.14`：设置页框架操作新增「查看备份/回滚」
- `v0.0.1-alpha.1-build.13`：框架更新走最新 Release tag ZIP + ZIP 魔数校验
- `v0.0.1-alpha.1-build.11`：框架更新检测改走 GitHub Release（tag 版本号对比）
- `v0.0.1-alpha.1-build.10`：MySQL 改 DBUtils 连接池，修复连接无限增长
- `v0.0.1-alpha.1-build.9`：扩展表 DDL 改 VARCHAR，修复 MySQL 建表失败
- `v0.0.1-alpha.0-build.4`：首个版本（基于 Python 的 OneBot v11 QQ 机器人框架）
