"""AC-08 的**服务层**基准：只量 `run_check`，不量首屏（规格 006 批次 1）。

为什么现在只测服务层：首屏对比要等批次 5 两边（旧 Streamlit / 新 FastAPI+SPA）都能跑才有意义；
本批能先钉死的是"同机同数据下核对本身要多久"，作为后续两版对比中**服务侧**的那一半。

口径（与 AC-08 一致的部分）：

- 固定输入：同一份合同正文 + 同一份企业事实，20 次（`--runs`）；
- 打桩生成器：把 `mla.services.tasks.DeepSeekLLM` 换成本文件里的假模型，**不含模型网络时间**，
  所以量到的是图 + 规则 + 条款审查编排 + 精排等纯本地开销；
- 预热 2 次再计时（`--warmup`），避免首次导入 / 缓存填充污染样本；
- 不联网、不读真实 Key、`save=False` 不写任务与日志；SQLite 建在系统临时目录，跑完删除。

产物：`--out` 指向的 JSON（版本号、时间戳、运行次数、四类统计、每次原始样本），便于 AC-08
把两次运行（旧版 / 新版）的原始样本放在一起算比值。

用法：
    python scripts/benchmark_frontend.py --runs 20 --out _tmp/frontend-perf-v0.3.2.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import statistics
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import mla  # noqa: E402
import mla.services.tasks as tasks  # noqa: E402
from mla.adapters.persist import SqliteRepository  # noqa: E402
from mla.config import LLMConfig, Paths, data_path  # noqa: E402
from mla.domain.models import Role, User  # noqa: E402
from mla.services.tasks import run_check  # noqa: E402

DEFAULT_RUNS = 20
DEFAULT_OUT = "_tmp/frontend-perf-v0.3.2.json"
DEFAULT_CONTRACT = "tests/api/fixtures/contract_mixed.txt"
# 只有门禁看"有没有 Key"，这里给一个明显不是真 Key 的占位值；`_deps` 用它构造的打桩实例。
FAKE_KEY = "benchmark-key-not-a-real-credential"
BASE_FACTS = {"employer_type": "有限责任公司", "written_contract": "是"}


class StubGenerator:
    """打桩生成器：条款审查按候选逐条回 C 级结论，处理建议回空。

    C 级 + 空 `statute_ids` 是核验层允许的最省路径（A/B 级缺法条会被降级、A 级还要抄法条原文），
    但它仍然走完"候选 → 判定 → 六道核验 → 共识层 → 条目"的完整编排，量的不是空转。
    """

    available = True
    usage_in = 0
    usage_out = 0

    def generate(self, system: str, user: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        if "条款风险审查" in system:
            try:
                payload = json.loads(user)
            except ValueError:  # 返工轮次的入参不是裸 payload，回空即"没修好"
                return {"findings": [], "extra": []}
            findings = [
                {
                    "idx": idx,
                    "grade": "C",
                    "reason": "基准用示例理由",
                    "statute_ids": [],
                    "evidence": candidate["span"],
                    "need_facts": [],
                }
                for idx, candidate in enumerate(payload.get("candidates") or [])
            ]
            return {"findings": findings, "extra": []}
        if "合规顾问" in system:
            return {"suggestions": []}
        return {}

    def classify(self, text: str, labels: list[str]) -> str:
        return labels[0] if labels else "不确定"


def percentile(samples: list[float], q: float) -> float:
    """最近秩法分位数（样本少时不做插值，避免造出一个不存在的中间值）。"""
    ordered = sorted(samples)
    if not ordered:
        return 0.0
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[rank - 1]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="量 run_check 的本地耗时（打桩生成器，不联网）")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS, help="计时次数（默认 20）")
    parser.add_argument("--warmup", type=int, default=2, help="预热次数（默认 2，不计入样本）")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"输出 JSON 路径（默认 {DEFAULT_OUT}）")
    parser.add_argument("--contract", default=DEFAULT_CONTRACT, help="合同正文路径（默认夹具合同）")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.runs <= 0:
        print("--runs 必须大于 0", file=sys.stderr)
        return 2
    root = Path(__file__).resolve().parents[1]
    contract_path = Path(args.contract)
    if not contract_path.is_absolute():
        contract_path = root / contract_path
    contract_text = contract_path.read_text(encoding="utf-8")

    workdir = Path(tempfile.mkdtemp(prefix="mla-bench-"))
    try:
        # 运行期目录全部指向临时目录：不读本机真实库 / Key，也不在工作目录留下 sqlite 文件。
        os.environ["MLA_EXTRACT_MODE"] = "regex"
        os.environ["MLA_MODEL_FIRST"] = "on"
        os.environ["MLA_HOME"] = str(workdir / "home")
        os.environ["MLA_INDEX_DIR"] = str(workdir / "index")
        os.environ["MLA_DB"] = str(workdir / "bench.sqlite3")
        os.environ.pop("MLA_ALLOW_NO_LLM", None)
        os.environ.pop("MLA_LLM_API_KEY", None)
        os.environ.pop("DASHSCOPE_API_KEY", None)

        task_module_generator = StubGenerator()
        tasks.DeepSeekLLM = lambda cfg: task_module_generator  # type: ignore[assignment]

        repo = SqliteRepository(
            paths=Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml")),
            db_path=str(workdir / "bench.sqlite3"),
        )
        user = User(
            user_id="U-bench",
            username="bench",
            password_hash="x",
            salt="y",
            role=Role.USER,
            enterprise_id="E-bench",
        )

        def once() -> Any:
            return run_check(
                user=user,
                contract_text=contract_text,
                facts=dict(BASE_FACTS),
                enterprise_id="E-bench",
                repo=repo,
                save=False,  # 不落库、不写日志
                llm=LLMConfig(api_key=FAKE_KEY),
                clause_review=True,
            )

        for _ in range(max(0, args.warmup)):
            once()

        samples: list[float] = []
        last: Any = None
        for _ in range(args.runs):
            started = time.perf_counter()
            last = once()
            samples.append((time.perf_counter() - started) * 1000.0)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    stats = {
        "median_ms": round(statistics.median(samples), 3),
        "p95_ms": round(percentile(samples, 0.95), 3),
        "min_ms": round(min(samples), 3),
        "max_ms": round(max(samples), 3),
    }
    payload = {
        "version": mla.__version__,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "runs": len(samples),
        "warmup": max(0, args.warmup),
        "scope": "run_check（服务层；不含模型网络时间与前端渲染）",
        "config": {
            "contract": str(contract_path.relative_to(root))
            if contract_path.is_relative_to(root)
            else str(contract_path),
            "contract_chars": len(contract_text),
            "model_first": True,
            "clause_review": True,
            "save": False,
            "items": len(getattr(last, "items", []) or []),
            "clause_findings": len(getattr(last, "clause_findings", []) or []),
        },
        "stats": stats,
        "samples_ms": [round(value, 3) for value in samples],
    }

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = root / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(
        f"frontend-perf v{mla.__version__} · run_check {len(samples)} 次 · "
        f"中位数 {stats['median_ms']} ms · P95 {stats['p95_ms']} ms · "
        f"最小 {stats['min_ms']} ms · 最大 {stats['max_ms']} ms → {out_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
