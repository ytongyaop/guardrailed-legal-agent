"""小微企业用工风险辅助系统（mla = microbusiness labor assistant）。

分层：domain（内核，纯逻辑） ← ports（接口） ← adapters（实现） ← graph（编排） ← api（HTTP 层）。
界面是 `web/` 的 Vue 3 单页应用，只消费 `src/mla/api` 的 DTO，不做判定。
依赖单向，内核不得 import 外部 SDK / 数据库 / 界面库。
"""

__version__ = "0.4.0"
