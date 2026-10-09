# -*- coding: utf-8 -*-
"""会话存储与上下文压缩。

Session：会话历史持久化到磁盘、按轮裁剪、超阈值时把较早的
对话压缩成一段摘要（compaction），使长会话不撑爆上下文窗口。

存储形态刻意保持「纯文本轮」——只落 user / assistant 两条自然语言，
工具调用与工具结果属于「轮内过程」，不跨轮持久化，避免历史里塞满易失效的
tool 消息导致下次请求格式不合法。

多会话：每个会话有独立 key（可用中文名），带元数据（名称 / 创建 / 更新 /
可选的覆盖模型与模式）；界面与命令都可列举、进入、重命名、删除。
文件格式 v2：

    {"version": 2,
     "sessions": {"<key>": {"name", "created", "updated", "model", "mode",
                            "messages": [...]}}}

旧格式（v1，``{"<key>": [messages]}``）在加载时自动迁移。
"""
import json
import os
import time


class SessionStore:
    """按会话键持久化对话历史（含元数据）。"""

    FORMAT_VERSION = 2

    def __init__(self, path, keep_turns=20, max_chars=24000, hard_cap=None):
        self.path = path
        self.keep_turns = max(1, int(keep_turns))
        self.max_chars = max(200, int(max_chars))
        # 硬上限：即便未触发压缩，也不让历史无限膨胀（压缩按需在此之前生效）。
        # 默认 = keep_turns × 20（且不低于 200）；可用 session_hard_cap 显式指定。
        self.hard_cap = int(hard_cap) if hard_cap else max(self.keep_turns * 20, 200)
        self.hard_cap = max(10, self.hard_cap)
        self._data = {}
        self.load()

    # ── 持久化 ─────────────────────────────────────────────────────────
    def load(self):
        raw = {}
        if os.path.exists(self.path):
            try:
                with open(self.path, encoding='utf-8') as f:
                    raw = json.load(f) or {}
            except Exception:
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        if raw.get('version') == self.FORMAT_VERSION and isinstance(raw.get('sessions'), dict):
            self._data = raw['sessions']
        else:
            # v1 迁移：{key: [messages]} → v2
            now = int(time.time())
            self._data = {}
            for key, msgs in raw.items():
                if not isinstance(msgs, list):
                    continue
                self._data[key] = {'name': key, 'created': now, 'updated': now,
                                   'model': '', 'mode': '',
                                   'messages': self._text_turns(msgs)}

    def save(self):
        d = os.path.dirname(self.path)
        if d:
            os.makedirs(d, exist_ok=True)
        payload = {'version': self.FORMAT_VERSION, 'sessions': self._data}
        tmp = self.path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    # ── 会话元数据 ─────────────────────────────────────────────────────
    def _rec(self, key, create=False):
        rec = self._data.get(key)
        if rec is None and create:
            now = int(time.time())
            rec = {'name': key, 'created': now, 'updated': now,
                   'model': '', 'mode': '', 'messages': []}
            self._data[key] = rec
        return rec

    def _ensure(self, key):
        rec = self._rec(key, create=True)
        rec.setdefault('messages', [])
        rec.setdefault('name', key)
        return rec

    def ensure(self, key, name=None):
        rec = self._ensure(key)
        if name:
            rec['name'] = name
        return rec

    def exists(self, key):
        return key in self._data

    def sessions(self):
        """列出全部会话（按最近更新倒序）。"""
        out = []
        for key, rec in self._data.items():
            msgs = rec.get('messages') or []
            out.append({'key': key, 'name': rec.get('name') or key,
                        'created': rec.get('created', 0), 'updated': rec.get('updated', 0),
                        'model': rec.get('model', ''), 'mode': rec.get('mode', ''),
                        'turns': len(msgs) // 2,
                        'chars': sum(len(m.get('content') or '') for m in msgs)})
        out.sort(key=lambda x: x['updated'], reverse=True)
        return out

    def meta(self, key):
        rec = self._rec(key)
        if rec is None:
            return {}
        return {'name': rec.get('name') or key, 'model': rec.get('model', ''),
                'mode': rec.get('mode', '')}

    def set_meta(self, key, name=None, model=None, mode=None):
        rec = self._ensure(key)
        if name is not None and str(name).strip():
            rec['name'] = str(name).strip()
        if model is not None:
            rec['model'] = str(model).strip()
        if mode is not None:
            rec['mode'] = str(mode).strip()
        rec['updated'] = int(time.time())
        return rec

    def rename(self, key, new_name):
        return self.set_meta(key, name=new_name)['name']

    def new_key(self, name, base='会话'):
        """按名称生成唯一 key（避免与现有会话撞车）。"""
        name = (name or '').strip() or base
        if not self.exists(name):
            return name
        i = 2
        while self.exists('%s-%d' % (name, i)):
            i += 1
        return '%s-%d' % (name, i)

    def delete(self, key):
        return self._data.pop(key, None) is not None

    # ── 读写 ───────────────────────────────────────────────────────────
    def history(self, key):
        rec = self._rec(key)
        return list((rec or {}).get('messages') or [])

    def clear(self, key):
        """清空某个会话的消息（保留会话本身与元数据）。"""
        if key in self._data:
            self._data[key]['messages'] = []
            self._data[key]['updated'] = int(time.time())

    def append_turn(self, key, user_text, assistant_text):
        """追加一轮（user + assistant）。

        只保留文本轮、并受硬上限约束；「保留最近若干轮」交给 compact 按需处理
        （否则每次追加都裁到 keep_turns*2，压缩永远触发不了）。
        """
        rec = self._ensure(key)
        hist = list(rec.get('messages') or [])
        hist.append({'role': 'user', 'content': user_text})
        hist.append({'role': 'assistant', 'content': assistant_text})
        clean = self._text_turns(hist)
        if len(clean) > self.hard_cap:
            clean = clean[-self.hard_cap:]
        rec['messages'] = clean
        rec['updated'] = int(time.time())
        return clean

    # ── 裁剪 ───────────────────────────────────────────────────────────
    @staticmethod
    def _text_turns(messages):
        """只保留 user / assistant 文本轮（丢弃 tool 角色与空内容）。"""
        return [m for m in messages
                if m.get('role') in ('user', 'assistant') and isinstance(m.get('content'), str)]

    # ── 组装请求消息 ───────────────────────────────────────────────────
    def build_messages(self, key, system_prompt, user_text):
        """system 提示 + 历史 + 本轮 user。返回全新列表（不改动存储）。"""
        msgs = [{'role': 'system', 'content': system_prompt}]
        msgs.extend(self.history(key))
        msgs.append({'role': 'user', 'content': user_text})
        return msgs

    # ── 压缩 ───────────────────────────────────────────────────────────
    def needs_compaction(self, key):
        hist = (self._rec(key) or {}).get('messages') or []
        size = sum(len(m.get('content') or '') for m in hist)
        return size > self.max_chars

    def compact(self, key, summarize):
        """把较早的对话压成一段摘要（保留最近若干轮原文）。

        :param summarize: 回调 (older_messages) -> str；返回摘要文本。
            由调用方注入（内部走一次 LLM 调用），便于离线测试与降级。
        """
        rec = self._rec(key)
        if rec is None:
            return []
        hist = rec.get('messages') or []
        if len(hist) <= self.keep_turns * 2:
            return hist
        keep = hist[-self.keep_turns * 2:]
        older = hist[:-self.keep_turns * 2]
        try:
            summary = summarize(older)
        except Exception:
            # 摘要失败则退化为纯裁剪（丢最早部分），不阻断会话
            rec['messages'] = keep
            return keep
        merged = [{'role': 'assistant',
                   'content': '（较早对话的摘要）%s' % (summary or '').strip()}]
        rec['messages'] = merged + keep
        rec['updated'] = int(time.time())
        return rec['messages']
