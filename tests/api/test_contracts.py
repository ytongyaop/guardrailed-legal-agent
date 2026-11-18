"""合同档案（规格 006 §2）：解析、列表、读取、删除；200 MiB 上限与文件类型。

批次 2 还没有 `/checks`，"核对成功后自动存档"属于批次 3；这里的存档一律由仓储直接写入。
"""

from __future__ import annotations

import pytest

from mla.api.routes import contracts as contracts_routes

from .api_helpers import csrf_of, error_of, seed_contract, setup_admin

pytestmark = pytest.mark.no_llm

CONTRACT_TEXT = "第一条 试用期三个月。第二条 工资按月发放。"


@pytest.fixture
def admin(api) -> dict[str, str]:
    state = setup_admin(api.client)
    enterprise = api.client.get("/api/enterprise").json()
    return {
        "csrf": csrf_of(state),
        "enterprise_id": enterprise["enterprise_id"],
    }


def test_parse_json_text(api, admin) -> None:
    response = api.client.post(
        "/api/contracts/parse",
        json={"filename": "劳动合同A.txt", "text": CONTRACT_TEXT},
        headers={"X-CSRF-Token": admin["csrf"]},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["filename"] == "劳动合同A"  # 与旧界面同一规范化口径（去扩展名）
    assert body["text"] == CONTRACT_TEXT
    assert body["char_count"] == len(CONTRACT_TEXT)


def test_parse_multipart_txt(api, admin) -> None:
    response = api.client.post(
        "/api/contracts/parse",
        files={"file": ("劳动合同B.txt", CONTRACT_TEXT.encode("utf-8"), "text/plain")},
        headers={"X-CSRF-Token": admin["csrf"]},
    )
    assert response.status_code == 200
    assert response.json()["char_count"] == len(CONTRACT_TEXT)


def test_parse_unsupported_type_is_415(api, admin) -> None:
    response = api.client.post(
        "/api/contracts/parse",
        files={"file": ("病毒.exe", b"MZ", "application/octet-stream")},
        headers={"X-CSRF-Token": admin["csrf"]},
    )
    assert response.status_code == 415
    assert error_of(response)["code"] == "UNSUPPORTED_FILE_TYPE"


def test_parse_broken_docx_is_422(api, admin) -> None:
    response = api.client.post(
        "/api/contracts/parse",
        files={"file": ("坏文档.docx", b"not-a-zip", "application/octet-stream")},
        headers={"X-CSRF-Token": admin["csrf"]},
    )
    assert response.status_code == 422
    assert error_of(response)["code"] == "FILE_PARSE_ERROR"


def test_upload_limit_is_200_mib() -> None:
    assert contracts_routes.MAX_UPLOAD_BYTES == 200 * 1024 * 1024


def test_oversize_upload_is_413(api, admin, monkeypatch) -> None:
    """把上限调成 1 KiB 后传 4 KiB：只验"超限就 413"，不必真造 200 MiB 请求体。"""
    monkeypatch.setattr(contracts_routes, "MAX_UPLOAD_BYTES", 1024)
    response = api.client.post(
        "/api/contracts/parse",
        files={"file": ("大文件.txt", b"x" * 4096, "text/plain")},
        headers={"X-CSRF-Token": admin["csrf"]},
    )
    assert response.status_code == 413
    assert error_of(response)["code"] == "PAYLOAD_TOO_LARGE"


def test_contract_lifecycle(api, admin) -> None:
    contract_id = seed_contract(api.db_path, admin["enterprise_id"], contract_id="C-LIFE1")

    listed = api.client.get("/api/contracts")
    assert listed.status_code == 200
    body = listed.json()
    assert body["total"] == 1
    assert body["total_all"] == 1
    assert body["items"][0]["contract_id"] == contract_id
    assert body["items"][0]["filename"] == "合同A"
    assert body["items"][0]["char_count"] == len(CONTRACT_TEXT)

    detail = api.client.get(f"/api/contracts/{contract_id}")
    assert detail.status_code == 200
    assert detail.json()["text"] == CONTRACT_TEXT
    assert detail.json()["enterprise_id"] == admin["enterprise_id"]

    deleted = api.client.delete(f"/api/contracts/{contract_id}", headers={"X-CSRF-Token": admin["csrf"]})
    assert deleted.status_code == 204

    assert api.client.get(f"/api/contracts/{contract_id}").status_code == 404
    assert api.client.get("/api/contracts").json()["total"] == 0


def test_contract_search_and_paging(api, admin) -> None:
    for index in range(3):
        seed_contract(
            api.db_path,
            admin["enterprise_id"],
            contract_id=f"C-S{index}",
            filename=f"合同{index}.txt",
            text=f"第{index}份正文，含试用期条款。",
        )

    hits = api.client.get("/api/contracts", params={"q": "试用期"})
    assert hits.json()["total"] == 3

    none = api.client.get("/api/contracts", params={"q": "竞业限制"})
    assert none.json()["total"] == 0
    assert none.json()["total_all"] == 3

    page = api.client.get("/api/contracts", params={"page": 2, "page_size": 2})
    body = page.json()
    assert body["total"] == 3
    assert body["page"] == 2
    assert body["page_size"] == 2
    assert len(body["items"]) == 1


def test_unknown_contract_is_404(api, admin) -> None:
    assert api.client.get("/api/contracts/C-NOPE").status_code == 404
    assert (
        api.client.delete("/api/contracts/C-NOPE", headers={"X-CSRF-Token": admin["csrf"]}).status_code == 404
    )


def test_contracts_are_empty_without_enterprise(api) -> None:
    """没有企业的普通账号：列表为空分页，而不是 404 或别人的数据。"""
    from .api_helpers import register

    register(api.client, username="lonely")
    body = api.client.get("/api/contracts").json()
    assert body == {"items": [], "total": 0, "page": 1, "page_size": 50, "total_all": 0}
