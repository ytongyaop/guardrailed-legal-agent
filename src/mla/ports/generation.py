"""生成接口：所有 prompt 都留在实现层，内核只传结构化请求。"""

from __future__ import annotations

from typing import Any, Protocol


class Generator(Protocol):
    def generate(self, system: str, user: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        """按 schema 产出结构化结果；解析失败由实现层重试。"""
        ...

    def classify(self, text: str, labels: list[str]) -> str:
        """领域分类（用于越界判定第一层）。"""
        ...
