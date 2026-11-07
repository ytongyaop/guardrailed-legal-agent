"""问答隐私边界：核对结果带进问答的只有结构化摘要，合同正文与条款片段不得外发。

两路都要守住：检索（含 dense_query）只用问题本身，生成提示词只用摘要里的
事实值、规则编号与条款段落号 / 等级。
"""

from __future__ import annotations

import json
from pathlib import Path

from mla.adapters.extract import RegexExtractor
from mla.adapters.persist import SqliteRepository
from mla.config import Paths, load_config
from mla.domain.models import ClauseFinding, Role, StatuteEntry, User
from mla.domain.rules import load_rules
from mla.graph.nodes import Deps
from mla.services import tasks as tasks_module
from mla.services.tasks import run_check, run_qa

SENTINEL = "SENTINEL_CONTRACT_ONLY_9F3A"
SPAN_SENTINEL = "SENTINEL_SPAN_ONLY_7B21"

CONTRACT = f"""劳动合同

甲方：某某科技有限公司
乙方：员工

第一条 本合同期限为 3 年，试用期为 12 个月。
第二条 乙方应遵守保密义务，标记 {SENTINEL} 只出现在本合同正文里。
第三条 试用期工资为转正工资的 60%。
"""


class CapturingRetriever:
    """记录每次检索的 query / dense_query，并返回给定条文。"""

    def __init__(self, hits: list[StatuteEntry]) -> None:
        self.hits = hits
        self.calls: list[dict[str, str | None]] = []

    def search(self, query: str, top_k: int = 8, dense_query: str | None = None) -> list[StatuteEntry]:
        self.calls.append({"query": query, "dense_query": dense_query})
        return list(self.hits)

    def health(self) -> bool:
        return True


class CapturingGenerator:
    """记录模型实际收到的全部文本（工具调用消息与固定作答的承载体）。"""

    available = True
    usage_in = 0
    usage_out = 0

    def __init__(self) -> None:
        self.prompts: list[str] = []

    def generate(self, system: str, user: str, schema: dict | None = None) -> dict:
        self.prompts.append(f"{system}\n{user}")
        return {"answers": [{"text": "试用期最长不得超过六个月。", "source_ids": ["LCL-19"]}]}

    def chat(self, messages: list[dict], tools: list | None = None, response_format=None) -> dict:
        self.prompts.append(json.dumps(messages, ensure_ascii=False))
        return {"content": json.dumps({"answers": []})}

    def classify(self, text: str, labels: list[str]) -> str:
        return labels[0]


def _repo(tmp_path) -> SqliteRepository:
    return SqliteRepository(
        paths=Paths(statutes="data/statutes/statutes.jsonl", rules="data/rules/rules.yaml"),
        db_path=str(tmp_path / "privacy.sqlite3"),
    )


def _user() -> User:
    return User(
        user_id="U1",
        username="privacy",
        password_hash="x",
        salt="y",
        role=Role.USER,
        enterprise_id="E1",
    )


def test_qa_context_and_prompts_exclude_contract_text(tmp_path, monkeypatch) -> None:
    from mla.ui.app import _qa_context

    repo = _repo(tmp_path)
    task = run_check(
        user=_user(),
        contract_text=CONTRACT,
        repo=repo,
        enterprise_id="E1",
        save=False,
    )
    # 条款审查结论在这里手工挂上：只验证上下文构造与提示词，不跑模型审查。
    task.clause_findings = [
        ClauseFinding(
            paragraph=3,
            span=SPAN_SENTINEL,
            grade="A",
            reason="试用期工资低于约定工资的百分之八十",
            statute_ids=("LCL-20",),
            trigger_id="TR-PROBATION-WAGE-BASE",
        )
    ]

    context = _qa_context(task)
    assert set(context) == {"facts", "items", "clauses"}
    assert "contract_text" not in context
    assert context["facts"], "这次核对应当抽到结构化事实，否则摘要为空、断言等于没跑"
    assert context["items"], "这次核对应当命中规则，否则摘要里没有规则清单"

    hits = [statute for statute in (repo.get_statute("LCL-19"),) if statute is not None]
    assert hits
    retriever = CapturingRetriever(hits)
    generator = CapturingGenerator()
    deps = Deps(
        rules=load_rules(Path("data/rules/rules.yaml")),
        retriever=retriever,
        generator=generator,
        extractor=RegexExtractor(),
        repo=repo,
        policy=load_config().policy,
    )
    monkeypatch.setattr(tasks_module, "_deps", lambda *args, **kwargs: deps)

    answer = run_qa(
        user=_user(),
        question="试用期约定有什么风险？",
        repo=repo,
        enterprise_id="E1",
        context=context,
    )
    assert answer.suggestions, "模型应当给出带依据的回答，否则测不到生成路"

    assert retriever.calls
    for call in retriever.calls:
        assert SENTINEL not in str(call["query"])
        assert SENTINEL not in str(call["dense_query"] or "")

    assert generator.prompts
    for prompt in generator.prompts:
        assert SENTINEL not in prompt
        assert SPAN_SENTINEL not in prompt
    # 非空断言：摘要确实进了提示词，而不是靠整段被丢掉才通过
    assert any("结构化事实" in prompt for prompt in generator.prompts)
