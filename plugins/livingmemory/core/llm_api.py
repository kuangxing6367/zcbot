"""
LLM 与 Embedding API 调用辅助模块
===================================

适配目标框架，通过直接调用 OpenAI 兼容 API 替代 AstrBot Provider。
配置从 ctx.get_config() 读取：
- api_base: OpenAI 兼容 API 地址
- api_key: API Key
- model: LLM 模型名
- embedding_model: Embedding 模型名

用法：
    from plugins.livingmemory.core.llm_api import call_llm, get_embedding, get_embedding_dim
    result = await call_llm(ctx, prompt, system_prompt)
    vector = await get_embedding(ctx, text)
    dim = await get_embedding_dim(ctx)
"""

import json
import time
from typing import Any

import numpy as np
import requests


def _get_config(ctx) -> dict:
    """获取 LLM 配置"""
    return {
        "api_base": ctx.get_config("api_base", "https://api.openai.com/v1"),
        "api_key": ctx.get_config("api_key", ""),
        "model": ctx.get_config("model", "gpt-3.5-turbo"),
        "embedding_model": ctx.get_config("embedding_model", "text-embedding-ada-002"),
        "max_tokens": ctx.get_config("max_tokens", 2048),
        "temperature": ctx.get_config("temperature", 0.8),
    }


async def call_llm(
    ctx: Any,
    prompt: str,
    system_prompt: str | None = None,
    max_retries: int = 3,
) -> str:
    """
    调用 OpenAI 兼容 LLM API

    Args:
        ctx: 目标框架上下文
        prompt: 用户提示词
        system_prompt: 系统提示词（可选）
        max_retries: 最大重试次数

    Returns:
        str: LLM 返回的文本
    """
    cfg = _get_config(ctx)
    url = cfg["api_base"].rstrip("/") + "/chat/completions"

    headers = {"Content-Type": "application/json"}
    if cfg["api_key"]:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"

    messages = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": prompt})

    payload = {
        "model": cfg["model"],
        "messages": messages,
        "max_tokens": cfg["max_tokens"],
        "temperature": cfg["temperature"],
    }

    last_error = None
    for attempt in range(max_retries):
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=120)
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                raise RuntimeError("LLM 返回空 choices")
            content = choices[0].get("message", {}).get("content", "")
            return content
        except requests.exceptions.Timeout as e:
            last_error = f"LLM 请求超时: {e}"
        except requests.exceptions.HTTPError as e:
            body = ""
            try:
                body = resp.text[:500]
            except Exception:
                pass
            last_error = f"LLM HTTP 错误: {e} | body={body}"
        except Exception as e:
            last_error = f"LLM 调用异常: {e}"

        if attempt < max_retries - 1:
            wait = (2 ** attempt) + 0.5
            import asyncio
            await asyncio.sleep(wait)

    raise RuntimeError(f"LLM 调用失败（{max_retries} 次重试后）: {last_error}")


async def get_embedding(ctx: Any, text: str) -> list[float]:
    """
    获取文本的 Embedding 向量

    Args:
        ctx: 目标框架上下文
        text: 输入文本

    Returns:
        list[float]: Embedding 向量
    """
    cfg = _get_config(ctx)
    url = cfg["api_base"].rstrip("/") + "/embeddings"

    headers = {"Content-Type": "application/json"}
    if cfg["api_key"]:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"

    payload = {
        "input": text,
        "model": cfg["embedding_model"],
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        vector = data["data"][0]["embedding"]
        return vector
    except Exception as e:
        raise RuntimeError(f"获取 Embedding 失败: {e}")


async def get_embeddings_batch(
    ctx: Any,
    texts: list[str],
    batch_size: int = 8,
    max_retries: int = 3,
    progress_callback=None,
) -> list[list[float]]:
    """
    批量获取文本的 Embedding 向量

    Args:
        ctx: 目标框架上下文
        texts: 输入文本列表
        batch_size: 每批处理数量
        max_retries: 最大重试次数
        progress_callback: 进度回调 (current, total)

    Returns:
        list[list[float]]: Embedding 向量列表
    """
    cfg = _get_config(ctx)
    url = cfg["api_base"].rstrip("/") + "/embeddings"

    headers = {"Content-Type": "application/json"}
    if cfg["api_key"]:
        headers["Authorization"] = f"Bearer {cfg['api_key']}"

    all_vectors: list[list[float]] = []
    total = len(texts)

    for i in range(0, total, batch_size):
        batch = texts[i:i + batch_size]

        last_error = None
        for attempt in range(max_retries):
            try:
                payload = {
                    "input": batch,
                    "model": cfg["embedding_model"],
                }
                resp = requests.post(url, headers=headers, json=payload, timeout=120)
                resp.raise_for_status()
                data = resp.json()

                # 按输入顺序排序
                sorted_data = sorted(data["data"], key=lambda x: x["index"])
                vectors = [item["embedding"] for item in sorted_data]
                all_vectors.extend(vectors)

                if progress_callback:
                    progress_callback(min(i + batch_size, total), total)

                last_error = None
                break
            except Exception as e:
                last_error = str(e)
                if attempt < max_retries - 1:
                    import asyncio
                    await asyncio.sleep(2 ** attempt)

        if last_error:
            raise RuntimeError(
                f"批量获取 Embedding 失败（批次 {i}-{i + batch_size}）: {last_error}"
            )

    return all_vectors


async def get_embedding_dim(ctx: Any) -> int:
    """
    获取 Embedding 向量的维度

    通过发送一个测试文本获取维度

    Args:
        ctx: 目标框架上下文

    Returns:
        int: Embedding 维度
    """
    vector = await get_embedding(ctx, "test")
    return len(vector)