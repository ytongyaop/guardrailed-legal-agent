"""进程内异步任务登记（规格 006 §2「核对采用异步」）。

为什么异步：条款审查常需几十秒，202 + 轮询避免 HTTP 长连接中断，并给前端
`queued / running / failed` 三个可见状态。为什么单 worker：单机单用户场景下有意选串行，
不做并发优化——多 worker 只会掩盖"内存任务登记丢失"这个问题。

三条硬语义：

1. **只用内存**：job 与"当前任务会话"都不落库，进程重启后未知 job 一律 404，界面提示重新提交；
2. **连接不跨线程**：worker 在自己的线程里新建并关闭 `SqliteRepository`，不共享请求连接；
3. **失败不留残缺任务**：`NoModelError` 在入队前（路由）与 worker 内都映射成 409 `NO_MODEL`
   的同一份文案；worker 内失败发生在 `run_check` 第一行，早于任何落库。
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from ..adapters.persist import SqliteRepository
from ..config import Paths
from ..domain.models import CheckTask
from ..services.tasks import NoModelError
from .errors import INTERNAL_ERROR, NO_MODEL, ApiError, make_action

KIND_CHECK = "check"
KIND_RECHECK = "recheck"
KIND_QA = "qa"

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"

WORKER_NAME = "mla-check-worker"


@dataclass
class TaskSession:
    """“当前任务”的内存上下文：重跑追问/补充/问答所需的最小状态。

    `CheckTask` 本身不保存合同正文（历史自包含只到结论层），而追问与补充都要按原合同重跑，
    旧界面把这些放在 `st.session_state`；API 侧等价物就是这份会话，重启即失。
    """

    task: CheckTask
    owner_id: str
    contract_text: str = ""
    contract_name: str = ""
    contract_id: str = ""
    clause_review: bool | None = None


@dataclass
class JobOutcome:
    """worker 的成功产物：要返回给轮询方的任务号、`result` 与可选的“当前任务”会话。"""

    task_id: str
    result: dict[str, Any]
    session: TaskSession | None = None


@dataclass
class Job:
    job_id: str
    kind: str
    owner_id: str
    status: str = STATUS_QUEUED
    task_id: str | None = None
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def to_view(self) -> dict[str, Any]:
        """轮询响应的 JSON 形态；`result` / `error` 与状态一致，二者互斥。"""
        with self.lock:
            return {
                "job_id": self.job_id,
                "kind": self.kind,
                "status": self.status,
                "task_id": self.task_id,
                "result": self.result,
                "error": self.error,
            }


class JobStore:
    """进程级任务登记：一个 `ThreadPoolExecutor(max_workers=1)` + 内存字典。"""

    def __init__(self, *, db_path: str, paths: Paths) -> None:
        self._db_path = db_path
        self._paths = paths
        self._jobs: dict[str, Job] = {}
        self._sessions: dict[str, TaskSession] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=WORKER_NAME)

    # ---------- 仓储（worker 自建自关，不共享连接）----------
    @contextmanager
    def repository(self) -> Iterator[SqliteRepository]:
        repo = SqliteRepository(paths=self._paths, db_path=self._db_path)
        try:
            yield repo
        finally:
            repo.close()

    # ---------- 提交与轮询 ----------
    def submit(
        self,
        *,
        kind: str,
        owner_id: str,
        work: Callable[[SqliteRepository], JobOutcome],
        no_model_action: dict[str, str],
    ) -> Job:
        """登记一个 job 并交给单 worker 执行；返回时刻状态恒为 `queued`。"""
        job = Job(job_id="J-" + uuid.uuid4().hex[:12], kind=kind, owner_id=owner_id)
        with self._lock:
            self._jobs[job.job_id] = job
        self._executor.submit(self._run, job, work, no_model_action)
        return job

    def get(self, job_id: str, owner_id: str) -> Job | None:
        """只按属主取：别人的 job 与不存在的 job 一律 `None`（路由转 404）。"""
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None or job.owner_id != owner_id:
            return None
        return job

    def session(self, task_id: str) -> TaskSession | None:
        with self._lock:
            return self._sessions.get(task_id)

    def remember(self, session: TaskSession) -> None:
        with self._lock:
            self._sessions[session.task.task_id] = session

    def forget(self, task_id: str) -> None:
        with self._lock:
            self._sessions.pop(task_id, None)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False)

    # ---------- worker ----------
    def _run(
        self,
        job: Job,
        work: Callable[[SqliteRepository], JobOutcome],
        no_model_action: dict[str, str],
    ) -> None:
        with job.lock:
            job.status = STATUS_RUNNING
        try:
            with self.repository() as repo:
                outcome = work(repo)
        except NoModelError as exc:
            # 服务层唯一真门禁：不生成残缺任务，文案原样采用 `NO_MODEL_MESSAGE*`
            self._fail(job, NO_MODEL, str(exc), details=None, action=no_model_action)
        except ApiError as exc:
            self._fail(job, exc.code, exc.message, details=exc.details, action=exc.action)
        except Exception:  # noqa: BLE001 - 未捕获异常的 message 不回显（可能含路径/SQL/凭据片段）
            self._fail(
                job,
                INTERNAL_ERROR,
                "服务器内部错误，请稍后重试",
                details=None,
                action=make_action("retry", "重试"),
            )
        else:
            with job.lock:
                job.task_id = outcome.task_id
                job.result = outcome.result
                job.status = STATUS_SUCCEEDED
            if outcome.session is not None:
                self.remember(outcome.session)

    @staticmethod
    def _fail(
        job: Job,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None,
        action: dict[str, Any] | None,
    ) -> None:
        with job.lock:
            job.status = STATUS_FAILED
            job.error = {"code": code, "message": message, "details": details, "action": action}


def no_model_action(is_admin: bool) -> dict[str, str]:
    """无模型时的下一步：管理员去设置页，普通用户联系管理员（规格 006 错误约定）。"""
    if is_admin:
        return make_action("open_settings", "去设置页配置 API Key")
    return make_action("contact_admin", "联系管理员")
