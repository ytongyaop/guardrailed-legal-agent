"""服务层测试：核对/问答跑完整链路并落库、写日志（显式禁用大模型，不联网）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mla.adapters.persist import SqliteRepository
from mla.config import LLMConfig, Paths, data_path, mla_home
from mla.domain.models import Role, TaskMode, User
from mla.services.tasks import has_model, run_check, run_qa

pytestmark = pytest.mark.no_llm

CONTRACT = Path(data_path("evalsets/sample_contract.txt")).read_text(encoding="utf-8")
NO_LLM = LLMConfig(api_key="")  # 测试不读 secrets、不联网
DUMMY_KEY = "sk-dummy-" + "0" * 26


def _repo(tmp_path) -> SqliteRepository:
    return SqliteRepository(
        paths=Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml")),
        db_path=str(tmp_path / "svc.sqlite3"),
    )


def _user() -> User:
    return User(
        user_id="U1",
        username="alice",
        password_hash="x",
        salt="y",
        role=Role.USER,
        enterprise_id="E1",
    )


def test_has_model_follows_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """无参时走 resolver：隔离 HOME 下无 Key → False；写入 auth.json 后 → True。"""
    assert has_model() is False

    home = mla_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(
        json.dumps({"deepseek": {"type": "api_key", "key": DUMMY_KEY}}), encoding="utf-8"
    )
    assert has_model() is True


def test_has_model_sees_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """环境变量优先：不写文件也能判为有模型。"""
    monkeypatch.setenv("MLA_LLM_API_KEY", DUMMY_KEY)
    assert has_model() is True


def test_has_model_keeps_explicit_argument_behavior() -> None:
    """显式传入仍按 api_key 判定，不看 resolver（测试与已解析配置走这条）。"""
    assert has_model(NO_LLM) is False
    assert has_model(LLMConfig(api_key=DUMMY_KEY)) is True


def test_run_check_persists_task_and_log(tmp_path) -> None:
    repo = _repo(tmp_path)
    task = run_check(user=_user(), contract_text=CONTRACT, repo=repo, enterprise_id="E1", llm=NO_LLM)

    assert task.mode is TaskMode.CHECK
    assert len(task.items) >= 4  # 试用期/工资比例/书面/竞业/加班
    assert all(it.source_id for it in task.items)
    assert task.suggestions == []  # 无模型不生成建议
    assert task.latency_s is not None
    assert task.model == "none"  # 没配模型时记 none，而不是空串

    saved = repo.list_tasks("E1")
    assert saved and saved[0].task_id == task.task_id
    assert saved[0].model == "none"
    logs = repo.list_logs()
    assert logs and logs[0].task_id == task.task_id
    assert "条目" in logs[0].detail


def test_run_qa_refuses_without_llm(tmp_path) -> None:
    repo = _repo(tmp_path)
    task = run_qa(
        user=_user(),
        question="试用期最长可以约定多久",
        repo=repo,
        enterprise_id="E1",
        llm=NO_LLM,
    )
    assert task.mode is TaskMode.QA
    assert task.refused is True
    assert task.boundary_layer == "llm"
    assert task.suggestions == []


def test_run_check_with_complete_facts_has_no_ask(tmp_path) -> None:
    repo = _repo(tmp_path)
    task = run_check(
        user=_user(),
        facts={"employer_type": "有限责任公司", "written_contract": "是", "term_years": "1"},
        repo=repo,
        llm=NO_LLM,
    )
    assert task.mode is TaskMode.CHECK
    assert task.facts["term_years"] == "1"


def test_check_ask_then_answer_flow(tmp_path) -> None:
    """第 1 步：先问一轮（不落库）→ 用户作答 → 重跑不再追问、轮数留痕、结果落库。"""
    repo = _repo(tmp_path)
    thin = "甲方聘用乙方，月工资 5000 元。"

    first = run_check(
        user=_user(),
        contract_text=thin,
        repo=repo,
        enterprise_id="E1",
        max_ask_rounds=1,
        save=False,
        llm=NO_LLM,
    )
    assert len(first.questions) == 3  # 主体类型 / 书面合同 / 合同期限
    assert first.degraded is True  # 缺阻断项，结论降级
    assert repo.list_tasks("E1") == []  # 首轮只是试探，不落库

    second = run_check(
        user=_user(),
        contract_text=thin,
        facts={"employer_type": "有限责任公司", "written_contract": "是", "term_years": "3"},
        repo=repo,
        enterprise_id="E1",
        ask_rounds=first.ask_rounds,
        max_ask_rounds=1,
        llm=NO_LLM,
    )
    assert second.questions == []
    assert second.ask_rounds == 1
    assert second.degraded is False
    assert repo.list_tasks("E1")[0].task_id == second.task_id
