"""首次向导的示例合同端点（`GET /api/setup/sample-contract`）。

两条底线：
1. 公开可达：向导第 3 步在任何登录态之前就要能取到教人跑通的样本；
2. 文件缺失必须 404，不能静默回空串——空合同会让核对「成功」却什么都查不出。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mla.api.routes import setup as setup_routes

from .api_helpers import error_of

pytestmark = pytest.mark.no_llm


def test_sample_contract_is_public_and_non_empty(api) -> None:
    """未登录也能取到示例合同：标题可读，正文够长（示例埋在正文里，不是占位符）。"""
    response = api.client.get("/api/setup/sample-contract")

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"name", "text"}
    assert body["name"] == "示例劳动合同（含典型问题）"
    assert isinstance(body["text"], str)
    assert len(body["text"]) > 100
    assert "试用期" in body["text"]


def test_missing_sample_contract_is_404(api, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """路径指向不存在的文件：404 `NOT_FOUND`，而不是 200 + 空正文。"""
    monkeypatch.setattr(setup_routes, "SAMPLE_CONTRACT_FILE", tmp_path / "no-such-contract.txt")

    response = api.client.get("/api/setup/sample-contract")

    assert response.status_code == 404
    assert error_of(response)["code"] == "NOT_FOUND"
