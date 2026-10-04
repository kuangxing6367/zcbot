# 接入大模型（LLM）

群里有人问「今天 scrapy 群里谁在」，管理员想问一句「把最近一周签到榜发我」——这些都不是写死的命令能覆盖的，你得让模型来**理解意图**，然后**调用你的函数**去执行。这篇讲的是 ZCBOT 里怎么把这件事跑起来，以及「函数」这个概念到底指什么。

动手前请先按 [安装](installation.md) 把框架跑起来，并且至少有一个能收发消息的[接入端](connect-im.md)。

## 先搞清两层：装载器和对话核心

ZCBOT 的 LLM 能力**不是一个插件**，是两层：

| 位置 | 角色 | 你要不要动它 |
| ---- | ---- | ---- |
| `core_plugins/llm_load` | 官方装载器。保证 `plugins/llm_core` 存在且逐字节正确 | 一般不用 |
| `plugins/llm_core` | 对话主体：提供商总线、函数总线、工具调用闭环、会话窗口 | 运行时产物，**别手改** |
| `core_plugins/llm_load/src/llm_core/` | 上面的源码真源 | 升级在这里改 |

为什么要绕这一圈：LLM 这套东西变化太快——新模型、新 MCP server、新工具，不可能每次都等框架发版。放在 `plugins/` 方便随时演进，但要有人保证它不会被人手改坏或者误删。`llm_load` 就是那个保证：

```
启动时读 llm_core.zip 里的 manifest（版本号 + 每个文件的 md5）
        │
        ├── 与 plugins/llm_core 逐文件比对，一致 → 放行
        └── 缺失 / 不一致 → 解压覆盖 → 再校验 → 一致则立刻重启框架
```

释放完会**再校验一遍**，只有真的对齐了才重启——所以不存在重启死循环。manifest 没收录的文件（你自己的数据文件）不会被删。

启用：

```bash
python tools/scan_core_plugins.py --enable llm_load
```

## 三个概念

整件事只有三个新概念，其余都是工程细节：

| 概念 | 一句话 | 类比 |
| ---- | ---- | ---- |
| **模型提供商 provider** | 把一段对话历史换成模型回复的一段代码 | 嘴 |
| **函数 function / tool** | 给模型用的一段可执行能力 | 手 |
| **Agent 循环** | 「让模型说话 → 它点名要用哪只手 → 我们执行 → 把结果告诉它 → 再说」的循环 | 做事的过程 |

provider 只负责**说话**：喂进去历史和工具列表，吐出来文本或一串工具调用指令。它不知道自己身处哪个群、也不知道你有什么工具实现。**这种刻意的无知**换来可插拔性——换模型厂商不用改一行业务代码。

## 第一步：配一个模型提供商

在 Web 面板「插件 → llm_core → 配置」里填 `providers`（JSON 数组）：

```json
[
  {
    "id": "deepseek",
    "type": "openai",
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "sk-xxxx",
    "model": "deepseek-chat",
    "default": true
  }
]
```

密钥不入仓库的第二条路：`api_key` 留空，用环境变量 `LLM_CORE_API_KEY`。

内置的 OpenAI 兼容 provider 覆盖 OpenAI 官方、DeepSeek、Moonshot、智谱部分版本、vLLM、Ollama、One-API / New-API 这类聚合网关——判断标准只有一条：能不能用 `Bearer` + `{"messages": [...]}` 说话。能，就只差一个 `base_url`。

群里试试：

```
/llm 你好              # 对话
/llmstatus            # 看提供商 / 工具 / 会话概况
/llmreset             # 清空当前会话
```

临时换模型：`/llm @qwen2.5 写一首关于夏天的小诗`。

## 第二步：给模型一只手（函数）

这是整篇的重点。**函数 = 名字 + 描述 + 参数 JSON Schema + 执行体**：

```python
# plugins/weather/main.py
def register(ctx):
    svc = ctx._framework.services.get('llm_core')
    if svc is None:
        ctx.log("llm_core 未加载，跳过工具注册", 'warning')
        return

    @svc.tool(description="查询某个城市今天的天气，返回温度和天气状况")
    def get_weather(location: str):
        """查询城市天气。"""
        return f"{location}: 26 度，晴"
```

就这样。用户在群里问「上海今天热吗」，模型会自己决定调用 `get_weather`，拿到结果后再组织成人话回答。你**不需要写任何触发词**。

### 规则一：描述是写给模型看的，不是注释

```python
# 正确：说清「什么时候用」和「能拿到什么」
@svc.tool(description="查询某个城市今天的天气，返回温度和天气状况")

# 错误：模型不知道该拿它干嘛，结果就是永远不会被调用
@svc.tool(description="查天气")
```

模型看不见你的实现，只看得到名字和描述。描述含糊 = 调用率为零。

### 规则二：参数 JSON Schema 是双向契约

```python
@svc.tool(
    description="给指定用户加积分，返回加分后的总积分",
    parameters={
        "type": "object",
        "properties": {
            "uid":    {"type": "integer", "description": "用户 QQ 号"},
            "score":  {"type": "integer", "description": "加多少分，可为负数"},
            "reason": {"type": "string",  "description": "加分理由，会写进日志"}
        },
        "required": ["uid", "score"]
    },
    require_perm="sign.admin",     # 需要权限节点才允许调用
    timeout=10)                    # 单次执行超时（秒）
def add_score(uid: int, score: int, reason: str = ''):
    """给用户加积分。"""
    ...
```

不写 `parameters` 时按类型注解自动推导：`str→string`、`int→integer`、`float→number`、
`bool→boolean`、`dict→object`、`list→array`；**带默认值的参数不算必填**；
名为 `event` 的参数会被跳过（那是框架注入的，不该由模型来填）。
推导结果能看图——`/llmtools` 里会把每个工具的必填参数列出来。

参数不对时的处理很关键：

```python
# 模型传了 {"uid": "12345"}（字符串而不是整数）
# 我们不会硬着头皮执行，而是返回一条工具结果：
[工具 add_score] 调用失败：参数 uid 类型不符：期望 integer，实际 str
```

这条错误会**回灌给模型**，让它自己改正再调一次。这就是工具调用比硬编码流程强的地方——模型会自纠。缺参、多传参数、类型不符都是这个处理路径。

### 规则三：返回值决定它进不进上下文

```python
@svc.tool(description="生成一张签到排行榜图片并直接发到群里")
def render_rank_image(top: int = 10):
    """生成签到排行榜图片。"""
    # 自己把图片发出去了
    ctx.send_msg(...)
    return None          # 约定：None = 不进上下文
```

| 返回 | 后果 |
| ---- | ---- |
| 字符串 | 进入下一轮上下文，模型据此生成最终回复（**绝大多数情况**） |
| `None` | 不进上下文。适合工具自己已经把结果发出去的场景（图片、长消息、表情） |
| 抛异常 | 被转成 `[工具 xxx] 执行异常：...` 回灌给模型，会话不会断 |

### 工具也可以用 `event` 拿到上下文

handler 里声明 `event` 参数即可，框架会自动注入当前事件：

```python
@svc.tool(description="把当前群的人数告诉模型")
def group_member_count(event=None):
    """查当前群人数。"""
    if event is None or not event.is_group:
        return "当前不在群聊里"
    ...
```

### 热开关

```
/llmtools                 列出全部工具与状态
/llmtool get_weather off  停用（管理员）
```

Web 面板「LLM 对话」页同样可以逐个开关。停用的工具**不会出现在给模型的工具声明里**——模型看不见它，自然也不会调。

## 第三步：理解工具调用闭环（多轮）

用户问「上海今天比北京热多少」，一次请求是不够的：

```
用户：上海今天比北京热多少
  │
  ├─ 第 1 轮  模型 → 我这没有实时天气，得调工具
  │           返回 tool_calls: [get_weather(上海), get_weather(北京)]
  │           我们把「谁调了什么」记进历史
  │
  ├─ 执行     本地跑两个工具 → 得到 "上海: 26度" "北京: 21度"
  │           把结果以 tool 消息塞回历史
  │
  └─ 第 2 轮  模型看到结果 → "上海 26 度，北京 21 度，上海更热 5 度。"
              没有 tool_calls → 循环结束，回复用户
```

两个必须做对的细节，llm_core 已经处理掉了，但你要知道：

- **中间状态必须进历史。** 模型得看得见自己刚才调了什么、结果是什么，否则下一轮它会重复调用同一个工具。
- **到达轮数上限要收口。** 配置项 `max_turns`（默认 6）用尽后，会去掉工具列表并明确提示「不要再造工具了」再问一次；如果模型还在调工具，就把已经拿到的工具结果整理好直接回答，**而不是甩一句「处理失败」**。

## 会话与上下文窗口

每个「接入端 + 群/私聊 + 用户」一个桶，互不串线。模型收到的是完整历史，所以历史长度直接等于钱：

| 配置项 | 默认 | 作用 |
| ---- | ---- | ---- |
| `history_turns` | 20 | 保留的用户轮数，超出按轮砍掉最老的 |
| `max_tokens` | 1024 | 单轮回复上限 |
| `temperature` | 0.7 | 越低越稳。工具调用多的场景建议 0.2–0.6 |
| `session_capacity` | 500 | 会话 LRU 上限，防群聊场景内存无限涨 |

窗口治理的第一道闸就是 `history_turns`。调小它是控制成本最直接的一招。

::: tip 工具调用时代的费用常识
一次「看起来普通」的问题可能走 2–3 轮、`max_turns` 拉满时 6 轮以上，因为每一轮都带着完整历史。**先把 `history_turns` 和风险工具的 `timeout` 定好**，再考虑别的优化。
:::

## 图片（多模态）

群里的命令和事件层拿到的是**被剥干净的文本**，图片只有原始消息里有完整消息段数组。llm_core 用原始消息处理器先截一手，把图片暂存 300 秒，等你真正发 `/llm` 时再拼成视觉输入：

```
/llm 看看这张图有什么问题
（附图）
```

- 本地图片转 base64 data URI（单张上限 5MB），远端 URL 直接透传；
- 模型收不收得了由 provider 的 `supports_vision` 说了算，**不支持就干脆不带图**；
- 不想花这份钱就把 `vision_enabled` 关掉。

## MCP：另一路工具来源

MCP server 上挂的工具会被自动拉进函数总线，`source` 标为 `mcp`——**对模型而言和插件注册的工具完全一样**，都是「名字 + 描述 + 参数 schema」。差别只在治理：一台 server 拉不起来不影响其它部分可用。

```json
[
  {"name": "fs", "transport": "stdio",
   "command": "npx",
   "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]},
  {"name": "remote", "transport": "http", "url": "https://example.com/mcp"}
]
```

## 自己写一个模型提供商

要说原生协议（比如 Anthropic 的 messages 格式），写个插件注册进来即可，不需要改 llm_core 一行代码：

```python
# plugins/llm_provider_mine/main.py
from plugin_llm_core.providers import Provider, ProviderReply

class MyProvider(Provider):
    id = "mine"
    model = "my-model"
    supports_tools = True
    supports_vision = False

    def chat(self, messages, tools=None, temperature=0.7, max_tokens=1024):
        # 自己去调厂商接口
        text = call_my_api(messages, tools)
        return ProviderReply(content=text)

def register(ctx):
    svc = ctx._framework.services.get('llm_core')
    if svc:
        svc.register_provider(MyProvider(api_key="..."))
```

依赖加载顺序：订阅 `system.plugin.loaded` 事件等 `llm_core` 起来后再注册，避免拿不到服务。

## 升级 llm_core

改源码流程：

```bash
# 1. 改 core_plugins/llm_load/src/llm_core/ 下的文件
# 2. 重新打包（释放按每个文件的 md5 比对触发，内容变了即生效，不必手动加版本号）
python tools/build_llm_payload.py --write
python tools/build_llm_payload.py --check       # 看现网是否已对齐
# 3. 重启框架 → llm_load 自动释放新载荷并再重启一次
```

`/llmload status` / `verify` / `reinstall` 三个命令可以查看和修复装载状态。

## 排错速查

| 现象 | 原因与处理 |
| ---- | ---- |
| `/llm` 说「没有可用的模型提供商」 | 面板里没填 `providers`，或 JSON 写错了（面板配置项是字符串，注意引号） |
| 报 404 | `base_url` 没以 `/v1` 结尾，或厂商前缀不同 |
| 报 401 | Key 填错或过期；也可改用环境变量 `LLM_CORE_API_KEY` |
| 报「tool call is not supported」 | 模型不支持函数调用。给该 provider 加 `"supports_tools": false`，或直接换模型 |
| 模型从来不调我的工具 | 描述写得太含糊；或用 `/llmtools` 确认它没被停用 |
| 工具报「参数 x 类型不符」 | 参数 schema 里类型声明与实际不一致，改 `parameters` 或放宽类型后往业务代码里转换 |
| 一次对话烧了很多钱 | 把 `history_turns` 调小、`max_turns` 调小、`temperature` 压低 |
| 改了目录里的 llm_core 不生效 | 那是运行时副本，会被 manifest 抹平。改 `src/llm_core/` 并 `--write` 重新打包 |
| 群里刷屏 | 别开 `free_chat`；它是「每条普通消息都进模型」的开关，默认关闭 |
