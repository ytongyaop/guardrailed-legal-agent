"""小微企业用工风险辅助系统（mla = microbusiness labor assistant）。

分层：domain（内核，纯逻辑） ← ports（接口） ← adapters（实现） ← graph（编排） ← ui（界面）。
依赖单向，内核不得 import 外部 SDK / 数据库 / 界面库。
"""

__version__ = "0.1.1"
