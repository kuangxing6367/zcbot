"""
框架运行时中立上下文（与具体协议无关）

这里只放"任何接入端都需要"的运行期状态，不包含任何 OneBot / HTTP 等协议细节。
"""
import contextvars

# 当前正在处理的事件来源实例名（由 router 在 handler 执行期间注入）。
# 协议中立命名：对 OneBot 它是 OneBot 实例名，对其它接入端是对应来源标识。
# 使用 contextvars：多来源并发消息互不干扰，协程与 to_thread 均携带上下文快照。
current_source_var: contextvars.ContextVar = contextvars.ContextVar(
    'zcbot_current_source', default=None)

# 向后兼容别名（旧名，语义等价）
current_bot_var = current_source_var
