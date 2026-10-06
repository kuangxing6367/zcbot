# llm_core —— LLM 对话核心

由官方装载器 `core_plugins/llm_load` 释放到 `plugins/` 下的运行时插件。
**不要直接改运行时目录里的副本**（它会被下一次自检按 manifest 抹平），
要改请改源码 `core_plugins/llm_load/src/llm_core/` 再重新打包，见文末「改源码的正确姿势」。

它负责整条对话链路：

```
原始消息 ──▶ media（抠图）
文本 + 图片 ──▶ history（会话窗口）──▶ agent（工具闭环）──▶ providers（模型）
                                          │
                                       tools ←── mcp / 其它插件注册进来的函数
```

## 1. 让模型说话

在 Web 面板里配 `providers`（JSON 数组）即可，密钥不入仓库：

```json
[
  {"id": "deepseek", "type": "openai",
   "base_url": "https://api.deepseek.com/v1",
   "api_key": "sk-xxxx",
   "model": "deepseek-chat",
   "default": true}
]
```

`api_key` 留空时会回落到环境变量 `LLM_CORE_API_KEY`。

命令：

| 命令 | 作用 |
| ---- | ---- |
| `/llm <内容>` | 对话（别名 `/chat`）。可用 `/llm @<模型名> 内容` 临时切换模型 |
| `/llmreset` | 清空当前会话 |
| `/llmtools` | 列出工具与开关状态 |
| `/llmtool <名> on\|off` | 开关某个工具（需管理员） |
| `/llmstatus` | 查看提供商 / 工具 / 会话概况 |

## 2. 让模型调用你的函数

三种来源，对模型而言完全等价——都是「名字 + 描述 + 参数 schema」：

| source | 怎么来 |
| ---- | ---- |
| `builtin` | 本插件自带（时间、计算器），当样板读 |
| `plugin` | 别的插件通过服务注册 |
| `mcp` | 从 MCP server 拉过来 |

在自己的插件里注册：

```python
svc = fw.services.get('llm_core')

@svc.tool(description="查询某个城市今天的天气，返回温度和天气状况")
def get_weather(location: str):
    '''查询城市天气。'''
    return "26 度，晴"
```

参数 schema 由类型注解自动推导，也可以显式给：

```python
@svc.tool(
    description="给用户加积分",
    parameters={"type": "object",
                "properties": {"uid": {"type": "integer", "description": "用户 ID"},
                               "score": {"type": "integer", "description": "加多少分"}},
                "required": ["uid", "score"]},
    require_perm="sign.admin",          # 需要权限节点才允许调用
    timeout=10)
def add_score(uid: int, score: int):
    ...
```

三条契约：

1. **描述写给模型看**，「处理数据」这种写法等于永远不会被调用。
2. **参数是双向契约**：缺参、类型错、传了未声明的参数都会被拒，并把错误回灌给模型让它自行改正重试。
3. **返回值**：返回字符串 → 进下一轮上下文让模型总结；返回 `None` → 不进上下文（适合函数自己已经把结果发出去的场景）。

## 3. 换别家的模型

写一个 provider 注册进来即可，不需要改这里一行代码：

```python
from plugins.llm_core.providers import Provider, ProviderReply

class MyProvider(Provider):
    id = "mine"
    model = "my-model"
    supports_tools = False

    def chat(self, messages, tools=None, temperature=0.7, max_tokens=1024):
        ...
        return ProviderReply(content="你好")

svc.register_provider(MyProvider(**cfg))
```

默认内置的 `OpenAICompatProvider` 覆盖 OpenAI / DeepSeek / Moonshot / 智谱部分版本 /
vLLM / Ollama / One-API 这类说同一种方言的服务——只差一个 `base_url`。

## 4. 图片（多模态）

图片只有在**原始消息**里才有完整的消息段数组，命令层拿到的是被剥干净的文本，
所以这里用 raw handler 先截一手存起来，真正发请求时再拼成视觉输入的内容块。

模型收不收得了由 provider 的 `supports_vision` 说了算：不支持就干脆不带图。
本地图片会转成 base64 data URI（上限 5MB/张），远端 URL 直接透传。

## 5. MCP

在面板 `mcp_servers` 里配 server，启动时自动握手并把 `tools/list` 拉到的工具
注册进总线（`source=mcp`）。支持两种传输：

```json
[
  {"name": "fs", "transport": "stdio",
   "command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]},
  {"name": "remote", "transport": "http", "url": "https://example.com/mcp"}
]
```

一台 server 挂掉不影响其它部分可用——它只是在日志里留一行错误。

## 6. Web 面板

侧边栏一个入口「LLM 对话」，进去是标签页（框架前端按插件名折叠侧边栏，
一个插件只能有一个入口，所以两页做在 `web/index.html` 这个外壳里切标签）：

- **运行概览**（`web/overview.html`）：提供商清单、工具清单与热开关、会话数与
  token 估算、一键清空会话。数据来自 `/api/llm_core/overview`（走框架鉴权）。
- **会话管理**（`web/sessions.html`）：逐会话查看多轮上下文。列表来自内存 ∪ 磁盘，
  重启后历史仍在；支持来源 / 群私聊 / 时间范围 / 关键词（会话键与消息正文）过滤与
  分页，行内勾选可批量删除或导出 JSONL，「查看」弹窗给对话气泡和 JSON 原文两种视图。
  接口：`/api/llm_core/sessions`、`/sessions/detail`、`/sessions/delete`、`/sessions/export`。

会话键是 `来源:群号(或 private):用户号`，三段都参与——**群聊同样按人隔离**，
所以同一个群里两个人各有各的上下文，面板上也是两行。

## 改源码的正确姿势

```bash
python tools/build_llm_payload.py        # dry-run，看会打包哪些文件
python tools/build_llm_payload.py --write  # 生成/更新 core_plugins/llm_load/llm_core.zip
```

llm_load 的 manifest 记录每个文件的 md5，释放按**逐文件内容比对**触发——只要
某个文件的字节变了就会重新释放并重启，不必手动加 `__version__`（版本号只用于
概况展示，不作为释放闸门）。
