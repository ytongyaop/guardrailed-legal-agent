"""检索装配：有向量 Key 就走 BM25 + 稠密融合，否则回退单路 BM25。"""

from __future__ import annotations

from ..adapters.embedding import DashScopeEmbedder
from ..adapters.rag import Bm25Retriever, DenseRetriever, HybridRetriever
from ..config import Config, load_config
from ..ports.retrieval import Retriever


def build_retriever(cfg: Config | None = None) -> Retriever:
    cfg = cfg or load_config()
    bm25 = Bm25Retriever(statutes_path=cfg.paths.statutes)
    if not cfg.embedding.api_key:
        return bm25
    dense = DenseRetriever(
        DashScopeEmbedder(cfg.embedding),
        statutes_path=cfg.paths.statutes,
        cfg=cfg.retrieval,
    )
    return HybridRetriever(bm25, dense)
