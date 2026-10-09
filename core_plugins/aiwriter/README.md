# 终端 AI 智能体（aiwriter）

官方插件（`process: core`）。**一个专门给 ZCBOT 写插件的 AI 智能体**：描述你要的插件，
它读仓库、写 `plugins/<名>/main.py`、跑命令、按框架 API 落地；也能做常规编码与运维。

工作区缺省就是**框架根目录**（直接在项目里干活），所以它能直接读 `framework/`、
`docs/`、现有插件源码来照着写。临时文件统一丢 `data/tmp/`。

界面用 **[Textual](https://github.com/Textualize/textual)（MIT）** 承载——成熟的 TUI 框架负责
输入 / 渲染 / resize / 键位，不再手搓 ANSI 重绘与按键解析。agent 自带最小 LLM 客户端
（OpenAI 兼容流式），**不依赖 `llm_core`**。

## 元信息

- 优先级：50
- 进程归属：**core**（bot 内 `code` 命令在核心进程）
- 默认开关：**禁用**（`core_plugins.yaml` 中 `aiwriter.enabled` 置 `true` 后，bot 内才有 `code` 命令）
- 独立 CLI：`python main.py code` **无需启用插件**即可使用（直接引导启动，不加载 bot）
- 外部依赖：**`textual`**（见 `requirements.txt`；缺了会提示 `pip install textual`）

## 用法

**两种入口，同一个全屏界面：**

```bash
# ① 独立 CLI：命令行直接启动，不启动 bot
python main.py code

# ② bot 运行中：在运维终端里输入（会以独立进程拉起界面，退出后回到 bot 终端）
code
```

界面内直接描述需求即可（见下）。不带子命令时 `python main.py` 仍按原样启动 bot。

> 界面在**独立进程**里跑（界面与后端分离），彻底避开在 bot 线程里
> 跑全屏 TUI 的坑。

例（在界面输入框里）：

```
给工作区写一个 hello.py，打印 1 到 5
给 hello.py 加个 main 守卫
看看工作区里有哪些 py 文件
```

## 界面（`code`）

```
 ZCBOT code  build · deepseek-chat · workspace              ┌ 人格 ─────────────┐
 ───────────────────────────────────────────────────────── │ ● build  通用编码/运维 │
 ZCBOT code · AI 智能体                                     │ 状态               │
 直接描述你要做的事，回车交给智能体…                          │ 模型  deepseek-chat │
 > 给工作区写一个 hello.py，打印 1 到 5                      │ 模式  workspace     │
 我来写这个文件。                                           │ 工作区 …/workspace  │
 [tool] write({"path": "hello.py", ...})                    │ 会话  2 轮 · 共 3 个 │
   -> ok: 已写入 hello.py（32 字节）                         │ 状态  就绪          │
 已创建 hello.py，用 for 循环打印 1 到 5。                    └────────────────────┘
 ─────────────────────────────────────────────────────────
 > 描述你的需求，回车交给智能体…（/help 看命令）
 ctrl+c 退出   tab 切模式   ctrl+l 清屏   ctrl+s 设置   ctrl+g 人格
```

右侧边栏：**人格列表**（`Tab` 聚焦、`↑↓` 选择、`Enter` 切换）+ **运行状态**
（模型 / 模式 / 工作区 / 当前会话与轮数 / 就绪·执行中）。

界面内的斜杠命令：

| 命令 | 作用 |
|---|---|
| `/agent` | 人格：`/agent` 列出 · `<名>` 切换 · **`new [名]` 弹窗新建** · **`edit [名]` 弹窗编辑** · `prompt <名> <文本>` 快速设提示 · `del <名>` 删除 |
| `/sessions` | **会话管理弹窗**：列举旧会话，`Enter` 进入 · `n` 新建 · `r` 重命名 · `c` 配置（覆盖模型/模式）· `Del` 删除（`Ctrl+N` 同效） |
| `/session <子命令>` | 命令行版：`new [名]` · `<名>` 进入 · `del <名>` · `rename <名> <新名>` · `model <名> <模型>` · `mode <名> <模式>` |
| `/new` | 清空**当前会话**的消息（会话本身与历史归档保留） |
| `/memory` | 长期记忆：`/memory` 查看 · `add <文本>` 追加 · `clear` 清空（跨会话注入系统提示） |
| `/history [N]` | 回看历史归档（每轮对话写入 `history.jsonl`） |
| `/settings` | 设置弹窗：人格 / 模型 / 温度 / 上下文 / 超时 / 工作区 / 接口 / Key（保存即写回 `core_plugins.yaml`） |
| `/tools [on\|off]` | 是否显示工具调用（防刷屏） |
| `/detail [on\|off]` | 是否展开工具参数 / 完整结果 / 思考全文（同 `Ctrl+T`） |
| `/think [on\|off]` | 是否请求深度思考（模型/网关需支持，如 `deepseek-reasoner`；字段按 `thinking_style` 注入） |
| `/model <名>` | 切换模型 |
| `/mode <模式>` | 切换 `readonly` / `workspace` / `full` |
| `/plugins` | 列出已加载插件 |
| `/plugin <名>` | 在工作区生成一个插件骨架（`plugins/<名>/main.py` + `README.md`） |
| `/help` | 帮助 |
| `/exit` | 退出界面 |

键位（由 Textual 提供）：Enter 发送 · ↑↓/PgUp/PgDn 滚动对话 · **Tab 切模式** ·
Ctrl+C 退出 · Ctrl+L 清屏 · **Ctrl+S 设置** · **Ctrl+G 聚焦人格**。
（F1/F2/F3 仅作别名、不在底栏展示——部分 Windows 控制台会抢 F 键，例如
conhost 的 F3 = 重复上一条命令。）设置弹窗内 Enter 保存、Esc 取消。

**模式（Tab 循环）**：`readonly`（只读）→ `workspace`（可写）→ `full`（可执行）。
切到 `full` 会自动打开 `allow_exec`（放开 shell / python / 写库 / 插件管理），
离开时关掉。当前模式在顶栏、右侧栏与输入框占位符里都能看到。


## 工具集

| 工具 | 能力 | 说明 |
|---|---|---|
| `read` | read | 读文件（带行号） |
| `list` | read | 列目录（最多两层） |
| `glob` | read | 按 glob 匹配文件路径 |
| `grep` | read | 按正则搜内容 |
| `webfetch` | read | 抓取 http/https 链接（HTML 自动转文本） |
| `websearch` | read | 联网搜索（默认免 key 的 Bing，配 `FIRECRAWL_API_KEY` 则走 Firecrawl） |
| `db_query` | read | 只读查询 ZCBOT 数据库（仅 SELECT/PRAGMA/WITH/EXPLAIN/SHOW/DESC） |
| `plugin_list` | read | 列出已加载插件 |
| `write` | write | 创建 / 整体重写文件 |
| `edit` | write | 定点替换 `oldString → newString`（默认须唯一命中，可 `replaceAll`） |
| `remember` | write | 把一条事实记入长期记忆（跨会话注入系统提示） |
| `plugin_new` | write | 生成 ZCBOT 插件骨架 `plugins/<名>/`（main.py + README.md） |
| `shell` | exec | 执行 shell 命令 |
| `python` | exec | 执行一段 Python 代码（子进程） |
| `db_execute` | exec | 对 ZCBOT 数据库执行写语句 |
| `plugin_action` | exec | 管理插件：`enable` / `disable` / `reload` |

**权限闸门**：工具按「能力 × 模式」放行，`specs()` 只暴露当前允许的工具，
调用前再强制校验一次——模型「点名」越权工具也直接拒绝。

| `mode` | 允许 |
|---|---|
| `readonly` | read / list / glob / grep / webfetch / websearch / db_query / plugin_list |
| `workspace`（默认） | + write / edit / remember / plugin_new |
| `full` + `allow_exec: true` | + shell / python / db_execute / plugin_action |

界面里按 **Tab** 在 `readonly → workspace → full` 间循环（切到 full 自动打开 `allow_exec`，
离开时关闭），底部状态栏与侧栏会同步。

所有文件路径以工作区根为边界，越界一律拒绝。数据库 / 插件类工具在独立 CLI
（无框架宿主）下会明确提示不可用。

## 人格（agent）

可以定义多个人格，各自带一段附加系统提示（可选覆盖 `model` / `mode`）。
默认内置一个 `build`（ZCBOT 插件开发）。右侧边栏列出全部人格，`Ctrl+G` 聚焦、`↑↓` 选择、
`Enter` 切换；也可用命令：

```
/agent                     列出人格
/agent <名>                切换
/agent new                 弹窗新建（填 名称 / 描述 / 附加提示 / 覆盖模型 / 覆盖模式）
/agent edit [名]           弹窗编辑
/agent prompt <名> <文本>   快速设附加提示
/agent add <名>            命令行快速新建（空提示）
/agent del <名>            删除
```

`F4` 也可直接打开「编辑当前人格」弹窗。人格存在 `core_plugins.yaml` 的 `aiwriter.agents`，
`aiwriter.agent` 记当前人格。

## 显示与深度思考（防刷屏）

默认「快速模式」，但工具调用与思考过程都做了收敛，避免刷屏：

- **工具调用**显示成一行（`⚙ write` / `✔`），默认**不展开参数与结果**；`/detail on`（或 `Ctrl+T`）才展开。
- **深度思考**（reasoning）默认**折叠成一行摘要**（`💭 思考：…（共 N 字）`），`/detail on` 显示全文，`/tools off` 可整体隐藏工具行。
- 请求层的深度思考用 **`/think on`**（或设置里 `thinking`）打开；不同厂商字段不一，
  用 `thinking_style` 选择注入方式（`auto` 同时尝试 `thinking` / `enable_thinking` / `reasoning_effort`，
  或指定 `anthropic` / `flag` / `openai`）。配合 `deepseek-reasoner` 这类模型即可。

相关配置：`show_tools` / `show_thinking` / `tool_detail` / `thinking` / `thinking_style`。

## 配置（`core_plugins.yaml` 段 `aiwriter`，或环境变量）

| 键 | 说明 | 默认 |
|---|---|---|
| `enabled` | 是否启用 | false |
| `base_url` | OpenAI 兼容端点（不带 `/chat/completions`） | `https://api.deepseek.com/v1` |
| `api_key` | API Key；留空则读环境变量 `AIWRITER_API_KEY` | `''` |
| `model` | 模型名 | `deepseek-chat` |
| `temperature` | 采样温度 | 0.7 |
| `max_rounds` | 工具调用最大轮数 | 12 |
| `timeout` | 单次请求超时（秒） | 120 |
| `mode` | `readonly` / `workspace` / `full` | `workspace` |
| `allow_exec` | `full` 模式下是否开放 shell / python / DB 写 / 插件管理 | false |
| `workspace` | 工作区根目录；**留空 = 框架根目录**（直接在项目里干活） | `''` |
| `agent` | 当前人格名 | `build` |
| `agents` | 人格列表 `[{name, desc, prompt, model?, mode?}]` | `[]`（用内置 build） |
| `keep_turns` | 压缩时保留的最近轮数 | 20 |
| `max_history_chars` | 历史超此长度触发压缩 | 24000 |
| `session_hard_cap` | 会话历史硬上限（条，0 = 按 keep_turns×20 推导） | 400 |
| `persist_session` | **跨启动保留会话**：关掉后每次启动都是全新会话 | true |
| `thinking` | 是否请求深度思考（模型/网关需支持） | false |
| `thinking_style` | 思考字段注入方式：`auto` / `anthropic` / `flag` / `openai` | auto |
| `show_thinking` | 界面是否显示思考过程（默认折叠成一行摘要） | true |
| `show_tools` | 界面是否显示工具调用 | true |
| `tool_detail` | 是否展开工具参数 / 完整结果 / 思考全文 | false |
| `max_output` | 单次工具输出截断 | 8000 |
| `shell_timeout` | shell 超时（秒） | 60 |
| `python_timeout` | python 工具超时（秒） | 60 |
| `net_timeout` | webfetch / websearch 超时（秒） | 20 |
| `session_key` | 当前会话键（= 会话名，可在 `/sessions` 里切换） | `terminal` |

## 结构

| 文件 | 角色 |
|---|---|
| `llm.py` | provider：OpenAI 兼容流式客户端 |
| `prompt.py` | 系统提示词（harness / communication / codebase 三节） |
| `tools.py` | 工作区工具集 + 权限 |
| `agent.py` | 工具调用闭环（流式事件） |
| `session.py` | 会话存储 + 上下文压缩（compaction） |
| `tui.py` | 全屏界面（Textual）：对话流 + 输入框 + 设置弹窗 + 键位 |
| `main.py` | 插件入口：注册终端命令 `code`（独立进程拉起界面）、配置持久化、插件脚手架 |

## 数据

都在 `data/plugins_dat/core_aiwriter/`（工作区除外）：

- `sessions.json` —— **多会话**（v2 格式：`{version, sessions:{key:{name,created,updated,model,mode,messages}}}`；
  旧格式自动迁移）。可列举 / 进入 / 新建 / 重命名 / 配置 / 删除。
- `memory.md` —— **长期记忆**（`/memory add` 或 `remember` 工具写入；每轮注入系统提示）
- `history.jsonl` —— **历史归档**（每轮对话追加一条，`/history` 回看；`/new` 不影响它）
- 工作区：缺省是**框架根目录**（可用 `workspace` 配置改）
- 临时文件：`data/tmp/`

## 数据库

- **bot 内**（`code` 命令 / 调试控制台）：用框架自身数据库，`db_query` 读、`db_execute` 写。
- **独立 CLI**（`python main.py code`）：**只读**打开 `config.yaml` 里配置的库
  （SQLite `mode=ro`），所以 `db_query` 可用、`db_execute` 会被拒（避免与运行中的 bot 争锁）。
  插件管理在独立模式下只给指引，不代管。

## 写 ZCBOT 插件

系统提示里会自动注入仓库的插件开发要点（`docs/llm-plugins.md` + `LLM.md`），
所以它知道 `__plugin_meta__`、`ctx.command/on/hook/register_api`、`ctx.get_data_dir()`
这些约定。典型用法：

```
帮我写个插件：群里发 /天气 城市，返回该城市的天气
```

它会用 `plugin_new` 生成骨架 → 读现有插件照着写 → 用 `write/edit` 落实现 →
提示你重载。也可直接 `/plugin <名>` 生成空骨架自己填。

## 安全

- 所有文件路径以工作区根为边界，越界拒绝。
- `mode=workspace` 不允许 `shell`；`shell` 仅在 `full` + `allow_exec` 时开放。
- 这是软约束，不是沙箱；跑不可信输入请另开容器。
