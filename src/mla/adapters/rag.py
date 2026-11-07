"""检索适配：BM25 + 阿里云向量双路，RRF 融合，法源时效过滤。

- 关键词路：BM25（纯 Python，离线可用）；
- 语义路：qwen3.7-text-embedding-flash（无 Key / 断网时自动退化为 BM25）；
- 融合：RRF（`rrf_fuse`），两路都不可用时返回空。

时效过滤：只召回 `status=有效` 且 `effective_date <= 今天`（无 expiry；有则要求未过期）的条文。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import date
from pathlib import Path

from ..config import RetrievalConfig, load_config
from ..domain.models import StatuteEntry, StatuteLevel, StatuteStatus
from .embedding import EmbeddingError


def tokenize(text: str) -> list[str]:
    """中文按字二元 + 英文/数字按词，够用且不引入分词依赖。"""
    text = (text or "").lower()
    ascii_tokens = re.findall(r"[a-z0-9]+", text)
    cjk = re.sub(r"[^\u4e00-\u9fa5]", "", text)
    bigrams = [cjk[i : i + 2] for i in range(len(cjk) - 1)]
    return ascii_tokens + bigrams


def rrf_fuse(ranked_lists: list[list[str]], k: int = 60) -> list[tuple[str, float]]:
    """Reciprocal Rank Fusion：多路结果按名次融合，返回 (id, score) 降序。

    输入是若干"已排序的 id 列表"（两路共用同一 ID 空间，见架构文档）。
    """
    scores: dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, doc_id in enumerate(ranked, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def load_statutes(path: str | Path) -> list[StatuteEntry]:
    rows: list[StatuteEntry] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        r = json.loads(line)
        rows.append(
            StatuteEntry(
                statute_id=str(r["statute_id"]),
                law_name=str(r["law_name"]),
                article_no=str(r["article_no"]),
                text=str(r["text"]),
                issuing_body=str(r["issuing_body"]),
                doc_no=str(r.get("doc_no") or ""),
                effective_date=date.fromisoformat(r["effective_date"]) if r.get("effective_date") else None,
                expiry_date=date.fromisoformat(r["expiry_date"]) if r.get("expiry_date") else None,
                status=StatuteStatus(str(r.get("status", "有效"))),
                superseded_by=r.get("superseded_by") or None,
                level=StatuteLevel(str(r.get("level", "法律"))),
                region=str(r.get("region") or "全国"),
                source_url=str(r.get("source_url") or ""),
                note=str(r.get("note") or ""),
            )
        )
    return rows


class Bm25Retriever:
    """实现 ports.retrieval.Retriever：Okapi BM25，纯 Python。"""

    def __init__(self, cfg: RetrievalConfig | None = None, statutes_path: str | None = None) -> None:
        self.cfg = cfg or load_config().retrieval
        path = statutes_path or load_config().paths.statutes
        self._docs: list[StatuteEntry] = []
        self._tokens: list[Counter[str]] = []
        self._df: Counter[str] = Counter()
        self._avg_len: float = 0.0
        self._load(path)

    def _load(self, path: str) -> None:
        today = date.today()
        docs: list[StatuteEntry] = []
        for s in load_statutes(path):
            if s.status is not StatuteStatus.EFFECTIVE:
                continue
            if s.effective_date and s.effective_date > today:
                continue
            if s.expiry_date and s.expiry_date < today:
                continue
            docs.append(s)
        self._docs = docs
        self._tokens = [Counter(tokenize(f"{d.law_name}{d.article_no}{d.text}")) for d in docs]
        self._df = Counter(tok for c in self._tokens for tok in c)
        total = sum(sum(c.values()) for c in self._tokens)
        self._avg_len = total / len(docs) if docs else 0.0

    def search(self, query: str, top_k: int = 8, dense_query: str | None = None) -> list[StatuteEntry]:
        if not self._docs or not query.strip():
            return []
        q_tokens = list(dict.fromkeys(tokenize(query)))
        n = len(self._docs)
        k1, b = 1.5, 0.75
        scored: list[tuple[float, int]] = []
        for idx, counts in enumerate(self._tokens):
            dl = sum(counts.values())
            score = 0.0
            for tok in q_tokens:
                tf = counts.get(tok, 0)
                if not tf:
                    continue
                idf = math.log(1 + (n - self._df[tok] + 0.5) / (self._df[tok] + 0.5))
                denom = tf + k1 * (1 - b + b * dl / (self._avg_len or 1))
                score += idf * tf * (k1 + 1) / denom
            if score > 0:
                scored.append((score, idx))
        scored.sort(key=lambda x: (-x[0], self._docs[x[1]].statute_id))
        return [self._docs[i] for _, i in scored[:top_k]]

    def health(self) -> bool:
        return bool(self._docs)


class DenseRetriever:
    """稠密路：阿里云 qwen3.7-text-embedding-flash + 本地向量缓存（余弦检索）。"""

    def __init__(
        self,
        embedder,
        statutes_path: str | None = None,
        cfg: RetrievalConfig | None = None,
    ) -> None:
        self.embedder = embedder
        self.cfg = cfg or load_config().retrieval
        self.path = Path(statutes_path or load_config().paths.statutes)
        self._docs: list[StatuteEntry] = []
        self._vectors: list[list[float]] = []
        self.last_error = ""
        self._ready = False

    def _cache_file(self, digest: str) -> Path:
        model = str(getattr(self.embedder.cfg, "model", "embed")).replace("/", "_")
        dim = getattr(self.embedder.cfg, "dimension", 0)
        return Path(self.cfg.index_dir) / f"emb_{model}_{dim}_{digest}.json"

    def _ensure_index(self) -> None:
        if self._ready:
            return
        if not getattr(self.embedder, "available", False):
            raise EmbeddingError("未配置向量模型 Key")
        statutes = load_statutes(self.path)
        digest = hashlib.sha256(self.path.read_bytes()).hexdigest()[:12]
        cache = self._cache_file(digest)
        if cache.exists():
            data = json.loads(cache.read_text(encoding="utf-8"))
            if data.get("model") == getattr(self.embedder.cfg, "model", "") and len(
                data.get("ids", [])
            ) == len(statutes):
                by_id = {s.statute_id: s for s in statutes}
                self._docs = [by_id[i] for i in data["ids"] if i in by_id]
                self._vectors = [list(map(float, v)) for v in data["vectors"]]
                self._ready = True
                return
        texts = [f"{s.law_name} {s.article_no} {s.text}" for s in statutes]
        vectors = self.embedder.embed(texts, text_type="document")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(
            json.dumps(
                {
                    "model": getattr(self.embedder.cfg, "model", ""),
                    "dimension": getattr(self.embedder.cfg, "dimension", 0),
                    "ids": [s.statute_id for s in statutes],
                    "vectors": vectors,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self._docs = statutes
        self._vectors = vectors
        self._ready = True

    def search(self, query: str, top_k: int = 8, dense_query: str | None = None) -> list[StatuteEntry]:
        try:
            self._ensure_index()
            query_vector = self.embedder.embed([dense_query or query], text_type="query")[0]
        except EmbeddingError as exc:
            self.last_error = str(exc)
            return []
        scored = []
        q_norm = math.sqrt(sum(x * x for x in query_vector)) or 1.0
        for doc, vector in zip(self._docs, self._vectors, strict=False):
            dot = sum(a * b for a, b in zip(query_vector, vector, strict=False))
            norm = math.sqrt(sum(x * x for x in vector)) or 1.0
            scored.append((dot / (q_norm * norm), doc))
        scored.sort(key=lambda pair: (-pair[0], pair[1].statute_id))
        return [doc for _, doc in scored[:top_k]]

    def health(self) -> bool:
        try:
            self._ensure_index()
            return True
        except EmbeddingError as exc:
            self.last_error = str(exc)
            return False


class HybridRetriever:
    """BM25 + 稠密 RRF 融合；稠密路不可用时自动退化为单路。"""

    def __init__(self, bm25: Bm25Retriever, dense: DenseRetriever | None = None) -> None:
        self.bm25 = bm25
        self.dense = dense

    def search(self, query: str, top_k: int = 8, dense_query: str | None = None) -> list[StatuteEntry]:
        lexical = self.bm25.search(query, top_k=top_k * 2)
        if self.dense is None:
            return lexical[:top_k]
        # 空字符串表示“本次不走向量路”（例如核对场景只有合同正文、没有结构化事实）
        dense_text = query if dense_query is None else dense_query
        if not dense_text.strip():
            return lexical[:top_k]
        try:
            dense_hits = self.dense.search(dense_text, top_k=top_k * 2)
        except EmbeddingError:
            dense_hits = []
        if not dense_hits:
            return lexical[:top_k]
        by_id = {s.statute_id: s for s in lexical + dense_hits}
        fused = rrf_fuse(
            [[s.statute_id for s in lexical], [s.statute_id for s in dense_hits]],
            k=self.bm25.cfg.rrf_k,
        )
        return [by_id[sid] for sid, _ in fused[:top_k] if sid in by_id]

    def health(self) -> bool:
        return self.bm25.health() and (self.dense is None or self.dense.health())
