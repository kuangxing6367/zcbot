# ZCBOT 🤖

> 一个开箱即用的 QQ 机器人框架。装上就能跑，**不会写代码也能用**——里面的 AI 助手能帮你写插件。
> 基于 OneBot 11 协议，全异步，自带网页管理面板。

**当前版本：v1.2.0-beta.1**

📚 项目地址：https://github.com/kuangxing6367/zcbot
💬 反馈交流：QQ 群 **1060129201**

---

## 一、它是干嘛的？（先说大白话）

你有一个 QQ 号（一般是小号）。ZCBOT 能让这个 QQ 号变成**机器人**：

- 群里发 `/echo 你好`，机器人回"你好"
- 接到大模型，**@机器人 就能和它聊天**
- 让它**定时干点事**（每天早上报天气、整点发提醒……）
- 想要新功能？**直接用聊天的方式让 AI 助手帮你写**，不用自己敲代码

它把最麻烦的部分（连 QQ、收消息、发消息、存数据、网页后台）全做好了，你只负责"想要什么"。

---

## 二、快速开始（跟着做，大约 5 分钟）

> 全程要敲的命令很少，复制粘贴就行。遇到不懂的词，点它旁边的 **「什么是 XX」** 展开看解释。

### 第 1 步：装 Python

机器人是用 Python 写的，先装一个 Python 3.10 或更高版本。

<details>
<summary><b>什么是 Python？</b>（点我展开）</summary>

Python 是一种编程语言，这个机器人就是用 Python 写的。你不需要会写 Python，只需要把它**当成一个"运行时"装好**，让机器人的代码能跑起来。

装好后，在终端（命令提示符 / PowerShell）输入下面命令验证：

```bash
python --version
```

看到 `Python 3.10.x` 或更高的数字就说明装好了。

</details>

### 第 2 步：下载代码 + 装依赖

```bash
git clone https://github.com/kuangxing6367/zcbot.git
cd zcbot
pip install -r requirements.txt
```

> 💡 依赖（就是机器人要用的零件）缺了不用慌，启动时框架会**自动补装**。

<details>
<summary><b>什么是 git / clone / pip？</b>（点我展开）</summary>

- **git**：一个下载和管理代码的工具。`git clone` 就是把整个项目的代码**下载到你的电脑上**。
- **pip**：Python 的"应用商店"。`pip install` 就是安装机器人在运行中需要的第三方小工具（叫"依赖"或"库"）。
- 如果提示找不到 `git` 或 `pip`，就先去网上搜"git 安装"或"Python 安装"，装好再回来继续。

</details>

### 第 3 步：启动机器人

```bash
python main.py
```

看到下面这行字，就说明机器人本体已经跑起来了 🎉

```
框架启动完成，等待消息...
```

第一次启动会自动生成一个 `config.yaml` 配置文件，一般不用改。

<details>
<summary><b>什么是 config.yaml？</b>（点我展开）</summary>

`config.yaml` 是机器人的"设置文件"，就像手机的设置 App。里面记录着端口号、数据库、密码等。第一次启动会自动生成，**默认就能用**。后面想改设置再打开它。

关键配置项（默认端口）：

| 配置项 | 默认值 | 作用 |
| ------ | ------ | ---- |
| `onebot.listen_port` | `6830` | OneBot 客户端**反向 WebSocket** 连接端口 |
| `onebot.access_token` | 空 | 客户端接入令牌（**必须设置**，留空等于任何人可接入） |
| `web.host` / `web.port` | `127.0.0.1` / `8080` | 网页后台监听地址与端口 |
| `web.session_timeout` | `3600` | 登录会话有效期（秒），同时也是登录 Token 有效期 |
| `database.type` | `sqlite` | `sqlite`（零配置）或 `mysql` |

</details>

### 第 4 步：让机器人连上 QQ（最关键的 1 步）

机器人本体跑起来之后，还**需要一个"桥"把它和 QQ 连起来**。这个桥叫 **OneBot 客户端**。

1. 下载一个 OneBot 客户端，比如 **NapCat**、**Lagrange** 或 **go-cqhttp**
2. 在客户端里设置**反向 WebSocket** 连接，填：

| 设置项 | 填什么 |
| ------ | ------ |
| 连接地址 | `ws://127.0.0.1:6830` |
| Access Token | 打开 `config.yaml`，把 `onebot.access_token` 的值抄过来 |

3. 保存后，客户端会自己连上机器人。看到日志或面板里出现"客户端已连接"，就成功了！

<details>
<summary><b>什么是 OneBot 客户端？什么是反向 WebSocket（WS）？</b>（点我展开）</summary>

QQ 官方不提供"把号变成机器人"的接口，所以社区做了 **OneBot 客户端** 这种软件：它能接管一个 QQ 号，然后把收到的消息**转发**给机器人程序。ZCBOT 就属于"机器人程序"这一端。

- **WebSocket（WS）**：一种"随时保持通话"的网络连接方式。消息一有就立刻传，不用反复拨号。你可以把它理解成**电话连线**，而不是发短信（那种叫 HTTP，一问一答）。
- **反向连接**：正常情况下是"客户端"找"服务器"（正向）。这里反过来了：**机器人是服务器**（它在 `6830` 端口等着），**客户端主动来连它**，所以叫"反向"。你只需要让客户端填对地址连过来就行，不用管正反向的术语。

简单说：**机器人开好门（6830 端口），客户端自己走进来，QQ 消息就能互通了。**

</details>

<details>
<summary><b>什么是 Access Token（令牌）？</b>（点我展开）</summary>

Access Token 就是**一串密码**。为了防止随便什么人都能连上你的机器人、冒充它乱发消息，机器人和客户端之间约定一个密码，连上来之前先对一下，对不上就拒绝。

所以：`config.yaml` 里写一个（改完要重启机器人），客户端里填同一个。**两个保持一致就行**。⚠️ 上线前一定要设一个自己的密码，别用默认的。

</details>

### 第 5 步：打开网页后台看看

浏览器打开 `http://localhost:8080`，用默认账号登录：

- 账号：`admin`
- 密码：`admin123`

<details>
<summary><b>什么是网页后台（Web 面板）？</b>（点我展开）</summary>

就是一个**网页版的管理页面**。你可以在浏览器里看机器人状态、管理插件、看日志、看有多少群和用户在用，不用对着终端敲命令。装完打开看看长什么样就明白了。

⚠️ 上线前记得在后台**改掉默认密码**！

</details>

---

到这里，机器人已经能用了。下面是"怎么玩"。

---

## 三、常见玩法

### 1️⃣ 测试一下：让机器人回话

在群里或私聊发：

```
/echo 你好
```

机器人会原样回你"你好"。

### 2️⃣ 和机器人聊天（接上大模型）

让机器人变成"AI 聊天机器人"：在后台插件配置里填上大模型的地址和密钥，然后 @机器人 或发 `/chat` 就能聊天。支持长期记忆、人格设定、自动 @ 回复。

### 3️⃣ 想要 AI 帮你写插件？

内置了 [AI 助手插件（llm_plugin_gen）](#五内置插件一览)，**想要什么功能，直接跟机器人聊天，它帮你把插件写好并装上**。全程不用你碰代码。

> 该功能由插件 `llm_plugin_gen` 提供，不是框架自带能力。该插件在官方插件仓库独立维护，从 [插件仓库](https://github.com/kuangxing6367/zcbot_plugins/tree/main/plugins/llm_plugin_gen) 获取，填好大模型信息后即可使用，详细用法见 [插件文档](https://github.com/kuangxing6367/zcbot_plugins/blob/main/plugins/llm_plugin_gen/docs/INDEX.md)。

---

## 四、我要自己写插件怎么办？

文档里专门有一篇**手把手教程**，从建文件夹开始带你写第一个插件，每个概念都有解释。

> 📖 [插件开发详解：一个示例插件，逐行讲透每个语法](docs/plugin-tutorial.md)（推荐，讲得最细）
> 📖 [快速入门](docs/getting-started.md)（精简版，10 分钟上手）

简单说，一个插件就是一个文件夹，里面有：

- `__plugin_meta__`：插件的"身份证"（名字、版本、简介）
- `register(ctx)`：告诉机器人"我有哪些命令"

写完把文件夹放进 `plugins/` 目录，在后台点"重载"就能用。**写好的插件也可以再让 AI 帮你改 bug、加功能。**

### 在插件里做权限控制

框架内置一套 LuckPerms 风格的权限组系统（见[第七节](#七权限系统luckperms-风格)）。在命令里用 `require_perm` 声明所需权限节点即可，框架会自动拦截无权限用户：

```python
@ctx.command("ban", require_perm="myplugin.ban", help="封禁用户")
def ban(ev):
    ...
```

也可以在 handler 内部手动判断（拿到三态结果：`True` 授予 / `False` 显式否决 / `None` 未定义）：

```python
if ev.has_perm("myplugin.ban"):       # 未定义按拒绝
    ...
if ev.check_perm("myplugin.ban") is False:   # 显式否决
    ...
```

---

## 五、内置插件一览

| 插件 | 作用 |
| ---- | ---- |
| **echo** | `/echo 内容` 原样返回，测试用 |
| **help** | `/help` 生成图片帮助菜单 |
| **image_renderer** | 通用图片渲染引擎（生成卡片、文字图） |
| **runtime_status** | `/status` `/info` 查看运行状态（带图片版状态卡） |
| **restart_manager** | 框架重启管理 |
| **message_guard** | 消息防护（防刷屏、限流、敏感词） |
| **plugin_depgraph** | 插件依赖关系扫描 |
| **session_waiter** | 多轮会话基础设施（`wait_for_user`） |
| **ui_ext_demo** | 网页后台扩展演示（列表 + 详情面板） |

> 还想要 AI 对话、群管理、签到积分、视频解析这些功能？把 `config.yaml` 的 `plugin.dir` 指向官方插件源仓库（`kuangxing6367/zcbot_plugins`），就有 20+ 个现成插件可用：

```yaml
plugin:
  dir: ../zcbot_plugins/plugins
```

常用官方插件：

| 插件 | 作用 | 怎么用 |
| ---- | ---- | ---- |
| **llm_chat** | 接入大模型，@机器人 或 `/chat` 就能聊天 | `/chat 你好`、@机器人 |
| **llm_plugin_gen** | AI 帮你写插件，描述需求即可 | `/ai 帮我写个签到插件` |
| **qqadmin** | 群管理全套（禁言、踢人、撤回、审批、违禁词、宵禁） | `禁言 @张三 10`、`设置禁词 广告` |
| **fun_score** | 签到积分、排行榜 | `签到`、`我的积分`、`排行榜` |
| **send_like** | 点赞、自动点赞 | `/赞我`、`/自动点赞` |
| **video_parse** | 群里发视频链接自动解析成卡片 | 直接发链接，或 `/解析` |
| **file** | 服务器文件管理 | `/文件列表`、`/发送文件 data/x` |
| **hitokoto** | 随机一言 | `一言` |
| **broadcast** | 消息批量广播 | 回复消息发 `广播` |
| **custom_ui** | 接管网页后台，换个性化主题 | 后台模板管理页下载/切换 |
| **minecraftconsole** | MC 服务器控制台（需另行设计工作流与玩家绑定） | `mc-command say 你好` |
| **dbcj-mcstatus** | MC 服务器状态 | `/mc状态` |
| **plugin_memmon** | 插件内存监控 | `/mem`、`/memdiag` |
| **llm_blacklist** | LLM 对话黑名单 | `/插件拉黑 12345` |

> 每个插件的完整命令列表和用法例子见 [📖 官方插件使用手册](docs/official-plugins.md)。

---

## 六、常见问题

**Q: 机器人不回复？**
A: ① 看后台或日志，确认 OneBot 客户端是否"已连接"；② 确认 `access_token` 两边填得一致；③ 确认消息是命令开头（如 `/echo`）。

**Q: 网页后台打不开？**
A: 确认 `web.host` 是 `127.0.0.1`（本机）或 `0.0.0.0`（局域网）。还是不行就重启机器人在看日志里有没有"端口被占用"。

**Q: 改了插件代码没生效？**
A: 到后台「插件」页点"重载"，或者重启机器人。

**Q: 忘了管理员密码？**
A: 看 `data/logs/` 里的日志提示，或者删掉 `data/zcbot.db` 重新初始化（会重置所有数据，慎用！）。

**Q: 内存一直涨？**
A: 机器人会自动定期释放空闲内存。持续上涨发 `/memdiag` 诊断看看。

**Q: 调用 REST API 时 session token 过期了怎么办？**
A: 用户登录 token 会随登录/登出轮换且有时效（`web.session_timeout`）。需要长期、稳定的外部调用，请改用[接口令牌（API Key）](#八接口令牌api-key)，它独立于用户会话、长期有效。

---

## 七、权限系统（LuckPerms 风格）

框架在原有「单一 `role` 字符串」身份轴之外，平行提供一套完整的**权限节点**模型（对齐 Minecraft LuckPerms v5）。两者**并存**：既有命令仍可沿用 `require_level`，新功能推荐改用 `require_perm` 权限节点，互不冲突。

### 核心概念

- **节点 node**：`plugin.action.sub` 形式的权限字符串，三态（授予 / 显式否决 / 未定义）
- **组 group**：一组节点的集合，带 `weight`（权重决定优先级与 primary group）
- **继承**：组通过 `group.xxx` 节点继承另一个组 —— 继承与权限统一用节点表达
- **上下文 context**：节点可限定只在特定环境生效
  - `group=<群号>` / `bot=<OneBot实例名>` / `msgtype=group|private`
  - 上下文为 NULL 表示全局生效
- **临时**：节点可带 `expire_at`（unix 时间戳），过期自动失效（框架每小时清理）
- **否决**：`value=0` 的同名节点优先于 `value=1`
- **通配符**：`a.b.*` 匹配 `a.b.c`；`*` 匹配一切

### 内置角色组（运行时虚拟注入，不入库）

框架原有的 `super / owner / admin / member` 四层身份，被映射为一条内置继承链：

```
__member(w0) ← __admin(w20) ← __owner(w30) ← __super(w100)
```

每个内置组自带 `zcbot.role.{member|admin|owner|super}` 节点，因此：

```
require_level='admin'  ≡  检查节点 zcbot.role.admin
```

语义与原 `Event.is_admin` 完全一致，但 `Event.role` 本身无需改动。

### 在网页后台管理

后台新增「**权限管理**」页面（侧边栏钥匙图标 → `/permissions`），包含 5 个标签页：

| 标签页 | 功能 |
| ------ | ---- |
| 权限组 | 创建/编辑/删除权限组，设置权重、前缀、默认组；查看每个组的节点 |
| 用户 | 给用户直接授予/撤销节点、加入/移出权限组（支持上下文与过期时间） |
| 轨道 | 配置 Tracks（如 `default,vip,admin`），用于一键晋升/降级 |
| 校验器 | 输入 QQ 号 + 上下文，实时查看其生效组与节点解析结果 |
| 审计 | 查看所有权限变更的审计日志 |

> 所有变更都会写入 `perm_audit` 审计表，可在「审计」标签页回溯。

### 节点命名约定（推荐）

| 前缀 | 含义 | 示例 |
| ---- | ---- | ---- |
| `插件名.动作.子项` | 插件自定义权限 | `myplugin.ban`、`myplugin.config.set` |
| `zcbot.role.*` | 框架内置身份 | `zcbot.role.admin` |
| `zcbot.wf.*` | 工作流权限（下游项目用） | `zcbot.wf.mc_command` |

默认权限组带 `zcbot.wf.*` 通配，新工作流默认对所有用户开放；如需收紧，移除该通配并改用显式节点。

---

## 八、接口令牌（API Key）

框架原先只有「用户登录会话 token」（2048 字符，存于 `admin_users`，随登录/登出轮换、受 `web.session_timeout` 时效限制）。这导致**外部程序长期调用 REST API 时 token 会过期失效**。

为此新增独立的**接口令牌（API Key）**系统，专供脚本/第三方服务稳定调用：

- 令牌长度 64 字符（`secrets.token_hex(32)`），独立于用户会话，**不随登录轮换**
- 存于 `api_tokens` 表，可设置**绝对过期时间**（也可永不过期）
- 支持**吊销**（软删除：`is_active=0`），吊销立即生效
- 创建后 **token 仅返回一次**，请妥善保存
- 仅 `super` 角色可创建/吊销

### 在网页后台创建

后台新增「**接口令牌**」页面（侧边栏钥匙图标 → `/apikeys`）：填写名称、选择角色（admin/super）、可选过期时长，点击创建后 token 明文显示一次，复制保存即可；列表可随时吊销。

### 调用方式

在 HTTP 请求头里带 `Authorization: Bearer <token>`（与登录 token 用法完全一致，`_verify_token` 同时兼容两类令牌）：

```bash
curl -H "Authorization: Bearer <你的API_KEY>" \
     http://127.0.0.1:8081/api/perm/groups
```

> 注意：Web 后台页面走 `web.port`（默认 8080），而 REST API 走 **8081** 端口（见 `docs/API.md`）。

---

## 九、开发者指南（开发文档）

### 9.1 项目结构

```
.
├── main.py                 # 启动入口
├── config.yaml             # 运行配置（首次启动自动生成）
├── framework/              # 框架核心
│   ├── core.py             # 框架主体、定时任务调度
│   ├── db.py               # 数据库抽象（SQLite / MySQL55 双方言自动建表）
│   ├── event.py            # Event 事件对象（含权限查询方法）
│   ├── ctx.py              # PluginContext（插件可用的全部能力）
│   ├── router.py           # 命令路由（支持 require_perm / require_level）
│   ├── loader.py           # 插件加载器
│   ├── apis.py             # Web 后台 + REST API（含权限/接口令牌接口）
│   └── perm.py             # 权限引擎（LuckPerms 风格，无第三方依赖）
├── sql/                    # 建表 SQL（init.sql / init_mysql55.sql）
├── plugins/                # 插件代码（可 GitHub 覆盖更新）
├── webui/                  # 前端源码（Vue 3 + Vite + Element Plus）
├── web/                    # 前端构建产物（由 webui/ 构建而来）
├── data/                   # 运行数据（db、日志、插件数据）
└── docs/                   # 文档
```

### 9.2 权限系统开发接口

**A. 在事件（Event）里判断**（handler 内最常用）：

| 方法 | 返回 | 说明 |
| ---- | ---- | ---- |
| `ev.has_perm(node)` | `bool` | 是否拥有节点（未定义按拒绝） |
| `ev.check_perm(node)` | `True/False/None` | 三态：授予 / 显式否决 / 未定义 |
| `ev.perms` | `PermissionSet` | 完整权限快照（`.groups` / `.nodes` / `.primary_group`） |
| `ev.perm_groups` | `list` | 生效的权限组（含继承展开，按 weight 降序） |
| `ev.primary_group` | `str` | 权重最高的非内置权限组 |

上下文自动从事件构造（`bot` / `group` / `msgtype`），无需手动传。

**B. 在任意位置用 `ctx` 判断**（非事件上下文，如定时任务）：

```python
ctx.has_perm(user_id, "myplugin.ban",
             context={"group": "123456", "msgtype": "group"}, role=ev_role)
ctx.check_perm(user_id, "myplugin.ban", ...)   # 三态
ctx.user_groups(user_id, context=..., role=...) # 生效组列表
```

底层全部走 `framework.perm` 模块（`resolve` / `has_perm` / `check_perm` / `user_groups` / `audit` / `create_group` / `set_group_node` / `add_user_group` / `promote` / `demote` / `cleanup_expired` 等），可直接调用实现自定义逻辑。

**C. 命令级声明**（推荐，框架自动拦截）：

```python
# 与 require_level 双轨并存：两者任一满足即放行
@ctx.command("ban", require_perm="myplugin.ban", require_level="admin")
def ban(ev): ...
```

- `require_perm` 缺省继承框架 `commands` 表里的配置（由后台「命令管理」维护）
- 解析优先级：① 用户直接节点 → ② 所属组（按 weight 降序）→ ③ 同来源内按精确度（精确 > 段级通配 > 全局 `*`）→ ④ 同精确度下否决优先

**D. 缓存**

权限解析结果有 60s TTL 缓存（`_PERM_CACHE_TTL`）；组快照缓存 10s。Web 端改动后框架自动调用 `invalidate_all()`，无需手动处理。

### 9.3 接口令牌（API Key）开发接口

`_verify_token(token)` 同时兼容两类令牌：

1. **用户会话 token**：长度 `== 2048`，查 `admin_users`，受 `session_timeout` 限制
2. **接口令牌**：长度 `>= 40`，查 `api_tokens`，独立有效，不受登录轮换影响

所以任何已有登录校验的接口，**无需改动即可直接用 API Key 调用**。

相关 REST 端点（均需 `super` 角色，除查询类由鉴权中间件统一处理）：

| 方法 & 路径 | 说明 |
| ---- | ---- |
| `GET /api/apikeys` | 列出全部接口令牌（不返回 token 明文） |
| `POST /api/apikeys` | 创建令牌（参数 `name`、`role`、`expires_in` 秒；token 仅此一次返回） |
| `POST /api/apikeys/<id>/revoke` | 吊销令牌（软删除） |

### 9.4 REST API 速查（权限相关）

| 方法 & 路径 | 说明 |
| ---- | ---- |
| `GET /api/perm/builtins` | 内置角色组定义 |
| `GET /api/perm/groups` / `POST /api/perm/groups` | 列出 / 创建权限组 |
| `PUT /api/perm/groups/<name>` / `DELETE /api/perm/groups/<name>` | 更新 / 删除权限组 |
| `GET /api/perm/groups/<name>/nodes` | 列出组节点 |
| `POST /api/perm/groups/<name>/nodes` / `DELETE ...` | 设置 / 删除组节点（支持 context、expire_at） |
| `GET /api/perm/users/<id>` | 用户权限快照 |
| `POST /api/perm/users/<id>/nodes` / `DELETE ...` | 设置 / 删除用户节点 |
| `POST /api/perm/users/<id>/groups` / `DELETE ...` | 加入 / 移出用户组 |
| `POST /api/perm/users/<id>/track` | 沿轨道晋升/降级 |
| `GET /api/perm/tracks` / `POST /api/perm/tracks` / `DELETE /api/perm/tracks/<name>` | 轨道管理 |
| `POST /api/perm/check` | 校验某用户+上下文的节点 |
| `GET /api/perm/audit` | 审计日志 |
| `POST /api/perm/cleanup` | 手动触发过期节点清理 |

> 完整请求/响应字段见 [📖 Web API 接口文档](docs/API.md)。

### 9.5 前端构建

后台前端源码在 `webui/`，构建产物输出到 `web/`。修改前端后需重新构建：

```bash
cd webui
npm install          # 首次需安装依赖
npm run build        # 产物输出到 ../web/
```

> ⚠️ 构建会清空 `web/` 下的旧产物再写入。若运行环境对批量删除有安全限制，请先手动清理 `web/{js,css,img,index.html}` 再构建，或使用允许该操作的执行方式。

数据库表结构见 `sql/init.sql`（SQLite）与 `sql/init_mysql55.sql`（MySQL 5.5 兼容），框架启动时会自动建表并补齐缺失表（`_auto_create_tables`）。

---

## 十、进阶文档

遇到看不懂的词，文档里都有解释。按下面的顺序读最顺：

- [📚 文档索引](docs/INDEX.md) — 所有文档的总目录
- [插件开发详解](docs/plugin-tutorial.md) — 完整示例插件，逐行讲透每个语法（新手推荐）
- [快速入门](docs/getting-started.md) — 手把手写第一个插件
- [插件目录结构](docs/plugin-structure.md) — 代码和数据怎么放
- [API 参考](docs/api-reference.md) — 写插件时的全部接口
- [配置系统](docs/configuration.md) — 插件的设置项怎么写
- [示例合集](docs/examples.md) — 一个完整的签到插件源码
- [官方插件使用手册](docs/official-plugins.md) — 官方插件仓库每个插件的命令与用法例子
- [Web API 接口文档](docs/API.md) — 后台后端 HTTP API 完整定义
- [调试指南](docs/debugging.md) — 插件出 bug 了？看日志、开 DEBUG、打断点、热重载
- [最佳实践](docs/best-practices.md) — 写插件的好习惯 + 提交前自查清单
- [已知问题](docs/KNOWN_ISSUES.md) — 框架已知的坑和修复进度（P0/P1/P2）

---

## 📜 开源协议

MIT + Apache 2.0 双协议，任选其一适用。

> 本项目代码由 AI 完成为主、人工辅助完成。用着顺手的话，给个 ⭐ 吧！
