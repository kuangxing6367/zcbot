"""
运行日志代理（参考 AstrBot LogBroker 设计）
- 线程安全环形缓存
- 捕获框架 logging 输出
- 支持 SSE 订阅实时推送
- 支持搜索和级别过滤
"""
import logging
import queue
import threading
import time
from collections import deque
from typing import Optional

MAX_CACHE = 2000  # 最大缓存条数
MAX_SSE_SUBSCRIBERS = 32  # SSE 订阅者上限（防资源耗尽）


class LogBroker:
    """运行日志代理，线程安全的环形缓存 + 发布订阅"""

    def __init__(self, max_size: int = MAX_CACHE):
        self._cache = deque(maxlen=max_size)
        self._lock = threading.Lock()
        self._seq = 0  # 自增序号
        self._subscribers: list[queue.Queue] = []

    def log(self, category: str, level: str, message: str, detail: dict = None,
            source: str = None):
        """
        记录一条运行日志
        :param category: 日志分类 message|connection|plugin|system|framework
        :param level: DEBUG|INFO|WARN|ERROR
        :param message: 日志摘要
        :param detail: 附加详情 dict
        :param source: 来源（文件名或插件名）
        """
        with self._lock:
            self._seq += 1
            entry = {
                'seq': self._seq,
                'time': time.time(),
                'category': category,
                'level': level.upper(),
                'message': message,
                'detail': detail or {},
                'source': source or '',
            }
            self._cache.append(entry)

        # 推送给 SSE 订阅者
        for q in self._subscribers:
            try:
                q.put_nowait(entry)
            except queue.Full:
                pass  # 订阅者消费太慢则丢弃

    def log_message(self, bot_name: str, message_type: str, user_id: int,
                    group_id: Optional[int], raw_message: str, message_id: int = None):
        """记录 OneBot 上报的消息"""
        source = f"群{group_id}" if group_id else f"私聊{user_id}"
        self.log('message', 'INFO',
                 f"[{bot_name}] {message_type} {source}: {raw_message[:200]}",
                 {
                     'bot': bot_name,
                     'message_type': message_type,
                     'user_id': user_id,
                     'group_id': group_id,
                     'raw_message': raw_message,
                     'message_id': message_id,
                 })

    def log_connection(self, bot_name: str, event: str, detail: dict = None):
        """记录 WebSocket 连接事件"""
        level = 'INFO' if event in ('connect',) else 'WARN' if event == 'disconnect' else 'ERROR'
        self.log('connection', level,
                 f"[{bot_name}] WebSocket {event}",
                 {'bot': bot_name, 'event': event, **(detail or {})})

    def log_plugin(self, plugin_name: str, action: str, detail: dict = None):
        """记录插件处理日志"""
        self.log('plugin', 'INFO',
                 f"[{plugin_name}] {action}",
                 {'plugin': plugin_name, 'action': action, **(detail or {})},
                 source=plugin_name)

    def log_system(self, level: str, message: str, detail: dict = None):
        """记录系统日志"""
        self.log('system', level, message, detail or {})

    def log_framework(self, level: str, message: str, source: str = ''):
        """记录框架日志（来自 Python logging）"""
        self.log('framework', level, message, {}, source=source)

    def subscribe(self):
        """订阅实时日志流（SSE 用），超过上限返回 None"""
        with self._lock:
            if len(self._subscribers) >= MAX_SSE_SUBSCRIBERS:
                return None
            q = queue.Queue(maxsize=500)
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue):
        """取消订阅"""
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    def get_logs(self, category: Optional[str] = None, level: Optional[str] = None,
                 keyword: str = None, limit: int = 100, after_seq: int = 0) -> list:
        """
        查询日志
        :param category: 分类过滤，None 表示全部
        :param level: 级别过滤，None 表示全部
        :param keyword: 关键词搜索
        :param limit: 最大返回条数
        :param after_seq: 只返回序号大于此值的日志（用于轮询增量）
        """
        with self._lock:
            result = []
            for entry in reversed(self._cache):
                if entry['seq'] <= after_seq:
                    break
                if category and entry['category'] != category:
                    continue
                if level and entry['level'] != level.upper():
                    continue
                if keyword and keyword.lower() not in entry['message'].lower():
                    continue
                result.append(entry)
                if len(result) >= limit:
                    break
            result.reverse()
            return result

    def get_stats(self) -> dict:
        """获取日志统计"""
        with self._lock:
            stats = {'total': len(self._cache), 'by_category': {}, 'by_level': {}}
            for entry in self._cache:
                cat = entry['category']
                lvl = entry['level']
                stats['by_category'][cat] = stats['by_category'].get(cat, 0) + 1
                stats['by_level'][lvl] = stats['by_level'].get(lvl, 0) + 1
            stats['latest_seq'] = self._seq
            stats['subscribers'] = len(self._subscribers)
            return stats

    def clear(self):
        """清空缓存"""
        with self._lock:
            self._cache.clear()
            self._seq = 0


class FrameworkLogHandler(logging.Handler):
    """将 Python logging 输出桥接到 LogBroker（参考 AstrBot LogQueueHandler）"""

    # Python logging 级别 → LogBroker 级别映射
    LEVEL_MAP = {
        logging.DEBUG: 'DEBUG',
        logging.INFO: 'INFO',
        logging.WARNING: 'WARN',
        logging.ERROR: 'ERROR',
        logging.CRITICAL: 'ERROR',
    }

    def __init__(self, broker: LogBroker):
        super().__init__()
        self._broker = broker

    def emit(self, record):
        try:
            level = self.LEVEL_MAP.get(record.levelno, 'INFO')
            # 格式化消息
            msg = record.getMessage()
            # 提取来源文件名
            source = ''
            if record.name and record.name != 'zcbot':
                source = record.name
            elif record.filename:
                source = record.filename

            # 跳过过于频繁的调试日志
            if record.levelno < logging.DEBUG:
                return

            self._broker.log_framework(level, msg, source=source)
        except Exception:
            pass  # 日志系统不能抛异常


# 全局单例
log_broker = LogBroker()
