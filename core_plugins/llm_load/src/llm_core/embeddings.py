"""可插拔的 embedding（向量化）客户端。

设计原则与 providers.py 一致：**不认识任何具体厂商**。这里只认一种方言——
OpenAI 兼容的 ``POST /embeddings``（deepseek / 智谱 / Moonshot / 自建网关 /
OpenAI 自己都说这种），换 base_url 即可；要接别的方言（本地推理服务、
Jina / Cohere 的特殊接口），写一个子类或传一个 embed 函数即可。

向量维度（dims）优先取配置；配置没给时，用首次返回的向量长度自动推断，
这样 BGE-M3（1024）/ text-embedding-3-small（1536）等不同维度都不用手填。
"""
import logging
import os
from typing import List

logger = logging.getLogger('zcbot.llm_core')

__all__ = ['EmbeddingClient', 'EmbeddingError']


class EmbeddingError(Exception):
    """embedding 调用失败（网络 / 鉴权 / 协议不符等）。"""


class EmbeddingClient:
    def __init__(self, base_url, api_key='', model='text-embedding-3-small',
                 dims=0, timeout=60, session=None):
        self.base_url = (base_url or 'https://api.openai.com/v1').rstrip('/')
        self.api_key = api_key or ''
        self.model = model or 'text-embedding-3-small'
        self._dims = int(dims) if dims else 0
        self.timeout = int(timeout) if timeout else 60
        self._session = session

    @classmethod
    def from_config(cls, cfg: dict):
        """从配置字典构造；缺 base_url 时返回 None（视为不启用）。"""
        if not cfg or not isinstance(cfg, dict):
            return None
        base = (cfg.get('base_url') or cfg.get('endpoint') or '').strip()
        if not base:
            return None
        return cls(
            base_url=base,
            api_key=cfg.get('api_key') or os.environ.get('LLM_CORE_EMBED_API_KEY', ''),
            model=cfg.get('model') or 'text-embedding-3-small',
            dims=cfg.get('dims') or 0,
            timeout=cfg.get('timeout') or 60,
        )

    @property
    def dims(self):
        return self._dims

    def embed(self, texts) -> list[list[float]]:
        """把一段或多段文本变成向量。返回与输入同序的向量列表。"""
        import requests

        if isinstance(texts, str):
            texts = [texts]
        try:
            session = self._session or requests.Session()
            resp = session.post(
                f"{self.base_url}/embeddings",
                headers={'Authorization': f'Bearer {self.api_key}',
                         'Content-Type': 'application/json'},
                json={'model': self.model, 'input': texts},
                timeout=self.timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as e:
            raise EmbeddingError(f"embedding 请求失败：{e}") from e
        data = payload.get('data')
        if not isinstance(data, list) or not data:
            raise EmbeddingError("embedding 返回格式异常（缺少 data 数组）")
        vecs = [d.get('embedding') for d in data]
        if any(v is None for v in vecs):
            raise EmbeddingError("embedding 返回项缺少 embedding 向量")
        if self._dims == 0 and vecs:
            self._dims = len(vecs[0])
        return vecs

    async def aembed(self, texts) -> list[list[float]]:
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.embed, texts)
