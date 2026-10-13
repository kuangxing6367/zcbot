# ZCBOT

> **通用化 IM 平台**：不绑定任何聊天协议——能接 QQ、Telegram、Discord，也能不接平台，只跑定时任务或收 HTTP 事件。
> 需要哪种能力就启用对应的扩展，业务写在自己的插件里。

**当前版本：v1.8.8** ｜ [CHANGELOG](https://gitee.com/liquanfeng123/zcbot/blob/main/CHANGELOG.md) ｜ [PyPI](https://pypi.org/project/zcbot/)

- 项目地址：https://gitee.com/liquanfeng123/zcbot
- 官方扩展仓库：https://gitee.com/liquanfeng123/zcbot_plugins
- **LLM / Agent 文档**：[LLM.md](https://gitee.com/liquanfeng123/zcbot/blob/main/LLM.md) —— 给 AI 编码助手的按需加载索引
- 反馈交流：群组 **1060129201**

---

## 一分钟跑起来

**方式 A · pip（推荐）**

```bash
pip install zcbot
zcbot
```

配置与数据落在 `~/.zcbot`（可用环境变量 `ZCBOT_HOME` 指定其它目录）。

**方式 B · 源码**

```bash
git clone https://gitee.com/liquanfeng123/zcbot.git
cd zcbot
pip install -r requirements.txt
python main.py
```

看到 `框架启动完成，等待消息...` 即启动成功（依赖缺了会自动补装）。

> **官方扩展默认全部关闭。** 要哪种能力就启用哪个：把 `core_plugins.yaml` 里对应块的 `enabled` 改为 `true`，
> 或运行 `python tools/scan_core_plugins.py --enable <扩展名>`，重启生效。
> 进 Web 后台启用 `webui`（`127.0.0.1:8080`，默认账号 `admin` / `admin123`，**登录后立即改密**）；
> 要收发聊天消息就启用某个接入端（如 `onebot_adapter`）。

详见 [安装](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/installation.md) · [开始使用](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/getting-started.md) · [配置系统](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/configuration.md)。

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
想插到几乎每个运行环节就用 `ctx.hook(...)`（见 [扩展点](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/hooks.md)）。
完整教程见 [编写插件](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/writing-plugins.md)。

## 文档

全部文档平铺在 `docs/` 一层，入口 [docs/index.md](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/index.md)。

- **指南**：[安装](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/installation.md) · [开始使用](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/getting-started.md) · [配置](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/configuration.md) · [对接 IM](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/connect-im.md) · [编写插件](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/writing-plugins.md) · [多轮会话](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/session.md) · [接入 LLM](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/llm-chat.md) · [最佳实践](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/best-practices.md)
- **API**：[ctx](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/ctx.md) · [Event](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/event.md) · [扩展点 Hook](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/hooks.md) · [协议适配器](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/protocol_adapter.md) · [Framework](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/framework.md) · [服务注册表](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/services.md) · [插件装饰器](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/plugin-decorators.md)
- **进阶**：[内核设计哲学](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/core-philosophy.md) · [架构详解](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/architecture.md) · [加载机制](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/loader.md) · [数据库](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/database.md) · [定时任务](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/scheduler.md) · [权限](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/permission.md) · [接口令牌](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/api-tokens.md) · [部署](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/deployment.md) · [双核心](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/dual-core.md) · [项目结构](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/project-structure.md) · [扩展清单](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/plugins-catalog.md)
- **LLM**：[LLM.md](https://gitee.com/liquanfeng123/zcbot/blob/main/LLM.md) · [框架结构](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/llm-framework.md) · [插件开发](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/llm-plugins.md) · [调试排错](https://gitee.com/liquanfeng123/zcbot/blob/main/docs/llm-debugging.md)

## 开源协议

MIT + Apache 2.0 双协议，任选其一适用。

> 本项目以 AI 生成为主、人工辅助完成。用着顺手的话，给个 Star 吧！
