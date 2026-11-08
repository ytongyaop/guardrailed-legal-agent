"""命令行入口：核对一份合同文本或一组事实，走 LangGraph 状态图。

    python -m mla check --contract src/mla/data/evalsets/sample_contract.txt --trace
    python -m mla check --facts src/mla/data/evalsets/sample_facts.json

`--trace` 打印实际经过的节点顺序（证明"行为可复现"）；校验不通过退出码为 1。
"""

from __future__ import annotations

import argparse
import json
import secrets
import string
import sys
from pathlib import Path
from typing import Any

from . import __version__
from .adapters.llm import DeepSeekLLM
from .config import load_config
from .domain.models import MISSING_TERM_BASE, MISSING_TERM_UNJUDGED_ID, MissingStatus
from .domain.rules import load_rules
from .graph.build import build_graph
from .graph.nodes import Deps
from .graph.state import CheckState
from .services.retrieval import build_retriever


def _load_facts(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit("facts 文件必须是 JSON 对象")
    return {str(k): str(v) for k, v in data.items()}


def _load_contract(path: str | None) -> str:
    if not path:
        return ""
    return Path(path).read_text(encoding="utf-8")


def _missing_summary(terms: list) -> dict[str, Any]:
    """必备条款汇总：一行话给终端看，另带各类条款名，方便脚本取用。"""
    written = [
        t for t in terms if t.status is MissingStatus.WRITTEN and t.term_id != MISSING_TERM_UNJUDGED_ID
    ]
    absent = [t for t in terms if t.status is MissingStatus.ABSENT]
    unseen = [t for t in terms if t.status is MissingStatus.UNSEEN]
    manual = [t for t in terms if t.status is MissingStatus.MANUAL]
    judged = [t for t in terms if t.status is not MissingStatus.MANUAL]
    if not terms or not judged:
        return {"summary": "必备条款：正文过短，未做判定", "written": 0, "total": MISSING_TERM_BASE}
    line = f"必备条款：已写 {len(written)}/{MISSING_TERM_BASE}"
    if absent:
        line += f"，缺失 {len(absent)} 类（{'、'.join(t.name for t in absent)}）"
    if unseen:
        line += f"，未见 {len(unseen)} 类"
    if manual:
        line += f"，需人工确认 {len(manual)} 类"
    return {
        "summary": line,
        "written": len(written),
        "total": MISSING_TERM_BASE,
        "absent": [t.name for t in absent],
        "unseen": [t.name for t in unseen],
        "manual": [t.name for t in manual],
    }


def _run(init: CheckState, deps: Deps, trace: bool) -> tuple[dict[str, Any], list[str]]:
    graph = build_graph(deps)
    if not trace:
        return dict(graph.invoke(init)), []

    final: dict[str, Any] = dict(init)
    order: list[str] = []
    for chunk in graph.stream(init, stream_mode="updates"):
        for node, delta in chunk.items():
            order.append(node)
            if isinstance(delta, dict):
                final.update(delta)
    return final, order


def cmd_check(args: argparse.Namespace) -> int:
    cfg = load_config()
    rules = load_rules(args.rules or cfg.paths.rules)
    facts = _load_facts(args.facts)
    contract_text = _load_contract(args.contract)

    init: CheckState = {
        "task_id": args.task_id,
        "mode": "核对",
        "facts": facts,
        "contract_text": contract_text,
        "question": args.question or "",
        "variant": args.variant,
    }
    deps = Deps(
        rules=rules,
        retriever=build_retriever(cfg),
        generator=DeepSeekLLM(cfg.llm) if cfg.llm.api_key else None,
    )
    final, order = _run(init, deps, args.trace)

    items = list(final.get("items") or [])
    payload = {
        "version": __version__,
        "task_id": args.task_id,
        "variant": args.variant,
        "rules_loaded": len(rules),
        "boundary": {
            "in_scope": bool(final.get("in_scope")),
            "layer": final.get("boundary_layer") or "",
            "reason": final.get("boundary_reason") or "",
        },
        "facts_used": final.get("facts") or {},
        "ask_rounds": int(final.get("ask_rounds") or 0),
        "questions": list(final.get("questions") or []),
        "retrieved": [s.statute_id for s in (final.get("retrieved") or [])],
        "items": [
            {
                "item_id": it.item_id,
                "kind": it.kind.value,
                "finding": it.finding,
                "source_id": it.source_id,
                "rule_id": it.rule_id,
            }
            for it in items
        ],
        "missing_terms": _missing_summary(list(final.get("missing_terms") or [])),
        "suggestions": [
            {"text": s.text, "source_ids": list(s.source_ids)} for s in (final.get("suggestions") or [])
        ],
        "verify": {"ok": bool(final.get("verify_ok")), "degraded": bool(final.get("degraded"))},
        "errors": list(final.get("errors") or []),
    }
    if args.trace:
        payload["node_order"] = order

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if final.get("verify_ok") else 1


def _random_password(length: int = 16) -> str:
    """生成随机口令：字母 + 数字混排，保证两类字符都出现。"""
    alphabet = string.ascii_letters + string.digits
    while True:
        pwd = "".join(secrets.choice(alphabet) for _ in range(length))
        if any(c.isalpha() for c in pwd) and any(c.isdigit() for c in pwd):
            return pwd


def cmd_init(args: argparse.Namespace) -> int:
    """初始化：建管理员账号（首次验收用）。

    不传 `--admin-password` 时生成随机口令并只打印一次：公开部署时没有可猜的默认口令。
    """
    from .adapters.persist import SqliteRepository
    from .domain.models import Enterprise, Role
    from .services.auth import MIN_PASSWORD_LEN, register

    repo = SqliteRepository()
    if repo.get_user_by_name(args.admin_username):
        print(f"账号已存在：{args.admin_username}")
        return 1
    generated = not args.admin_password
    password = args.admin_password or _random_password()
    if not generated and len(password) < MIN_PASSWORD_LEN:
        print(f"口令至少 {MIN_PASSWORD_LEN} 位。", file=sys.stderr)
        return 1
    ent_id = ""
    if args.enterprise:
        ent_id = "E-" + args.admin_username[:6]
        repo.save_enterprise(Enterprise(enterprise_id=ent_id, name=args.enterprise))
    register(repo, args.admin_username, password, Role.ADMIN, ent_id)
    print(f"已创建管理员：{args.admin_username}（企业：{args.enterprise or '未建'}）")
    if generated:
        print(f"管理员口令：{password}")
        print("请立即记录，之后不再显示。")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mla", description="小微企业用工风险辅助系统")
    parser.add_argument("--version", action="version", version=f"mla {__version__}")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="初始化管理员账号（首次运行）")
    p_init.add_argument("--admin-username", default="admin")
    p_init.add_argument(
        "--admin-password",
        default=None,
        help="管理员口令；不传则随机生成 16 位并只打印一次，指定时至少 8 位",
    )
    p_init.add_argument("--enterprise", default="")
    p_init.set_defaults(func=cmd_init)

    p_check = sub.add_parser("check", help="核对合同文本或用工事实（走状态图）")
    p_check.add_argument("--contract", help="合同文本文件（.txt）")
    p_check.add_argument("--facts", help="事实 JSON 文件")
    p_check.add_argument("--question", help="受限问答模式下用户的问题")
    p_check.add_argument("--rules", help="规则表路径（默认取配置）")
    p_check.add_argument("--task-id", default="T-0001", help="任务 ID（写库与日志用）")
    p_check.add_argument(
        "--variant",
        default="full",
        choices=["llm_only", "rules_only", "rules_retrieval", "full"],
        help="消融档位（纯模型 / 仅规则 / 规则+检索 / 全量）",
    )
    p_check.add_argument("--trace", action="store_true", help="打印实际经过的节点顺序")
    p_check.set_defaults(func=cmd_check)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
