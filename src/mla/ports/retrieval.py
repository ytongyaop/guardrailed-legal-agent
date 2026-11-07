"""检索接口：双路召回由实现方负责，内核只看到"条文列表"。"""

from __future__ import annotations

from typing import Protocol

from ..domain.models import StatuteEntry


class Retriever(Protocol):
    def search(self, query: str, top_k: int = 8, dense_query: str | None = None) -> list[StatuteEntry]:
        """按查询返回条文（已做时效过滤）。

        `dense_query` 供语义路单独使用：核对场景传结构化事实，避免把合同正文发往云端。
        """
        ...

    def health(self) -> bool:
        """索引是否可用（启动自检用）。"""
        ...
