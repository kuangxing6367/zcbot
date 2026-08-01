"""
本地 FaissVecDB 实现
====================

替代 AstrBot 的 FaissVecDB，使用：
- sqlite3 (同步) 用于文档存储
- faiss 用于向量索引
- requests 调用 OpenAI 兼容 Embedding API

适配目标框架的 ctx.get_config() 配置系统。
"""

import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Result:
    """检索结果"""
    similarity: float
    data: dict


class FaissVecDB:
    """向量数据库 - 本地实现（替代 AstrBot FaissVecDB）"""

    def __init__(
        self,
        doc_store_path: str,
        index_store_path: str,
        embedding_provider: Any = None,
        ctx: Any = None,
    ):
        """
        初始化

        Args:
            doc_store_path: SQLite 文档存储路径
            index_store_path: FAISS 索引文件路径
            embedding_provider: 兼容 EmbeddingProvider 接口的对象（可选）
            ctx: 目标框架上下文（用于获取 embedding API 配置）
        """
        self.doc_store_path = doc_store_path
        self.index_store_path = index_store_path
        self.embedding_provider = embedding_provider
        self.ctx = ctx
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._index = None
        self._dimension = 0
        self._initialized = False

    @property
    def document_storage(self):
        """获取文档存储对象（兼容 AstrBot 接口）"""
        return DocumentStorageAccessor(self)

    async def initialize(self):
        """初始化数据库和 FAISS 索引"""
        with self._lock:
            if self._initialized:
                return

            # 初始化 SQLite
            os.makedirs(os.path.dirname(self.doc_store_path) or ".", exist_ok=True)
            self._conn = sqlite3.connect(self.doc_store_path, check_same_thread=False)
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS documents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    doc_id TEXT UNIQUE NOT NULL,
                    text TEXT NOT NULL,
                    metadata TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            self._conn.commit()

            # 初始化 FAISS 索引
            import faiss
            import numpy as np

            if os.path.exists(self.index_store_path) and os.path.getsize(self.index_store_path) > 0:
                try:
                    self._index = faiss.read_index(self.index_store_path)
                    self._dimension = self._index.d
                except Exception:
                    self._index = None

            if self._index is None:
                # 获取维度
                if self.embedding_provider and hasattr(self.embedding_provider, "get_dim"):
                    self._dimension = self.embedding_provider.get_dim()
                elif self.ctx:
                    try:
                        from plugins.livingmemory.core.llm_api import get_embedding_dim
                        loop = asyncio.get_event_loop()
                        self._dimension = loop.run_until_complete(get_embedding_dim(self.ctx))
                    except Exception:
                        self._dimension = 1536  # 默认 text-embedding-ada-002 维度

                if self._dimension <= 0:
                    self._dimension = 1536

                self._index = faiss.IndexIDMap(faiss.IndexFlatL2(self._dimension))

            self._initialized = True

    def _get_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("FaissVecDB 未初始化")
        return self._conn

    async def _get_embedding_vector(self, text: str) -> list[float]:
        """获取文本的 embedding 向量"""
        if self.embedding_provider and hasattr(self.embedding_provider, "get_embedding"):
            vector = await self.embedding_provider.get_embedding(text)
            return vector
        elif self.ctx:
            from plugins.livingmemory.core.llm_api import get_embedding
            return await get_embedding(self.ctx, text)
        else:
            raise RuntimeError("无法获取 Embedding：未提供 embedding_provider 或 ctx")

    async def _get_embeddings_batch(self, texts: list[str]) -> list[list[float]]:
        """批量获取 embedding 向量"""
        if self.embedding_provider and hasattr(self.embedding_provider, "get_embeddings_batch"):
            return await self.embedding_provider.get_embeddings_batch(texts)
        elif self.ctx:
            from plugins.livingmemory.core.llm_api import get_embeddings_batch
            return await get_embeddings_batch(self.ctx, texts)
        else:
            result = []
            for text in texts:
                result.append(await self._get_embedding_vector(text))
            return result

    async def insert(
        self,
        content: str,
        metadata: dict | None = None,
        id: str | None = None,
    ) -> int:
        """插入一条文档"""
        import numpy as np

        metadata = metadata or {}
        str_id = id or str(uuid.uuid4())

        vector = await self._get_embedding_vector(content)
        vector = np.array(vector, dtype=np.float32)

        with self._lock:
            conn = self._get_connection()
            cursor = conn.execute(
                "INSERT INTO documents (doc_id, text, metadata) VALUES (?, ?, ?)",
                (str_id, content, json.dumps(metadata, ensure_ascii=False)),
            )
            int_id = cursor.lastrowid
            conn.commit()

            self._index.add_with_ids(
                vector.reshape(1, -1), np.array([int_id], dtype=np.int64)
            )

        return int_id

    async def insert_batch(
        self,
        contents: list[str],
        metadatas: list[dict] | None = None,
        ids: list[str] | None = None,
        batch_size: int = 32,
        tasks_limit: int = 3,
        max_retries: int = 3,
        progress_callback=None,
    ) -> list[int]:
        """批量插入文档"""
        import numpy as np

        metadatas = metadatas or [{} for _ in contents]
        ids = ids or [str(uuid.uuid4()) for _ in contents]

        if not contents:
            return []

        vectors = await self._get_embeddings_batch(contents)
        if len(vectors) != len(contents):
            raise RuntimeError(
                f"Embedding 返回数量不匹配: 期望 {len(contents)}，实际 {len(vectors)}"
            )

        int_ids = []
        with self._lock:
            conn = self._get_connection()
            for i, content in enumerate(contents):
                cursor = conn.execute(
                    "INSERT INTO documents (doc_id, text, metadata) VALUES (?, ?, ?)",
                    (ids[i], content, json.dumps(metadatas[i], ensure_ascii=False)),
                )
                int_ids.append(cursor.lastrowid)

            conn.commit()

            vectors_array = np.asarray(vectors, dtype=np.float32)
            self._index.add_with_ids(
                vectors_array, np.array(int_ids, dtype=np.int64)
            )

        return int_ids

    async def retrieve(
        self,
        query: str,
        k: int = 5,
        fetch_k: int = 20,
        rerank: bool = False,
        metadata_filters: dict | None = None,
    ) -> list[Result]:
        """搜索最相似的文档"""
        import numpy as np

        embedding = await self._get_embedding_vector(query)
        query_vector = np.array([embedding], dtype=np.float32)

        with self._lock:
            scores, indices = self._index.search(query_vector, fetch_k)

        if len(indices[0]) == 0 or indices[0][0] == -1:
            return []

        # 归一化分数
        scores[0] = 1.0 - (scores[0] / 2.0)

        # 获取文档
        valid_ids = [int(idx) for idx in indices[0] if idx != -1]
        if not valid_ids:
            return []

        with self._lock:
            conn = self._get_connection()
            placeholders = ",".join("?" for _ in valid_ids)
            rows = conn.execute(
                f"SELECT id, doc_id, text, metadata FROM documents WHERE id IN ({placeholders})",
                valid_ids,
            ).fetchall()

        doc_map = {row[0]: row for row in rows}

        results = []
        for i, idx in enumerate(indices[0]):
            if idx == -1:
                continue
            int_idx = int(idx)
            doc = doc_map.get(int_idx)
            if doc is None:
                continue

            doc_metadata = {}
            if doc[3]:
                try:
                    doc_metadata = json.loads(doc[3])
                except (json.JSONDecodeError, TypeError):
                    doc_metadata = {}

            # 应用 metadata 过滤
            if metadata_filters:
                matched = True
                for key, value in metadata_filters.items():
                    if doc_metadata.get(key) != value:
                        matched = False
                        break
                if not matched:
                    continue

            results.append(
                Result(
                    similarity=float(scores[0][i]),
                    data={
                        "id": int_idx,
                        "doc_id": doc[1],
                        "text": doc[2],
                        "metadata": doc_metadata,
                    },
                )
            )

            if len(results) >= k:
                break

        return results

    async def delete(self, doc_id: str) -> None:
        """删除文档"""
        with self._lock:
            conn = self._get_connection()
            result = conn.execute(
                "SELECT id FROM documents WHERE doc_id = ?", (doc_id,)
            ).fetchone()
            if result is None:
                return

            int_id = result[0]
            conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
            conn.commit()

            try:
                import numpy as np
                import faiss
                id_selector = faiss.IDSelectorArray([int_id])
                self._index.remove_ids(id_selector)
            except Exception:
                pass

    async def close(self):
        """关闭数据库"""
        with self._lock:
            if self._index is not None:
                import faiss
                faiss.write_index(self._index, self.index_store_path)
            if self._conn:
                self._conn.close()
                self._conn = None

    async def count_documents(self, metadata_filter: dict | None = None) -> int:
        """计算文档数量"""
        with self._lock:
            conn = self._get_connection()
            if metadata_filter:
                return conn.execute(
                    "SELECT COUNT(*) FROM documents WHERE metadata LIKE ?",
                    (f"%{json.dumps(metadata_filter, ensure_ascii=False)[1:-1]}%",),
                ).fetchone()[0]
            return conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]

    async def delete_documents(self, metadata_filters: dict) -> None:
        """根据元数据过滤器删除文档"""
        with self._lock:
            conn = self._get_connection()
            docs = conn.execute(
                "SELECT id, doc_id FROM documents WHERE metadata LIKE ?",
                (f"%{json.dumps(metadata_filters, ensure_ascii=False)[1:-1]}%",),
            ).fetchall()

            for doc in docs:
                int_id, doc_id_val = doc[0], doc[1]
                conn.execute("DELETE FROM documents WHERE id = ?", (int_id,))
                try:
                    import faiss
                    import numpy as np
                    id_selector = faiss.IDSelectorArray([int_id])
                    self._index.remove_ids(id_selector)
                except Exception:
                    pass

            conn.commit()

    def save_index(self):
        """保存 FAISS 索引到磁盘"""
        if self._index is not None:
            import faiss
            os.makedirs(os.path.dirname(self.index_store_path) or ".", exist_ok=True)
            faiss.write_index(self._index, self.index_store_path)


class DocumentStorageAccessor:
    """文档存储访问器 - 兼容 AstrBot 的 FaissVecDB.document_storage 接口"""

    def __init__(self, db: FaissVecDB):
        self._db = db

    async def get_documents(
        self,
        offset: int = 0,
        limit: int = 100,
        metadata_filters: dict | None = None,
        ids: list[int] | None = None,
    ) -> list[dict]:
        """获取文档列表"""
        with self._db._lock:
            conn = self._db._get_connection()
            if ids:
                placeholders = ",".join("?" for _ in ids)
                rows = conn.execute(
                    f"SELECT id, doc_id, text, metadata, created_at FROM documents "
                    f"WHERE id IN ({placeholders}) ORDER BY id",
                    ids,
                ).fetchall()
            elif metadata_filters:
                like_pattern = "%" + json.dumps(metadata_filters, ensure_ascii=False)[1:-1] + "%"
                rows = conn.execute(
                    "SELECT id, doc_id, text, metadata, created_at FROM documents "
                    "WHERE metadata LIKE ? ORDER BY id LIMIT ? OFFSET ?",
                    (like_pattern, limit, offset),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, doc_id, text, metadata, created_at FROM documents "
                    "ORDER BY id LIMIT ? OFFSET ?",
                    (limit, offset),
                ).fetchall()

        result = []
        for row in rows:
            doc_metadata = {}
            if row[3]:
                try:
                    doc_metadata = json.loads(row[3])
                except (json.JSONDecodeError, TypeError):
                    doc_metadata = {}
            result.append({
                "id": row[0],
                "doc_id": row[1],
                "text": row[2],
                "metadata": doc_metadata,
                "created_at": row[4] if len(row) > 4 else None,
            })
        return result

    async def count_documents(self, metadata_filters: dict | None = None) -> int:
        """计算文档数量"""
        return await self._db.count_documents(metadata_filters)

    async def get_session(self):
        """获取会话（兼容接口）"""
        return None

    async def get_document_by_doc_id(self, doc_id: str) -> dict | None:
        """根据 doc_id 获取文档"""
        with self._db._lock:
            conn = self._db._get_connection()
            row = conn.execute(
                "SELECT id, doc_id, text, metadata FROM documents WHERE doc_id = ?",
                (doc_id,),
            ).fetchone()
            if not row:
                return None
            doc_metadata = {}
            if row[3]:
                try:
                    doc_metadata = json.loads(row[3])
                except (json.JSONDecodeError, TypeError):
                    doc_metadata = {}
            return {
                "id": row[0],
                "doc_id": row[1],
                "text": row[2],
                "metadata": doc_metadata,
            }

    async def delete_document_by_doc_id(self, doc_id: str) -> None:
        """根据 doc_id 删除文档"""
        with self._db._lock:
            conn = self._db._get_connection()
            conn.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
            conn.commit()