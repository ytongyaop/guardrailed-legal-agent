"""条款候选调试打印。

把一份合同跑一遍触发器层，输出候选 JSON（供人工核对）。
本脚本只调用纯函数 `mla.domain.clauses.detect_clauses`，不联网、不调模型。

用法：
    python scripts/detect_clauses.py --contract src/mla/data/evalsets/contract_traps_01.txt
    python scripts/detect_clauses.py --contract <path> --trigger TR-SI-WAIVER
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mla.config import data_path  # noqa: E402
from mla.domain.clauses import detect_clauses, load_triggers  # noqa: E402

TRIGGERS_PATH = Path(data_path("clauses/triggers.yaml"))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--contract", required=True, help="合同文本路径（UTF-8）")
    ap.add_argument("--trigger", default="", help="只打印指定 trigger_id 的候选")
    ap.add_argument("--triggers", default=str(TRIGGERS_PATH), help="触发器表路径")
    args = ap.parse_args(argv)

    contract_path = Path(args.contract)
    if not contract_path.is_file():
        print(f"[FAIL] 合同文件不存在：{contract_path}", file=sys.stderr)
        return 1

    triggers = load_triggers(args.triggers)
    if args.trigger and args.trigger not in {t.trigger_id for t in triggers}:
        print(f"[FAIL] 触发器不存在：{args.trigger}", file=sys.stderr)
        return 1

    candidates = detect_clauses(contract_path.read_text(encoding="utf-8"), triggers)
    if args.trigger:
        candidates = tuple(c for c in candidates if c.trigger_id == args.trigger)

    payload = [
        {
            "paragraph": c.paragraph,
            "span": c.span,
            "trigger_id": c.trigger_id,
            "name": c.name,
            "candidate_statute_ids": list(c.candidate_statute_ids),
            "severity_hint": c.severity_hint,
        }
        for c in candidates
    ]
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print(
        f"# 合同 {contract_path.name}：候选 {len(payload)} 条"
        f"（触发器 {len(triggers)} 条，过滤条件 {args.trigger or '无'}）",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
