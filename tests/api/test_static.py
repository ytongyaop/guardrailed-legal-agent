"""生产静态托管（规格 006 §1 / AC-09）。

断言四件事：

1. 单进程同时服务 SPA 与 API：`/` 与深层路由（`/history/T-1`）都回 `index.html`；
2. `/assets/*` 是 Vite 的资源目录，`public/` 产物（`/logo.svg`）按原路径直出；
3. 未知 `/api/*` 仍是 006 的 JSON 404，`docs/redoc/openapi.json` 也保持 404（未被回退成 200）；
4. 构建产物缺失时给出可执行的提示且不崩溃，`/api` 照常工作。

测试自己造一个最小的构建目录，不依赖 `web/dist` 是否存在（CI 上不一定先跑 `npm run build`）。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mla.api.app import MISSING_BUILD_HINT, STATIC_DIR_ENV, _safe_static_file, create_app, resolve_static_dir

pytestmark = pytest.mark.no_llm

INDEX_HTML = '<!doctype html><html><body><div id="app"></div></body></html>'


@pytest.fixture
def dist(tmp_path: Path) -> Path:
    """最小前端构建产物：index.html + assets/ + public 直出文件。"""
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (root / "assets" / "app-abc123.js").write_text("console.log('spa')\n", encoding="utf-8")
    (root / "logo.svg").write_text("<svg xmlns='http://www.w3.org/2000/svg'></svg>\n", encoding="utf-8")
    return root


@pytest.fixture
def spa(dist: Path) -> Iterator[TestClient]:
    app = create_app(static_dir=dist)
    with TestClient(app) as client:
        yield client


def test_root_serves_spa_entry(spa: TestClient) -> None:
    response = spa.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<div id="app"></div>' in response.text


@pytest.mark.parametrize("path", ["/history/T-1", "/history/T-1?x=1", "/settings/deep/route"])
def test_deep_route_falls_back_to_index(spa: TestClient, path: str) -> None:
    """刷新深层路由不能 404：浏览器直接访问时服务器不解析前端路由，只能回入口。"""
    response = spa.get(path)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert '<div id="app"></div>' in response.text


def test_assets_and_public_files_are_served(spa: TestClient) -> None:
    script = spa.get("/assets/app-abc123.js")
    assert script.status_code == 200
    assert "console.log('spa')" in script.text
    logo = spa.get("/logo.svg")
    assert logo.status_code == 200
    assert "<svg" in logo.text


def test_unknown_api_is_json_404(spa: TestClient) -> None:
    """回退只服务页面；`/api/*` 未命中必须是 JSON 错误体，不能被 index.html 盖掉。"""
    response = spa.get("/api/nope")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert body["error"]["code"] == "NOT_FOUND"
    assert set(body) == {"error", "request_id"}


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
def test_disabled_docs_stay_404(spa: TestClient, path: str) -> None:
    """文档端点默认关闭（006 未决问题 9）：SPA 回退不得把它们变成 200。"""
    response = spa.get(path)
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")


def test_api_still_works_with_spa_mounted(spa: TestClient) -> None:
    response = spa.get("/api/setup")
    assert response.status_code == 200
    assert "needs_admin" in response.json()


def test_traversal_cannot_escape_build_dir(dist: Path) -> None:
    """`..` 越界的路径解析成构建目录外的文件时必须返回 `None`（不能读到源码/密钥）。"""
    assert _safe_static_file(dist, "assets/app-abc123.js") is not None
    assert _safe_static_file(dist, "../outside.txt") is None
    assert _safe_static_file(dist, "../../etc/passwd") is None


def test_missing_build_gives_actionable_hint(tmp_path: Path) -> None:
    """没有构建产物：页面回 503 + 可执行提示；`/api` 与 JSON 404 的行为不变。"""
    empty = tmp_path / "not-built"
    empty.mkdir()
    app = create_app(static_dir=empty)
    with TestClient(app) as client:
        page = client.get("/")
        assert page.status_code == 503
        assert "npm --prefix web run build" in page.text
        assert MISSING_BUILD_HINT in page.text
        assert client.get("/api/setup").status_code == 200
        missing = client.get("/api/nope")
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "NOT_FOUND"


def test_static_dir_resolution_order(dist: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """显式参数优先于环境变量；两者都没有时按包内 static/ → web/dist 查找。"""
    assert resolve_static_dir(dist) == dist.resolve()
    monkeypatch.setenv(STATIC_DIR_ENV, str(dist))
    assert resolve_static_dir() == dist.resolve()
    explicit_other = tmp_path / "other"
    (explicit_other / "assets").mkdir(parents=True)
    (explicit_other / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    assert resolve_static_dir(explicit_other) == explicit_other.resolve()
    # 指向空目录时视为「没构建」而不是「构建在这里」
    monkeypatch.setenv(STATIC_DIR_ENV, str(tmp_path / "nope"))
    assert resolve_static_dir(tmp_path / "nope") is None
