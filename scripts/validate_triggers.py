"""触发器表校验器（负样本零触发为硬门禁）。

检查项：
1. `trigger_id` 唯一、以 `TR-` 开头
2. `patterns` 非空、每条都能被 `re` 编译
3. `candidate_statute_ids` 非空，且全部存在于 `data/statutes/statutes.jsonl`（闭集引用）
4. `severity_hint` ∈ {A, B, C}；`ask_fields` 是字符串列表
5. **硬门禁**——以下四条任一不满足即 `FAIL` 并以退出码 1 结束：
   a. gold 正样本覆盖率 = 38/38（`data/evalsets/clause_gold.jsonl` 中 grade ≠ 合规的样本）
   b. 负样本（grade = 合规）被触发 = 0 条
   c. `contract_compliant_01.txt` 候选 = 0 条
   d. 四份合同候选数 = 22 / 9 / 12 / 0

⚠️ 第 5 条这四项是**硬门禁**，不是「仅供参考的指标」。放宽其中任何一条，都要在 PR
说明里给出理由（为什么放宽、放宽后由什么补上防误报能力），并同步更新本文件与
`tests/test_clauses.py` 里的基线数字——只改一处等于自我豁免。

覆盖率口径：`overlap = 最长公共子串长度 / min(len(gold_span), len(candidate_span))`。
分母取较短的 span，避免"候选 span 比 gold 长"被误判为覆盖不足（触发器只要求捞到即可）。

用法：
    python scripts/validate_triggers.py [--min-overlap 0.5]
失败退出码 1（任一结构问题、任一硬门禁不过）。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from mla.adapters.extract import _norm
from mla.domain.clauses import ClauseCandidate, detect_clauses, load_triggers

TRIGGERS_PATH = Path("data/clauses/triggers.yaml")
GOLD_PATH = Path("data/evalsets/clause_gold.jsonl")
STATUTES_PATH = Path("data/statutes/statutes.jsonl")
CONTRACT_DIR = Path("data/evalsets")
TRIGGER_ID_RE = re.compile(r"^TR-[A-Z0-9-]+$")

# ---- 硬门禁基线（改动前先读模块 docstring 第 5 条）----
EXPECTED_GOLD_POSITIVES = 38
EXPECTED_CANDIDATES: dict[str, int] = {
    "contract_traps_01.txt": 22,
    "contract_traps_02.txt": 9,
    "contract_traps_03.txt": 12,
    "contract_compliant_01.txt": 0,
}


def load_statute_ids(path: Path) -> set[str]:
    ids: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            ids.add(str(json.loads(line)["statute_id"]))
    return ids


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串长度（滚动数组；合同段落不大，够用）。"""
    if not a or not b:
        return 0
    if len(a) < len(b):
        a, b = b, a
    best = 0
    prev = [0] * (len(b) + 1)
    for ch in a:
        cur = [0] * (len(b) + 1)
        for j, other in enumerate(b, start=1):
            if ch == other:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def overlap_ratio(gold_span: str, cand_span: str) -> float:
    """归一化后的重叠比例（分母取较短一方）。"""
    a, b = _norm(gold_span), _norm(cand_span)
    shortest = min(len(a), len(b))
    if shortest == 0:
        return 0.0
    return _lcs_len(a, b) / shortest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--triggers", default=str(TRIGGERS_PATH))
    ap.add_argument("--gold", default=str(GOLD_PATH))
    ap.add_argument("--statutes", default=str(STATUTES_PATH))
    ap.add_argument("--contracts", default=str(CONTRACT_DIR), help="gold 合同与基线合同所在目录")
    ap.add_argument("--min-overlap", type=float, default=0.5)
    args = ap.parse_args(argv)

    errors: list[str] = []
    gates: list[str] = []  # 硬门禁失败项（打印「期望 / 实测」）

    # ---- 1/2/3/4 结构校验 ----
    try:
        triggers = load_triggers(args.triggers)
    except ValueError as exc:
        print(f"[FAIL] 触发器表结构错误：{exc}")
        return 1

    statute_ids = load_statute_ids(Path(args.statutes))
    for t in triggers:
        if not TRIGGER_ID_RE.match(t.trigger_id):
            errors.append(f"{t.trigger_id}: trigger_id 不符 ^TR-[A-Z0-9-]+$")
        for pat in t.patterns:
            try:
                re.compile(pat)
            except re.error as exc:
                errors.append(f"{t.trigger_id}: pattern 无法编译（{pat}）：{exc}")
        missing = [s for s in t.candidate_statute_ids if s not in statute_ids]
        if missing:
            errors.append(f"{t.trigger_id}: 法条不存在 {' / '.join(missing)}")

    ids = [t.trigger_id for t in triggers]
    dupes = {i for i in ids if ids.count(i) > 1}
    if dupes:
        errors.append(f"trigger_id 重复：{' / '.join(sorted(dupes))}")

    # ---- 基线合同：逐份召回（硬门禁 c/d 的事实来源）----
    contracts_dir = Path(args.contracts)
    candidates_by_contract: dict[str, tuple[ClauseCandidate, ...]] = {}
    for name in EXPECTED_CANDIDATES:
        path = contracts_dir / name
        if not path.is_file():
            print(f"[FAIL] 基线合同不存在：{path}")
            return 1
        candidates_by_contract[name] = detect_clauses(path.read_text(encoding="utf-8"), triggers)

    # ---- 5. gold 覆盖率 / 负样本 ----
    rows = [
        json.loads(line) for line in Path(args.gold).read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    covered: list[str] = []
    uncovered: list[str] = []
    negative_hit: list[str] = []

    for row in rows:
        gid = str(row["gold_id"])
        contract = str(row["contract"])
        if contract not in candidates_by_contract:
            path = contracts_dir / contract
            if not path.is_file():
                errors.append(f"{gid}: 合同文件不存在 {path}")
                candidates_by_contract[contract] = ()
            else:
                candidates_by_contract[contract] = detect_clauses(path.read_text(encoding="utf-8"), triggers)
        candidates = candidates_by_contract[contract]

        if str(row["grade"]) == "合规":
            hits = [c for c in candidates if overlap_ratio(row["span"], c.span) >= 0.5]
            if hits:
                negative_hit.append(f"{gid}（{' / '.join(sorted({c.trigger_id for c in hits}))}）")
            continue

        best = max((overlap_ratio(row["span"], c.span) for c in candidates), default=0.0)
        if best >= args.min_overlap:
            covered.append(f"{gid}（{best:.2f}）")
        else:
            uncovered.append(f"{gid}（最高重叠 {best:.2f}）span={row['span']}")

    # ---- 硬门禁判定 ----
    expected_cov = f"{EXPECTED_GOLD_POSITIVES}/{EXPECTED_GOLD_POSITIVES}"
    actual_cov = f"{len(covered)}/{len(covered) + len(uncovered)}"
    if uncovered:
        gates.append(f"[正样本覆盖] 期望 {expected_cov}，实测未覆盖 {len(uncovered)} 条")
    if len(covered) != EXPECTED_GOLD_POSITIVES:
        gates.append(f"[正样本覆盖] 期望 {expected_cov}，实测 {actual_cov}")
    if negative_hit:
        gates.append(f"[负样本零触发] 期望 0 条，实测 {len(negative_hit)} 条")
    for name, expected in EXPECTED_CANDIDATES.items():
        actual = len(candidates_by_contract[name])
        if actual != expected:
            gates.append(f"[候选数] {name}：期望 {expected}，实测 {actual}")

    # ---- 输出 ----
    print(f"触发器表：{args.triggers}（{len(triggers)} 条触发器，法条库 {len(statute_ids)} 条）")
    for name in EXPECTED_CANDIDATES:
        cands = candidates_by_contract[name]
        by_trigger = len({c.trigger_id for c in cands})
        print(f"  · {name}：候选 {len(cands)} 条（覆盖 {by_trigger} 条触发器）")
    for name, cands in candidates_by_contract.items():
        if name not in EXPECTED_CANDIDATES:
            by_trigger = len({c.trigger_id for c in cands})
            print(f"  · {name}：候选 {len(cands)} 条（覆盖 {by_trigger} 条触发器）")

    print(
        f"\ngold 正样本覆盖率：{len(covered)}/{len(covered) + len(uncovered)}"
        f"（阈值 overlap ≥ {args.min_overlap:.0%}）"
    )
    if uncovered:
        print("[FAIL] 未被覆盖的正样本：")
        for item in uncovered:
            print("  -", item)
    print(
        f"负样本被触发：{len(negative_hit)} 条" + (f"（{'；'.join(negative_hit)}）" if negative_hit else "")
    )
    print(
        "合同候选数基线："
        + " / ".join(f"{name}={len(candidates_by_contract[name])}" for name in EXPECTED_CANDIDATES)
        + "（期望 "
        + " / ".join(str(v) for v in EXPECTED_CANDIDATES.values())
        + "）"
    )

    if errors:
        print(f"\n[FAIL] {len(errors)} 个结构问题：")
        for e in errors:
            print("  -", e)
    if gates:
        print(f"\n[FAIL] {len(gates)} 条硬门禁未通过：")
        for g in gates:
            print("  -", g)
    if errors or gates:
        return 1
    print("\n[OK] 触发器表结构合法，gold 正样本全部覆盖，负样本零触发，四份合同候选数与基线一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
