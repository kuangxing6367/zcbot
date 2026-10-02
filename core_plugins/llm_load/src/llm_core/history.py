# -*- coding: utf-8 -*-
"""
会话历史与窗口治理

每个会话是一条按 OpenAI 格式排布的消息数组，外加一串计数器。会话 key 由
``来源 + 群/私聊 + 用户`` 三元组拼成，所以同一个人在不同群里有各自独立的记忆，
同一个人在同一个群里换了个接入端（机器人账号）也互不串线。

为什么需要「窗口治理」：

    模型收到的是完整历史，历史越长越贵，超过窗口还会直接被服务端拒掉。
    治理策略两级可选：

    truncate    按轮数砍掉最老的对话（默认，零成本、立竿见影）
    summary     到阈值时让模型把历史压成一段摘要，替换掉被压缩的部分
                （保留信息更多，代价是多花一次请求）

无论哪种策略，system 永远保留，且工具调用的「请求 ↔ 结果」成对存在：
assistant 消息里带了 tool_calls 却没有对应 tool 结果，某些服务端会直接报错。
"""
import hashlib
import json
import logging
import os
import re
import time
from collections import OrderedDict
from typing import Dict, List, Optional

logger = logging.getLogger('zcbot.llm_core')

__all__ = ['Conversation', 'ConversationStore', 'SessionPersist',
           'estimate_tokens', 'session_key']

_CJK = re.compile(r'[\u4e00-\u9fff]')
_LATIN = re.compile(r'[A-Za-z]+')


def estimate_tokens(messages: List[dict]) -> int:
    """粗略估算 token 数。

    中文按 1 字 ≈ 1 token，西文按 4 字符 ≈ 1 token。不求精确，只为了在没有
    tokenizer 依赖的情况下决定「该不该压缩」——误差 20% 不影响决策。
    """
    total = 0
    for msg in messages or []:
        text = msg.get('content')
        if isinstance(text, list):  # 多模态内容块
            for part in text:
                if isinstance(part, dict) and part.get('type') == 'text':
                    text = part.get('text', '')
                    break
            else:
                text = ''
        if not isinstance(text, str):
            text = str(text or '')
        total += len(_CJK.findall(text))
        latin = ' '.join(_LATIN.findall(text))
        total += max(1, len(latin) // 4) if latin else 0
        # 每条消息的 role/工具声明都有固定开销
        total += 4
    return total


def session_key(source: str = '', group_id=None, user_id=None) -> str:
    """会话唯一键。群聊时 user_id 不参与（群里是公共上下文）。"""
    gid = str(group_id) if group_id else 'private'
    return f"{source or '-'}:{gid}:{user_id or 0}"


class Conversation:
    """单个会话"""

    def __init__(self, key: str, system_prompt: str = '', max_turns: int = 20,
                 max_items: int = 200):
        self.key = key
        self.system_prompt = system_prompt or ''
        self.max_turns = max(1, int(max_turns))
        self.max_items = max(10, int(max_items))
        self._messages: List[dict] = []
        self.created_at = time.time()
        self.updated_at = time.time()
        self.compressed_times = 0
        self.total_calls = 0
        # 会话级人格覆盖（空 = 用全局 system_prompt）
        self.persona = ''
        # 由 ConversationStore 注入：任何变更写穿到磁盘（None = 不持久化）
        self._persist_cb = None

    def _persist(self) -> None:
        if self._persist_cb is not None:
            try:
                self._persist_cb(self)
            except Exception as e:  # noqa: BLE001 - 持久化失败不影响对话
                logger.debug(f"[history] 会话落盘失败 {self.key}: {e}")

    # ---- 读写 ----

    @property
    def messages(self) -> List[dict]:
        return list(self._messages)

    @property
    def turns(self) -> int:
        """用户轮数（一条 user 消息算一轮）"""
        return sum(1 for m in self._messages if m.get('role') == 'user')

    def append(self, message: dict) -> None:
        self._messages.append(message)
        self.updated_at = time.time()
        self._enforce_limits()
        self._persist()

    def extend(self, messages: List[dict]) -> None:
        self._messages.extend(messages or [])
        self.updated_at = time.time()
        self._enforce_limits()
        self._persist()

    def clear(self) -> None:
        self._messages = []
        self.compressed_times = 0
        self.updated_at = time.time()
        self._persist()

    @property
    def effective_prompt(self) -> str:
        """实际生效的系统提示词：会话人格优先，全局兜底"""
        return self.persona or self.system_prompt

    def as_request(self) -> List[dict]:
        """导出给 provider 的消息数组（system 排头）"""
        out: List[dict] = []
        if self.effective_prompt:
            out.append({'role': 'system', 'content': self.effective_prompt})
        out.extend(self._drop_dangling_tool_calls(self._messages))
        return out

    # ---- 治理 ----

    def _enforce_limits(self) -> None:
        if len(self._messages) > self.max_items:
            keep = self._safe_head(self._messages, self.max_items)
            del self._messages[:len(self._messages) - len(keep)]
        user_count = self.turns
        if user_count > self.max_turns:
            drop = user_count - self.max_turns
            self._messages = self._trim_by_turns(self._messages, drop)

    @staticmethod
    def _trim_by_turns(messages: List[dict], rounds: int) -> List[dict]:
        """砍掉最老的 N 轮 user 及其后续消息（含工具结果）。"""
        result = list(messages)
        dropped = 0
        while dropped < rounds and result:
            # 找到第一条 user 的位置
            start = next((i for i, m in enumerate(result) if m.get('role') == 'user'), None)
            if start is None:
                break
            # 砍掉它到第二条 user 之前（没有第二条就全砍）
            nxt = next((i for i, m in enumerate(result[start + 1:], start + 1)
                        if m.get('role') == 'user'), None)
            end = nxt if nxt is not None else len(result)
            # 工具结果必须跟它的请求一起删，否则留下孤儿 tool 消息
            del result[start:end]
            dropped += 1
        return result

    @staticmethod
    def _drop_dangling_tool_calls(messages: List[dict]) -> List[dict]:
        """清理「有 tool_calls 但没有对应 tool 结果」的 assistant 消息。

        这类孤儿会在历史被截断后出现，服务端（尤其 OpenAI 官方）会直接 400。
        """
        answered = {m.get('tool_call_id') for m in messages if m.get('role') == 'tool'}
        out = []
        for msg in messages:
            calls = msg.get('tool_calls') or []
            if msg.get('role') == 'assistant' and calls:
                ids = [c.get('id') for c in calls if isinstance(c, dict)]
                if ids and not any(i in answered for i in ids):
                    # 兜底：既没有结果也没写 content，整条作废
                    if not msg.get('content'):
                        continue
                    msg = {k: v for k, v in msg.items() if k != 'tool_calls'}
            out.append(msg)
        return out

    @staticmethod
    def _safe_head(messages: List[dict], size: int) -> List[dict]:
        return list(messages[-size:])

    def replace_history(self, messages: List[dict]) -> None:
        """压缩后整体替换（summary 策略用）"""
        self._messages = list(messages or [])
        self.compressed_times += 1
        self.updated_at = time.time()
        self._persist()

    def summary(self) -> dict:
        return {
            'key': self.key,
            'turns': self.turns,
            'messages': len(self._messages),
            'tokens': estimate_tokens(self.as_request()),
            'compressed': self.compressed_times,
            'idle_seconds': int(time.time() - self.updated_at),
        }


class SessionPersist:
    """会话磁盘持久层：每个会话一个 JSON 文件，写入即落盘。

    文件名用会话 key 的 SHA1（key 里可能带任意字符，不冒险）；真实 key 存在
    文件内容里。崩溃/重启后由 ConversationStore 惰性恢复，用户无感。
    """

    def __init__(self, directory: str, retention: int = 7 * 24 * 3600):
        self._dir = directory
        self._retention = max(3600, int(retention))
        try:
            os.makedirs(self._dir, exist_ok=True)
        except Exception as e:  # noqa: BLE001
            logger.error(f"[history] 会话目录创建失败 {self._dir}: {e}")

    def _path(self, key: str) -> str:
        return os.path.join(self._dir, hashlib.sha1(key.encode('utf-8')).hexdigest() + '.json')

    def save(self, conv: 'Conversation') -> None:
        try:
            data = {
                'key': conv.key,
                'saved_at': time.time(),
                'compressed_times': conv.compressed_times,
                'persona': conv.persona,
                'messages': conv._messages,
            }
            tmp = self._path(conv.key) + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, self._path(conv.key))  # 原子替换，避免半截文件
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[history] 会话保存失败 {conv.key}: {e}")

    def load(self, key: str) -> Optional[dict]:
        try:
            with open(self._path(key), 'r', encoding='utf-8') as f:
                data = json.load(f)
            if data.get('key') != key or not isinstance(data.get('messages'), list):
                return None
            return data
        except Exception:  # noqa: BLE001 - 不存在/损坏都当没有
            return None

    def delete(self, key: str) -> None:
        try:
            os.remove(self._path(key))
        except OSError:
            pass

    def clear(self) -> None:
        try:
            for name in os.listdir(self._dir):
                if name.endswith('.json'):
                    try:
                        os.remove(os.path.join(self._dir, name))
                    except OSError:
                        pass
        except OSError:
            pass

    def sweep(self) -> None:
        """清掉超过保留期的会话文件（防磁盘无限涨；与内存 TTL 是两回事）"""
        now = time.time()
        try:
            for name in os.listdir(self._dir):
                if not name.endswith('.json'):
                    continue
                path = os.path.join(self._dir, name)
                try:
                    if now - os.path.getmtime(path) > self._retention:
                        os.remove(path)
                except OSError:
                    pass
        except OSError:
            pass


class ConversationStore:
    """会话仓库：带容量上限的 LRU 容器 + 可选磁盘持久化。

    容量上限是必须的：群机器人可能面对几千个会话，不回收就会一直涨。
    """

    def __init__(self, system_prompt: str = '', max_turns: int = 20,
                 max_items: int = 200, capacity: int = 500, ttl: int = 3600 * 6,
                 persist: Optional[SessionPersist] = None):
        self._system_prompt = system_prompt
        self._max_turns = max_turns
        self._max_items = max_items
        self._capacity = max(10, int(capacity))
        self._ttl = max(60, int(ttl))
        self._persist = persist
        self._sessions: 'OrderedDict[str, Conversation]' = OrderedDict()
        if self._persist is not None:
            self._persist.sweep()

    def configure(self, system_prompt: str = None, max_turns: int = None,
                  max_items: int = None) -> None:
        if system_prompt is not None:
            self._system_prompt = system_prompt
            for conv in self._sessions.values():
                conv.system_prompt = system_prompt
        if max_turns is not None:
            self._max_turns = max(1, int(max_turns))
        if max_items is not None:
            self._max_items = max(10, int(max_items))

    def get(self, key: str, create: bool = True) -> Optional[Conversation]:
        self._gc()
        conv = self._sessions.get(key)
        if conv is not None:
            self._sessions.move_to_end(key)
            return conv
        if not create:
            return None
        conv = Conversation(key, self._system_prompt, self._max_turns, self._max_items)
        self._hydrate(conv)
        self._sessions[key] = conv
        if len(self._sessions) > self._capacity:
            self._sessions.popitem(last=False)
        return conv

    def _hydrate(self, conv: Conversation) -> None:
        """新会话先看磁盘上有没有历史，有就恢复"""
        conv._persist_cb = self._persist.save if self._persist is not None else None
        if self._persist is None:
            return
        data = self._persist.load(conv.key)
        if not data:
            return
        conv._messages = [m for m in data['messages'] if isinstance(m, dict)]
        conv.persona = str(data.get('persona') or '')
        conv.compressed_times = int(data.get('compressed_times') or 0)
        conv._enforce_limits()
        if data.get('saved_at'):
            conv.updated_at = float(data['saved_at'])
            conv.created_at = conv.created_at if conv.created_at < conv.updated_at else conv.updated_at

    def drop(self, key: str) -> bool:
        existed = self._sessions.pop(key, None) is not None
        if self._persist is not None:
            self._persist.delete(key)
        return existed

    def clear_all(self) -> int:
        n = len(self._sessions)
        self._sessions.clear()
        if self._persist is not None:
            self._persist.clear()
        return n

    def keys(self) -> List[str]:
        return list(self._sessions.keys())

    def stats(self) -> Dict[str, object]:
        total_tokens = 0
        for conv in self._sessions.values():
            total_tokens += estimate_tokens(conv.as_request())
        return {
            'sessions': len(self._sessions),
            'capacity': self._capacity,
            'estimated_tokens': total_tokens,
        }

    def _gc(self) -> None:
        """清理过期会话"""
        now = time.time()
        expired = [k for k, v in self._sessions.items()
                   if now - v.updated_at > self._ttl]
        for k in expired:
            self._sessions.pop(k, None)
