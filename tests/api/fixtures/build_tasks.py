"""批次 1「冻结基线」的夹具构造与快照函数（可重放，规格 006 第 1 批）。

为什么单独一个模块而不是写进测试文件：
- 这批要冻的是**领域输出**（`summarize_verdict` / `sort_items` / `merge_findings` /
  `build_report_docx`），夹具必须先能"再造一次"，测试再拿去和 JSON 字面量比；
- 录制（写 JSON / TXT）与断言（读 JSON / TXT）必须用**同一份构造代码**，否则两边一漂，
  快照就成了自说自话。因此构造逻辑放这里，`python tests/api/fixtures/build_tasks.py`
  负责录制，`tests/api/test_legacy_snapshot.py` 负责断言。

刻意**不 import 任何界面模块**：0.4.0 批次 6 已删除 Streamlit 界面，快照只冻领域输出，
所以它今天仍然能重放。这里只碰 `domain/` 与 `services/`（报告导出）。

模型一律打桩：`tests/helpers.py` 的 `FakeGenerator`，按条款审查请求里的候选逐条回放
"等级 + 该候选自带的法条原文"，零网络、零 Key、同输入同输出。
"""

from __future__ import annotations

import io
import json
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from docx import Document

HERE = Path(__file__).resolve().parent
TESTS_DIR = HERE.parents[1]
ROOT = TESTS_DIR.parent
for _extra in (str(TESTS_DIR), str(ROOT / "src")):
    if _extra not in sys.path:
        sys.path.insert(0, _extra)

from helpers import FakeGenerator  # noqa: E402

from mla.adapters.persist import SqliteRepository  # noqa: E402
from mla.config import LLMConfig, Paths, data_path  # noqa: E402
from mla.domain.grading import display_of, sort_items  # noqa: E402
from mla.domain.models import CheckItem, CheckTask, ClauseFinding, Role, User  # noqa: E402
from mla.domain.verdict import summarize_verdict  # noqa: E402
from mla.services.report import build_report_docx, merge_findings  # noqa: E402
from mla.services.tasks import run_check  # noqa: E402

# 打桩模型的 Key：只为让 `has_model` 为真（真 Key 一律不读，`_deps` 也不联网）。
FAKE_KEY = "fixture-key-not-a-real-credential"
# 企业档案事实（等价界面里用户填的"用工主体类型"）。期限、试用期、加班时长这类**合同自身**的
# 数值一律交给正则抽取，不在事实里预置——两个来源同时给同一个字段，会让快照里"事实说 3 年、
# 规则却按 2 年判"这种矛盾混进基线。
BASE_FACTS = {"employer_type": "有限责任公司", "written_contract": "是"}
# 报告里的易变值：任务号是随机 uuid、生成日期是当天。快照只冻内容，不冻这两项。
TASK_ID_PLACEHOLDER = "<task-id>"
DATE_PLACEHOLDER = "<date>"
_DATE_ROW = re.compile(r"^生成日期\t.*$", re.MULTILINE)


@dataclass(frozen=True)
class FixtureSpec:
    """一个夹具：合同正文 + 路径开关 + 打桩模型的等级映射。

    `grades` 是 `trigger_id → 等级`；不在表里的候选模型不给结论（等价"这条没查出来"）。
    等级是**打桩模型自己说的**，程序核验与共识层照常跑——快照冻的是核验、降级、合并之后
    的最终结果，不是我们希望的等级。
    """

    name: str
    contract: str
    model_first: bool
    clause_review: bool
    grades: dict[str, str] = field(default_factory=dict)
    note: str = ""


# 主合同（7 个触发器候选，跨 高/中/低 三个影响度；含 3 条规则违规供旧路径产出规则条目）
CONTRACT_MIXED = "contract_mixed.txt"
# 安静合同：没有任何触发器候选、也没有可抽取的数值事实 —— 条目为空但**不等于**"没问题"
CONTRACT_QUIET = "contract_quiet.txt"

FIXTURES: tuple[FixtureSpec, ...] = (
    FixtureSpec(
        name="model_first_rich",
        contract=CONTRACT_MIXED,
        model_first=True,
        clause_review=True,
        grades={
            "TR-PROBATION-TERM-EXCEED": "A",
            "TR-PROBATION-WAGE-BASE": "B",
            "TR-WAGE-DELAY": "A",
            "TR-OVERTIME-WAIVER": "B",
            "TR-NONCOMPETE-ALL-STAFF": "A",
            "TR-CONTRACT-COPY-INVALID": "B",
            "TR-ARBITRATION-WAIVER": "C",
        },
        note=(
            "模型主导路径的富任务：7 条模型结论，覆盖 高/中/低 三个影响度与"
            "确定冲突/待确认两档；另含程序追加的数值复核条目（source=rule_check）。"
        ),
    ),
    FixtureSpec(
        name="model_first_empty",
        contract=CONTRACT_QUIET,
        model_first=True,
        clause_review=True,
        grades={},
        note=(
            "模型主导，零条目：证明「没查出来（覆盖不完整）」与「没问题（clean）」"
            "在 verdict 上必须分成两种说法。"
        ),
    ),
    FixtureSpec(
        name="legacy_path",
        contract=CONTRACT_MIXED,
        model_first=False,
        clause_review=True,
        grades={
            "TR-PROBATION-TERM-EXCEED": "A",
            "TR-WAGE-DELAY": "A",
            "TR-ARBITRATION-WAIVER": "C",
        },
        note=(
            "旧路径（model_first=False）：items 只有规则条目，条款结论另行叠加，"
            "两者由 merge_findings 按「规则 → A → B → C」合并排序。"
        ),
    ),
    FixtureSpec(
        name="mixed_grades",
        contract=CONTRACT_MIXED,
        model_first=True,
        clause_review=True,
        grades={"TR-ARBITRATION-WAIVER": "C"},
        note=(
            "同一影响度内混合确定性：中影响组里既有程序数值复核条目（确定冲突、"
            "来源序号 1、段落 0），也有模型 C 级结论（待确认、来源序号 0、段落 15），"
            "专门锁四键排序里「确定性 → 来源 → 段落号」这三键。"
        ),
    ),
)


def _user() -> User:
    """固定账号：报告与日志里不出现随机值。"""
    return User(
        user_id="U-fixed",
        username="fixture-user",
        password_hash="x",
        salt="y",
        role=Role.USER,
        enterprise_id="E-fixed",
    )


def contract_text(spec: FixtureSpec) -> str:
    return (HERE / spec.contract).read_text(encoding="utf-8")


def _responder(grades: dict[str, str]) -> Callable[[str, str], dict[str, Any]]:
    """打桩模型的应答：只看系统提示判断是哪条链路，绝不联网。

    - 条款审查：按本批候选逐条回放。法条原文直接抄 payload 里下发的 `statutes[0].text`，
      这样 A 级的"摘抄必须是法条原文连续子串"这道核验是真的被跑到的，而不是被绕开；
    - 处理建议：回空（夹具不冻建议文案，避免把提示词变化牵进快照）；
    - 其它链路：空对象（抽取走正则档，不会到这里）。
    """

    def respond(system: str, user: str) -> dict[str, Any]:
        if "条款风险审查" in system:
            try:
                payload = json.loads(user)
            except ValueError:  # 返工轮次的入参是 messages 形态，回空即"没修好"
                return {"findings": [], "extra": []}
            findings: list[dict[str, Any]] = []
            for idx, candidate in enumerate(payload.get("candidates") or []):
                grade = grades.get(str(candidate.get("trigger_id") or ""))
                if grade is None:
                    continue
                statutes = candidate.get("statutes") or []
                statute_id = str(statutes[0]["statute_id"]) if statutes else ""
                findings.append(
                    {
                        "idx": idx,
                        "grade": grade,
                        "reason": f"{grade} 级示例理由（{candidate['trigger_id']}）",
                        "statute_ids": [statute_id] if statute_id else [],
                        "quote_statute": str(statutes[0]["text"])[:60] if statutes else "",
                        "evidence": candidate["span"],
                        "need_facts": [],
                    }
                )
            return {"findings": findings, "extra": []}
        if "合规顾问" in system:
            return {"suggestions": []}
        return {}

    return respond


def make_repo(db_path: str) -> SqliteRepository:
    """法源 / 规则读包内数据，库文件放调用方给的临时路径（不碰工作目录的 mla.sqlite3）。"""
    paths = Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml"))
    return SqliteRepository(paths=paths, db_path=db_path)


def build_task(spec: FixtureSpec, *, patch: Any, repo: SqliteRepository) -> CheckTask:
    """重放一个夹具：同一份合同 + 同一份打桩应答 → 同一个 `CheckTask`（除随机任务号/耗时）。

    `patch` 传 pytest 的 `monkeypatch`（测试）或本模块的 `EnvPatch`（录制脚本），
    两者接口相同，保证录制与断言走同一条路径。
    """
    patch.setenv("MLA_MODEL_FIRST", "on" if spec.model_first else "off")
    patch.delenv("MLA_ALLOW_NO_LLM", raising=False)
    patch.setattr("mla.services.tasks.DeepSeekLLM", lambda cfg: FakeGenerator(_responder(spec.grades)))
    return run_check(
        user=_user(),
        contract_text=contract_text(spec),
        facts=dict(BASE_FACTS),
        enterprise_id="E-fixed",
        repo=repo,
        save=False,  # 快照不落库：不写数据库、不写日志
        llm=LLMConfig(api_key=FAKE_KEY),
        clause_review=spec.clause_review,
    )


# ---------------- 快照：只取领域输出 ----------------


def _item_row(item: CheckItem) -> dict[str, Any]:
    """条目快照：展示层级现算一次（`display_of` 是唯一真源），四键排序键一并留档。"""
    level = display_of(item)
    return {
        "item_id": item.item_id,
        "finding": item.finding,
        "impact": level.impact,
        "certainty": level.certainty,
        "sort_key": list(level.sort_key),
        "grade": item.grade,
        "source": item.source,
        "element_id": item.element_id,
        "clause_ref": item.clause_ref,
        "kind": str(item.kind),
        "severity": str(item.severity),
        "source_id": item.source_id,
        "statute_ids": list(item.statute_ids),
        "evidence_text": item.evidence_text,
        "note": item.note,
        "rule_id": item.rule_id,
    }


def _merge_row(rank: int, entry: object) -> dict[str, Any]:
    """`merge_findings` 的一行：规则条目和条款结论都记下，`rank` 就是合并档位。"""
    if isinstance(entry, CheckItem):
        return {
            "rank": rank,
            "kind": "item",
            "item_id": entry.item_id,
            "finding": entry.finding,
            "rule_id": entry.rule_id,
            "source_id": entry.source_id,
        }
    assert isinstance(entry, ClauseFinding), entry
    return {
        "rank": rank,
        "kind": "clause_finding",
        "paragraph": entry.paragraph,
        "grade": entry.grade,
        "trigger_id": entry.trigger_id,
        "source": entry.source,
        "reason": entry.reason,
        "statute_ids": list(entry.statute_ids),
    }


def report_text(task: CheckTask, repo: SqliteRepository) -> str:
    """取 Word 报告的正文（段落 + 表格单元格），并把随机任务号与当天日期换成占位符。

    只取文字、不存二进制：docx 是 zip 容器，字节流里含时间戳与压缩差异，逐个字节比没有意义；
    用户看得见的段落与表格文字才是要冻的东西。
    """
    document = Document(io.BytesIO(build_report_docx(task, repo)))
    lines = [paragraph.text for paragraph in document.paragraphs]
    for table in document.tables:
        for row in table.rows:
            lines.append("\t".join(cell.text for cell in row.cells))
    text = "\n".join(lines)
    text = text.replace(task.task_id, TASK_ID_PLACEHOLDER)
    return _DATE_ROW.sub(f"生成日期\t{DATE_PLACEHOLDER}", text)


def snapshot(task: CheckTask, repo: SqliteRepository) -> dict[str, Any]:
    """一个夹具的全部可冻结输出（不含易变值：任务号、耗时、token 计数、时间戳）。"""
    verdict = summarize_verdict(task)
    ordered = sort_items(task.items)
    merged = merge_findings(task.items, task.clause_findings)
    return {
        "task": {
            "model_first": bool(task.model_first),
            "clause_review": bool(task.clause_review),
            "contract_chars": task.contract_chars,
            "facts": dict(task.facts),
            "item_count": len(task.items),
            "clause_finding_count": len(task.clause_findings),
            "suggestion_count": len(task.suggestions),
        },
        "verdict": {
            "status": verdict.status,
            "risk_count": verdict.risk_count,
            "review_count": verdict.review_count,
            "headline": verdict.headline,
            "details": list(verdict.details),
        },
        "sorted_items": [_item_row(item) for item in ordered],
        "merge_findings": [_merge_row(rank, entry) for rank, entry in merged],
    }


def normalize_ws(text: str) -> str:
    """换行与首尾空白的归一化：只抹排版差异，不动任何可见字符。"""
    return "\n".join(line.strip() for line in text.replace("\r\n", "\n").split("\n") if line.strip())


def record(spec: FixtureSpec, *, patch: Any, db_path: str, out_dir: Path) -> tuple[Path, Path]:
    """录制一个夹具：写 `<name>.snapshot.json` 与 `<name>.report.txt`，返回两个路径。"""
    repo = make_repo(db_path)
    task = build_task(spec, patch=patch, repo=repo)
    payload = snapshot(task, repo)
    payload["fixture"] = spec.name
    payload["note"] = spec.note
    payload["contract"] = spec.contract
    payload["report"] = f"{spec.name}.report.txt"
    payload["spec"] = "docs/design/006-frontend-migration.md 批次 1（冻结基线）"
    json_path = out_dir / f"{spec.name}.snapshot.json"
    text_path = out_dir / f"{spec.name}.report.txt"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    # 报告正文单独存 .txt（不塞进 JSON）：逐行可读、可 diff，出差异时一眼看出改了哪句。
    text_path.write_text(report_text(task, repo), encoding="utf-8")
    return json_path, text_path


class EnvPatch:
    """最小环境的 `monkeypatch` 替身：只在录制脚本里用（pytest 走真的 monkeypatch）。"""

    def __init__(self) -> None:
        self._undo: list[tuple[str, str, Any]] = []

    def setenv(self, key: str, value: str) -> None:
        self._undo.append(("env", key, os.environ.get(key)))
        os.environ[key] = value

    def delenv(self, key: str, raising: bool = True) -> None:  # noqa: ARG002 - 与 monkeypatch 同签名
        self._undo.append(("env", key, os.environ.get(key)))
        os.environ.pop(key, None)

    def setattr(self, target: str, value: Any) -> None:
        import importlib

        module_name, _, attr = target.rpartition(".")
        module = importlib.import_module(module_name)
        self._undo.append(("attr", target, getattr(module, attr)))
        setattr(module, attr, value)

    def undo(self) -> None:
        while self._undo:
            kind, key, old = self._undo.pop()
            if kind == "env":
                if old is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = old
                continue
            import importlib

            module_name, _, attr = key.rpartition(".")
            setattr(importlib.import_module(module_name), attr, old)


def _main() -> int:
    """录制入口：临时目录隔离运行期数据，跑完写回 `tests/api/fixtures/`。"""
    tmp = ROOT / "_tmp" / "fixture-record"
    tmp.mkdir(parents=True, exist_ok=True)
    patch = EnvPatch()
    try:
        patch.setenv("MLA_EXTRACT_MODE", "regex")
        patch.setenv("MLA_HOME", str(tmp / "home"))
        patch.setenv("MLA_DB", str(tmp / "record.sqlite3"))
        patch.setenv("MLA_INDEX_DIR", str(tmp / "index"))
        patch.delenv("MLA_LLM_API_KEY", raising=False)
        patch.delenv("DASHSCOPE_API_KEY", raising=False)
        for spec in FIXTURES:
            json_path, text_path = record(
                spec, patch=patch, db_path=str(tmp / f"{spec.name}.sqlite3"), out_dir=HERE
            )
            print(f"recorded {json_path.name} / {text_path.name}")
    finally:
        patch.undo()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
