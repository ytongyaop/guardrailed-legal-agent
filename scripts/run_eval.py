"""评测脚本：跑评测集 → 8 项指标（第 9 项无依据率同表输出）→ 三档消融 → 落盘。

    python scripts/run_eval.py                    # 三档全跑
    python scripts/run_eval.py --variant full      # 只跑完整档
    python scripts/run_eval.py --check-determinism # 同集跑两次，断言结果一致

结果写到 data/evalsets/last_eval.json，管理端“评测”页只读展示。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mla.adapters.extract import RegexExtractor  # noqa: E402
from mla.adapters.extract_llm import LLMExtractor  # noqa: E402
from mla.adapters.llm import DeepSeekLLM  # noqa: E402
from mla.config import load_config  # noqa: E402
from mla.domain.models import CheckItem, Suggestion  # noqa: E402
from mla.domain.policy import ACCEPTANCE  # noqa: E402
from mla.domain.rules import load_rules  # noqa: E402
from mla.eval.metrics import (  # noqa: E402
    LOWER_IS_BETTER,
    micro_accuracy,
    rate,
    traceable_rate,
    unsourced_suggestion_rate,
)
from mla.graph.build import build_graph  # noqa: E402
from mla.graph.nodes import Deps  # noqa: E402
from mla.services.retrieval import build_retriever  # noqa: E402

LABELS = {
    "accuracy": "风险/缺项识别准确率",
    "recall": "风险/缺项召回率",
    "traceable": "依据可追溯率",
    "ask_trigger": "事实补问触发率",
    "no_false_ask": "不误问率",
    "refuse": "越界拒答率",
    "false_refuse": "误拒率",
    "latency_s": "首轮处理时间（秒）",
    "unsourced_suggestion": "建议无依据率",
    "statute_hit": "法条召回率（诊断）",
}


def load_cases(path: str) -> list[dict[str, Any]]:
    lines = Path(path).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def evaluate(cases: list[dict[str, Any]], deps: Deps, variant: str) -> dict[str, Any]:
    graph = build_graph(deps)
    accs: list[float] = []
    hit = pred_total = gold_total = 0
    items_all: list[CheckItem] = []
    sugg_all: list[Suggestion] = []
    traceable_case: list[float] = []
    ask_flags: list[bool] = []
    noask_flags: list[bool] = []
    refuse_out: list[bool] = []
    refuse_in: list[bool] = []
    statute_checks: list[bool] = []
    latencies: list[float] = []
    details: list[dict[str, Any]] = []
    rule_statute = {r.rule_id: r.statute_id for r in deps.rules}

    for case in cases:
        init = {
            "task_id": f"E-{case['case_id']}",
            "mode": "问答" if case.get("mode") == "qa" else "核对",
            "facts": case.get("facts") or {},
            "contract_text": case.get("contract", ""),
            "question": case.get("question", ""),
            "variant": variant,
        }
        started = time.perf_counter()
        final = dict(graph.invoke(init))
        latency = time.perf_counter() - started

        items = list(final.get("items") or [])
        suggestions = list(final.get("suggestions") or [])
        refused = bool(final.get("refused"))
        ask_rounds = int(final.get("ask_rounds") or 0)

        predicted = {i.rule_id or i.item_id for i in items}
        gold = set(case.get("gold_items") or [])
        accs.append(micro_accuracy(predicted, gold))
        hit += len(predicted & gold)
        pred_total += len(predicted)
        gold_total += len(gold)
        items_all.extend(items)
        sugg_all.extend(suggestions)

        allowed = {r.statute_id for r in deps.rules} | {r.rule_id for r in deps.rules}
        allowed |= {s.statute_id for s in (final.get("retrieved") or [])}
        traceable_case.append(traceable_rate(items, allowed))

        retrieved_ids = {s.statute_id for s in (final.get("retrieved") or [])}
        required = {rule_statute[g] for g in gold if g in rule_statute}
        if required:
            statute_checks.append(required <= retrieved_ids)

        kind = case["kind"]
        if kind == "deficient":
            ask_flags.append(ask_rounds > 0)
        if kind == "complete":
            noask_flags.append(ask_rounds == 0)
        if kind == "boundary_out":
            refuse_out.append(refused)
        if kind == "boundary_in":
            # 只有“给出带依据的回答”才算没误拒；空回答也算失败
            refuse_in.append(refused or not suggestions)
        latencies.append(latency)

        if predicted != gold or (case.get("expect_refuse") is not None and refused != case["expect_refuse"]):
            details.append(
                {
                    "case_id": case["case_id"],
                    "kind": kind,
                    "gold": sorted(gold),
                    "predicted": sorted(predicted),
                    "refused": refused,
                }
            )

    recall = hit / gold_total if gold_total else 1.0
    metrics = {
        "accuracy": round(sum(accs) / len(accs), 4) if accs else 0.0,
        "recall": round(recall, 4),
        "traceable": round(sum(traceable_case) / len(traceable_case), 4) if traceable_case else 1.0,
        "ask_trigger": round(rate(ask_flags), 4) if ask_flags else None,
        "no_false_ask": round(rate(noask_flags), 4) if noask_flags else None,
        "refuse": round(rate(refuse_out), 4) if refuse_out else None,
        "false_refuse": round(rate(refuse_in), 4) if refuse_in else None,
        "latency_s": round(sum(latencies) / len(latencies), 4) if latencies else 0.0,
        "unsourced_suggestion": round(unsourced_suggestion_rate(sugg_all), 4),
        "statute_hit": round(rate(statute_checks), 4) if statute_checks else None,
    }
    passed = {
        key: (value <= ACCEPTANCE[key] if key in LOWER_IS_BETTER else value >= ACCEPTANCE[key])
        for key, value in metrics.items()
        if key in ACCEPTANCE and value is not None
    }
    return {
        "metrics": metrics,
        "pass": passed,
        "n_cases": len(cases),
        "n_items": len(items_all),
        "mismatches": details,
    }


def print_table(name: str, result: dict[str, Any]) -> None:
    print(f"\n== 档位 {name}（样本 {result['n_cases']}，命中条目 {result['n_items']}）==")
    print(f"{'指标':<26}{'实测':>10}{'阈值':>10}  达标")
    for key, value in result["metrics"].items():
        threshold = ACCEPTANCE.get(key)
        ok = result["pass"].get(key)
        mark = "达标" if ok else ("未达" if ok is False else "-")
        threshold_text = f"{threshold:g}" if threshold is not None else "-"
        value_text = f"{value:.4f}" if value is not None else "-"
        print(f"{LABELS[key]:<24}{value_text:>12}{threshold_text:>12}  {mark}")
    if result["mismatches"]:
        print(f"不一致样本 {len(result['mismatches'])} 条：")
        for row in result["mismatches"][:8]:
            print(
                f"  - {row['case_id']}: gold={row['gold']} pred={row['predicted']} refused={row['refused']}"
            )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MLA 评测")
    parser.add_argument("--cases", default="data/evalsets/eval_cases.jsonl")
    parser.add_argument("--out", default="data/evalsets/last_eval.json")
    parser.add_argument(
        "--variant",
        default="all",
        choices=["all", "llm_only", "rules_only", "rules_retrieval", "full"],
    )
    parser.add_argument("--check-determinism", action="store_true")
    parser.add_argument("--no-llm", action="store_true", help="不调用大模型（生成层降级为空）")
    parser.add_argument(
        "--extract",
        default="regex",
        choices=["regex", "llm", "auto"],
        help="抽取档：regex 纯正则（默认，可复现）；llm 强制模型抽取；auto 有 Key 才用模型",
    )
    args = parser.parse_args(argv)

    cfg = load_config()
    cases = load_cases(args.cases)
    generator = None if args.no_llm else (DeepSeekLLM(cfg.llm) if cfg.llm.api_key else None)
    print(f"大模型：{'已启用 ' + cfg.llm.model if generator else '未配置（生成层将拒答/为空）'}")
    print(f"抽取档：{args.extract}（regex 纯正则；llm 强制模型；auto 有 Key 才用模型）")
    rules = load_rules(cfg.paths.rules)
    retriever = build_retriever(cfg)

    def deps_for(variant: str) -> Deps:
        use_llm = variant in ("full", "llm_only")
        gen = generator if use_llm else None
        # 抽取档与生成档分开装配：rules_only 只是 gen=None（不生成处理建议），
        # 抽取仍用独立的 generator，才能把“抽取升级”和“生成升级”分开度量（规格 2.7）。
        # auto / llm 也交给 LLMExtractor 自己判断，无 generator 时按“未配置 Key”降级。
        extractor = RegexExtractor() if args.extract == "regex" else LLMExtractor(generator)
        return Deps(rules=rules, retriever=retriever, generator=gen, extractor=extractor)

    variants = ["rules_only", "rules_retrieval", "full"] if args.variant == "all" else [args.variant]

    payload: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "cases_file": args.cases,
        "case_count": len(cases),
        "extract": args.extract,
        "variants": {},
    }
    for variant in variants:
        result = evaluate(cases, deps_for(variant), variant)
        payload["variants"][variant] = result
        print_table(variant, result)
        if args.check_determinism:
            if args.extract != "regex" or (generator is not None and variant in ("full", "llm_only")):
                print("[跳过] 抽取档含大模型，不做逐位确定性校验")
                continue
            again = evaluate(cases, deps_for(variant), variant)
            stable = {k: v for k, v in result["metrics"].items() if k != "latency_s"}
            again_stable = {k: v for k, v in again["metrics"].items() if k != "latency_s"}
            if again_stable != stable:
                print(f"[失败] 确定性检查失败：{variant} 两次结果不同")
                return 1
            print(f"[通过] 确定性检查通过：{variant} 两次指标完全一致（不计时延）")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    history = Path(args.out).parent / "eval_history.jsonl"
    history_row = {
        "generated_at": payload["generated_at"],
        "cases_file": args.cases,
        "case_count": len(cases),
        "acceptance": dict(ACCEPTANCE),
        "variants": {
            name: {
                "metrics": result["metrics"],
                "pass": result["pass"],
            }
            for name, result in payload["variants"].items()  # type: ignore[union-attr]
        },
    }
    with history.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(history_row, ensure_ascii=False) + "\n")
    print(f"\n已写入 {args.out}（历史追加 {history}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
