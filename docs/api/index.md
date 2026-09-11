# API 参考总览

ZCBOT 的 API 分为两大部分：**基础参考**（写插件每天都会用到的对象）与**进阶扩展**（把框架当微内核、往内核运行环节插行为的契约）。

## 基础参考

日常写业务插件最常查的接口：

| 文档 | 内容 |
| ---- | ---- |
| [PluginContext (ctx)](./basic/ctx.md) | 插件在 `register(ctx)` 里拿到的全部能力：命令、事件、权限、数据库、定时任务、WebUI、消息发送、扩展点…… |
| [Event 事件对象](./basic/event.md) | handler 收到的消息/事件对象：字段、富媒体段、传播控制（`stop_event`）、权限查询 |
| [Framework 核心](./basic/framework.md) | 底层容器 `fw`：生命周期、服务注册表、数据库、运行时上下文与高级用法 |
| [服务注册表（DI）](./basic/services.md) | 内核与插件如何通过 `services` 解耦：能力注册 / 取用、内置服务名清单 |

## 进阶扩展

当你不再满足于"写命令"，而想**改造内核行为本身**时：

| 文档 | 内容 |
| ---- | ---- |
| [扩展点（Hook 系统）](./advanced/hooks.md) | 微内核最核心的契约：在启动/关闭、Web 请求、事件分发、命令执行、协议动作、出站文本等几乎每个环节插入自己的逻辑 |
| [协议适配器 ProtocolAdapter](./advanced/protocol_adapter.md) | 写一个接入端（Telegram / Discord / MQTT / 自定义）的契约、ActionProxy、内置 `http_inject` 示例 |

## 微内核视角

ZCBOT 的"内核"极小：`Framework` 只负责加载插件、路由事件、提供公共服务（数据库 / 权限 / 服务注册 / 事件总线）。
你在 `docs/guide/writing-plugins.md` 里写的命令、在 `core_plugins/` 里看到的 Web/接入端/调度器，全都是**挂在内核扩展点上的扩展**。

```text
┌─────────────── 微内核（framework/） ───────────────┐
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
