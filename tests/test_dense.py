"""稠密检索测试：向量排序、缓存复用、无 Key 回退（不联网）。"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from mla.adapters.rag import Bm25Retriever, DenseRetriever, HybridRetriever
from mla.config import RetrievalConfig

ROWS = [
    {
        "statute_id": "LCL-19",
        "law_name": "劳动合同法",
        "article_no": "第十九条",
        "text": "试用期不得超过六个月。",
        "issuing_body": "全国人大常委会",
        "effective_date": "2013-07-01",
        "status": "有效",
        "level": "法律",
    },
    {
        "statute_id": "SIL-58",
        "law_name": "社会保险法",
        "article_no": "第五十八条",
        "text": "用人单位应当办理社会保险登记。",
        "issuing_body": "全国人大常委会",
        "effective_date": "2018-12-29",
        "status": "有效",
        "level": "法律",
    },
]


def _statutes(tmp_path) -> Path:
    path = tmp_path / "statutes.jsonl"
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in ROWS), encoding="utf-8")
    return path


class FakeEmbedder:
    def __init__(self) -> None:
        self.cfg = SimpleNamespace(model="fake-embed", dimension=4)
        self.available = True
        self.document_calls = 0

    def embed(self, texts: list[str], text_type: str = "document") -> list[list[float]]:
        if text_type == "document":
            self.document_calls += 1
        out = []
        for text in texts:
            if "试用期" in text:
                out.append([1.0, 0.0, 0.0, 0.0])
            elif "社会保险" in text:
                out.append([0.0, 1.0, 0.0, 0.0])
            else:
                out.append([0.0, 0.0, 1.0, 0.0])
        return out


def test_dense_ranks_by_cosine(tmp_path) -> None:
    dense = DenseRetriever(
        FakeEmbedder(), statutes_path=str(_statutes(tmp_path)), cfg=RetrievalConfig(index_dir=str(tmp_path))
    )
    hits = dense.search("试用期最长多久", top_k=2)
    assert hits and hits[0].statute_id == "LCL-19"


def test_dense_cache_is_reused(tmp_path) -> None:
    embedder = FakeEmbedder()
    path = str(_statutes(tmp_path))
    cfg = RetrievalConfig(index_dir=str(tmp_path))
    DenseRetriever(embedder, statutes_path=path, cfg=cfg).search("试用期", top_k=1)
    first_calls = embedder.document_calls
    DenseRetriever(embedder, statutes_path=path, cfg=cfg).search("试用期", top_k=1)
    assert embedder.document_calls == first_calls  # 第二次直接读缓存


class BoomDense:
    def search(self, *args, **kwargs):
        raise AssertionError("dense_query 为空时不应调用向量路")


def test_hybrid_skips_dense_when_query_empty(tmp_path) -> None:
    bm25 = Bm25Retriever(statutes_path=str(_statutes(tmp_path)), cfg=RetrievalConfig())
    hybrid = HybridRetriever(bm25, BoomDense())  # type: ignore[arg-type]
    hits = hybrid.search("试用期", top_k=2, dense_query="")
    assert hits and hits[0].statute_id == "LCL-19"
