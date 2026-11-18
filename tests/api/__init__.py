"""`tests/api` 是包：避免 `tests/api/test_auth.py` 与 `tests/test_auth.py` 同名冲突。

pytest 默认的 `prepend` 导入模式按"最近的无 `__init__.py` 祖先目录"决定模块名；
本目录加了 `__init__.py` 后模块名变成 `api.test_auth`，与根目录同名文件互不覆盖。
目录内的工具模块统一用相对导入（`from .api_helpers import ...`）。
"""
