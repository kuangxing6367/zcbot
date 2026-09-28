# -*- coding: utf-8 -*-
"""
IpcLogHandler —— 宿主进程（进程2）日志转发

把宿主进程的框架/插件日志经 IPC notify('log', ...) 推送到核心进程，
由核心进程写入其 log_broker（WebUI 日志页可见），实现双进程日志合并。

性能：日志是高频小消息（每次 logging 调用一条），逐条 notify 会产生
大量 IPC 帧与重复序列化。这里做批量合并——攒批后一次 notify 携带多条
记录的 batch（{'batch': [...]}），核心侧逐条落盘；单条旧格式仍兼容。
"""
import logging
import threading


class IpcLogHandler(logging.Handler):
    """把日志记录批量转发为核心侧的 host 日志（经 IPC）"""

    MAX_BATCH = 200          # 一批最多多少条（防单帧过大）
    FLUSH_INTERVAL = 0.05    # 时间窗口（秒）：此窗口内未满批也发送

    def __init__(self, client):
        super().__init__()
        self._client = client
        self._buf = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._flush_thread = threading.Thread(
            target=self._flush_loop, daemon=True, name='ipc-log-flush')

    def start(self):
        """启动定时冲刷线程（宿主连接就绪后调用）"""
        self._flush_thread.start()

    def emit(self, record):
        try:
            entry = {
                'time': record.created,
                'level': record.levelname,
                'logger': record.name,
                'msg': record.getMessage(),
            }
        except Exception:
            return  # 记录格式化异常则丢弃，不影响日志系统
        batch = None
        with self._lock:
            self._buf.append(entry)
            if len(self._buf) >= self.MAX_BATCH:
                batch, self._buf = self._buf, []
        if batch:
            self._flush(batch)

    def _flush_loop(self):
        while not self._stop.wait(self.FLUSH_INTERVAL):
            with self._lock:
                batch, self._buf = self._buf, []
            if batch:
                self._flush(batch)

    def _flush(self, batch):
        try:
            self._client.notify('log', {'batch': batch})
        except Exception:
            # 连接未就绪 / 已断开时静默丢弃，不影响插件运行
            pass

    def flush_logs(self):
        """同步冲刷剩余日志（宿主关闭前调用，避免丢最后一批）"""
        with self._lock:
            batch, self._buf = self._buf, []
        if batch:
            self._flush(batch)

    def close(self):
        self._stop.set()
        self.flush_logs()
        super().close()
