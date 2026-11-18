"""合同核对全链路（规格 006 §2：`/checks`、`/jobs`、`/tasks/*`）。

结构：路由只做**入参校验 + 鉴权 + 入队**，真正的核对/问答在 `jobs.JobStore` 的单 worker 里跑，
结果由 `presenters` 从现有领域函数搬成 DTO。这样：

- 请求线程不持有长事务，也不跨线程共享 SQLite 连接；
- `NoModelError` 在入队前先拦一次（409 `NO_MODEL`），worker 内再拦一次（job.status=failed +
  `error.code=NO_MODEL`），两条路径都发生在任何落库之前，不会生成残缺任务；
- 首次核对沿用旧 `_check_form`：`save=False`、`max_ask_rounds=1`，新合同（粘贴/上传）核对时
  按原逻辑自动存档；`run_check` 内部不落库，落库只由这里的一个出口决定：
  **无追问**（核对就是最终结果）立刻落库，否则「看完就走」的记录会整条丢失；
  **有追问**先留在内存会话里，等用户作答重跑或点「跳过」时由 `/answers` 落库。
"""

from __future__ import annotations

import uuid
from dataclasses import replace

from fastapi import APIRouter, Depends, Request

from ...adapters.files import normalize_contract_name
from ...adapters.persist import SqliteRepository
from ...config import load_config
from ...domain.models import CheckTask, ContractRecord, TaskMode, User
from ...services.auth import is_admin
from ...services.tasks import (
    NO_MODEL_MESSAGE,
    NO_MODEL_QA_MESSAGE,
    _qa_requires_model,  # 私有但唯一：问答是否需要模型由服务层口径决定，不在路由重判
    allow_no_model,
    has_model,
    persist,
    run_check,
    run_qa,
)
from .. import presenters
from ..deps import AuthDep, RepoDep, require_capability
from ..errors import (
    INTERNAL_ERROR,
    NO_MODEL,
    NOT_FOUND,
    STATE_CONFLICT,
    VALIDATION_ERROR,
    ApiError,
)
from ..jobs import KIND_CHECK, KIND_QA, KIND_RECHECK, Job, JobOutcome, JobStore, TaskSession, no_model_action
from ..schemas import AnswersIn, CheckIn, JobView, OptionalFactsIn, QaIn, TaskDetail

router = APIRouter()

#: `DELETE /history/{id}` 按任务号反查时扫描的窗口（详情 / 报告已改走 `get_task` 单条查询）。
HISTORY_SCAN_LIMIT = 5000

#: 首轮结果落库失败时的提示：写进任务诊断里，界面能看见，不许静默丢掉整份结果。
#: 文案刻意避开「失败 / 回退 / 超时」三个词——`presenters.process_of` 拿它们判定「抽取已回退」，
#: 落库问题不该被读成模型抽取降级（真正的抽取降级由抽取器自己写诊断）。
SAVE_FAILED_NOTE = "本次结果未能存入历史（{reason}），历史页看不到这条记录，请重新核对一次"


# ---------------------------------------------------------------- 公共小工具（history / report 共用）
def job_store(request: Request) -> JobStore:
    store = getattr(request.app.state, "jobs", None)
    if not isinstance(store, JobStore):
        raise ApiError(500, INTERNAL_ERROR, "服务尚未完成初始化，请重启后重试")
    return store


def find_history_task(repo: SqliteRepository, task_id: str, enterprise_id: str) -> CheckTask | None:
    """在本企业历史里按任务号找（软删与「不存档」都不算）；空企业等于没有可见记录。

    只给 `DELETE /history/{task_id}` 用：历史页能看到什么，就能删什么。
    详情 / 报告走 `repo.get_task` 单条查询，不再扫这个窗口。
    """
    if not enterprise_id:
        return None
    for task in repo.list_tasks(enterprise_id, limit=HISTORY_SCAN_LIMIT):
        if task.task_id == task_id:
            return task
    return None


def task_visible(task: CheckTask, user: User, owner_id: str) -> bool:
    """落库任务的可见性矩阵：属主恒可读；同企业只读得到「存入企业档案」的记录。

    未存档（`shared=False`）的任务只给属主，同企业其他人（含管理员）一律当不存在，
    避免「不存档」被同事从任务链接读出来。
    """
    if owner_id and owner_id == user.user_id:
        return True
    if not task.shared:
        return False
    return bool(user.enterprise_id) and task.enterprise_id == user.enterprise_id


def can_read_session(session: TaskSession, user: User) -> bool:
    """内存任务：属主恒可读；存档任务给同企业管理员；**不存档任务只给属主**。

    未落库的任务没有企业隔离做兜底，所以同企业只放宽到管理员（沿用原口径）；
    但「不存档」是用户明确的选择，管理员也不能读。
    """
    if session.owner_id == user.user_id:
        return True
    if not session.task.shared:
        return False
    return is_admin(user) and session.task.enterprise_id == user.enterprise_id


def resolve_readable_task(
    request: Request,
    repo: SqliteRepository,
    task_id: str,
    user: User,
) -> tuple[CheckTask, bool, bool | None]:
    """取一个可读任务：返回（任务, 是否历史, 本次会话的条款开关）。

    越权与不存在都返回 404，不区分——避免探测他人任务是否存在。
    内存会话读不到**不代表**落库记录读不到：存档任务仍按「同企业」开放给同事，
    所以会话分支不放行时继续查库，而不是直接 404。
    落库任务按「属主 / 同企业且存档」判权；软删、问答记录与未知任务号同样 404。
    """
    session = job_store(request).session(task_id)
    if session is not None and can_read_session(session, user):
        return session.task, False, session.clause_review
    task = repo.get_task(task_id)
    if (
        task is None
        or task.mode is not TaskMode.CHECK
        or task.deleted
        or not task_visible(task, user, repo.task_owner(task_id))
    ):
        raise ApiError(404, NOT_FOUND, "任务不存在")
    return task, True, None


def ensure_model(user: User, *, qa: bool = False) -> None:
    """入队前的无模型门禁：409 `NO_MODEL`，文案用服务层常量，管理员/普通用户动作不同。"""
    if allow_no_model() or has_model():
        return
    message = NO_MODEL_QA_MESSAGE if qa else NO_MODEL_MESSAGE
    raise ApiError(409, NO_MODEL, message, action=no_model_action(is_admin(user)))


def _job_view(job: Job) -> JobView:
    return JobView(**job.to_view())


# ---------------------------------------------------------------- 核对
def save_first_round(repo: SqliteRepository, task: CheckTask, user: User) -> None:
    """首轮结果落库（无追问时的唯一出口）：落库 + 操作日志，失败只记诊断不抛。

    有追问时不走这里：作答重跑由 `run_check` 自己落库、跳过追问由 `/answers` 补存，
    那两处都已落库，这里再写就是重复写。
    失败不能吞掉（否则用户以为存了、历史里却没有），也不能让已经算好的结论整份丢掉：
    把原因写进 `task.errors`，随结果 DTO 回给界面。
    """
    try:
        persist(task, user=user, repo=repo, verify_ok=bool(task.verify_ok))
    except Exception as exc:  # noqa: BLE001 - 落库失败是「结果仍可看」的场景，不该升级成 job 失败
        task.errors.append(SAVE_FAILED_NOTE.format(reason=type(exc).__name__))


@router.post(
    "/checks",
    status_code=202,
    dependencies=[Depends(require_capability("check"))],
    response_model=JobView,
)
def post_checks(
    payload: CheckIn,
    auth: AuthDep,
    repo: RepoDep,
    request: Request,
) -> JobView:
    """发起核对：粘贴文本与已存合同二选一；企业事实由服务端读取，客户端不得覆盖。"""
    user = auth.user
    # 门禁顺序：先模型（缺 Key 是"功能不可用"），再企业（缺档案是"状态未就绪"）
    ensure_model(user)
    text = str(payload.text or "").strip()
    contract_id = str(payload.contract_id or "").strip()
    if bool(text) == bool(contract_id):
        raise ApiError(
            400,
            VALIDATION_ERROR,
            "请在合同正文与已存合同之间二选一",
            details={"fields": ["text", "contract_id"]},
        )
    if not user.enterprise_id:
        raise ApiError(409, STATE_CONFLICT, "当前账号未绑定企业，核对结果按企业归档，请先完善企业档案")
    contract_name = str(payload.contract_name or "").strip()
    if contract_id:
        record = repo.get_contract(contract_id, user.enterprise_id)
        if record is None:
            raise ApiError(404, NOT_FOUND, "合同不存在")
        text = record.text
        contract_name = contract_name or normalize_contract_name(record.filename)
    facts = presenters.enterprise_facts(repo, user)
    clause_review = payload.clause_review
    session_clause_review = (
        bool(clause_review) if clause_review is not None else bool(load_config().clause_review.enabled)
    )
    # 存档开关：默认 True（存入企业档案）；False 时任务仍落库，但不进历史、同企业不可读。
    archive = bool(payload.archive)
    store = job_store(request)

    def work(worker_repo: SqliteRepository) -> JobOutcome:
        task = run_check(
            user=user,
            contract_text=text,
            facts=facts,
            repo=worker_repo,
            enterprise_id=user.enterprise_id,
            max_ask_rounds=1,
            save=False,
            contract_name=contract_name,
            contract_id=contract_id,
            clause_review=clause_review,
            shared=archive,
        )
        final_contract_id = contract_id
        if not final_contract_id:
            # 与旧 `_check_form` 同一顺序：核对成功后新合同按原名存档（失败则不落）
            final_contract_id = "C-" + uuid.uuid4().hex[:8]
            worker_repo.save_contract(
                ContractRecord(
                    contract_id=final_contract_id,
                    enterprise_id=user.enterprise_id,
                    filename=f"{contract_name or '合同'}.txt",
                    text=text,
                )
            )
        if not task.questions:
            # 无追问 = 这次核对就是最终结果：立刻落库，避免「看完就走」的记录丢失。
            # 有追问时先不落库，等用户作答重跑或点「跳过」再存（单一出口见模块文档）。
            save_first_round(worker_repo, task, user)
        session = TaskSession(
            task=task,
            owner_id=user.user_id,
            contract_text=text,
            contract_name=contract_name,
            contract_id=final_contract_id,
            clause_review=session_clause_review,
        )
        detail = presenters.task_detail(
            task,
            worker_repo,
            show_process=presenters.include_process(worker_repo),
            clause_review=session_clause_review,
        )
        return JobOutcome(
            task_id=task.task_id,
            result=detail.model_dump(mode="json"),
            session=session,
        )

    return _job_view(
        store.submit(
            kind=KIND_CHECK,
            owner_id=user.user_id,
            work=work,
            no_model_action=no_model_action(is_admin(user)),
        )
    )


@router.get(
    "/jobs/{job_id}",
    dependencies=[Depends(require_capability("check"))],
    response_model=JobView,
)
def get_job(job_id: str, auth: AuthDep, request: Request) -> JobView:
    """轮询状态：仅任务属主；未知 job（含进程重启后的旧 job）404。"""
    job = job_store(request).get(job_id, auth.user.user_id)
    if job is None:
        raise ApiError(404, NOT_FOUND, "任务不存在或已失效，请重新提交")
    return JobView(**job.to_view())


@router.get(
    "/tasks/{task_id}",
    dependencies=[Depends(require_capability("check"))],
    response_model=TaskDetail,
)
def get_task(task_id: str, auth: AuthDep, repo: RepoDep, request: Request) -> TaskDetail:
    """结果详情：过程开关由服务端全局设置决定，客户端没有覆盖入口。"""
    task, history, clause_review = resolve_readable_task(request, repo, task_id, auth.user)
    return presenters.task_detail(
        task,
        repo,
        show_process=presenters.include_process(repo),
        history=history,
        clause_review=clause_review,
    )


# ---------------------------------------------------------------- 追问 / 补充 / 问答
@router.post(
    "/tasks/{task_id}/answers",
    status_code=202,
    dependencies=[Depends(require_capability("check"))],
    response_model=JobView,
)
def post_answers(
    task_id: str,
    payload: AnswersIn,
    auth: AuthDep,
    repo: RepoDep,
    request: Request,
) -> JobView:
    """追问作答：提交则合并事实重跑，跳过则把首轮结果补存；两者都保持原合同、条款与存档开关。"""
    store = job_store(request)
    session = store.session(task_id)
    if session is None or not can_read_session(session, auth.user):
        raise ApiError(404, NOT_FOUND, "任务不存在或已失效，请重新提交")
    user = auth.user
    if payload.skip:

        def work(worker_repo: SqliteRepository) -> JobOutcome:
            persist(session.task, user=user, repo=worker_repo, verify_ok=bool(session.task.verify_ok))
            detail = presenters.task_detail(
                session.task,
                worker_repo,
                show_process=presenters.include_process(worker_repo),
                clause_review=session.clause_review,
            )
            return JobOutcome(
                task_id=session.task.task_id,
                result=detail.model_dump(mode="json"),
                session=session,
            )

    else:
        # 提交追问要重跑核对（要模型）；跳过只是补存首轮结果，不需要模型
        ensure_model(user)
        answers = {
            str(key): str(value).strip()
            for key, value in (payload.answers or {}).items()
            if str(value).strip() and not str(key).startswith("未识别")
        }
        facts = presenters.enterprise_facts(repo, user)
        facts.update(answers)
        ask_rounds = int(session.task.ask_rounds or 0)

        def work(worker_repo: SqliteRepository) -> JobOutcome:
            task = run_check(
                user=user,
                contract_text=session.contract_text,
                facts=facts,
                repo=worker_repo,
                enterprise_id=user.enterprise_id,
                ask_rounds=ask_rounds,
                max_ask_rounds=1,
                contract_name=session.contract_name,
                contract_id=session.contract_id,
                clause_review=session.clause_review,
                shared=session.task.shared,  # 重跑继承原任务的存档开关，不能被默认值洗成 True
                # 属主也继承：管理员代属主重跑时，新记录仍归原属主
                owner_id=session.task.owner_id,
            )
            detail = presenters.task_detail(
                task,
                worker_repo,
                show_process=presenters.include_process(worker_repo),
                clause_review=session.clause_review,
            )
            return JobOutcome(
                task_id=task.task_id,
                result=detail.model_dump(mode="json"),
                session=replace(session, task=task),
            )

    return _job_view(
        store.submit(
            kind=KIND_RECHECK,
            owner_id=user.user_id,
            work=work,
            no_model_action=no_model_action(is_admin(user)),
        )
    )


@router.post(
    "/tasks/{task_id}/optional-facts",
    status_code=202,
    dependencies=[Depends(require_capability("check"))],
    response_model=JobView,
)
def post_optional_facts(
    task_id: str,
    payload: OptionalFactsIn,
    auth: AuthDep,
    repo: RepoDep,
    request: Request,
) -> JobView:
    """可补充事实：只接受本任务页面上出现过的字段，按原合同、原轮次重跑。"""
    store = job_store(request)
    session = store.session(task_id)
    if session is None or not can_read_session(session, auth.user):
        raise ApiError(404, NOT_FOUND, "任务不存在或已失效，请重新提交")
    user = auth.user
    ensure_model(user)
    allowed = set(presenters.optional_picks(session.task, repo))
    unknown = sorted(set(str(key) for key in payload.facts) - allowed)
    if unknown:
        raise ApiError(
            400,
            VALIDATION_ERROR,
            "只能补充本次任务列出的可选字段",
            details={"fields": unknown},
        )
    extra = {str(key): str(value).strip() for key, value in payload.facts.items() if str(value).strip()}
    if not extra:
        raise ApiError(400, VALIDATION_ERROR, "没有填写任何内容，本次未重新核对", details={"fields": []})
    if not str(session.contract_text or "").strip():
        raise ApiError(409, STATE_CONFLICT, "缺少合同正文，无法重新核对，请重新提交合同")
    facts = {str(key): str(value) for key, value in (session.task.facts or {}).items()}
    facts.update(extra)
    ask_rounds = int(session.task.ask_rounds or 0)
    contract_name = session.task.contract_name or session.contract_name
    contract_id = session.task.contract_id or session.contract_id

    def work(worker_repo: SqliteRepository) -> JobOutcome:
        task = run_check(
            user=user,
            contract_text=session.contract_text,
            facts=facts,
            repo=worker_repo,
            enterprise_id=user.enterprise_id,
            ask_rounds=ask_rounds,
            max_ask_rounds=1,
            contract_name=contract_name,
            contract_id=contract_id,
            clause_review=session.clause_review,
            shared=session.task.shared,  # 同上：可选事实重跑也不改存档开关
            owner_id=session.task.owner_id,  # 同上：属主也不改
        )
        detail = presenters.task_detail(
            task,
            worker_repo,
            show_process=presenters.include_process(worker_repo),
            clause_review=session.clause_review,
        )
        return JobOutcome(
            task_id=task.task_id,
            result=detail.model_dump(mode="json"),
            session=replace(session, task=task),
        )

    return _job_view(
        store.submit(
            kind=KIND_RECHECK,
            owner_id=user.user_id,
            work=work,
            no_model_action=no_model_action(is_admin(user)),
        )
    )


@router.post(
    "/tasks/{task_id}/qa",
    status_code=202,
    dependencies=[Depends(require_capability("qa"))],
    response_model=JobView,
)
def post_qa(
    task_id: str,
    payload: QaIn,
    auth: AuthDep,
    request: Request,
) -> JobView:
    """就本合同提问：上下文由服务端按 `_qa_context` 生成，不接收客户端注入的正文或上下文。"""
    store = job_store(request)
    session = store.session(task_id)
    if session is None or not can_read_session(session, auth.user):
        raise ApiError(404, NOT_FOUND, "任务不存在或已失效，请重新提交")
    question = str(payload.question or "").strip()
    if not question:
        raise ApiError(400, VALIDATION_ERROR, "请先输入问题", details={"fields": ["question"]})
    user = auth.user
    # 越界问题由纯规则拒答、不需要模型：只有范围内问题才走 409 门禁（与 run_qa 同一口径）
    if not allow_no_model() and not has_model() and _qa_requires_model(question):
        raise ApiError(409, NO_MODEL, NO_MODEL_QA_MESSAGE, action=no_model_action(is_admin(user)))
    context = presenters.qa_context(session.task)

    def work(worker_repo: SqliteRepository) -> JobOutcome:
        answer = run_qa(
            user=user,
            question=question,
            repo=worker_repo,
            enterprise_id=user.enterprise_id,
            context=context,
        )
        result = presenters.qa_result(
            answer,
            worker_repo,
            parent_task_id=task_id,
            show_process=presenters.include_process(worker_repo),
        )
        return JobOutcome(task_id=answer.task_id, result=result.model_dump(mode="json"))

    return _job_view(
        store.submit(
            kind=KIND_QA,
            owner_id=user.user_id,
            work=work,
            no_model_action=no_model_action(is_admin(user)),
        )
    )
