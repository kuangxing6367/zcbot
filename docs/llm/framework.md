# 框架结构（给 LLM）

## 目录

| 路径 | 职责 |
| ---- | ---- |
| `framework/core/` | Framework 主体(base/dispatch/runtime mixin)、`event_buffer.py` 三层缓冲 |
| `framework/messaging/` | `router.py` 路由、`event_bus.py` 总线、`event.py` Event、`protocol.py` 适配器契约+服务注册表 |
| `framework/loader/` | 插件加载器:合成包名 `plugin_<id>`、热重载、`_conf_schema.json` |
| `framework/ctx/` | PluginContext(base/messaging/events mixins)= 插件拿到的 `ctx` |
| `framework/perm/` | 节点式权限引擎(三态+组继承+审计) |
| `framework/api/` | Web 后台 REST(Flask,登录+API Key) |
| `core_plugins/` | 官方插件,模块名 `core_plugin_<name>`;`plugins/` 用户插件,包名 `plugin_<id>` |
| `data/` | `zcbot.db`(SQLite 默认)、`event_buffer.db`、`logs/`、`plugins_dat/<插件>/` |

## 事件流水线(顺序固定)

```
适配器 → dispatch_event(event)
  → hook event.before_dispatch(返回 False 丢弃)
  → EventBuffer:L1 内存(512KB/2000条)→L2 sqlite 溢出→L3 兜底(4MB)→满则丢弃
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
注册:`ctx.hook(point, handler, priority)`;签名见 docs/api/advanced/hooks.md。

## 官方插件 → 服务名(`fw.services.get`)

| 插件 | 服务 | 默认 |
| ---- | ---- | ---- |
| onebot_adapter | `onebot_api`(反向 WS :6830) | 开 |
| webui | Web 后台 :8080 | 开 |
| session | `session_manager` | 开 |
| scheduler | `scheduler` | 开 |
| html_assembler | `html_assembler`(HTML 装配) | 开 |
| image_renderer | 图片渲染(Rust 原生+PIL 回退) | 开 |
| http_api / http_inject | REST / HTTP 注入 | 关 |
| qq_official / telegram / discord / ws_client | 其余接入端 | 关 |

配置中心:启动扫 `core_plugins/` → 补缺失块/删卸载块 → 回写 `core_plugins.yaml` → 合并进
主 config(`fw.config.get('onebot')` 等读法不变)。双进程模式下 `process:"core"` 的插件
跑核心进程,其余与用户插件跑宿主,跨进程自动代理,插件代码无感。
