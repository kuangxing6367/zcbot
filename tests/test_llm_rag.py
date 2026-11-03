# -*- coding: utf-8 -*-
"""验证本地知识库（RAG）：入库 / 混合检索（向量 + 关键词）/ 删除 / 工具函数。

不依赖网络与原生扩展：用确定性假 embedding + memory 后端，CI 也能跑。
"""
import asyncio
import os
import sys
import tempfile

# 从 __file__ 推导源码目录，别写死本机绝对路径（CI 是 Linux，硬编码必挂）
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(_ROOT, 'core_plugins', 'llm_load', 'src')
if SRC not in sys.path:
    sys.path.insert(0, SRC)

from llm_core.knowledge import KnowledgeBase, _split_text, _rrf  # noqa: E402
from llm_core import embeddings as emb_mod  # noqa: E402
import llm_core.main as llm_main  # noqa: E402

_FAILED = False


def check(cond, msg):
    global _FAILED
    print('PASS' if cond else 'FAIL', msg)
    if not cond:
        _FAILED = True


def fake_embed(texts):
    """确定性假向量：苹果→[1,0,0]，香蕉→[0,1,0]，其它→[0,0,1]。维度 3。"""
    out = []
    for t in texts:
        if '苹果' in t:
            out.append([1.0, 0.0, 0.0])
        elif '香蕉' in t:
            out.append([0.0, 1.0, 0.0])
        else:
            out.append([0.0, 0.0, 1.0])
    return out


def new_kb():
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    os.remove(path)
    kb = KnowledgeBase(db_path=path, embed=fake_embed, dims=3, backend='memory')
    kb._test_path = path
    return kb


def teardown(kb):
    try:
        os.remove(kb._test_path)
    except Exception:  # noqa: BLE001
        pass


# ── 工具函数 ──────────────────────────────────────────────
def test_helpers():
    chunks = _split_text("段落一内容。\n\n段落二内容较长需要切分处理一下看看效果。", size=10, overlap=2)
    check(len(chunks) >= 1, "切分产出至少一个碎片")
    rr = _rrf([[1, 2, 3], [2, 3, 1]])
    check(rr.get(2, 0) > rr.get(1, 0), "RRF：两列表都出现的中段 id 得分更高")


# ── EmbeddingClient.from_config ───────────────────────────
def test_embed_config():
    check(emb_mod.EmbeddingClient.from_config({}) is None, "无 base_url 返回 None")
    check(emb_mod.EmbeddingClient.from_config({'base_url': 'https://x/v1'}) is not None,
          "有 base_url 能构造")
    # 通过 session 参数注入假 session，避免真实网络请求
    client = emb_mod.EmbeddingClient(
        base_url='https://x/v1', api_key='k', model='m', dims=2,
        session=_FakeSession([{'embedding': [0.1, 0.2]}, {'embedding': [0.3, 0.4]}]))
    v = client.embed(['a', 'b'])
    check(v == [[0.1, 0.2], [0.3, 0.4]], "EmbeddingClient 解析 data 数组")
    check(client.dims == 2, "维度从返回推断并固定")


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, data):
        self.data = data

    def post(self, *a, **k):
        return _FakeResp({'data': self.data})


# ── 入库 + 混合检索 ───────────────────────────────────────
def test_kb_core():
    kb = new_kb()
    try:
        d1 = kb.add_text('苹果文档', '苹果是一种水果，富含维生素C。每天吃一个苹果有益健康。')
        d2 = kb.add_text('香蕉文档', '香蕉是热带水果，富含钾。香蕉口感软糯适合运动后补充能量。')
        check(d1 and d2, "两篇文档入库成功")
        cnt = kb.count()
        check(cnt['docs'] == 2 and cnt['chunks'] >= 2, f"计数正确: {cnt}")
        check(len(kb.list_docs()) == 2, "列表返回两篇文档")

        # 向量召回：短查询（<3 字关闭 FTS）只走向量，应命中苹果文档
        hits = kb.search('苹果营养', top_k=3)
        check(any('苹果' in h['text'] for h in hits), "向量召回：苹果查询命中苹果碎片")

        # 关键词召回：查询串真实出现在香蕉文档里
        if kb._has_fts:
            hits2 = kb.search('富含钾', top_k=3)
            check(any('香蕉' in h['text'] for h in hits2), "关键词召回(FTS5)：命中香蕉碎片")
        else:
            print('SKIP 关键词召回（当前 SQLite 无 FTS5）')

        # 删除
        ok = kb.delete_doc(d1)
        check(ok and kb.count()['docs'] == 1, "删除文档后计数减一")
        # 清空
        before = kb.clear()
        check(before['docs'] == 1 and kb.count()['docs'] == 0, "清空知识库")
    finally:
        teardown(kb)


# ── knowledge_search 工具函数 ─────────────────────────────
def test_kb_tool():
    kb = new_kb()
    try:
        kb.add_text('苹果文档', '苹果是一种水果，富含维生素C。')
        llm_main._KB = kb
        out = llm_main.knowledge_search('苹果营养')
        check('苹果' in out, "knowledge_search 工具返回命中片段")
        llm_main._KB = None
        out2 = llm_main.knowledge_search('x')
        check('尚未启用' in out2, "未启用时返回提示而非报错")
    finally:
        llm_main._KB = None
        teardown(kb)


if __name__ == '__main__':
    test_helpers()
    test_embed_config()
    test_kb_core()
    test_kb_tool()
    print('RESULT:', 'ALL PASS' if not _FAILED else 'HAS FAIL')
    sys.exit(1 if _FAILED else 0)
