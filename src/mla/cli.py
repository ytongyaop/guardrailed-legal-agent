"""命令行入口：核对一份合同文本或一组事实，走 LangGraph 状态图。

    python -m mla check --contract src/mla/data/evalsets/sample_contract.txt --trace
    python -m mla check --facts src/mla/data/evalsets/sample_facts.json

`--trace` 打印实际经过的节点顺序（证明"行为可复现"）；校验不通过退出码为 1。
无模型时核对被门禁拒绝（退出码 2，提示指向设置页）；`MLA_ALLOW_NO_LLM=1` 时放行进离线诊断。
"""

from __future__ import annotations

import argparse
import getpass
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
from .services.bootstrap import STATUS_FAILED, ensure_runtime_ready
from .services.credentials import (
    DEFAULT_PROVIDER,
    CredentialError,
    clear_key,
    list_key_status,
    record_check_status,
    resolve_key,
    resolve_llm_config,
    set_key,
)
from .services.retrieval import build_retriever
from .services.tasks import NO_MODEL_MESSAGE, allow_no_model

# 校验失败的三类固定文案（规格 002 第 10.9 节），与设置页同一套口径。
# 不从界面层导入：文案属于领域口径，CLI 与前端拿到的是同一份字符串。
AUTH_PROBE_MESSAGES: dict[str, str] = {
    "auth": "Key 不正确，请检查后重新输入",
    "network": "网络不通，请稍后重试（本次未保存）",
    "server": "服务暂时不可用，本次未保存",
}


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


def _runtime_db_path(args: argparse.Namespace) -> str | None:
    """`main()` 引导运行目录后写入 `args.runtime_db_path`，让各命令用同一个库。

    直接调用命令函数（不带 `main()`）时返回 `None`，`SqliteRepository` 回落到配置默认路径。
    """
    return getattr(args, "runtime_db_path", None)


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
    # 门禁（规格 003 第 8 章「无模型时可看不可新算」）：模型是核对的大脑。
    # 放在读规则 / 建检索器之前，保证被拒时不建库、不落任务、不产生任何记录。
    # Key 统一由 resolver 解析：环境变量 > auth.json > 未配置；旧库 Key 已由引导阶段
    # 一次性迁移并清理（规格 002 第 7 章），解析路径不再读数据库。
    llm_cfg, _resolved = resolve_llm_config(cfg)
    offline = allow_no_model()
    if llm_cfg is None and not offline:
        print(NO_MODEL_MESSAGE, file=sys.stderr)
        print("（离线诊断可用：设置 MLA_ALLOW_NO_LLM=1 后照常输出规则与缺项结果）", file=sys.stderr)
        # 退出码 2：1 已表示"跑完但校验未过"，这里表示"命令根本没跑起来"，
        # 与 argparse 用法错误同属一类（都没产出可用结论）。
        return 2
    if llm_cfg is None:
        print("离线诊断模式：无模型（MLA_ALLOW_NO_LLM=1），结论只来自规则与缺项检测。", file=sys.stderr)

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
        generator=DeepSeekLLM(llm_cfg) if llm_cfg is not None else None,
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

    repo = SqliteRepository(db_path=_runtime_db_path(args))
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


def cmd_admin(args: argparse.Namespace) -> int:
    """本地找回：列账号 / 重置口令 / 提升管理员。

    本地单机应用没有邮箱与短信，信任模型是"能读写这台机器上的库就能重置"，
    所以找回只走命令行，不做网页找回。输出里不出现哈希、盐或明文以外的口令。
    """
    from .adapters.persist import SqliteRepository
    from .domain.models import Role
    from .services.auth import MIN_PASSWORD_LEN, hash_password

    repo = SqliteRepository(db_path=_runtime_db_path(args))

    if args.admin_cmd == "list":
        users = repo.list_users()
        if not users:
            print("尚无账号。")
            return 0
        print("用户名｜角色｜企业绑定")
        for user in users:
            bound = "已绑定企业" if user.enterprise_id else "未绑定企业"
            print(f"{user.username}｜{user.role}｜{bound}")
        return 0

    username = args.username.strip()
    user = repo.get_user_by_name(username)
    if user is None:
        print(f"账号不存在：{username}", file=sys.stderr)
        return 1

    if args.admin_cmd == "reset":
        generated = not args.password
        password = args.password or _random_password()
        if not generated and len(password) < MIN_PASSWORD_LEN:
            print(f"口令至少 {MIN_PASSWORD_LEN} 位。", file=sys.stderr)
            return 1
        digest, salt = hash_password(password)
        repo.set_user_password(user.user_id, digest, salt)
        print(f"已重置口令：{username}")
        if generated:
            print(f"新口令：{password}")
            print("请立即记录，之后不再显示。")
        return 0

    if user.role is Role.ADMIN:
        print(f"{username} 已是管理员。")
        return 0
    repo.set_user_role(user.user_id, str(Role.ADMIN))
    print(f"已提升为管理员：{username}")
    return 0


def _file_entry_exists(provider: str) -> bool:
    """凭据文件里是否有该 provider 条目：把环境变量映射清空，只看 `auth.json` 这一级。"""
    return resolve_key(provider, env={}).source == "auth_file"


def cmd_auth(args: argparse.Namespace) -> int:
    """管理服务商 API Key（规格 002 第 6 章 CLI 开放问题建议、第 10.9 节）。

    - `list`：只显示 provider / 类型 / 来源标签 / 凭据文件路径，**不含 Key、不含长度**；
    - `set`：`getpass` 隐藏输入（不接受命令行参数传 Key，避免进 shell 历史），
      保存前先发 `GET /models` 校验，校验不通过不落盘；
    - `remove`：只删文件里对应 provider 条目，幂等；环境变量需用户自行管理。

    Key 一律不进 stdout / stderr：成功只报结论，失败只报 10.9 的固定文案或路径类诊断。
    """
    try:
        statuses = list_key_status()
    except CredentialError as exc:
        # 文件损坏是显式配置错误：给出可操作的修复提示，不吐堆栈、不静默当“未配置”
        print(f"无法读取凭据文件：{exc}", file=sys.stderr)
        return 1
    if args.auth_cmd == "list":
        # `list_key_status()` 覆盖全部 provider；只挑真正有来源的（env 或 auth.json）
        configured = [status for status in statuses if status.source != "none"]
        if not configured:
            print("未配置任何服务商凭据。")
            return 0
        print("服务商｜类型｜来源｜凭据文件")
        for status in configured:
            print(f"{status.provider}｜{status.type}｜{status.label}｜{status.path}")
        return 0

    provider = (getattr(args, "provider", "") or DEFAULT_PROVIDER).strip().lower()
    supported = [status.provider for status in statuses]
    if provider not in supported:
        print(f"未支持的服务商：{provider}（当前可选：{'、'.join(supported)}）", file=sys.stderr)
        return 1

    if args.auth_cmd == "remove":
        try:
            exists = _file_entry_exists(provider)
        except CredentialError as exc:
            print(f"无法读取凭据文件：{exc}", file=sys.stderr)
            return 1
        if not exists:
            print("未配置该服务商凭据。")
            return 0
        try:
            clear_key(provider)
        except CredentialError as exc:
            # 异常消息只含 provider 与路径，不含 Key
            print(f"清除失败：{exc}", file=sys.stderr)
            return 1
        print(f"已清除 {provider} 的 Key。")
        return 0

    try:
        raw = getpass.getpass(f"请输入 {provider} 的 API Key（输入不回显）：")
    except (EOFError, KeyboardInterrupt):
        # 非交互环境（管道输入、Ctrl+C）按“没输入”处理：不打堆栈，也不写文件
        print("未输入 Key，未做任何改动。", file=sys.stderr)
        return 1
    key = raw.strip()
    if not key:
        print("未输入 Key，未做任何改动。", file=sys.stderr)
        return 1
    # 唯一的结构化校验出口：只发 GET /models，不构造 messages，不产生 token 费用
    probe = DeepSeekLLM(load_config().llm).probe_models(key)
    if probe.kind != "ok":
        print(AUTH_PROBE_MESSAGES.get(probe.kind, AUTH_PROBE_MESSAGES["server"]), file=sys.stderr)
        return 1
    try:
        set_key(provider, key)
    except CredentialError as exc:
        print(f"保存失败：{exc}", file=sys.stderr)
        return 1
    try:
        record_check_status(provider, True, "ok")
    except OSError:
        # 校验历史是可丢的展示数据：写失败只让状态行回落"未校验"，不回滚已成功的保存
        pass
    print("已保存到用户凭据文件（连接校验通过）")
    return 0


def _bootstrap_runtime(args: argparse.Namespace) -> None:
    """解析出命令后、执行前引导运行目录：旧库复制与首次凭据迁移，并把结果路径写回 `args`。

    `--version` 由 argparse 在解析阶段直接退出，因此这类不碰数据库的命令不会触发复制。
    诊断只打到 stderr，不含任何 Key；迁移失败必须可见，不能静默显示成成功。
    """
    report = ensure_runtime_ready()
    args.runtime_db_path = report.db_path
    if report.copied_from:
        print(
            f"已从旧数据库复制业务数据到 {report.db_path}；旧库保留在 {report.copied_from}",
            file=sys.stderr,
        )
    for warning in report.warnings:
        print(f"警告：{warning}", file=sys.stderr)
    for message in report.migration.messages:
        prefix = "警告：" if report.migration.status == STATUS_FAILED else ""
        print(f"{prefix}{message}", file=sys.stderr)


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

    p_admin = sub.add_parser("admin", help="本地找回：列账号、重置口令、提升管理员")
    admin_sub = p_admin.add_subparsers(dest="admin_cmd", required=True)
    p_admin_list = admin_sub.add_parser("list", help="列出所有账号（不含口令信息）")
    p_admin_list.set_defaults(func=cmd_admin)
    p_admin_reset = admin_sub.add_parser("reset", help="重置指定账号的口令")
    p_admin_reset.add_argument("--username", required=True)
    p_admin_reset.add_argument(
        "--password",
        default=None,
        help="新口令；不传则随机生成 16 位并只打印一次，指定时至少 8 位",
    )
    p_admin_reset.set_defaults(func=cmd_admin)
    p_admin_promote = admin_sub.add_parser("promote", help="把指定账号提升为管理员")
    p_admin_promote.add_argument("--username", required=True)
    p_admin_promote.set_defaults(func=cmd_admin)

    # `auth`：服务商 Key 的 CLI 管理闭环（与设置页调同一套 `services.credentials`）。
    # `set` **没有**接收 Key 的参数：Key 只能从隐藏输入读，避免进 shell 历史。
    p_auth = sub.add_parser("auth", help="管理服务商 API Key（用户级 auth.json）")
    auth_sub = p_auth.add_subparsers(dest="auth_cmd", required=True)
    p_auth_list = auth_sub.add_parser("list", help="列出已配置的服务商（不含 Key 与长度）")
    p_auth_list.set_defaults(func=cmd_auth)
    p_auth_set = auth_sub.add_parser(
        "set", help="隐藏输入保存服务商 Key（先做 GET /models 校验，不通过不落盘）"
    )
    p_auth_set.add_argument(
        "provider", nargs="?", default=DEFAULT_PROVIDER, help="服务商键名（默认 deepseek）"
    )
    p_auth_set.set_defaults(func=cmd_auth)
    p_auth_remove = auth_sub.add_parser("remove", help="清除某服务商 Key（幂等；环境变量需自行管理）")
    p_auth_remove.add_argument(
        "provider", nargs="?", default=DEFAULT_PROVIDER, help="服务商键名（默认 deepseek）"
    )
    p_auth_remove.set_defaults(func=cmd_auth)

    args = parser.parse_args(argv)
    _bootstrap_runtime(args)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
