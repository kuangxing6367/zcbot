# -*- coding: utf-8 -*-
"""本地知识库（RAG）核心。

存储：复用单个 SQLite 文件（放在插件 data 目录），把「文档 / 切片 / 向量 /
全文索引」都收进同一个库，单文件即可迁移备份。

向量后端（backend）：
  - 'sqlite_vec'：用 sqlite-vec 扩展的 vec0 虚拟表，向量真落进 SQLite，
    零额外服务。环境装不上扩展时自动退到 'memory'。
  - 'memory'：向量以 JSON 存进普通表，检索时用 numpy（装了的话）或纯 Python
    暴力算余弦。bot 规模（几千切片）足够快，且完全不依赖原生扩展。

检索：混合检索（hybrid）= 向量语义召回 + FTS5 trigram 关键词召回（BM25），
再用倒数排名融合（RRF）合并。trigram 对中文按字做子串匹配，不需要分词，
但查询少于 3 个字时 FTS5 不生效，此时只走向量。
"""
import json
import time
import struct
import logging
import sqlite3
from typing import Callable, Dict, List, Optional

logger = logging.getLogger('zcbot.llm_core')

try:
    import sqlite_vec
    _HAS_SQLITE_VEC = True
except Exception:  # noqa: BLE001
    sqlite_vec = None
    _HAS_SQLITE_VEC = False

try:
    import numpy as np
    _HAS_NUMPY = True
except Exception:  # noqa: BLE001
    np = None
    _HAS_NUMPY = False


def _vec_blob(vec: List[float]) -> bytes:
    """把向量打包成 sqlite-vec 需要的 float 二进制 blob。"""
    return struct.pack(f"{len(vec)}f", *vec)


def _split_text(text: str, size: int = 400, overlap: int = 80) -> List[str]:
    """按段落优先、再按长度滑窗切分，保证碎片不丢上下文。"""
    text = (text or '').strip()
    if not text:
        return []
    chunks: List[str] = []
    buf = ''
    step = max(1, size - overlap)
    for para in text.split('\n'):
        para = para.rstrip()
        if not para:
            if buf:
                chunks.append(buf)
                buf = ''
            continue
        if len(buf) + len(para) + 1 <= size:
            buf = (buf + '\n' + para) if buf else para
        else:
            if buf:
                chunks.append(buf)
            if len(para) <= size:
                buf = para
            else:
                for i in range(0, len(para), step):
                    chunks.append(para[i:i + size])
                buf = ''
    if buf:
        chunks.append(buf)
    return [c.strip() for c in chunks if c.strip()]


def _cosine(a: List[float], b: List[float]) -> float:
    if _HAS_NUMPY:
        a = np.asarray(a, dtype=np.float32)
        b = np.asarray(b, dtype=np.float32)
        denom = float(np.linalg.norm(a)) * float(np.linalg.norm(b))
        return 0.0 if denom == 0 else float(np.dot(a, b) / denom)
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return 0.0 if (na == 0 or nb == 0) else dot / (na * nb)


def _rrf(rank_lists: List[List], k: int = 60) -> Dict[int, float]:
    """倒数排名融合：每个有序列表按 1/(k+rank) 累加，返回 {id: score}。"""
    scores: Dict[int, float] = {}
    for lst in rank_lists:
        for rank, item in enumerate(lst):
            cid = item[0] if isinstance(item, (list, tuple)) else item
            scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank + 1)
    return scores


class KnowledgeBase:
    def __init__(self, db_path, embed: Callable[[List[str]], List[List[float]]],
                 dims: int = 0, chunk_size: int = 400, chunk_overlap: int = 80,
                 backend: str = 'auto'):
        self.db_path = db_path
        self.embed = embed
        if not callable(self.embed):
            raise ValueError("KnowledgeBase 需要一个 callable 作为 embed")
        self.chunk_size = int(chunk_size) if chunk_size else 400
        self.chunk_overlap = int(chunk_overlap) if chunk_overlap else 80
        self._dims = int(dims) if dims else 0
        self._vec_ready = self._dims > 0

        if backend == 'auto':
            backend = 'sqlite_vec' if _HAS_SQLITE_VEC else 'memory'
        if backend == 'sqlite_vec' and not _HAS_SQLITE_VEC:
            logger.warning("sqlite_vec 不可用，退回 memory 向量后端")
            backend = 'memory'
        self.backend = backend

        self._conn = sqlite3.connect(db_path)
        self._conn.row_factory = sqlite3.Row
        if self.backend == 'sqlite_vec':
            try:
                self._conn.enable_load_extension(True)
                self._conn.load_extension(sqlite_vec.loadable_path())
            except Exception as e:  # noqa: BLE001
                logger.warning(f"加载 sqlite_vec 失败，退回 memory: {e}")
                self.backend = 'memory'
        self._init_schema()

    # ── schema ───────────────────────────────────────────────
    def _init_schema(self):
        c = self._conn
        c.executescript("""
        CREATE TABLE IF NOT EXISTS kb_docs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            source TEXT,
            meta TEXT,
            created_at REAL
        );
        CREATE TABLE IF NOT EXISTS kb_chunks(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_id INTEGER,
            idx INTEGER,
            text TEXT,
            created_at REAL
        );
        CREATE INDEX IF NOT EXISTS idx_chunk_doc ON kb_chunks(doc_id);
        CREATE TABLE IF NOT EXISTS kb_vec_mem(
            rowid INTEGER PRIMARY KEY,
            vec TEXT
        );
        """)
        try:
            c.execute("CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(text, tokenize='trigram')")
            self._has_fts = True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"FTS5 不可用，关键词召回关闭: {e}")
            self._has_fts = False
        if self.backend == 'sqlite_vec' and self._vec_ready:
            c.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS kb_vec USING vec0(embedding float[{self._dims}])")
        c.commit()

    @property
    def dims(self) -> int:
        """向量维度（配置给定或首次入库后推断；0 表示尚未确定）。"""
        return self._dims

    # ── 入库 ─────────────────────────────────────────────────
    def add_text(self, title, text, source='', meta=None) -> int:
        from .embeddings import EmbeddingError
        chunks = _split_text(text, self.chunk_size, self.chunk_overlap)
        if not chunks:
            return 0
        try:
            vecs = self.embed(chunks)
        except EmbeddingError:
            raise
        except Exception as e:  # noqa: BLE001
            raise EmbeddingError(f"向量化失败：{e}") from e
        if len(vecs) != len(chunks):
            raise EmbeddingError(f"embedding 返回 {len(vecs)} 条，期望 {len(chunks)} 条")
        if self._dims == 0 and vecs:
            self._dims = len(vecs[0])
            self._vec_ready = True
            if self.backend == 'sqlite_vec':
                self._conn.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS kb_vec USING vec0(embedding float[{self._dims}])")
        c = self._conn
        now = time.time()
        cur = c.execute("INSERT INTO kb_docs(title, source, meta, created_at) VALUES(?,?,?,?)",
                        (title, source or '', json.dumps(meta or {}), now))
        doc_id = cur.lastrowid
        for i, (ch, vec) in enumerate(zip(chunks, vecs)):
            cur = c.execute("INSERT INTO kb_chunks(doc_id, idx, text, created_at) VALUES(?,?,?,?)",
                            (doc_id, i, ch, now))
            cid = cur.lastrowid
            if self.backend == 'sqlite_vec':
                c.execute("INSERT INTO kb_vec(rowid, embedding) VALUES(?,?)", (cid, _vec_blob(vec)))
            else:
                c.execute("INSERT INTO kb_vec_mem(rowid, vec) VALUES(?,?)", (cid, json.dumps(vec)))
            if self._has_fts:
                c.execute("INSERT INTO kb_fts(rowid, text) VALUES(?,?)", (cid, ch))
        c.commit()
        return doc_id

    def add_file(self, path: str, title: str = None, source: str = None) -> int:
        with open(path, 'r', encoding='utf-8', errors='ignore') as f:
            text = f.read()
        return self.add_text(title or (source or path).split('/')[-1], text, source=source or path)

    # ── 检索 ─────────────────────────────────────────────────
    def search(self, query: str, top_k: int = 5) -> List[Dict]:
        from .embeddings import EmbeddingError
        query = (query or '').strip()
        if not query:
            return []
        try:
            qvec = self.embed([query])[0]
        except EmbeddingError:
            raise
        except Exception as e:  # noqa: BLE001
            raise EmbeddingError(f"查询向量化失败：{e}") from e
        vec_rank = self._vector_search(qvec, top_k * 3)
        fts_rank = (self._fts_search(query, top_k * 3)
                    if (self._has_fts and len(query) >= 3) else [])
        fused = _rrf([vec_rank, fts_rank])
        ordered = sorted(fused.items(), key=lambda kv: kv[1], reverse=True)[:top_k]
        out: List[Dict] = []
        for cid, score in ordered:
            row = self._conn.execute(
                "SELECT c.text, c.doc_id, c.idx, d.title, d.source "
                "FROM kb_chunks c JOIN kb_docs d ON d.id=c.doc_id WHERE c.id=?",
                (cid,)).fetchone()
            if row:
                out.append({
                    'chunk_id': cid, 'doc_id': row['doc_id'], 'idx': row['idx'],
                    'text': row['text'], 'title': row['title'], 'source': row['source'],
                    'score': round(score, 5),
                })
        return out

    def _vector_search(self, qvec, limit) -> List:
        if self.backend == 'sqlite_vec' and self._vec_ready:
            try:
                rows = self._conn.execute(
                    "SELECT rowid, distance FROM kb_vec WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
                    (_vec_blob(qvec), limit)).fetchall()
                return [(r['rowid'], r['distance']) for r in rows]
            except Exception as e:  # noqa: BLE001
                logger.warning(f"vec0 检索失败，退回内存检索: {e}")
        rows = self._conn.execute("SELECT rowid, vec FROM kb_vec_mem").fetchall()
        scored = [(r['rowid'], _cosine(qvec, json.loads(r['vec']))) for r in rows]
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:limit]

    def _fts_search(self, query, limit) -> List:
        q = '"' + query.replace('"', '') + '"'
        rows = self._conn.execute(
            "SELECT rowid FROM kb_fts WHERE kb_fts MATCH ? ORDER BY rank LIMIT ?",
            (q, limit)).fetchall()
        return [r['rowid'] for r in rows]

    # ── 管理 ─────────────────────────────────────────────────
    def list_docs(self) -> List[Dict]:
        rows = self._conn.execute(
            "SELECT d.id, d.title, d.source, d.created_at, "
            "(SELECT COUNT(*) FROM kb_chunks c WHERE c.doc_id=d.id) AS chunks "
            "FROM kb_docs d ORDER BY d.created_at DESC").fetchall()
        return [dict(r) for r in rows]

    def delete_doc(self, doc_id: int) -> bool:
        c = self._conn
        row = c.execute("SELECT id FROM kb_docs WHERE id=?", (doc_id,)).fetchone()
        if not row:
            return False
        cids = [r[0] for r in c.execute("SELECT id FROM kb_chunks WHERE doc_id=?", (doc_id,)).fetchall()]
        for cid in cids:
            if self.backend == 'sqlite_vec' and self._vec_ready:
                try:
                    c.execute("DELETE FROM kb_vec WHERE rowid=?", (cid,))
                except Exception:  # noqa: BLE001
                    pass
            c.execute("DELETE FROM kb_vec_mem WHERE rowid=?", (cid,))
            if self._has_fts:
                try:
                    c.execute("DELETE FROM kb_fts WHERE rowid=?", (cid,))
                except Exception:  # noqa: BLE001
                    pass
        c.execute("DELETE FROM kb_chunks WHERE doc_id=?", (doc_id,))
        c.execute("DELETE FROM kb_docs WHERE id=?", (doc_id,))
        c.commit()
        return True

    def count(self) -> Dict[str, int]:
        docs = self._conn.execute("SELECT COUNT(*) FROM kb_docs").fetchone()[0]
        chunks = self._conn.execute("SELECT COUNT(*) FROM kb_chunks").fetchone()[0]
        return {'docs': docs, 'chunks': chunks}

    def clear(self) -> Dict[str, int]:
        c = self._conn
        before = self.count()
        c.execute("DELETE FROM kb_chunks")
        c.execute("DELETE FROM kb_docs")
        c.execute("DELETE FROM kb_vec_mem")
        if self._has_fts:
            try:
                c.execute("DELETE FROM kb_fts")
            except Exception:  # noqa: BLE001
                pass
        if self.backend == 'sqlite_vec' and self._vec_ready:
            try:
                c.execute("DELETE FROM kb_vec")
            except Exception:  # noqa: BLE001
                pass
        c.commit()
        return before

    def close(self):
        try:
            self._conn.close()
        except Exception:  # noqa: BLE001
            pass
