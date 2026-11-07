"""阿里云百炼（DashScope）文本向量适配：qwen3.7-text-embedding-flash。

用原生接口而不是 OpenAI 兼容层，因为需要 `text_type=query/document` 区分，
检索效果更好。Key 从环境变量 DASHSCOPE_API_KEY 或 .streamlit/secrets.toml 读取。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from ..config import EmbeddingConfig

EMBED_PATH = "/api/v1/services/embeddings/text-embedding/text-embedding"
BATCH_SIZE = 20


class EmbeddingError(RuntimeError):
    """向量服务调用失败（网络 / 鉴权 / 返回格式异常）。"""


class DashScopeEmbedder:
    def __init__(self, cfg: EmbeddingConfig) -> None:
        self.cfg = cfg
        self.last_error = ""

    @property
    def available(self) -> bool:
        return bool(self.cfg.api_key)

    def _endpoint(self) -> str:
        base = (self.cfg.base_url or "https://dashscope.aliyuncs.com").rstrip("/")
        return f"{base}{EMBED_PATH}"

    def embed(self, texts: list[str], text_type: str = "document") -> list[list[float]]:
        if not self.available:
            raise EmbeddingError("未配置 DASHSCOPE_API_KEY")
        if not texts:
            return []
        vectors: list[list[float]] = []
        for start in range(0, len(texts), BATCH_SIZE):
            batch = texts[start : start + BATCH_SIZE]
            vectors.extend(self._embed_batch(batch, text_type))
        return vectors

    def _embed_batch(self, batch: list[str], text_type: str) -> list[list[float]]:
        payload = {
            "model": self.cfg.model,
            "input": {"texts": batch},
            "parameters": {"dimension": self.cfg.dimension, "text_type": text_type},
        }
        req = urllib.request.Request(
            self._endpoint(),
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.cfg.api_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.cfg.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="ignore")[:200]
            raise EmbeddingError(f"HTTP {exc.code}：{detail}") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise EmbeddingError(f"调用失败：{exc}") from exc

        rows = (data.get("output") or {}).get("embeddings") or []
        if len(rows) != len(batch):
            raise EmbeddingError(f"返回条数不符：期望 {len(batch)}，实际 {len(rows)}")
        rows.sort(key=lambda row: int(row.get("index", 0)))
        return [[float(x) for x in row["embedding"]] for row in rows]

    def health(self) -> bool:
        try:
            vector = self.embed(["劳动用工风险"], text_type="query")[0]
            return bool(vector)
        except EmbeddingError as exc:
            self.last_error = str(exc)
            return False
