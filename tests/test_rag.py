"""检索层测试：BM25 命中、时效过滤、RRF 融合（全离线）。"""

from __future__ import annotations

import json
from pathlib import Path

from mla.adapters.rag import Bm25Retriever, rrf_fuse

STATUTES = "data/statutes/statutes.jsonl"


def _retriever() -> Bm25Retriever:
    return Bm25Retriever(statutes_path=STATUTES)


def test_health_and_size() -> None:
    r = _retriever()
    assert r.health() is True
    assert len(Bm25Retriever(statutes_path=STATUTES).search("试用期", top_k=5)) > 0


def test_search_hits_expected_article() -> None:
    hits = _retriever().search("试用期不得超过六个月", top_k=5)
    assert "LCL-19" in [s.statute_id for s in hits]


def test_search_noncompete() -> None:
    hits = _retriever().search("竞业限制期限不得超过二年", top_k=5)
    assert "LCL-24" in [s.statute_id for s in hits]


def test_empty_query_returns_nothing() -> None:
    assert _retriever().search("   ") == []


def test_expired_statute_is_filtered(tmp_path: Path) -> None:
    row = {
        "statute_id": "TST-1",
        "law_name": "测试法",
        "article_no": "第一条",
        "text": "这是一条已经失效的测试条文，内容包含试用期字样。",
        "issuing_body": "测试机关",
        "effective_date": "2000-01-01",
        "expiry_date": "2001-01-01",
        "status": "有效",
        "level": "法律",
        "region": "全国",
        "source_url": "https://flk.npc.gov.cn/",
    }
    p = tmp_path / "s.jsonl"
    p.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
    r = Bm25Retriever(statutes_path=str(p))
    assert r.health() is False  # 过期条文被过滤 → 无可用文档
    assert r.search("试用期") == []


def test_rrf_prefers_documents_hit_by_both() -> None:
    fused = rrf_fuse([["A", "B", "C"], ["B", "D"]], k=60)
    assert fused[0][0] == "B"  # 两路都命中，排第一
    assert [x[0] for x in fused] == ["B", "A", "D", "C"]  # 1/61 > 1/62 > 1/63
