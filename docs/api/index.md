# API 参考总览

不知道该翻哪一页？按你手头的任务选：**发消息/注册命令**查 [ctx](./basic/ctx.md)，**读消息字段**查 [event](./basic/event.md)，**往运行环节插逻辑**查 [hooks](./advanced/hooks.md)，**换一个接入端**查 [protocol_adapter](./advanced/protocol_adapter.md)。

## 按任务选页

| 你要做的事 | 打开这页 |
| ---- | ---- |
| 注册命令、发消息、查库、定时任务、WebUI、权限 | [PluginContext (ctx)](./basic/ctx.md) — 插件在 `register(ctx)` 里拿到的全部能力 |
| 读消息内容、判断图片/@/回复、拦事件、查权限节点 | [Event 事件对象](./basic/event.md) — handler 第二参数，字段、富媒体段、`stop_event` |
| 改生命周期、拦 HTTP 请求、审动作、自定义扩展点 | [扩展点（Hook 系统）](./advanced/hooks.md) — 启动/关闭、Web、事件分发、命令执行、协议动作、出站文本 |
| 接 Telegram / Discord / MQTT / 自定义事件源 | [协议适配器 ProtocolAdapter](./advanced/protocol_adapter.md) — 契约、ActionProxy、`http_inject` / `ws_client` / `qq_official` / `telegram` / `discord` 示例 |
| 摸底层容器：生命周期、服务注册表、事件循环 | [Framework 核心](./basic/framework.md) |
| 取官方能力、查内置服务名、判断服务是否可用 | [服务注册表（DI）](./basic/services.md) |

### 基础参考

| 文档 | 内容 |
| ---- | ---- |
| [PluginContext (ctx)](./basic/ctx.md) | 命令、事件、权限、数据库、定时任务、WebUI、消息发送、扩展点…… |
| [Event 事件对象](./basic/event.md) | 字段、富媒体段、传播控制（`stop_event`）、权限查询 |
| [Framework 核心](./basic/framework.md) | 底层容器 `fw`：生命周期、服务注册表、数据库、运行时上下文与高级用法 |
| [服务注册表（DI）](./basic/services.md) | 能力注册 / 取用、内置服务名清单 |

### 进阶扩展

| 文档 | 内容 |
| ---- | ---- |
| [扩展点（Hook 系统）](./advanced/hooks.md) | 在启动/关闭、Web 请求、事件分发、命令执行、协议动作、出站文本等几乎每个环节插入自己的逻辑 |
| [协议适配器 ProtocolAdapter](./advanced/protocol_adapter.md) | 写一个接入端的契约、ActionProxy、内置接入端示例 |

## 内核视角

ZCBOT 的内核极小：`Framework` 只负责加载插件、路由事件、提供公共服务（数据库 / 权限 / 服务注册 / 事件总线）。
你在 `docs/guide/writing-plugins.md` 里写的命令、在 `core_plugins/` 里看到的 Web/接入端/调度器，全都是**挂在内核扩展点上的扩展**。

```text
┌─────────────── 平台内核（framework/） ───────────────┐
│  Framework  ·  HookRegistry  ·  ServiceRegistry     │
│  EventBus    ·  MessageRouter ·  Database ·  Perm    │
└───────────────────────┬────────────────────────────┘
                         │ 扩展点 / 服务注册 / 事件总线
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
  核心插件(core_)   用户插件(plugins/)   你的自定义接入端
  webui/http_inject 业务命令/定时任务     ProtocolAdapter
  scheduler/session  扩展点切面/中间件
```

想"插到几乎每个地方"？从 [扩展点（Hook 系统）](./advanced/hooks.md) 开始。
