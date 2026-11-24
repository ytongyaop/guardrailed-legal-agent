"""配置：可调参数集中处。用 `load_config()` 显式加载，便于测试与三档消融各自传参。

**LLM 的 Key 不在这里合成**（规格 002 第 4、6 章）：本模块只装非秘密参数，
`LLMConfig.api_key` 保留为显式注入字段，默认必须为空。真正的取值由
`services.credentials.resolve_llm_config()` 统一提供，优先级为
`MLA_LLM_API_KEY` > `<MLA_HOME>/auth.json`（阶段三另有最低优先级的旧库只读兜底，
阶段四删除）。`.streamlit/secrets.toml` 不再参与 LLM Key，只保留 embedding 一侧。

向量检索（embedding）的 Key 在这里合成，优先级为
`DASHSCOPE_API_KEY` > `<MLA_HOME>/auth.json` 的 `dashscope` 条目 > 旧 secrets.toml（兼容）。
合成放在 `load_config()` 里而不是单独的 resolver：检索、设置页、CLI 全都从这里取配置，
分成两条路径就一定会有人在某一侧忘记接。

相关环境变量：
    MLA_LLM_PROVIDER / MLA_LLM_MODEL / MLA_LLM_API_KEY / MLA_LLM_BASE_URL
    MLA_LLM_TIMEOUT（推理超时） / MLA_AUTH_CHECK_TIMEOUT（只用于 GET /models 校验）
"""

from __future__ import annotations

import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

PACKAGE_DATA_DIR = Path(__file__).resolve().parent / "data"


class ConfigError(Exception):
    """运行期配置错误：路径或凭据形状不合法时抛出。"""


def _is_windows() -> bool:
    """平台判定单独成函数：生产口径就是 `os.name`，测试可替换分支。"""
    return os.name == "nt"


def _user_home() -> Path:
    """取当前用户主目录；取不到就报配置错误，绝不回退到当前工作目录。"""
    try:
        home = Path.home()
    except RuntimeError as exc:  # 3.13+ 无法解析用户目录时抛 RuntimeError
        raise ConfigError("无法确定当前用户目录，请把 MLA_HOME 设为绝对路径") from exc
    if not str(home).strip() or not home.is_absolute():
        raise ConfigError("无法确定当前用户目录，请把 MLA_HOME 设为绝对路径")
    return home


def mla_home(env: Mapping[str, str] | None = None) -> Path:
    """用户级运行目录（凭据、默认数据库、向量缓存都放在它下面）。

    纯路径计算：不创建目录、不读凭据、不碰数据库，也不把 cwd 当兜底。
    """
    e = _env(env)
    override = (e.get("MLA_HOME") or "").strip()
    if override:
        path = Path(override)
        if not path.is_absolute():
            raise ConfigError(f"MLA_HOME 必须是绝对路径：{override}")
        return path
    if _is_windows():
        local_appdata = (e.get("LOCALAPPDATA") or "").strip()
        if local_appdata:
            return Path(local_appdata) / "mla"
        return _user_home() / "AppData" / "Local" / "mla"
    xdg_data = (e.get("XDG_DATA_HOME") or "").strip()
    if xdg_data:
        return Path(xdg_data) / "mla"
    return _user_home() / ".local" / "share" / "mla"


def data_path(relative: str) -> str:
    """把相对包内数据目录的路径解析成绝对路径字符串。

    数据随包分发：仓库里是 `src/mla/data`，安装后在 `site-packages/mla/data`。
    因此不能依赖运行时工作目录。需要指向别处时用对应的 `MLA_*` 环境变量覆盖。
    """
    return str(PACKAGE_DATA_DIR / relative)


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


@dataclass(frozen=True)
class LLMConfig:
    provider: str = "deepseek"
    model: str = "deepseek-chat"
    api_key: str = ""  # 显式注入字段：由 resolver 填，`load_config()` 只装环境变量值
    base_url: str = "https://api.deepseek.com"
    temperature: float = 0.0
    timeout_s: int = 60
    # 保存 Key / 测试连接的校验超时（只作用于 `GET /models`），与推理超时互不串用
    auth_check_timeout_s: int = 10


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
    # 向量缓存是可写的运行期产物，默认落在用户级运行目录（安装后包目录可能只读）
    index_dir: str = field(default_factory=lambda: str(mla_home() / "index"))


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
    # L4 返工上限（规格 003 第 4.5 节）：一批最多 1 次原始 + retry 次重试；0 = 关闭返工
    retry: int = 2


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
    # 数据库是运行期可写文件：默认落在用户级运行目录，不进包目录（site-packages 可能只读）
    db: str = field(default_factory=lambda: str(mla_home() / "mla.sqlite3"))


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


def env_flag(name: str, default: str = "") -> bool:
    """读一个开关型环境变量，口径与 `_truthy` 相同（默认读当前进程环境）。

    调用期读而不是加载期读：测试用 monkeypatch 改环境变量后立即生效。
    """
    return _truthy(os.environ.get(name, default))


def _secrets_section(name: str) -> dict:
    """读 `secrets.toml` 的某个段；**只服务 embedding**（LLM Key 已不从这里读）。"""
    path = Path(".streamlit/secrets.toml")
    if not path.exists():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    section = data.get(name, {})
    return section if isinstance(section, dict) else {}


def _auth_file_key(home: Path) -> str:
    """从 `<MLA_HOME>/auth.json` 的向量检索条目取 Key；读不到返回空串。

    惰性导入适配层与服务层里的常量（**不在模块级 import**）：本模块是依赖图最底层
    （`adapters.embedding` 等反过来依赖它），模块级引入上层会让将来任一处新增
    `from ..config import ...` 直接成环。运行时再导入只多一次 `sys.modules` 查表。

    Key 的**写入**入口仍然只有 `services.credentials`：这里只做配置合成时的读取。

    凭据文件损坏时降级为「未配置」而不是抛错：向量 Key 是可选能力，而文件损坏会由
    必需的大模型路径（`services.credentials.resolve_llm_config`）显式报出，不必在这里再炸一次
    （与 `api.deps.model_configured` 对损坏文件的处理口径一致）。
    """
    from .adapters.auth_file import CredentialError, get_entry
    from .services.credentials import DASHSCOPE_PROVIDER

    try:
        entry = get_entry(home, DASHSCOPE_PROVIDER)
    except CredentialError:
        return ""
    return "" if entry is None else str(entry.get("key", "")).strip()


def _embedding_api_key(e: Mapping[str, str], *, read_files: bool) -> str:
    """向量检索 Key 的来源优先级（高 → 低）：

    1. `DASHSCOPE_API_KEY` 环境变量；
    2. `<MLA_HOME>/auth.json` 的 `dashscope` 条目（设置页「测试并保存」写的就是它）；
    3. 旧 `.streamlit/secrets.toml` 的 `[embedding].api_key`（兼容旧部署，最低优先级）。

    `read_files=False`（调用方传了显式 `env`）时只认第 1 级：`load_config(env=...)`
    必须是纯函数，不读任何文件。判定口径与 `load_config()` 里 secrets 那一级一致。
    """
    from_env = (e.get("DASHSCOPE_API_KEY") or "").strip()
    if from_env:
        return from_env
    if not read_files:
        return ""
    from_file = _auth_file_key(mla_home(e))
    if from_file:
        return from_file
    return str(_secrets_section("embedding").get("api_key", "")).strip()


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """从环境变量加载配置（默认读 os.environ）。

    LLM 的 `api_key` 只取 `MLA_LLM_API_KEY`：完整优先级由
    `services.credentials.resolve_llm_config()` 负责，这里不做任何来源合成。
    embedding 的 `api_key` 在这里合成三级来源（环境变量 > auth.json > 旧 secrets.toml，
    见 `_embedding_api_key`）；传显式 `env` 时只读该映射，不碰文件。
    """
    e = _env(env)
    return Config(
        llm=LLMConfig(
            provider=e.get("MLA_LLM_PROVIDER", "deepseek"),
            model=e.get("MLA_LLM_MODEL", "deepseek-chat"),
            api_key=e.get("MLA_LLM_API_KEY", ""),
            base_url=e.get("MLA_LLM_BASE_URL", "https://api.deepseek.com"),
            temperature=float(e.get("MLA_LLM_TEMPERATURE", "0")),
            timeout_s=int(e.get("MLA_LLM_TIMEOUT", "60")),
            # 校验超时单独成参数：只用于 `GET /models`，不得与推理超时混用
            auth_check_timeout_s=int(e.get("MLA_AUTH_CHECK_TIMEOUT", "10")),
        ),
        embedding=EmbeddingConfig(
            provider=e.get("MLA_EMBED_PROVIDER", "aliyun"),
            model=e.get("MLA_EMBED_MODEL", "qwen3.7-text-embedding-flash"),
            api_key=_embedding_api_key(e, read_files=env is None),
            base_url=e.get("MLA_EMBED_BASE_URL", "https://dashscope.aliyuncs.com"),
            dimension=int(e.get("MLA_EMBED_DIM", "1024")),
            timeout_s=int(e.get("MLA_EMBED_TIMEOUT", "30")),
        ),
        retrieval=RetrievalConfig(index_dir=e.get("MLA_INDEX_DIR", str(mla_home(e) / "index"))),
        policy=PolicyConfig(),
        extraction=ExtractionConfig(
            mode=e.get("MLA_EXTRACT_MODE", "auto").strip().lower(),
            max_chars=int(e.get("MLA_EXTRACT_MAX_CHARS", "20000")),
        ),
        clause_review=ClauseReviewConfig(
            enabled=_truthy(e.get("MLA_CLAUSE_REVIEW", "on")),
            batch_size=int(e.get("MLA_CLAUSE_REVIEW_BATCH", "5")),
            max_extra=int(e.get("MLA_CLAUSE_REVIEW_MAX_EXTRA", "3")),
            retry=int(e.get("MLA_CLAUSE_RETRY", "2")),
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
            db=e.get("MLA_DB", str(mla_home(e) / "mla.sqlite3")),
        ),
    )
