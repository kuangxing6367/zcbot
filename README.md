# ZCBOT

> **通用化 IM 平台**：不绑定任何聊天协议——能接 QQ、Telegram、Discord，也能不接平台，只跑定时任务或收 HTTP 事件。
> 需要哪种能力就启用对应的扩展，业务写在自己的插件里。

**当前正式版：v1.8.2** ｜ [CHANGELOG.md](CHANGELOG.md)

- 项目地址：https://github.com/kuangxing6367/zcbot
- 官方扩展仓库：https://github.com/kuangxing6367/zcbot_plugins
- **LLM / Agent 文档**：[LLM.md](LLM.md) —— 给 AI 编码助手的按需加载索引
- 反馈交流：群组 **1060129201**

---

## 一分钟跑起来

```bash
git clone https://github.com/kuangxing6367/zcbot.git
cd zcbot
pip install -r requirements.txt
python main.py
```

看到 `框架启动完成，等待消息...` 即启动成功（依赖缺了会自动补装）。

> **官方扩展默认全部关闭。** 要哪种能力就启用哪个：把 `core_plugins.yaml` 里对应块的 `enabled` 改为 `true`，
> 或运行 `python tools/scan_core_plugins.py --enable <扩展名>`，重启生效。
> 进 Web 后台启用 `webui`（`127.0.0.1:8080`，默认账号 `admin` / `admin123`，**登录后立即改密**）；
> 要收发聊天消息就启用某个接入端（如 `onebot_adapter`）。

详见 [安装](docs/installation.md) · [开始使用](docs/getting-started.md) · [配置系统](docs/configuration.md)。

## 写个插件

`plugins/hello/main.py`：

```python
__plugin_meta__ = {"name": "Hello", "version": "1.0.0",
                   "author": "你", "desc": "打招呼", "priority": 50}

def register(ctx):
    ctx.command("/hello", handle_hello, description="打个招呼")

async def handle_hello(event, match):
    await ctx.asend_msg(user_id=event.user_id,
                        group_id=event.group_id if event.is_group else None,
                        message="Hello, World!")
```

放进 `plugins/` 后点「重载」即可。权限用 `require_perm="myplugin.ban"` 声明式拦截；
想插到几乎每个运行环节就用 `ctx.hook(...)`（见 [扩展点](docs/hooks.md)）。
完整教程见 [编写插件](docs/writing-plugins.md)。

## 文档

全部文档平铺在 `docs/` 一层，入口 [docs/index.md](docs/index.md)。

- **指南**：[安装](docs/installation.md) · [开始使用](docs/getting-started.md) · [配置](docs/configuration.md) · [对接 IM](docs/connect-im.md) · [编写插件](docs/writing-plugins.md) · [多轮会话](docs/session.md) · [接入 LLM](docs/llm-chat.md) · [最佳实践](docs/best-practices.md)
- **API**：[ctx](docs/ctx.md) · [Event](docs/event.md) · [扩展点 Hook](docs/hooks.md) · [协议适配器](docs/protocol_adapter.md) · [Framework](docs/framework.md) · [服务注册表](docs/services.md) · [插件装饰器](docs/plugin-decorators.md)
- **进阶**：[内核设计哲学](docs/core-philosophy.md) · [架构详解](docs/architecture.md) · [加载机制](docs/loader.md) · [数据库](docs/database.md) · [定时任务](docs/scheduler.md) · [权限](docs/permission.md) · [接口令牌](docs/api-tokens.md) · [部署](docs/deployment.md) · [双核心](docs/dual-core.md) · [项目结构](docs/project-structure.md) · [扩展清单](docs/plugins-catalog.md)
- **LLM**：[LLM.md](LLM.md) · [框架结构](docs/llm-framework.md) · [插件开发](docs/llm-plugins.md) · [调试排错](docs/llm-debugging.md)

## 开源协议

MIT + Apache 2.0 双协议，任选其一适用。

> 本项目以 AI 生成为主、人工辅助完成。用着顺手的话，给个 Star 吧！
