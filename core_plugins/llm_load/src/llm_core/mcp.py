# -*- coding: utf-8 -*-
"""
MCP（Model Context Protocol）客户端

一句话说清 MCP 在这儿的位置：它是**工具的另一个来源**。

函数总线上每个工具都带着 source 字段：

    builtin    llm_core 自带的示例工具
    plugin     插件通过服务注册进来的
    mcp        从 MCP server 拉过来的

对模型而言三者完全一样——都是一份「名字 + 描述 + 参数 schema」。差别只在治理：
MCP 工具如果一个都拉不下来，那台 server 就当没配，不影响对话本身。

这里实现的是客户端里真正会被用到的两个能力：

    tools/list   问 server 有哪些工具（含 inputSchema）
    tools/call   执行某个工具

协议是 JSON-RPC 2.0，两种传输：

    stdio   本地拉起子进程，通过 stdin/stdout 一行一个 JSON
    http    远端 HTTP 端点，POST 请求体为单个 JSON-RPC 对象

刻意没做资源（resources）与提示词（prompts）——它们对聊天机器人没什么用。
"""
import json
import logging
import os
import subprocess
import threading
import time
from typing import Any, Dict, List

from .tools import FunctionTool

logger = logging.getLogger('zcbot.llm_core')

__all__ = ['MCPClient', 'MCPTransportError', 'bind_mcp_tools']

_INIT_TIMEOUT = 15


class MCPTransportError(Exception):
    """与 MCP server 通信失败。"""


class _StdioTransport:
    """本地子进程传输：一行一个 JSON-RPC 对象。"""

    def __init__(self, command: str, args: List[str] = None, env: dict = None,
                 cwd: str = None):
        if not command:
            raise MCPTransportError("MCP stdio 传输缺少 command")
        merged_env = dict(os.environ)
        for k, v in (env or {}).items():
            merged_env[str(k)] = '' if v is None else str(v)
        try:
            self._proc = subprocess.Popen(
                [command] + list(args or []),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                env=merged_env, cwd=cwd or None,
            )
        except FileNotFoundError as e:
            raise MCPTransportError(f"找不到 MCP server 可执行文件: {command}") from e
        except Exception as e:  # noqa: BLE001
            raise MCPTransportError(f"启动 MCP server 失败: {e}") from e
        self._lock = threading.Lock()

    def request(self, payload: dict, timeout: float = _INIT_TIMEOUT) -> dict:
        line = json.dumps(payload, ensure_ascii=False)
        with self._lock:
            if self._proc.poll() is not None:
                raise MCPTransportError("MCP server 已退出")
            try:
                self._proc.stdin.write((line + '\n').encode('utf-8'))
                self._proc.stdin.flush()
            except Exception as e:  # noqa: BLE001
                raise MCPTransportError(f"写入 MCP server 失败: {e}") from e
            deadline = time.time() + timeout
            while time.time() < deadline:
                raw = self._proc.stdout.readline()
                if not raw:
                    raise MCPTransportError("MCP server 关闭了 stdout")
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue  # 心跳 / 日志行，跳过
                # 通知（无 id 且带 method）继续等真正的响应
                if 'id' in msg and str(msg.get('id')) == str(payload.get('id')):
                    return msg
                if 'error' in msg and 'id' in msg:
                    return msg
        raise MCPTransportError("等待 MCP server 响应超时")

    def close(self) -> None:
        if getattr(self, '_proc', None) is not None:
            try:
                self._proc.terminate()
            except Exception:  # noqa: BLE001
                pass


class _HttpTransport:
    """远端 HTTP 传输：POST 一个 JSON-RPC 对象，返回同结构的响应。"""

    def __init__(self, url: str, headers: dict = None, timeout: float = _INIT_TIMEOUT):
        self.url = url
        self.headers = {'Content-Type': 'application/json'}
        for k, v in (headers or {}).items():
            self.headers[str(k)] = str(v)
        self.timeout = timeout

    def request(self, payload: dict, timeout: float = None) -> dict:
        import requests
        try:
            resp = requests.post(self.url, json=payload, headers=self.headers,
                                 timeout=timeout or self.timeout)
        except Exception as e:  # noqa: BLE001
            raise MCPTransportError(f"请求 MCP 端点失败: {e}") from e
        if resp.status_code != 200:
            raise MCPTransportError(f"MCP 端点返回 {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError as e:
            raise MCPTransportError("MCP 端点返回的不是合法 JSON") from e

    def close(self) -> None:
        pass


class MCPClient:
    """一个 MCP server 的客户端连接。"""

    def __init__(self, name: str, transport: str = 'stdio', **cfg):
        self.name = name
        self.transport_kind = (transport or 'stdio').lower()
        self._transport: Any = None
        self._id = 0
        if self.transport_kind == 'stdio':
            self._transport = _StdioTransport(
                cfg.get('command', ''), cfg.get('args') or [],
                cfg.get('env'), cfg.get('cwd'))
        elif self.transport_kind in ('http', 'sse'):
            self._transport = _HttpTransport(
                cfg.get('url', ''), cfg.get('headers'), cfg.get('timeout'))
        else:
            raise MCPTransportError(f"不支持的 MCP 传输方式: {self.transport_kind}")

    # ---- JSON-RPC ----

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _rpc(self, method: str, params: dict = None, timeout: float = None) -> dict:
        payload = {'jsonrpc': '2.0', 'id': self._next_id(), 'method': method}
        if params is not None:
            payload['params'] = params
        msg = self._transport.request(payload, timeout or _INIT_TIMEOUT)
        if msg.get('error'):
            err = msg['error']
            raise MCPTransportError(
                f"MCP 返回错误 {err.get('code')}: {err.get('message')}")
        return msg.get('result') or {}

    def _notify(self, method: str, params: dict = None) -> None:
        payload = {'jsonrpc': '2.0', 'method': method}
        if params is not None:
            payload['params'] = params
        try:
            self._transport.request(payload, timeout=5)
        except MCPTransportError as e:
            logger.debug(f"[mcp:{self.name}] 通知 {method} 未收到响应（可忽略）: {e}")

    def handshake(self) -> None:
        """initialize + initialized。没有这一步，多数 server 不认后续请求。"""
        self._rpc('initialize', {
            'protocolVersion': '2024-11-05',
            'capabilities': {'tools': {}},
            'clientInfo': {'name': 'zcbot-llm-core', 'version': '1.0.0'},
        })
        self._notify('notifications/initialized')

    # ---- 工具 ----

    def list_tools(self) -> List[dict]:
        result = self._rpc('tools/list')
        return list(result.get('tools') or [])

    def call_tool(self, name: str, arguments: dict) -> Any:
        result = self._rpc('tools/call', {'name': name, 'arguments': arguments or {}})
        blocks = result.get('content') or []
        texts = [b.get('text', '') for b in blocks
                 if isinstance(b, dict) and b.get('type') == 'text']
        joined = '\n'.join(t for t in texts if t)
        if not joined and result.get('isError'):
            joined = f"MCP 工具返回错误：{result}"
        return joined or json.dumps(result, ensure_ascii=False)

    def close(self) -> None:
        try:
            self._transport.close()
        except Exception:  # noqa: BLE001
            pass


def bind_mcp_tools(registry, servers: List[dict]) -> Dict[str, int]:
    """把配置里的 MCP server 全部拉起，把它们的工具注册进总线。

    :param registry: ToolRegistry
    :param servers: [{"name": "fs", "transport": "stdio",
                      "command": "npx", "args": [...], "enabled": true}, ...]
    :return: {server 名: 注册了多少个工具}
    """
    summary: Dict[str, int] = {}
    for cfg in servers or []:
        if not isinstance(cfg, dict) or cfg.get('enabled') is False:
            continue
        name = cfg.get('name') or cfg.get('command') or f"mcp-{len(summary)}"
        cfg = dict(cfg)
        cfg.pop('name', None)
        cfg.pop('enabled', None)
        try:
            client = MCPClient(name, **cfg)
            client.handshake()
            tools = client.list_tools()
        except Exception as e:  # noqa: BLE001 - 一台 server 挂掉不能影响整体启用
            logger.error(f"[llm_core] MCP server [{name}] 接入失败: {e}")
            summary[name] = 0
            continue

        count = 0
        for spec in tools:
            tool_name = spec.get('name')
            if not tool_name:
                continue
            schema = spec.get('inputSchema') or {'type': 'object', 'properties': {}}
            # 名字冲突时带上 server 名做前缀，避免两台 server 的通用工具互相覆盖
            final_name = tool_name
            if registry.get(tool_name) is not None:
                final_name = f"{name}_{tool_name}"

            def _make(client_ref, tool_ref):
                def handler(**kwargs):
                    return client_ref.call_tool(tool_ref, kwargs)
                return handler

            handler = _make(client, tool_name)
            registry.register(FunctionTool(
                name=final_name,
                description=spec.get('description') or f"MCP 工具 {tool_name}",
                parameters=schema,
                handler=_make(client, tool_name),
                source='mcp',
                owner=f"mcp:{name}",
                timeout=float(cfg.get('call_timeout') or 30),
            ))
            count += 1
        summary[name] = count
        logger.info(f"[llm_core] MCP server [{name}] 已注册 {count} 个工具")
    return summary
