"""问答维度消融：模型 / +BM25 / +向量 / +RRF。

识别维度用规则（确定性）；问答衡量“模型 + 检索”的组合。
指标：可答率（非空回答）、引用存在率（source_id 都在 143 条法条库内）、
引用命中率（引用里至少有一条是人工标注的应引法条）、工具轮数、平均耗时。
模型档不接工具，用来证明“无检索时引用不可靠”。
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mla.adapters.embedding import DashScopeEmbedder  # noqa: E402
from mla.adapters.llm import DeepSeekLLM  # noqa: E402
from mla.adapters.rag import Bm25Retriever, DenseRetriever, HybridRetriever  # noqa: E402
from mla.config import data_path, load_config  # noqa: E402
from mla.services.qa_agent import run_qa_agent  # noqa: E402

MODEL_SYSTEM = """你是劳动用工问答助手。只输出 JSON：{"answers":[{"text":"结论","source_ids":["法条ID"]}]}。
不得编造条号；没有把握就返回 {"answers":[]}。"""

QA_FILE = data_path("evalsets/qa_cases.jsonl")


class DenseOnly:
    def __init__(self, dense: DenseRetriever) -> None:
        self.dense = dense

    def search(self, query: str, top_k: int = 8, dense_query: str | None = None):
        return self.dense.search(dense_query or query, top_k=top_k)

    def health(self) -> bool:
        return self.dense.health()


def _load_cases() -> list[dict]:
    path = Path(QA_FILE)
    if path.exists():
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    rows = [
        json.loads(x)
        for x in Path(data_path("evalsets/eval_cases.jsonl")).read_text(encoding="utf-8").splitlines()
        if x.strip()
    ]
    return [
        {"question": r["question"], "gold_statutes": []}
        for r in rows
        if r.get("kind") == "boundary_in" and r.get("question")
    ]


def _model_only(llm: DeepSeekLLM, question: str) -> list[tuple[str, ...]]:
    payload = llm.generate(MODEL_SYSTEM, question)
    rows = payload.get("answers") or []
    return [tuple(str(x) for x in (r.get("source_ids") or [])) for r in rows if isinstance(r, dict)]


def main() -> None:
    cfg = load_config()
    if not cfg.llm.api_key:
        raise SystemExit("未配置 DeepSeek Key")
    llm = DeepSeekLLM(cfg.llm)
    cases = _load_cases()
    library = {
        json.loads(x)["statute_id"]
        for x in Path(cfg.paths.statutes).read_text(encoding="utf-8").splitlines()
        if x.strip()
    }
    embedder = DashScopeEmbedder(cfg.embedding)
    dense = DenseRetriever(embedder, statutes_path=cfg.paths.statutes, cfg=cfg.retrieval)
    retrievers = {
        "model_only": None,
        "bm25": Bm25Retriever(statutes_path=cfg.paths.statutes),
        "vector": DenseOnly(dense),
        "rrf": HybridRetriever(Bm25Retriever(statutes_path=cfg.paths.statutes), dense),
    }

    print(f"问答消融（{len(cases)} 题）")
    print(f"{'档位':<12}{'可答率':>8}{'引用存在率':>10}{'引用命中率':>10}{'工具轮':>8}{'耗时(s)':>9}")
    payload: dict[str, object] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "case_count": len(cases),
        "tiers": {},
    }
    for name, retriever in retrievers.items():
        answered = exists = hit = 0
        rounds = 0
        elapsed = 0.0
        for case in cases:
            gold = set(case.get("gold_statutes") or [])
            started = time.perf_counter()
            if retriever is None:
                citations = _model_only(llm, case["question"])
                trace: list[str] = []
            else:
                answers, trace, _ = run_qa_agent(llm, retriever, tuple(), case["question"])
                citations = [tuple(a.source_ids) for a in answers]
            answered += int(bool(citations))
            flat = {sid for ids in citations for sid in ids}
            exists += int(bool(flat) and flat <= library)
            hit += int(bool(flat & gold)) if gold else 0
            rounds += len(trace)
            elapsed += time.perf_counter() - started
        total = len(cases)
        payload["tiers"][name] = {  # type: ignore[index]
            "answer_rate": round(answered / total, 4),
            "citation_exists_rate": round(exists / total, 4),
            "citation_hit_rate": round(hit / total, 4),
            "avg_tool_rounds": round(rounds / total, 2),
            "avg_latency_s": round(elapsed / total, 2),
        }
        print(
            f"{name:<12}{answered / total:>8.2f}{exists / total:>10.2f}"
            f"{hit / total:>10.2f}{rounds / total:>8.2f}{elapsed / total:>9.2f}"
        )

    out = Path(data_path("evalsets/last_qa_eval.json"))
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已写入 {out}")


if __name__ == "__main__":
    main()
