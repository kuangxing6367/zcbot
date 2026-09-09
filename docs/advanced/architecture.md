# 架构详解

## 分层总览

```
┌─────────────────────────────────────────────────┐
│            Core Framework（framework/ 极简壳）     │
│  插件加载器 loader · 事件总线 event_bus          │
│  消息路由 router · 上下文 ctx · 数据库 db         │
└──────────────────────┬──────────────────────────┘
                       │ 先加载，提供基础服务
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
┌──────────────┐ ┌──────────────┐ ┌──────────────┐
│onebot_adapter│ │  scheduler   │ │ session/webui│  core_plugins/（可开关）
└──────────────┘ └──────────────┘ └──────────────┘
                       │ 后加载
                       ▼
                ┌──────────────┐
                │  plugins/    │ 用户插件（每个一个目录，含 main.py）
                └──────────────┘
```

## 启动时序

`main.py → Framework.start()`（`framework/core.py`）：

1. 打印安全提示（监听 `0.0.0.0` 且无 token 时告警）；
2. `_load_core_plugins()`：按 `config.yaml → core_plugins` 开关加载官方插件，
   它们向服务注册表注册基础能力；
3. 创建 `data/plugins_dat/`，把旧版散落在代码目录的配置迁移过去；
4. `plugin_loader.load_all()`：发现并加载全部未被禁用的用户插件
   （依赖检查/自动安装 → 建合成包 → 预载子模块 → 执行 main.py）；
5. 依赖自愈：首轮缺依赖失败的插件，补装后再尝试一次；
6. 逐个 `register_commands()`：执行 `register(ctx)`，落库命令/任务/卡片，触发 `on_loaded`；
7. 启动路由表刷新、统计批量写库器、心跳、内存看门狗；
8. 广播 `system.plugin.loaded`，启动内置终端。

关闭 `Framework.stop()` 按相反顺序停止 WebSocket、调度器、Web 服务并关闭数据库。

## 消息处理流程

```
OneBot 客户端
    │  WebSocket 反向连接
    ▼
WebSocket 服务端 (core_plugins/onebot_adapter)
    │
    ▼
事件标准化为 Event (framework/event.py)
    │
    ▼
框架核心 (framework/core.py)
    │
    ├─→ 原始消息处理器 (ctx.on_raw_message)
    │       │  返回 True 则被接管，流程终止
    │       ▼  未接管继续
    ├─→ 消息路由器 (router.py)
    │       ├─→ 插件命令匹配（按 priority 升序）
    │       │       │  命中且未 continue_route → 不再走关键词
    │       │       ▼  未命中
    │       ├─→ 关键词自动回复（dynamic_commands）
    │       │       ▼  未命中
    │       └─→ message 事件广播（ctx.on("message")）
    │
    ├─→ 通知事件 notice
    └─→ 请求事件 request
```

命令/事件处理器支持 `def`（转线程）与 `async def`（事件循环内）两种写法；
`event.stop_event()` 可截断后续传播。

## 服务注册表

核心框架通过 `services` 注册表解耦官方插件与用户插件：

```python
# 官方插件侧：注册能力
fw.services.register('api_caller', api_caller)

# 用户插件侧：按需取用（可能尚未就绪，必要时监听 system.plugin.loaded）
caller = ctx._framework.services.get('api_caller')
```

| 服务名 | 提供者 | 说明 |
|--------|--------|------|
| `protocol_adapter` | onebot_adapter | 协议适配器抽象 |
| `api_caller` | onebot_adapter | OneBot API 调用器 |
| `onebot_api` | onebot_adapter | OneBot API 面向对象封装 |
| `ws_server` | onebot_adapter | WebSocket 服务端 |
| `scheduler` | scheduler | 定时任务调度器（APScheduler） |
| `session_manager` | session | 多轮会话管理器 |
| `web_server` | webui | Web 管理后台服务 |

详见 [ServiceRegistry](../api/services.md)。

## 插件加载机制（要点）

- 每个用户插件的主模块注册为 `plugin_<插件名>`，它同时是一个带 `__path__`
  的“合成包”，因此插件内部可以用 `from .xxx import Y` 做相对导入；
- 子模块同时拥有 `plugin_<名>.<模块>`（相对导入）、`plugin_<名>_<模块>`（旧唯一名）、
  `<模块>`（短名绝对导入）三个名字，指向同一对象；
- 卸载按模块 `__file__` 前缀一次性扫净 `sys.modules`。

完整原理、导入规则、热重载行为、排错见
[插件加载与模块机制](./loader.md)。

## 插件优先级

数字越小越先加载、越先收到原始消息、命令匹配越优先。

| 优先级 | 典型用途 |
|--------|----------|
| 0 | 官方插件（onebot_adapter、session 等） |
| 1–20 | 基础设施类用户插件（session_waiter、message_guard、依赖图等） |
| 50 | 默认（大多数用户插件） |
| 100+ | 低优先级/展示类（如 help=100） |

## 心跳、增量注册与热重载

- 每 `plugin.heartbeat_interval`（默认 60s）执行一次 `heartbeat_register()`：
  扫描插件目录 `.py` 文件的最大 mtime，**仅对发生变化的插件重新执行
  `register(ctx)`**，不重新 import；
- 因此“改了注册结构（新增命令/任务）”靠心跳即可刷新，
  而“改了函数体逻辑”需要 Web 面板的**完全重载**（unload + load，重新读盘）；
- 心跳后路由缓存失效并兜底重建，保证命令表与内存快照一致。

## 自检与自愈

- **依赖自愈**：启动时对缺依赖导致加载失败的插件，在补装依赖后自动再试；
- **孤儿自检 `self_check_orphans`**：周期性清理代码目录已不存在的命令/任务，
  以及调度器里属于未加载插件的“幽灵任务”；
- **内存看门狗**：每 3s 采样，单插件模块估算内存连续超过
  `plugin.max_memory_mb`（默认 64MB）两次即自动卸载并记录日志。

## 事件总线

```python
ctx.emit("user_sign_in", {"user_id": 123456})      # 同步
await ctx.aemit("user_sign_in", {...})             # 异步
ctx.on("notice.group_increase", on_member_join)    # 订阅（同步/异步 handler 均可）
```

### 内置事件

| 事件名 | 触发时机 |
|--------|----------|
| `message` | 收到文本消息且命令/关键词均未命中 |
| `notice.group_increase` | 新成员入群 |
| `notice.group_decrease` | 成员退群 |
| `request.friend` | 好友请求 |
| `request.group` | 加群请求 |
| `meta.heartbeat` | OneBot 心跳包 |
| `bot.connected` / `bot.disconnected` | OneBot 客户端连接/断开 |
| `system.plugin.loaded` | 本轮插件全部加载注册完成（payload 含插件列表） |
| `after_message_sent` | 消息发送完成后 |

插件可自定义任意事件名，通过 `emit/aemit` 在插件间解耦通信。
