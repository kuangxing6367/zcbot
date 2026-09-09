# 架构详解

## 消息处理流程

```
OneBot 客户端
    │
    ▼
WebSocket 服务端 (core_plugins/onebot_adapter)
    │
    ▼
事件标准化 (normalize_event)
    │
    ▼
框架核心 (framework/core.py)
    │
    ├─→ 原始消息处理器 (on_raw_message)
    │       │
    │       ▼ (未接管)
    ├─→ 消息路由器 (router.py)
    │       │
    │       ├─→ 插件命令匹配 (按 priority 升序)
    │       │       │
    │       │       ▼ (未命中)
    │       ├─→ 关键词自动回复
    │       │       │
    │       │       ▼ (未命中)
    │       └─→ message 事件广播
    │
    ├─→ 通知事件 (notice)
    └─→ 请求事件 (request)
```

## 服务注册表

核心框架通过 `services` 注册表获取官方插件提供的能力：

```python
# 框架内部
fw.services.register('api_caller', api_caller)
fw.services.register('session_manager', session_manager)

# 插件调用
caller = ctx._framework.services.get('api_caller')
```

| 服务名 | 提供者 | 说明 |
|--------|--------|------|
| `protocol_adapter` | onebot_adapter | 协议适配器 |
| `api_caller` | onebot_adapter | API 调用器 |
| `onebot_api` | onebot_adapter | OneBot API 封装 |
| `ws_server` | onebot_adapter | WebSocket 服务端 |
| `scheduler` | scheduler | 定时任务调度器 |
| `session_manager` | session | 会话管理器 |
| `web_server` | webui | Web 服务 |

## 插件优先级

数字越小越先加载、越先收到消息、命令匹配越优先。

| 优先级 | 用途 |
|--------|------|
| 0 | 官方插件（onebot_adapter, session 等） |
| 1 | session_waiter（旧版会话） |
| 2 | message_guard（消息防护） |
| 50 | 默认（大多数用户插件） |
| 100 | 低优先级插件 |

## 事件总线

```python
# 发布事件
ctx.emit("user_sign_in", {"user_id": 123456})

# 订阅事件
ctx.on("user_sign_in", handler)
ctx.on("notice.group_increase", on_member_join)
ctx.on("meta.heartbeat", on_heartbeat)
```

### 内置事件

| 事件名 | 触发时机 |
|--------|----------|
| `message` | 收到文本消息（命令未命中时） |
| `notice.group_increase` | 新成员入群 |
| `notice.group_decrease` | 成员退群 |
| `request.friend` | 好友请求 |
| `request.group` | 加群请求 |
| `meta.heartbeat` | 心跳包 |
| `system.plugin.loaded` | 插件加载完成 |
| `after_message_sent` | 消息发送后 |
