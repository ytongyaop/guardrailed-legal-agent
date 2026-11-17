"""测试共用辅助：假 `Generator`（规格 003 第 9.3 节 P4-1）。

为什么放 `tests/helpers.py` 而不是 `tests/conftest.py`：

- `conftest.py` 是 pytest 的**插件入口**（夹具 / hook），里面放测试替身会让"夹具注入"与
  "可直接实例化的普通类"混在一处，读代码的人分不清哪个是自动生效的副作用；
- `helpers.py` 是普通模块，谁用谁 `from helpers import FakeGenerator`，没有隐式行为；
- 测试目录不是包（没有 `__init__.py`），pytest 的 `prepend` 导入模式会把 `tests/` 放进
  `sys.path`，所以 `import helpers` 与 `import test_xxx` 同一口径，无需改 `pythonpath`。

只实现"零网络、可断言"的那部分：按调用顺序回放预置结论、记录每次调用的 system / user、
解析 JSON payload。需要专用协议（`chat` 工具调用、按每次调用累加 token）的替身仍留在各自
文件里——把那些差异塞进来会让这个类变成"什么都做一点"的万能桩。
"""

from __future__ import annotations

import json
from typing import Any, cast

from mla.adapters.llm import LLMError

# 没有预置结论时回的空结果：条款审查 / 抽取 / 建议三条链路的"什么都没发现"。
EMPTY_RESPONSE: dict[str, Any] = {"findings": [], "extra": []}


class FakeGenerator:
    """假的生成端口（实现 `ports/generation.py:Generator`）：零网络、可断言。

    用法（按调用顺序回放）：

        llm = FakeGenerator({"findings": [row]}, {"findings": [fixed]})   # 第 2 次调用回 fixed
        llm = FakeGenerator({"findings": [row]})                          # 之后每次都是同一份
        llm = FakeGenerator(raises=True)                                  # 每次调用都抛 LLMError
        llm = FakeGenerator(lambda system, user: {...})                   # 按入参应答（可自己数调用）

    约定：

    - 响应用尽后重复最后一项（`_scripted` 用例的常见写法）；
    - 一项都没给时返回 `EMPTY_RESPONSE`；
    - 响应项可以是 `callable(system, user) -> dict`，也可以是异常实例（抛出来）；
    - `calls` 是调用次数；`users` / `systems` 是逐次入参；`payloads` 是解析后的 user（非 JSON 时跳过）；
    - `last_user` / `last_system` 是最后一次入参；`prompts` 把两次入参拼成一段文本，供隐私用例检索。
    """

    available = True
    # 与 `DeepSeekLLM` 同口径的用量字段（`services.tasks` 会读它们写 token 计数）；假模型不产生用量。
    usage_in = 0
    usage_out = 0

    def __init__(self, *responses: Any, label: str = "labor", raises: bool = False) -> None:
        self.responses: list[Any] = list(responses)
        self.label = label
        self.raises = raises
        self.users: list[str] = []
        self.systems: list[str] = []
        self.payloads: list[dict[str, Any]] = []

    # ---------- 观测面 ----------

    @property
    def calls(self) -> int:
        return len(self.users)

    @property
    def last_user(self) -> str:
        return self.users[-1] if self.users else ""

    @property
    def last_system(self) -> str:
        return self.systems[-1] if self.systems else ""

    @property
    def prompts(self) -> list[str]:
        """逐次的 `system + user`，用于"外发了什么"这类断言。"""
        return [f"{system}\n{user}" for system, user in zip(self.systems, self.users, strict=True)]

    # ---------- 端口 ----------

    def generate(self, system: str, user: str, schema: dict[str, Any] | None = None) -> dict[str, Any]:
        self.systems.append(system)
        self.users.append(user)
        try:
            parsed = json.loads(user)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            self.payloads.append(parsed)

        if self.raises:
            raise LLMError("假模型：调用失败（测试桩）")
        if not self.responses:
            return dict(EMPTY_RESPONSE)
        response = self.responses[min(self.calls - 1, len(self.responses) - 1)]
        if callable(response):
            return cast("dict[str, Any]", response(system, user))
        if isinstance(response, Exception):
            raise response
        return cast("dict[str, Any]", response)

    def classify(self, text: str, labels: list[str]) -> str:
        return self.label if labels else "不确定"
