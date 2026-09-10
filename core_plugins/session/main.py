"""
多轮会话管理器（官方插件）
为插件提供内置的多轮对话能力：wait_for / create_session
"""
import asyncio
import logging
import threading
import time
from contextlib import asynccontextmanager

logger = logging.getLogger('zcbot')

__plugin_meta__ = {
    "name": "多轮会话管理器",
    "version": "1.0.0",
    "author": "ZCBOT",
    "desc": "内置多轮会话：ctx.wait_for() / ctx.create_session()",
    "priority": 0,
    "official": True,
    "process": "host",
}

_DEFAULT_TIMEOUT = 60
_MAX_SESSIONS = 5000


class Session:
    """单个会话对象"""

    def __init__(self, session_id: str, framework, event, timeout: float = 60):
        self.session_id = session_id
        self._framework = framework
        self.event = event
        self.timeout = timeout
        self.data = {}
        self._future = None
        self._expires = 0
        self._closed = False

    async def wait(self, timeout: float = None) -> dict:
        """等待用户下一条消息"""
        if self._closed:
            return None
        t = timeout if timeout is not None else self.timeout
        loop = asyncio.get_running_loop()
        self._future = loop.create_future()
        self._expires = time.time() + t
        try:
            return await asyncio.wait_for(self._future, t)
        except asyncio.TimeoutError:
            return None

    async def ask(self, prompt: str, timeout: float = None) -> dict:
        """发送提示并等待回复"""
        target = {'group_id': self.event.group_id} if self.event.is_group else {'user_id': self.event.user_id}
        await self._framework.api_caller.acall('send_msg', **target, message=prompt)
        return await self.wait(timeout)

    def close(self):
        """关闭会话"""
        self._closed = True
        if self._future and not self._future.done():
            self._future.set_result(None)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.close()


class SessionManager:
    """会话管理器"""

    def __init__(self, framework):
        self.framework = framework
        self._sessions = {}
        self._lock = threading.Lock()

    def _session_key(self, event) -> str:
        gid = getattr(event, 'group_id', None) or 0
        uid = getattr(event, 'user_id', 0)
        return f"{uid}:{gid}"

    async def wait_for(self, ctx, event, prompt=None, timeout=_DEFAULT_TIMEOUT, handler=None):
        """
        等待用户下一条消息
        :param ctx: 插件上下文
        :param event: 当前事件
        :param prompt: 可选提示消息（自动发送）
        :param timeout: 超时秒数
        :param handler: 可选过滤 handler(raw_event) -> bool
        :return: 消息 dict 或 None（超时）
        """
        if prompt:
            target = {'group_id': event.group_id} if event.is_group else {'user_id': event.user_id}
            await self.framework.api_caller.acall('send_msg', **target, message=prompt)

        key = self._session_key(event)
        loop = asyncio.get_running_loop()
        fut = loop.create_future()

        with self._lock:
            if len(self._sessions) >= _MAX_SESSIONS:
                self._cleanup_locked()
            self._sessions[key] = {
                'future': fut,
                'expires': time.time() + timeout,
                'handler': handler,
            }

        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            with self._lock:
                self._sessions.pop(key, None)
            return None

    def create_session(self, ctx, event, timeout=60) -> Session:
        """创建会话对象"""
        key = self._session_key(event)
        return Session(key, self.framework, event, timeout)

    @asynccontextmanager
    async def session_context(self, ctx, event, timeout=60):
        """会话上下文管理器"""
        sess = self.create_session(ctx, event, timeout)
        key = sess.session_id
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        with self._lock:
            self._sessions[key] = {
                'future': fut,
                'expires': time.time() + timeout,
                'handler': None,
                'session': sess,
            }
        sess._future = fut
        try:
            yield sess
        finally:
            sess.close()
            with self._lock:
                self._sessions.pop(key, None)

    def on_raw_message(self, raw_event: dict, bot_name: str) -> bool:
        """原始消息拦截：检查是否有等待中的会话"""
        uid = raw_event.get('user_id', 0)
        gid = raw_event.get('group_id') or 0
        key = f"{uid}:{gid}"

        with self._lock:
            waiter = self._sessions.get(key)

        if waiter is None:
            return False

        fut = waiter['future']
        if fut.done():
            return False

        handler = waiter.get('handler')
        if handler is not None:
            try:
                import asyncio
                if asyncio.iscoroutinefunction(handler):
                    consume = asyncio.get_event_loop().run_until_complete(handler(raw_event))
                else:
                    consume = handler(raw_event)
            except Exception:
                consume = True
            if not consume:
                return False

        # 填充 Session 对象的 data（如果有）
        session = waiter.get('session')
        if session:
            session.data['_raw'] = raw_event

        fut.set_result(raw_event)
        with self._lock:
            self._sessions.pop(key, None)
        return True

    def _cleanup_locked(self):
        """清理过期会话"""
        now = time.time()
        for sid in [s for s, w in self._sessions.items() if w['expires'] < now]:
            w = self._sessions.pop(sid, None)
            if w and not w['future'].done():
                w['future'].set_result(None)

    def cleanup(self):
        """定时清理"""
        with self._lock:
            self._cleanup_locked()

    def active_count(self) -> int:
        with self._lock:
            return len(self._sessions)


_manager = None


def register(ctx):
    """注册会话管理器"""
    global _manager
    fw = ctx._framework

    session_cfg = fw.config.get('session', {})
    if session_cfg.get('enabled') is False:
        ctx.log("会话管理器已禁用 (session.enabled: false)")
        fw.services.register('session_manager', None)
        return

    _manager = SessionManager(fw)
    fw.services.register('session_manager', _manager)

    # 注册原始消息拦截（priority=0，最先执行）
    fw.register_raw_message_handler('session_manager', _manager.on_raw_message, priority=0)

    # 注册定时清理任务（延迟注册，确保调度器已加载）
    def _register_cleanup_task():
        scheduler = fw.services.get('scheduler')
        if scheduler:
            scheduler.add_plugin_task({
                'plugin_name': 'session',
                'cron_expression': '*/5 * * * *',
                'handler': '_cleanup_task',
                'handler_name': '_cleanup_task',
                'description': '清理过期会话',
            })
        else:
            ctx.log("调度器未加载，跳过清理任务注册", level="warning")
    
    # 延迟 2 秒注册，确保调度器已启动
    import threading
    threading.Timer(2.0, _register_cleanup_task).start()

    ctx.log("会话管理器已就绪")


def _cleanup_task():
    """定时清理过期会话"""
    if _manager:
        _manager.cleanup()


def unregister():
    global _manager
    _manager = None
