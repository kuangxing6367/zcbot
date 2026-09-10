# -*- coding: utf-8 -*-
"""
IpcLogHandler —— 宿主进程（进程2）日志转发

把宿主进程的框架/插件日志经 IPC notify('log', ...) 推送到核心进程，
由核心进程写入其 log_broker（WebUI 日志页可见），实现双进程日志合并。
"""
import logging


class IpcLogHandler(logging.Handler):
    """把日志记录转发为核心侧的 host 日志（经 IPC）"""

    def __init__(self, client):
        super().__init__()
        self._client = client

    def emit(self, record):
        try:
            self._client.notify('log', {
                'time': record.created,
                'level': record.levelname,
                'logger': record.name,
                'msg': record.getMessage(),
            })
        except Exception:
            # 连接未就绪 / 已断开时静默丢弃，不影响插件运行
            pass
