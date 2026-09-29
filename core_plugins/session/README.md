# 多轮会话管理器

多轮会话管理插件（官方插件）。为插件提供内置的多轮对话能力：`ctx.wait_for()` /
`ctx.create_session()`。

## 元信息

- 优先级：0
- 进程归属：host（单进程与双进程宿主均加载）
- 默认开关：启用（`session.enabled` 设为 `false` 可禁用）
- 提供的服务：`session_manager`（禁用时注册为 `None`）

## 能力

- `wait_for(ctx, event, prompt=None, timeout=60, handler=None)`：发送可选提示后，等待该
  用户下一条消息，返回消息 dict 或超时返回 `None`。`handler` 可过滤是否消费该消息。
- `create_session(ctx, event, timeout=60)`：返回 `Session` 对象，支持 `await session.wait()`、
  `await session.ask(prompt)`、上下文管理器 `async with`。
- 会话以 `{user_id}:{group_id}` 为键；群消息按 `group_id`、私聊按 `user_id` 匹配。

## 配置上限

- 最大并发会话数：`5000`，达到上限时触发过期清理
- 默认超时：60 秒
- 过期会话由调度器每 5 分钟清理一次（`*/5 * * * *`）；会话管理器注册时会延迟 2 秒再向
  调度器注册清理任务，确保调度器已启动

## 实现要点

- 通过 `fw.register_raw_message_handler('session_manager', ..., priority=0)` 在原始消息层
  拦截，最先执行
- 异步 `handler` 经事件循环线程 `run_coroutine_threadsafe` 求值，避免工作线程无循环报错
- `Session.ask` 经 `api_caller.acall('send_msg', ...)` 发送提示后等待

## 可配置项（config.yaml 段 `session`）

| 键 | 说明 | 默认值 |
|---|---|---|
| `enabled` | 是否启用 | 启用 |
