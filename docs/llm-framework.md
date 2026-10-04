# 框架结构（给 LLM）

## 内核设计哲学（改 framework/ 前必读）

这是内核的设计信条，是所有架构决策的依据。改 `framework/` 或写官方/用户扩展前，先确认你的改动符合这些信条。

1. **最小必要内核**：`framework/` 只做加载/路由/公共服务（DB、权限、服务注册、事件总线、运行时上下文），不含任何业务，也不含 OneBot 代码。
2. **扩展点即契约**：`HookRegistry`（`framework/hooks.py`）是内核区别于普通框架的核心；`lifecycle/http/event/command/message/action/router` 各环节都留了插槽，`ctx.hook(point, handler)` 往里插逻辑。
3. **三层叠加**：内核（不可关）→ `core_plugins/`（可开关）→ `plugins/`（业务）；换形态只动 `core_plugins.yaml`，骨架不丢。
4. **协议无关、事件归一**：内核不区分事件来自 OneBot/Telegram/HTTP/WS，归一化后走同一条管线。
5. **热路径零开销**：`HookRegistry` 写时复制快照，无订阅者的点位直接返回空；每事件 5 次 `trigger`、多数无人注册是常态。
6. **优雅降级与故障隔离**：DB 不可用降级 JSON/YAML；`core/host` 双进程隔离用户扩展故障；依赖自愈/孤儿清理/内存看门狗/热重载；事件缓冲 L1→sqlite→L4 兜底，全满告警丢弃不崩。
7. **短路语义**：`before_*` 返回 `False` 即短路（丢事件/跳命令/取消发送），过滤/鉴权/黑名单前置到管线入口。
8. **单一事实源**：`config.yaml` 全局 + `core_plugins.yaml` 官方扩展开关中心；`enabled` 以 `config.yaml` 为准并回写。
9. **同步/异步双轨**：`trigger_async`（事件循环内可 `await`）/ `trigger_sync`（Web 线程上下文，async handler 经内核 loop fire-and-forget，绝不阻塞请求线程）。
10. **声明式权限**：`require_perm` 声明式自动拦截，三态节点（授予/否决/未定义）。

**铁律**：新增能力先问"它是内核最小必要能力，还是某个扩展的责任？"若是后者，放 `core_plugins/` 或 `plugins/`，通过 hook/服务注册接入，**禁止往内核塞业务或特判某个接入端**。完整阐述见 `docs/core-philosophy.md`。

## 目录

| 路径 | 职责 |
| ---- | ---- |
| `framework/core/` | Framework 主体(base/dispatch/runtime)、`event_buffer.py` 分层事件缓冲(L1/L4/L2/L3) |
| `framework/messaging/` | `router.py` 路由、`event_bus.py` 总线、`event.py` Event、`protocol.py` 适配器契约+服务注册表 |
| `framework/loader/` | 插件加载器:合成包名 `plugin_<id>`、热重载、`_conf_schema.json` |
| `framework/ctx/` | PluginContext(base/messaging/events/webui/db mixins)= 插件拿到的 `ctx` |
| `framework/perm/` | 节点式权限引擎(三态+组继承+审计) |
| `framework/api/` | Web 后台 REST(Flask,登录+API Key) |
| `core_plugins/` | 官方插件,模块名 `core_plugin_<name>`;`plugins/` 用户插件,包名 `plugin_<id>` |
| `data/` | `zcbot.db`(SQLite 默认)、`event_buffer.db`、`logs/`、`plugins_dat/<插件>/` |

## 事件流水线(顺序固定)

```
适配器 → dispatch_event(event)
  → hook event.before_dispatch(返回 False 丢弃)
  → EventBuffer:L1 内存(512KB/2000条)→L4 写缓冲(4MB)→L2 sqlite 溢出(data/event_buffer.db)→L3 兜底(4MB)→全满告警丢弃
  → worker 消费(默认 workers=1) → _process_event 按 type 分流:
      message: raw handlers(True=接管) → 提取文本 → 记日志 → stats 入队 → router.route
      meta/notice/request → event_bus.aemit('meta./notice./request.<子类型>')
  → hook event.after_dispatch(必触发)
```

router 热路径**零 DB**:内存路由表(后台 5s 重建)→ 按插件 priority 匹配预编译命令 →
未命中依次:广播 `message` 事件 → 关键词回复 → `router.message_unmatched` 扩展点。
命令 pattern 含正则元字符按 re 处理,否则前缀匹配(`match.group(1)`=参数);
`is_dynamic=1` 仅展示不匹配。

## 扩展点(hook)

`lifecycle.startup/shutdown`、`http.before/after_request`、`event.before/after_dispatch`(before 可丢弃)、
`command.before/after`(before 可跳过)、`message.before/after_send`(before 可取消)、
`action.before/after`(通知)、`router.before_route/after_route/message_unmatched`。
注册:`ctx.hook(point, handler, priority)`;签名见 docs/hooks.md。

## 官方插件 → 服务名(`fw.services.get`)

> **全部官方插件默认关闭**:启动同步(`_autoload_core_plugins`)对「新发现」的插件一律写
> `enabled: false`,绝不静默启用;必须手动在 `core_plugins.yaml` 把对应块的 `enabled` 改为 `true`
> 才会加载。当前仓库提交的 `core_plugins.yaml` 即全部 `false`。

| 插件 | 服务 | 默认 |
| ---- | ---- | ---- |
| onebot_adapter | `onebot_api`(反向 WS :6830;另注册 `protocol_adapter`/`api_caller`/`ws_server`) | 关 |
| webui | `web_server`(Web 后台 :8080) | 关 |
| session | `session_manager` | 关 |
| scheduler | `scheduler` | 关 |
| html_assembler | `html_assembler`(HTML 装配) | 关 |
| image_renderer | 图片渲染(Rust 原生+PIL 回退;经 `/render_card`·`/render_text` 命令调用,不注册 `fw` 服务) | 关 |
| http_api / http_inject | `http_api` / `protocol_adapter`+`api_caller`(REST / HTTP 注入) | 关 |
| qq_official / telegram / discord / ws_client | `protocol_adapter`+`api_caller`(其余接入端) | 关 |
| rust_accel | `rust_accel`(原生加速,Rust 子进程) | 关 |
| llm_load | `llm_core`(LLM 核心,由 payload 释放到 `plugins/llm_core`) | 关 |

配置中心:启动扫 `core_plugins/` → 补缺失块(`enabled` 强制 `false`)/删卸载块 → 回写
`core_plugins.yaml` → 合并进主 config(`fw.config.get('onebot')` 等读法不变)。双进程模式下
`process:"core"` 的插件跑核心进程,其余与用户插件跑宿主,跨进程自动代理,插件代码无感。
