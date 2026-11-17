"""无模型门禁（规格 003 第 8 章 / 13 章 P0-3）：服务层断言 + CLI 非零退出。

三件事各有断言：
1. 服务层是唯一真门禁：`run_check` 拿不到模型就抛 `NoModelError`，任务不落库；
2. 显式开关 `MLA_ALLOW_NO_LLM=1` 放行，放行后的行为与改造前一致（旧路径不受影响）；
3. CLI 无模型时 stderr 给可读提示且退出码非 0，开启关时标注"离线诊断模式"并退出 0。

CLI 一律用子进程调真实入口（`python -m mla`），环境变量显式构造，不看开发者机器的
Key / 开关 / `.streamlit/secrets.toml`（cwd 也指向临时目录）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from mla.adapters.persist import SqliteRepository
from mla.config import Paths, data_path
from mla.domain.models import Role, TaskMode, User
from mla.services.tasks import NO_MODEL_MESSAGE, NO_MODEL_QA_MESSAGE, NoModelError, run_check, run_qa

pytestmark = pytest.mark.no_llm

CONTRACT = Path(data_path("evalsets/sample_contract.txt")).read_text(encoding="utf-8")
SRC_DIR = Path(__file__).resolve().parents[1] / "src"


def _repo(tmp_path: Path) -> SqliteRepository:
    return SqliteRepository(
        paths=Paths(statutes=data_path("statutes/statutes.jsonl"), rules=data_path("rules/rules.yaml")),
        db_path=str(tmp_path / "gate.sqlite3"),
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


# ---------- 服务层门禁 ----------


def test_run_check_without_model_raises_and_persists_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """默认拒绝：抛 NoModelError，异常信息可读，且不产生任务与操作日志。"""
    monkeypatch.delenv("MLA_ALLOW_NO_LLM", raising=False)
    repo = _repo(tmp_path)
    with pytest.raises(NoModelError, match="未配置模型") as excinfo:
        run_check(user=_user(), contract_text=CONTRACT, repo=repo, enterprise_id="E1")
    assert str(excinfo.value) == NO_MODEL_MESSAGE
    assert repo.list_tasks("E1") == []
    assert repo.list_logs() == []


def test_run_check_with_switch_runs_old_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """开关打开后放行：行为与改造前逐项一致（无建议、model=none、照常落库）。"""
    monkeypatch.setenv("MLA_ALLOW_NO_LLM", "1")
    repo = _repo(tmp_path)
    task = run_check(user=_user(), contract_text=CONTRACT, repo=repo, enterprise_id="E1")

    assert task.mode is TaskMode.CHECK
    assert task.model == "none"
    assert task.suggestions == []
    assert task.verify_ok is True
    assert len(task.items) >= 4
    assert repo.list_tasks("E1")[0].task_id == task.task_id


def test_run_qa_out_of_scope_still_refuses_without_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """越界问题由纯规则拒答，根本不需要模型：保持既有"拒答结果"语义，不抛错。"""
    monkeypatch.delenv("MLA_ALLOW_NO_LLM", raising=False)
    task = run_qa(
        user=_user(),
        question="离婚时房产怎么分割",
        repo=_repo(tmp_path),
        enterprise_id="E1",
    )
    assert task.mode is TaskMode.QA
    assert task.refused is True
    assert task.boundary_layer == "terms"
    assert task.suggestions == []


def test_run_qa_in_scope_raises_without_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """范围内问题真正需要模型作答：拿不到模型就抛错，不再产出"未配置模型"的残缺任务。"""
    monkeypatch.delenv("MLA_ALLOW_NO_LLM", raising=False)
    repo = _repo(tmp_path)
    with pytest.raises(NoModelError, match="未配置模型") as excinfo:
        run_qa(user=_user(), question="试用期最长可以约定多久", repo=repo, enterprise_id="E1")
    assert str(excinfo.value) == NO_MODEL_QA_MESSAGE
    assert repo.list_tasks("E1") == []


# ---------- CLI 门禁（子进程调真实入口） ----------


def _cli_env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    """子进程环境：显式构造，不继承开发者机器的 Key / 开关取值。"""
    env = dict(os.environ)
    env.update(
        {
            "PYTHONPATH": str(SRC_DIR),
            "PYTHONIOENCODING": "utf-8",
            "MLA_EXTRACT_MODE": "regex",
            "MLA_LLM_API_KEY": "",
            "DASHSCOPE_API_KEY": "",
            "MLA_ALLOW_NO_LLM": "",
            "MLA_DB": str(tmp_path / "cli.sqlite3"),
            "MLA_INDEX_DIR": str(tmp_path / "index"),
        }
    )
    env.update(overrides)
    return env


def _run_cli(tmp_path: Path, *args: str, **overrides: str) -> subprocess.CompletedProcess[str]:
    # cwd 指到临时目录：开发机的 .streamlit/secrets.toml 不会被读到
    return subprocess.run(
        [sys.executable, "-m", "mla", *args],
        cwd=str(tmp_path),
        env=_cli_env(tmp_path, **overrides),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )


def test_cli_check_without_key_exits_non_zero(tmp_path: Path) -> None:
    """无 Key：stderr 提示指向设置页，退出码非 0，且不建库、不落任务。"""
    proc = _run_cli(tmp_path, "check", "--contract", data_path("evalsets/sample_contract.txt"))
    assert proc.returncode == 2
    assert "未配置模型" in proc.stderr
    assert "MLA_ALLOW_NO_LLM" in proc.stderr
    assert proc.stdout.strip() == ""
    assert not (tmp_path / "cli.sqlite3").exists()


def test_cli_check_with_switch_exits_zero_and_marks_offline(tmp_path: Path) -> None:
    """开关打开：退出码 0，并明确标注"离线诊断模式：无模型"。"""
    proc = _run_cli(
        tmp_path,
        "check",
        "--contract",
        data_path("evalsets/sample_contract.txt"),
        MLA_ALLOW_NO_LLM="1",
    )
    assert proc.returncode == 0
    assert "离线诊断模式：无模型" in proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["verify"]["ok"] is True
    assert payload["items"]


def test_cli_check_with_key_keeps_old_behavior(tmp_path: Path) -> None:
    """有 Key（假 Key + 不通的地址）：门禁不拦，行为与改造前一致，且绝不实际联网。"""
    proc = _run_cli(
        tmp_path,
        "check",
        "--contract",
        data_path("evalsets/sample_contract.txt"),
        MLA_LLM_API_KEY="fake-key",
        MLA_LLM_BASE_URL="http://127.0.0.1:9",
        MLA_LLM_TIMEOUT="1",
        MLA_CLAUSE_REVIEW="off",
    )
    assert proc.returncode == 0
    assert "未配置模型" not in proc.stderr
    assert "离线诊断模式" not in proc.stderr
    payload = json.loads(proc.stdout)
    assert payload["rules_loaded"] > 0
    assert payload["boundary"]["in_scope"] is True
