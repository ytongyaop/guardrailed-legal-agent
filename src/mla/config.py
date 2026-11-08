"""配置：可调参数集中处。用 `load_config()` 显式加载，便于测试与三档消融各自传参。

密钥只从环境变量读，不写进仓库、不落盘：
    MLA_LLM_PROVIDER / MLA_LLM_MODEL / MLA_LLM_API_KEY / MLA_LLM_BASE_URL
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DATA_DIR = Path(__file__).resolve().parent / "data"


def data_path(relative: str) -> str:
    """把相对包内数据目录的路径解析成绝对路径字符串。

    数据随包分发：仓库里是 `src/mla/data`，安装后在 `site-packages/mla/data`。
    因此不能依赖运行时工作目录。需要指向别处时用对应的 `MLA_*` 环境变量覆盖。
    """
    return str(PACKAGE_DATA_DIR / relative)


def _cwd_path(name: str) -> str:
    """运行期可写文件（数据库 / 向量缓存）默认放在工作目录，不写进只读的包目录。"""
    return str(Path.cwd() / name)


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


@dataclass(frozen=True)
class LLMConfig:
    provider: str = "deepseek"
    model: str = "deepseek-chat"
    api_key: str = ""
    base_url: str = "https://api.deepseek.com"
    temperature: float = 0.0
    timeout_s: int = 60


@dataclass(frozen=True)
class EmbeddingConfig:
    provider: str = "aliyun"
    model: str = "qwen3.7-text-embedding-flash"
    api_key: str = ""
    base_url: str = "https://dashscope.aliyuncs.com"
    dimension: int = 1024
    timeout_s: int = 30


@dataclass(frozen=True)
class RetrievalConfig:
    top_k: int = 8
    rrf_k: int = 60
    embed_model: str = "qwen3.7-text-embedding-flash"
    # 向量缓存是可写的运行期产物，默认落在工作目录（安装后包目录可能只读）
    index_dir: str = field(default_factory=lambda: str(Path.cwd() / "data" / "index"))


@dataclass(frozen=True)
class PolicyConfig:
    max_ask_rounds: int = 3
    max_retry: int = 1
    deadline_s: int = 90
    require_source_id: bool = True


@dataclass(frozen=True)
class ExtractionConfig:
    mode: str = "auto"  # auto / llm / regex
    max_chars: int = 20000


@dataclass(frozen=True)
class ClauseReviewConfig:
    """条款级审查（存在性域）开关：默认开启；显式关闭后只出确定性规则结论。"""

    enabled: bool = True
    batch_size: int = 5  # 每次模型调用判定的候选条数（4 至 5 条，减少串行耗时）
    max_extra: int = 3  # 模型自提发现的封顶条数（一律降为 C 级）


@dataclass(frozen=True)
class MissingTermsConfig:
    """必备条款缺项检测：正文短于阈值时一律"需人工确认"，不产生缺失结论。"""

    min_chars: int = 400


@dataclass(frozen=True)
class Paths:
    statutes: str = data_path("statutes/statutes.jsonl")
    rules: str = data_path("rules/rules.yaml")
    cases: str = data_path("cases/cases.jsonl")
    clauses: str = data_path("clauses/triggers.yaml")
    missing_terms: str = data_path("clauses/missing_terms.yaml")
    evalsets: str = data_path("evalsets")
    # 数据库是运行期可写文件：默认落在工作目录，不进包目录（site-packages 可能只读）
    db: str = field(default_factory=lambda: _cwd_path("mla.sqlite3"))


@dataclass(frozen=True)
class Config:
    llm: LLMConfig
    embedding: EmbeddingConfig
    retrieval: RetrievalConfig
    policy: PolicyConfig
    paths: Paths
    extraction: ExtractionConfig
    clause_review: ClauseReviewConfig = field(default_factory=ClauseReviewConfig)
    missing_terms: MissingTermsConfig = field(default_factory=MissingTermsConfig)


def _truthy(value: str) -> bool:
    """开关解析：on / true / 1 / yes 为开，其余为关。"""
    return value.strip().lower() in ("on", "true", "1", "yes")


def _secrets_section(name: str) -> dict:
    path = Path(".streamlit/secrets.toml")
    if not path.exists():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    section = data.get(name, {})
    return section if isinstance(section, dict) else {}


def _secrets_api_key() -> str:
    """兼容旧调用：默认 LLM Key。"""
    return str(_secrets_section("llm").get("api_key", ""))


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """从环境变量加载配置（默认读 os.environ）。"""
    e = _env(env)
    return Config(
        llm=LLMConfig(
            provider=e.get("MLA_LLM_PROVIDER", "deepseek"),
            model=e.get("MLA_LLM_MODEL", "deepseek-chat"),
            api_key=e.get("MLA_LLM_API_KEY", "") or (_secrets_api_key() if env is None else ""),
            base_url=e.get("MLA_LLM_BASE_URL", "https://api.deepseek.com"),
            temperature=float(e.get("MLA_LLM_TEMPERATURE", "0")),
            timeout_s=int(e.get("MLA_LLM_TIMEOUT", "60")),
        ),
        embedding=EmbeddingConfig(
            provider=e.get("MLA_EMBED_PROVIDER", "aliyun"),
            model=e.get("MLA_EMBED_MODEL", "qwen3.7-text-embedding-flash"),
            api_key=e.get("DASHSCOPE_API_KEY", "")
            or (str(_secrets_section("embedding").get("api_key", "")) if env is None else ""),
            base_url=e.get("MLA_EMBED_BASE_URL", "https://dashscope.aliyuncs.com"),
            dimension=int(e.get("MLA_EMBED_DIM", "1024")),
            timeout_s=int(e.get("MLA_EMBED_TIMEOUT", "30")),
        ),
        retrieval=RetrievalConfig(index_dir=e.get("MLA_INDEX_DIR", str(Path.cwd() / "data" / "index"))),
        policy=PolicyConfig(),
        extraction=ExtractionConfig(
            mode=e.get("MLA_EXTRACT_MODE", "auto").strip().lower(),
            max_chars=int(e.get("MLA_EXTRACT_MAX_CHARS", "20000")),
        ),
        clause_review=ClauseReviewConfig(
            enabled=_truthy(e.get("MLA_CLAUSE_REVIEW", "on")),
            batch_size=int(e.get("MLA_CLAUSE_REVIEW_BATCH", "5")),
            max_extra=int(e.get("MLA_CLAUSE_REVIEW_MAX_EXTRA", "3")),
        ),
        missing_terms=MissingTermsConfig(
            min_chars=int(e.get("MLA_MISSING_MIN_CHARS", "400")),
        ),
        paths=Paths(
            statutes=e.get("MLA_STATUTES", data_path("statutes/statutes.jsonl")),
            rules=e.get("MLA_RULES", data_path("rules/rules.yaml")),
            cases=e.get("MLA_CASES", data_path("cases/cases.jsonl")),
            clauses=e.get("MLA_CLAUSES", data_path("clauses/triggers.yaml")),
            missing_terms=e.get("MLA_MISSING_TERMS", data_path("clauses/missing_terms.yaml")),
            evalsets=e.get("MLA_EVALSETS", data_path("evalsets")),
            db=e.get("MLA_DB", _cwd_path("mla.sqlite3")),
        ),
    )
