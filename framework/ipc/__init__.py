# -*- coding: utf-8 -*-
"""
framework.ipc —— 双进程模式跨进程通信层

进程1（核心）：core_runtime.py / ipc_server.py
进程2（宿主）：host_entry.py / ipc_client.py / ipc_adapter.py / remote_db.py / remote_api_caller.py
公共协议：protocol.py

由 config.yaml 的 `dual_process.enabled` 门控；未启用时本包不参与运行，
单进程行为完全不变。
"""
