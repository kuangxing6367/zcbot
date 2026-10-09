# -*- coding: utf-8 -*-
"""OpenAI 兼容的流式 LLM 客户端（零三方依赖，仅用标准库）。

对应 dsh 的「模型提供商 / provider」一层：把对话历史换成模型回复。
这里刻意不依赖 llm_core，插件自带一份最小实现，开箱即连任意 OpenAI 兼容端点
（DeepSeek / Moonshot / 智谱 / vLLM / Ollama / One-API 等，判断标准只有一条：
能不能用 Bearer + {"messages": [...]} 说话）。
"""
import json
import urllib.error
import urllib.request


class LLMError(Exception):
    pass


def _sse_stream(cfg, messages, tools):
    """向 chat/completions 发流式请求，逐事件 yield。

    yield 形状：
      ("text", str)            文本增量
      ("reason", str)          深度思考 / 推理过程增量（reasoning_content 等）
      ("tool", {"name","arguments"})  完整的一次工具调用（流结束后汇总）
      ("done", None)           流结束
    """
    base = cfg["base_url"].rstrip("/")
    url = base + "/chat/completions"
    payload = {
        "model": cfg["model"],
        "messages": messages,
        "temperature": cfg.get("temperature", 0.7),
        "stream": True,
    }
    if tools:
        payload["tools"] = [{"type": "function", "function": t} for t in tools]
        payload["tool_choice"] = "auto"

    # 深度思考开关：各家字段不统一，这里按需注入常见写法
    if cfg.get("thinking"):
        style = str(cfg.get("thinking_style", "auto") or "auto").lower()
        if style in ("auto", "anthropic", "claude"):
            payload["thinking"] = {"type": "enabled"}
        if style in ("auto", "flag"):
            payload["enable_thinking"] = True
        if style in ("auto", "openai"):
            payload["reasoning_effort"] = str(cfg.get("reasoning_effort", "medium"))

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer %s" % cfg["api_key"],
        },
    )
    try:
        resp = urllib.request.urlopen(req, timeout=cfg.get("timeout", 120))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")[:400]
        raise LLMError("HTTP %s: %s" % (e.code, body))
    except Exception as e:  # 网络层
        raise LLMError("请求失败: %s" % e)

    tool_acc = {}  # index -> {name, arguments}
    for raw in resp:
        line = raw.decode("utf-8", "replace").strip()
        if not line or not line.startswith("data:"):
            continue
        chunk = line[5:].strip()
        if chunk == "[DONE]":
            break
        try:
            obj = json.loads(chunk)
        except Exception:
            continue
        if not obj.get("choices"):
            continue
        delta = obj["choices"][0].get("delta", {})
        # 深度思考 / 推理：各家字段名不统一（DeepSeek=reasoning_content、
        # OpenAI-o 系=reasoning、部分网关=thinking / reasoning_delta）
        reason = (delta.get("reasoning_content")
                  or delta.get("reasoning")
                  or delta.get("thinking")
                  or (delta.get("reasoning_delta") or {}).get("content")
                  or "")
        if reason:
            yield ("reason", reason)
        if delta.get("content"):
            yield ("text", delta["content"])
        for tc in delta.get("tool_calls", []) or []:
            idx = tc.get("index", 0)
            acc = tool_acc.setdefault(idx, {"name": "", "arguments": ""})
            fn = tc.get("function", {}) or {}
            if fn.get("name"):
                acc["name"] += fn["name"]
            if fn.get("arguments"):
                acc["arguments"] += fn["arguments"]
    for acc in tool_acc.values():
        if acc["name"]:
            yield ("tool", dict(acc))
    yield ("done", None)


def stream_chat(cfg, messages, tools=None):
    """公开入口：包一层，便于调用方直接 for kind, val in stream_chat(...)。"""
    return _sse_stream(cfg, messages, tools or [])
