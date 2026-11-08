"""存储适配：SQLite（单机、零依赖、够用）。

设计：
- 法源 / 规则 / 案例来自文件（jsonl / yaml），库只做只读加载；
- 账号 / 企业 / 合同 / 任务 / 日志落库；
- 任务里的 items / suggestions / facts 以 JSON 文本存，读取时还原。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from pathlib import Path

from ..config import Paths, load_config
from ..domain.models import (
    CaseEntry,
    CaseRef,
    CheckItem,
    CheckTask,
    ClauseFinding,
    ContractRecord,
    Enterprise,
    ItemKind,
    MissingStatus,
    MissingTerm,
    OperationLog,
    Role,
    RuleCoverage,
    RuleEntry,
    Severity,
    StatuteEntry,
    Suggestion,
    TaskMode,
    UndeterminedRule,
    UnmetTerm,
    User,
    UserDeletion,
)
from ..domain.rules import load_rules
from .rag import load_statutes

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  user_id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
  password_hash TEXT NOT NULL, salt TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT '用户', enterprise_id TEXT DEFAULT '', created_at TEXT,
  onboarded INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS enterprises (
  enterprise_id TEXT PRIMARY KEY, name TEXT NOT NULL, employer_type TEXT,
  staff_size INTEGER DEFAULT 0, working_hours TEXT, social_insurance TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS contracts (
  contract_id TEXT PRIMARY KEY, enterprise_id TEXT, filename TEXT, text TEXT, uploaded_at TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
  task_id TEXT PRIMARY KEY, enterprise_id TEXT, mode TEXT, variant TEXT,
  facts TEXT, items TEXT, suggestions TEXT, verify_ok INTEGER, degraded INTEGER,
  latency_s REAL, token_in INTEGER, token_out INTEGER, created_at TEXT,
  questions TEXT, ask_rounds INTEGER DEFAULT 0, refused INTEGER DEFAULT 0,
  boundary_layer TEXT, boundary_reason TEXT, ask_history TEXT, errors TEXT,
  contract_id TEXT, contract_name TEXT, contract_chars INTEGER DEFAULT 0,
  deleted INTEGER DEFAULT 0, case_refs TEXT, agent_trace TEXT, clause_findings TEXT,
  undetermined TEXT, rule_coverage TEXT, missing_terms TEXT, ask_items TEXT,
  unmet TEXT, statutory_na INTEGER DEFAULT 0,
  clause_review INTEGER DEFAULT 0, model TEXT
);
CREATE TABLE IF NOT EXISTS logs (
  log_id TEXT PRIMARY KEY, task_id TEXT, user_id TEXT, action TEXT, detail TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT
);
"""

# 旧库迁移：早期版本的 tasks 表没有这些列，启动时按需补（不重建表、不丢数据）。
TASK_MIGRATIONS: dict[str, str] = {
    "questions": "TEXT",
    "ask_rounds": "INTEGER DEFAULT 0",
    "refused": "INTEGER DEFAULT 0",
    "boundary_layer": "TEXT",
    "boundary_reason": "TEXT",
    "ask_history": "TEXT",
    "errors": "TEXT",
    "contract_id": "TEXT",
    "contract_name": "TEXT",
    "contract_chars": "INTEGER DEFAULT 0",
    "deleted": "INTEGER DEFAULT 0",
    "case_refs": "TEXT",
    "agent_trace": "TEXT",
    "clause_findings": "TEXT",
    "undetermined": "TEXT",
    "unmet": "TEXT",
    "statutory_na": "INTEGER DEFAULT 0",
    "rule_coverage": "TEXT",
    "missing_terms": "TEXT",
    "ask_items": "TEXT",
    "clause_review": "INTEGER DEFAULT 0",
    "model": "TEXT",
}

# 旧库迁移（users 表）：早期版本的 users 表没有 onboarded 列，启动时按需补。
USER_MIGRATIONS: dict[str, str] = {
    "onboarded": "INTEGER DEFAULT 0",
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_dt(value: str | None) -> datetime | None:
    """兼容新（带时分秒）旧（纯日期）两种写法。"""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        try:
            return datetime.combine(date.fromisoformat(value), datetime.min.time())
        except ValueError:
            return None


def _json_column(row: sqlite3.Row, name: str) -> object:
    """读一列 JSON 文本；列不存在（旧库未迁移）或内容坏掉都返回 None，不抛异常。"""
    try:
        raw = row[name]
    except (IndexError, KeyError):
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _undetermined_from_row(row: sqlite3.Row) -> list[UndeterminedRule]:
    """还原"判不了的规则"；坏行逐条跳过，绝不因为一条脏数据打不开旧库。"""
    data = _json_column(row, "undetermined")
    if not isinstance(data, list):
        return []
    out: list[UndeterminedRule] = []
    for entry in data:
        if not isinstance(entry, dict) or "rule_id" not in entry:
            continue
        fields = entry.get("missing_fields")
        out.append(
            UndeterminedRule(
                rule_id=str(entry["rule_id"]),
                name=str(entry.get("name", "")),
                statute_id=str(entry.get("statute_id", "")),
                missing_fields=tuple(str(x) for x in fields) if isinstance(fields, list) else (),
                reason=str(entry.get("reason", "")),
            )
        )
    return out


def _unmet_from_row(row: sqlite3.Row) -> list[UnmetTerm]:
    """还原"合同未约定"；坏行逐条跳过，旧库缺列时读成空列表。"""
    data = _json_column(row, "unmet")
    if not isinstance(data, list):
        return []
    out: list[UnmetTerm] = []
    for entry in data:
        if not isinstance(entry, dict) or "rule_id" not in entry:
            continue
        fields = entry.get("fields")
        out.append(
            UnmetTerm(
                rule_id=str(entry["rule_id"]),
                name=str(entry.get("name", "")),
                statute_id=str(entry.get("statute_id", "")),
                fields=tuple(str(x) for x in fields) if isinstance(fields, list) else (),
            )
        )
    return out


def _coverage_from_row(row: sqlite3.Row) -> RuleCoverage | None:
    """还原四态分布；缺列 / 空值 / 坏 JSON / 字段不全一律返回 None。"""
    data = _json_column(row, "rule_coverage")
    if not isinstance(data, dict):
        return None
    try:
        return RuleCoverage(
            total=int(data["total"]),
            violated=int(data["violated"]),
            compliant=int(data["compliant"]),
            not_applicable=int(data["not_applicable"]),
            unknown=int(data["unknown"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _ask_items_from_row(row: sqlite3.Row) -> list[dict]:
    """还原追问清单；缺列 / 坏 JSON / 非对象条目都跳过，旧库必须能打开。"""
    data = _json_column(row, "ask_items")
    if not isinstance(data, list):
        return []
    out: list[dict] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        out.append(
            {
                "slot_id": str(entry.get("slot_id", "")),
                "question": str(entry.get("question", "")),
                "source": str(entry.get("source", "fixed")),
            }
        )
    return out


def _missing_terms_from_row(row: sqlite3.Row) -> list[MissingTerm]:
    """还原必备条款判定；缺列 / 坏 JSON / 状态值不认识都只跳过，不抛异常（旧库必须能打开）。"""
    data = _json_column(row, "missing_terms")
    if not isinstance(data, list):
        return []
    out: list[MissingTerm] = []
    for entry in data:
        if not isinstance(entry, dict) or "term_id" not in entry:
            continue
        try:
            status = MissingStatus(str(entry.get("status", "")))
        except ValueError:
            continue
        out.append(
            MissingTerm(
                term_id=str(entry["term_id"]),
                name=str(entry.get("name", "")),
                clause_no=str(entry.get("clause_no", "")),
                statute_id=str(entry.get("statute_id", "")),
                status=status,
                certainty=str(entry.get("certainty", "")),
                note=str(entry.get("note", "")),
            )
        )
    return out


class SqliteRepository:
    """实现 ports.repository.Repository（法源/规则来自文件，其余落库）。"""

    def __init__(self, paths: Paths | None = None, db_path: str | None = None) -> None:
        self.paths = paths or load_config().paths
        self.db_path = db_path or self.paths.db
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()
        self._statutes: list[StatuteEntry] | None = None
        self._rules: list[RuleEntry] | None = None

    def _migrate(self) -> None:
        """旧库补列（幂等）：只加列，不改列、不删数据。"""
        cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(tasks)")}
        for name, ddl in TASK_MIGRATIONS.items():
            if name not in cols:
                self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {ddl}")
        user_cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(users)")}
        for name, ddl in USER_MIGRATIONS.items():
            if name not in user_cols:
                self._conn.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")
        # 旧记录只有日期：补成当天 00:00:00，保证按时间排序时新旧可比较
        for table in ("tasks", "logs"):
            self._conn.execute(
                f"UPDATE {table} SET created_at = created_at || 'T00:00:00' WHERE length(created_at) = 10"
            )
        self._conn.commit()

    # ---------- 法源 / 规则 / 案例（文件） ----------
    def list_statutes(self) -> list[StatuteEntry]:
        if self._statutes is None:
            self._statutes = load_statutes(self.paths.statutes)
        return list(self._statutes)

    def get_statute(self, statute_id: str) -> StatuteEntry | None:
        return next((s for s in self.list_statutes() if s.statute_id == statute_id), None)

    def list_rules(self) -> list[RuleEntry]:
        if self._rules is None:
            self._rules = list(load_rules(self.paths.rules))
        return list(self._rules)

    def list_cases(self, dispute_type: str = "") -> list[CaseEntry]:
        p = Path(self.paths.cases)
        if not p.exists():
            return []
        out: list[CaseEntry] = []
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if dispute_type and dispute_type not in str(r.get("dispute_type", "")):
                continue
            out.append(
                CaseEntry(
                    case_id=str(r["case_id"]),
                    title=str(r["title"]),
                    source=str(r["source"]),
                    holding=str(r["holding"]),
                    statute_ids=tuple(r.get("statute_ids", [])),
                    batch=str(r.get("batch", "")),
                    dispute_type=str(r.get("dispute_type", "")),
                    region=str(r.get("region", "全国")),
                    level=str(r.get("level", "")),
                    source_url=str(r.get("source_url", "")),
                )
            )
        return out

    # ---------- 账号 ----------
    def create_user(self, user: User) -> None:
        self._conn.execute(
            "INSERT INTO users(user_id,username,password_hash,salt,role,enterprise_id,created_at,onboarded)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (
                user.user_id,
                user.username,
                user.password_hash,
                user.salt,
                str(user.role),
                user.enterprise_id,
                user.created_at or _now(),
                int(user.onboarded),
            ),
        )
        self._conn.commit()

    def get_user_by_name(self, username: str) -> User | None:
        row = self._conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        return self._row_to_user(row) if row else None

    def list_users(self) -> list[User]:
        rows = self._conn.execute("SELECT * FROM users ORDER BY created_at").fetchall()
        return [self._row_to_user(r) for r in rows]

    def set_user_role(self, user_id: str, role: str) -> None:
        self._conn.execute("UPDATE users SET role=? WHERE user_id=?", (role, user_id))
        self._conn.commit()

    def set_user_enterprise(self, user_id: str, enterprise_id: str) -> None:
        """把账号绑定到企业（新建档案后回填，避免“存了档案但账号没绑”）。"""
        self._conn.execute("UPDATE users SET enterprise_id=? WHERE user_id=?", (enterprise_id, user_id))
        self._conn.commit()

    def set_user_password(self, user_id: str, password_hash: str, salt: str) -> None:
        """改口令：只覆盖哈希与盐（调用方负责生成，明文不落库）。"""
        self._conn.execute(
            "UPDATE users SET password_hash=?, salt=? WHERE user_id=?",
            (password_hash, salt, user_id),
        )
        self._conn.commit()

    def mark_onboarded(self, user_id: str) -> None:
        """标记"已完成首次登录引导"；重复标记幂等。"""
        self._conn.execute("UPDATE users SET onboarded=1 WHERE user_id=?", (user_id,))
        self._conn.commit()

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> User:
        return User(
            user_id=row["user_id"],
            username=row["username"],
            password_hash=row["password_hash"],
            salt=row["salt"],
            role=Role(row["role"]),
            enterprise_id=row["enterprise_id"] or "",
            created_at=row["created_at"] or "",
            onboarded=bool(row["onboarded"]) if "onboarded" in row.keys() else False,
        )

    def delete_user(self, user_id: str, *, actor_id: str, log_id: str) -> UserDeletion:
        """账号硬删：单事务级联企业范围的数据，并写一条管理员审计日志（同生共死）。

        级联顺序按真实表结构（无外键、无 ON DELETE CASCADE）手工执行：
        logs → tasks → contracts → enterprises → users，最后插入审计日志。
        `enterprise_id` 为空时只删该账号自己的日志，**不按空串删企业/合同/任务**
        （否则会连带抹掉历史遗留的 `enterprise_id=''` 记录）。
        """
        with self._conn:
            row = self._conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
            if row is None:
                return UserDeletion(deleted=False, reason="账号不存在")
            username = row["username"] or ""
            enterprise_id = row["enterprise_id"] or ""
            contracts = tasks = logs = 0
            if enterprise_id:
                # 任务集：按企业兜，同时兜住 contract_id 指向该企业但 enterprise_id 写错的历史数据
                task_ids = [
                    r["task_id"]
                    for r in self._conn.execute(
                        "SELECT task_id FROM tasks WHERE enterprise_id=? OR contract_id IN "
                        "(SELECT contract_id FROM contracts WHERE enterprise_id=?)",
                        (enterprise_id, enterprise_id),
                    ).fetchall()
                ]
                if task_ids:
                    marks = ",".join("?" * len(task_ids))
                    logs = self._conn.execute(
                        f"DELETE FROM logs WHERE user_id=? OR task_id IN ({marks})",
                        (user_id, *task_ids),
                    ).rowcount
                    tasks = self._conn.execute(
                        f"DELETE FROM tasks WHERE task_id IN ({marks})", tuple(task_ids)
                    ).rowcount
                else:
                    logs = self._conn.execute("DELETE FROM logs WHERE user_id=?", (user_id,)).rowcount
                contracts = self._conn.execute(
                    "DELETE FROM contracts WHERE enterprise_id=?", (enterprise_id,)
                ).rowcount
                self._conn.execute("DELETE FROM enterprises WHERE enterprise_id=?", (enterprise_id,))
            else:
                logs = self._conn.execute("DELETE FROM logs WHERE user_id=?", (user_id,)).rowcount
            self._conn.execute("DELETE FROM users WHERE user_id=?", (user_id,))
            detail = (
                f"删除账号 {username}（{user_id}）；企业 {enterprise_id or '无'}；"
                f"合同 {contracts}；任务 {tasks}；日志 {logs}"
            )
            self._conn.execute(
                "INSERT INTO logs(log_id,task_id,user_id,action,detail,created_at) VALUES(?,?,?,?,?,?)",
                (log_id, "", actor_id, "删除账号", detail, _now()),
            )
        return UserDeletion(
            deleted=True,
            username=username,
            enterprise_id=enterprise_id,
            contracts=contracts,
            tasks=tasks,
            logs=logs,
        )

    # ---------- 企业 / 合同 ----------
    def save_enterprise(self, ent: Enterprise) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO enterprises(enterprise_id,name,employer_type,staff_size,"
            "working_hours,social_insurance,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                ent.enterprise_id,
                ent.name,
                ent.employer_type,
                ent.staff_size,
                ent.working_hours,
                ent.social_insurance,
                ent.created_at or _now(),
            ),
        )
        self._conn.commit()

    def get_enterprise(self, enterprise_id: str) -> Enterprise | None:
        row = self._conn.execute(
            "SELECT * FROM enterprises WHERE enterprise_id=?", (enterprise_id,)
        ).fetchone()
        if not row:
            return None
        return Enterprise(
            enterprise_id=row["enterprise_id"],
            name=row["name"],
            employer_type=row["employer_type"] or "",
            staff_size=int(row["staff_size"] or 0),
            working_hours=row["working_hours"] or "",
            social_insurance=row["social_insurance"] or "",
            created_at=row["created_at"] or "",
        )

    def list_enterprises(self) -> list[Enterprise]:
        rows = self._conn.execute("SELECT enterprise_id FROM enterprises ORDER BY created_at").fetchall()
        return [e for r in rows if (e := self.get_enterprise(r["enterprise_id"])) is not None]

    def delete_enterprise(self, enterprise_id: str) -> None:
        """企业档案可随时删除。合同随档案一起删，核对历史保留（已脱敏的统计留痕）。"""
        self._conn.execute("DELETE FROM contracts WHERE enterprise_id=?", (enterprise_id,))
        self._conn.execute("DELETE FROM enterprises WHERE enterprise_id=?", (enterprise_id,))
        self._conn.commit()

    def save_contract(self, rec: ContractRecord) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO contracts(contract_id,enterprise_id,filename,text,uploaded_at)"
            " VALUES(?,?,?,?,?)",
            (rec.contract_id, rec.enterprise_id, rec.filename, rec.text, rec.uploaded_at or _now()),
        )
        self._conn.commit()

    def list_contracts(self, enterprise_id: str) -> list[ContractRecord]:
        rows = self._conn.execute(
            "SELECT * FROM contracts WHERE enterprise_id=? ORDER BY uploaded_at DESC", (enterprise_id,)
        ).fetchall()
        return [self._row_to_contract(r) for r in rows]

    def get_contract(self, contract_id: str, enterprise_id: str = "") -> ContractRecord | None:
        """按 ID 取合同，必须带企业 ID（企业隔离）。

        企业 ID 为空时直接返回 None，不退化成"按主键查全库"，避免跨账号读到别人的合同。
        """
        if not enterprise_id:
            return None
        row = self._conn.execute(
            "SELECT * FROM contracts WHERE contract_id=? AND enterprise_id=?",
            (contract_id, enterprise_id),
        ).fetchone()
        return self._row_to_contract(row) if row else None

    def delete_contract(self, contract_id: str, enterprise_id: str = "") -> None:
        """删合同，必须带企业 ID；企业 ID 为空直接忽略（与 `list_tasks` 口径一致）。"""
        if not enterprise_id:
            return
        self._conn.execute(
            "DELETE FROM contracts WHERE contract_id=? AND enterprise_id=?",
            (contract_id, enterprise_id),
        )
        self._conn.commit()

    @staticmethod
    def _row_to_contract(row: sqlite3.Row) -> ContractRecord:
        return ContractRecord(
            contract_id=row["contract_id"],
            enterprise_id=row["enterprise_id"] or "",
            filename=row["filename"] or "",
            text=row["text"] or "",
            uploaded_at=row["uploaded_at"] or "",
        )

    # ---------- 任务 / 日志 ----------
    def save_task(self, task: CheckTask) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO tasks(task_id,enterprise_id,mode,variant,facts,items,suggestions,"
            "verify_ok,degraded,latency_s,token_in,token_out,created_at,"
            "questions,ask_rounds,refused,boundary_layer,boundary_reason,ask_history,errors,"
            "contract_id,contract_name,contract_chars,deleted,case_refs,agent_trace,clause_findings,"
            "undetermined,rule_coverage,missing_terms,ask_items,unmet,statutory_na,clause_review,model)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                task.task_id,
                task.enterprise_id,
                str(task.mode),
                task.variant,
                json.dumps(task.facts, ensure_ascii=False),
                json.dumps([self._item_to_dict(i) for i in task.items], ensure_ascii=False),
                json.dumps([self._sugg_to_dict(s) for s in task.suggestions], ensure_ascii=False),
                int(task.verify_ok),
                int(task.degraded),
                task.latency_s,
                task.token_in,
                task.token_out,
                task.created_at.isoformat(timespec="seconds")
                if isinstance(task.created_at, datetime)
                else (
                    datetime.combine(task.created_at, datetime.min.time()).isoformat(timespec="seconds")
                    if isinstance(task.created_at, date)
                    else _now()
                ),
                json.dumps(task.questions, ensure_ascii=False),
                task.ask_rounds,
                int(task.refused),
                task.boundary_layer,
                task.boundary_reason,
                json.dumps(task.ask_history, ensure_ascii=False),
                json.dumps(task.errors, ensure_ascii=False),
                task.contract_id,
                task.contract_name,
                task.contract_chars,
                int(task.deleted),
                json.dumps(
                    [{"case_id": c.case_id, "holding": c.holding, "note": c.note} for c in task.case_refs],
                    ensure_ascii=False,
                ),
                json.dumps(task.agent_trace, ensure_ascii=False),
                json.dumps([self._finding_to_dict(f) for f in task.clause_findings], ensure_ascii=False),
                json.dumps([self._undetermined_to_dict(u) for u in task.undetermined], ensure_ascii=False),
                json.dumps(self._coverage_to_dict(task.rule_coverage), ensure_ascii=False)
                if task.rule_coverage is not None
                else "",
                json.dumps([self._missing_to_dict(t) for t in task.missing_terms], ensure_ascii=False),
                json.dumps(task.ask_items, ensure_ascii=False),
                json.dumps([self._unmet_to_dict(u) for u in task.unmet], ensure_ascii=False),
                int(task.statutory_na),
                int(task.clause_review),
                task.model,
            ),
        )
        self._conn.commit()

    def list_tasks(
        self, enterprise_id: str = "", limit: int = 50, include_deleted: bool = False
    ) -> list[CheckTask]:
        # 企业隔离：没有企业 ID 不等于“查全部”，而是没有可见记录（防止跨账号泄漏）
        if not enterprise_id:
            return []
        where = "enterprise_id=?"
        if not include_deleted:
            where += " AND deleted=0"
        rows = self._conn.execute(
            f"SELECT * FROM tasks WHERE {where} ORDER BY created_at DESC, rowid DESC LIMIT ?",
            (enterprise_id, limit),
        ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def delete_task(self, task_id: str, enterprise_id: str = "") -> None:
        """软删：历史页不显示，但操作日志与记录本身保留（审计可追溯）。

        必须带企业 ID，避免跨账号误删；空企业直接忽略。
        """
        if not enterprise_id:
            return
        self._conn.execute(
            "UPDATE tasks SET deleted=1 WHERE task_id=? AND enterprise_id=?",
            (task_id, enterprise_id),
        )
        self._conn.commit()

    def count_tasks(self, enterprise_id: str = "") -> tuple[int, int]:
        """返回（有效条数, 已软删条数），供历史页写明统计口径；空企业返回 (0, 0)。"""
        if not enterprise_id:
            return (0, 0)
        row = self._conn.execute(
            "SELECT SUM(CASE WHEN deleted=0 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN deleted=1 THEN 1 ELSE 0 END) FROM tasks WHERE enterprise_id=?",
            (enterprise_id,),
        ).fetchone()
        return int(row[0] or 0), int(row[1] or 0)

    def add_log(self, log: OperationLog) -> None:
        self._conn.execute(
            "INSERT INTO logs(log_id,task_id,user_id,action,detail,created_at) VALUES(?,?,?,?,?,?)",
            (log.log_id, log.task_id, log.user_id, log.action, log.detail, log.created_at or _now()),
        )
        self._conn.commit()

    def list_logs(self, limit: int = 100) -> list[OperationLog]:
        rows = self._conn.execute("SELECT * FROM logs ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        return [
            OperationLog(
                log_id=r["log_id"],
                task_id=r["task_id"] or "",
                user_id=r["user_id"] or "",
                action=r["action"] or "",
                detail=r["detail"] or "",
                created_at=r["created_at"] or "",
            )
            for r in rows
        ]

    # ---------- 设置（键值对） ----------
    def get_setting(self, key: str) -> str:
        """读设置；键不存在返回空串（不抛异常，界面按"未配置"处理）。"""
        row = self._conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            return ""
        return str(row["value"] or "")

    def set_setting(self, key: str, value: str) -> None:
        """写设置：已存在则覆盖，并记下更新时间。"""
        self._conn.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, _now()),
        )
        self._conn.commit()

    # ---------- 序列化 ----------
    @staticmethod
    def _item_to_dict(item: CheckItem) -> dict:
        return {
            "item_id": item.item_id,
            "kind": str(item.kind),
            "clause_ref": item.clause_ref,
            "finding": item.finding,
            "source_id": item.source_id,
            "evidence_text": item.evidence_text,
            "severity": str(item.severity),
            "rule_id": item.rule_id,
        }

    @staticmethod
    def _sugg_to_dict(s: Suggestion) -> dict:
        return {"text": s.text, "source_ids": list(s.source_ids), "disclaimer": s.disclaimer}

    @staticmethod
    def _finding_to_dict(f: ClauseFinding) -> dict:
        return {
            "paragraph": f.paragraph,
            "span": f.span,
            "grade": f.grade,
            "reason": f.reason,
            "statute_ids": list(f.statute_ids),
            "trigger_id": f.trigger_id,
            "source": f.source,
            "need_facts": list(f.need_facts),
        }

    @staticmethod
    def _undetermined_to_dict(u: UndeterminedRule) -> dict:
        return {
            "rule_id": u.rule_id,
            "name": u.name,
            "statute_id": u.statute_id,
            "missing_fields": list(u.missing_fields),
            "reason": u.reason,
        }

    @staticmethod
    def _unmet_to_dict(u: UnmetTerm) -> dict:
        return {
            "rule_id": u.rule_id,
            "name": u.name,
            "statute_id": u.statute_id,
            "fields": list(u.fields),
        }

    @staticmethod
    def _coverage_to_dict(c: RuleCoverage) -> dict:
        return {
            "total": c.total,
            "violated": c.violated,
            "compliant": c.compliant,
            "not_applicable": c.not_applicable,
            "unknown": c.unknown,
        }

    @staticmethod
    def _missing_to_dict(t: MissingTerm) -> dict:
        return {
            "term_id": t.term_id,
            "name": t.name,
            "clause_no": t.clause_no,
            "statute_id": t.statute_id,
            "status": str(t.status),
            "certainty": t.certainty,
            "note": t.note,
        }

    @staticmethod
    def _row_to_task(row: sqlite3.Row) -> CheckTask:
        items = [
            CheckItem(
                item_id=d["item_id"],
                kind=ItemKind(d["kind"]),
                clause_ref=d.get("clause_ref", ""),
                finding=d["finding"],
                source_id=d["source_id"],
                evidence_text=d.get("evidence_text", ""),
                severity=Severity(d.get("severity", "参考")),
                rule_id=d.get("rule_id", ""),
            )
            for d in json.loads(row["items"] or "[]")
        ]
        suggestions = [
            Suggestion(
                text=d["text"], source_ids=tuple(d.get("source_ids", [])), disclaimer=d.get("disclaimer", "")
            )
            for d in json.loads(row["suggestions"] or "[]")
        ]
        return CheckTask(
            task_id=row["task_id"],
            mode=TaskMode(row["mode"]),
            enterprise_id=row["enterprise_id"] or "",
            variant=row["variant"] or "full",
            model=row["model"] or "",
            facts=json.loads(row["facts"] or "{}"),
            items=items,
            suggestions=suggestions,
            case_refs=[
                CaseRef(case_id=d["case_id"], holding=d["holding"], note=d.get("note", ""))
                for d in json.loads(row["case_refs"] or "[]")
            ],
            agent_trace=[str(x) for x in json.loads(row["agent_trace"] or "[]")],
            clause_review=bool(row["clause_review"] or 0),
            clause_findings=[
                ClauseFinding(
                    paragraph=int(d["paragraph"]),
                    span=str(d["span"]),
                    grade=str(d["grade"]),
                    reason=str(d["reason"]),
                    statute_ids=tuple(d.get("statute_ids", [])),
                    trigger_id=str(d.get("trigger_id", "")),
                    source=str(d.get("source", "trigger")),
                    need_facts=tuple(d.get("need_facts", [])),
                )
                for d in json.loads(row["clause_findings"] or "[]")
            ],
            questions=[str(q) for q in json.loads(row["questions"] or "[]")],
            ask_items=_ask_items_from_row(row),
            ask_rounds=int(row["ask_rounds"] or 0),
            refused=bool(row["refused"]),
            boundary_layer=row["boundary_layer"] or "",
            boundary_reason=row["boundary_reason"] or "",
            ask_history=list(json.loads(row["ask_history"] or "[]")),
            verify_ok=bool(row["verify_ok"]),
            degraded=bool(row["degraded"]),
            errors=[str(e) for e in json.loads(row["errors"] or "[]")],
            latency_s=row["latency_s"],
            token_in=int(row["token_in"] or 0),
            token_out=int(row["token_out"] or 0),
            contract_id=row["contract_id"] or "",
            contract_name=row["contract_name"] or "",
            contract_chars=int(row["contract_chars"] or 0),
            deleted=bool(row["deleted"]),
            undetermined=_undetermined_from_row(row),
            unmet=_unmet_from_row(row),
            statutory_na=int(row["statutory_na"] or 0),
            rule_coverage=_coverage_from_row(row),
            missing_terms=_missing_terms_from_row(row),
            created_at=_parse_dt(row["created_at"]),
        )
