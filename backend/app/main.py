import os
import secrets
import time
from datetime import timedelta
from collections import defaultdict
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy import select, delete, func, or_
from sqlalchemy.exc import IntegrityError
from psycopg.conninfo import make_conninfo, conninfo_to_dict
from .db import Session, transaction
from .models import (
    User,
    LoginSession,
    DataSource,
    Task,
    Run,
    Pair,
    ParagraphSnapshot,
    Decision,
    Operation,
    now,
    ScanEstimate,
)
from .security import passwords, digest, encrypt, decrypt, cipher, content_hash
from .source import (
    list_knowledge,
    validated_ids,
    current_paragraph,
    connect,
    paragraph_data,
)
from .security import schedule_confirmation
from .jobs import next_fire, enqueue
from .estimation import estimate, scan_config
from .maxkb_api import (
    MaxKBAPI,
    APIError,
    normalize_base,
    document_snapshot,
    document_hash,
)
from .source_operations import execute_operation


@asynccontextmanager
async def lifespan(app):
    import asyncio
    from .initialize import initialize

    await asyncio.to_thread(initialize)
    from .runtime import BackgroundRuntime
    from .ingestion.integration import lifecycle
    async with lifecycle(app.state.ingestion_app):
        runtime=BackgroundRuntime()
        app.state.background_runtime=runtime
        try:
            runtime.start()
            yield
        finally:await asyncio.to_thread(runtime.stop)


app = FastAPI(title="MaxKB 相似片段治理", lifespan=lifespan)
origins = os.environ.get(
    "ALLOWED_ORIGINS", "http://localhost:5173,http://localhost:8080"
).split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type", "X-Requested-With"],
)


@app.middleware("http")
async def mutation_guard(request, call_next):
    if request.method in {"POST", "PUT", "DELETE", "PATCH"}:
        if request.headers.get("X-Requested-With") != "dedup" or (
            request.headers.get("origin") and request.headers["origin"] not in origins
        ):
            from fastapi.responses import JSONResponse

            return JSONResponse({"detail": "请求来源校验失败"}, status_code=403)
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    return response


@app.exception_handler(IntegrityError)
async def conflict(request, exc):
    from fastapi.responses import JSONResponse

    return JSONResponse({"detail": "数据冲突，请刷新后重试"}, status_code=409)


def db():
    with Session() as s:
        yield s


def auth(request: Request):
    token = request.cookies.get("dedup_session", "")
    with Session() as s:
        session = s.get(LoginSession, digest(token)) if token else None
        if not session or session.expires_at <= now():
            raise HTTPException(401, "请先登录")
        user = s.get(User, session.user_id)
        if not user:
            raise HTTPException(401, "账号不存在")
        s.expunge(user)
        return user


def get(s, model, key, *, for_update=False):
    obj = s.get(model, key, with_for_update=for_update)
    if not obj:
        raise HTTPException(404, "记录不存在")
    return obj


def serialize(obj):
    return {
        c.name: getattr(obj, c.name)
        for c in obj.__table__.columns
        if c.name
        not in {"connection", "password_hash", "token", "token_hash", "api_key"}
    }


class LoginBody(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=1024)


attempts = defaultdict(list)


@app.post("/api/login")
def login(body: LoginBody, request: Request, response: Response):
    key = request.client.host
    current = time.monotonic()
    attempts[key] = [t for t in attempts[key] if current - t < 300]
    if len(attempts[key]) >= 10:
        raise HTTPException(429, "登录尝试过多，请稍后重试")
    attempts[key].append(current)
    with transaction() as s:
        user = s.scalar(select(User).where(User.username == body.username).with_for_update())
        if not user or not passwords.verify(body.password, user.password_hash):
            raise HTTPException(401, "用户名或密码错误")
        token = secrets.token_urlsafe(32)
        s.add(
            LoginSession(
                token_hash=digest(token),
                user_id=user.id,
                expires_at=now() + timedelta(hours=12),
            )
        )
        response.set_cookie(
            "dedup_session",
            token,
            httponly=True,
            samesite="strict",
            max_age=43200,
            secure=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
        )
        attempts.pop(key, None)
        return {"username": user.username}


@app.get("/api/me")
def me(user=Depends(auth)):
    return {"username": user.username}


@app.post("/api/logout")
def logout(request: Request, response: Response, user=Depends(auth)):
    with transaction() as s:
        s.execute(
            delete(LoginSession).where(
                LoginSession.token_hash
                == digest(request.cookies.get("dedup_session", ""))
            )
        )
    response.delete_cookie("dedup_session")
    return {"ok": True}


class ChangePasswordBody(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=12, max_length=1024)
    confirm_password: str = Field(min_length=1, max_length=1024)


password_attempts = defaultdict(list)


@app.post("/api/me/password")
def change_password(body: ChangePasswordBody, response: Response, user=Depends(auth)):
    if body.new_password != body.confirm_password:
        raise HTTPException(400, "两次输入的新密码不一致")
    if body.current_password == body.new_password:
        raise HTTPException(400, "新密码不能与原密码相同")
    current = time.monotonic()
    password_attempts[user.id] = [t for t in password_attempts[user.id] if current - t < 300]
    if len(password_attempts[user.id]) >= 10:
        raise HTTPException(429, "原密码验证尝试过多，请稍后重试")
    password_attempts[user.id].append(current)
    with transaction() as s:
        # Lock the current hash, rather than verifying the detached auth snapshot.
        # Login takes the same lock so an old password cannot create a late session.
        account = s.scalar(select(User).where(User.id == user.id).with_for_update())
        if not account or not passwords.verify(body.current_password, account.password_hash):
            raise HTTPException(400, "原密码错误")
        account.password_hash = passwords.hash(body.new_password)
        s.execute(delete(LoginSession).where(LoginSession.user_id == user.id))
    password_attempts.pop(user.id, None)
    response.delete_cookie("dedup_session")
    return {"ok": True}


@app.get("/api/health")
def health(s=Depends(db)):
    s.execute(select(1))
    return {"status": "ok"}


class SourceBody(BaseModel):
    api_base_url: str = Field(default="", max_length=2048)
    api_key: str = Field(default="", max_length=4096)
    name: str = Field(min_length=1, max_length=150)
    host: str = Field(default="", max_length=255)
    port: int = Field(default=5432, ge=1, le=65535)
    dbname: str = Field(default="", max_length=100)
    user: str = Field(default="", max_length=100)
    password: str = Field(default="", max_length=1024)
    sslmode: str = Field(
        default="prefer",
        pattern="^(disable|allow|prefer|require|verify-ca|verify-full)$",
    )
    enabled: bool = True


def source_target_in_use(source_id):
    return or_(
        select(Task.id).where(Task.source_id == source_id).exists(),
        select(Run.id).where(Run.config["source_id"].as_string() == source_id).exists(),
        select(Operation.id).where(
            Operation.target["source_id"].as_string() == source_id
        ).exists(),
    )


def source_view(source, target_in_use=False):
    value = serialize(source)
    value["database_target_locked"] = bool(source.connection and target_in_use)
    value["api_configured"] = bool(source.api_base_url and source.api_key)
    info = conninfo_to_dict(decrypt(source.connection)) if source.connection else {}
    value["api_only"] = not bool(source.connection)
    value.update(
        {k: info.get(k, "") for k in ["host", "port", "dbname", "user", "sslmode"]}
    )
    if not source.connection: value.update(port=5432, sslmode="prefer")
    return value


def connection_info(body):
    if not body.host.strip():
        if not body.api_base_url.strip():raise HTTPException(400,"请填写数据库主机或 MaxKB API 地址")
        return None
    if not body.dbname.strip() or not body.user.strip():raise HTTPException(400,"数据库名称和用户不能为空")
    return body.model_dump(exclude={"name","enabled","api_base_url","api_key"})


@app.get("/api/sources")
def sources(s=Depends(db), user=Depends(auth)):
    rows = s.execute(
        select(DataSource, source_target_in_use(DataSource.id))
        .order_by(DataSource.created_at.desc())
    )
    return [source_view(source, target_in_use) for source, target_in_use in rows]


@app.post("/api/sources")
def create_source(body: SourceBody, user=Depends(auth)):
    try:
        base = normalize_base(body.api_base_url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    with transaction() as s:
        info = connection_info(body)
        source = DataSource(
            name=body.name,
            enabled=body.enabled,
            connection=encrypt(make_conninfo(**info)) if info else "",
            api_base_url=base,
            api_key=encrypt(body.api_key.strip()) if body.api_key.strip() else "",
        )
        s.add(source)
        s.flush()
        return source_view(source)


@app.put("/api/sources/{source_id}")
def edit_source(source_id: str, body: SourceBody, user=Depends(auth)):
    try:
        base = normalize_base(body.api_base_url)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    with transaction() as s:
        source = get(s, DataSource, source_id, for_update=True)
        old = conninfo_to_dict(decrypt(source.connection)) if source.connection else {}
        target_changed = bool(old) and any(
            str(getattr(body, key)) != str(old.get(key)) for key in ("host", "port", "dbname")
        )
        target_in_use = s.scalar(select(source_target_in_use(source.id)))
        if target_changed and target_in_use:
            raise HTTPException(400, "该数据源已关联扫描任务或历史记录，请新建数据源以更换数据库目标")
        info = connection_info(body)
        if info:
            if target_changed and old.get("password") and not body.password:
                raise HTTPException(400, "数据库目标已更换，请重新填写数据库密码")
            info["password"] = body.password or old.get("password", "")
        source.name, source.enabled, source.connection = (
            body.name,
            body.enabled,
            encrypt(make_conninfo(**info)) if info else "",
        )
        if base != source.api_base_url and not body.api_key.strip():
            source.api_key = ""
        source.api_base_url = base
        if body.api_key.strip():
            source.api_key = encrypt(body.api_key.strip())
        return source_view(source, target_in_use)


@app.get("/api/sources/{source_id}/knowledge")
def knowledge(source_id: str, s=Depends(db), user=Depends(auth)):
    source = get(s, DataSource, source_id)
    try:
        return list_knowledge(source)
    except Exception:
        raise HTTPException(400, "无法读取知识库，请检查连接、只读权限及 MaxKB 表结构")


@app.post("/api/sources/{source_id}/test")
def test_source(source_id: str, s=Depends(db), user=Depends(auth)):
    source = get(s, DataSource, source_id)
    try:
        with connect(source) as conn:
            readonly = conn.execute("SHOW transaction_read_only").fetchone()[
                "transaction_read_only"
            ]
        return {"ok": True, "read_only": readonly == "on"}
    except Exception:
        raise HTTPException(400, "连接失败，请检查地址、认证、SSL 和数据库权限")


class TaskBody(BaseModel):
    save_estimate: str | None = Field(default=None, max_length=100000)
    name: str = Field(min_length=1, max_length=150)
    source_id: str
    knowledge_ids: list[str] = Field(min_length=1)
    threshold: float = Field(default=0.9, ge=0, le=1, allow_inf_nan=False)
    exclude_same_document: bool = False
    include_inactive_documents: bool = True
    batch_size: int = Field(default=32, ge=1, le=256)
    cron: str | None = None
    timezone: str = "Asia/Shanghai"
    enabled: bool = False


def task_values(body):
    try:
        values = body.model_dump(exclude={"save_estimate"})
        values["knowledge_ids"] = validated_ids(body.knowledge_ids)
        values["cron"] = body.cron.strip() if body.cron and body.cron.strip() else None
        if body.enabled and not values["cron"]:
            raise ValueError("启用定时需要 Cron 表达式")
        values["next_fire"] = (
            next_fire(values["cron"], body.timezone) if values["cron"] else None
        )
        return values
    except Exception as exc:
        raise HTTPException(400, f"任务配置无效：{exc}")


@app.get("/api/tasks")
def tasks(s=Depends(db), user=Depends(auth)):
    return [serialize(t) for t in s.scalars(select(Task).order_by(Task.name))]


def require_schedule_confirmation(body, values, user, task_id=None):
    if not body.enabled:
        return
    if not body.save_estimate:
        raise HTTPException(409, "请先预估数据量和耗时，并确认保存定时任务")
    try:
        schedule_confirmation(user.id, task_id, values, body.save_estimate)
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/tasks/preview")
@app.post("/api/tasks/{task_id}/preview")
def preview_schedule(body: TaskBody, task_id: str | None = None, user=Depends(auth)):
    values = task_values(body)
    with Session() as s:
        if task_id:
            get(s, Task, task_id)
        source = get(s, DataSource, body.source_id)
        if not source.connection:raise HTTPException(400,"此连接仅配置 API，扫描需要数据库连接")
        if not source.enabled:
            raise HTTPException(409, "数据源已停用")
        config = scan_config(Task(**values))
        try:
            summary = estimate(source, config, s)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except Exception:
            raise HTTPException(502, "统计失败，请检查数据源连接和权限")
    return {
        **summary,
        "task_name": body.name,
        "cron": values["cron"],
        "timezone": body.timezone,
        "next_fire": values["next_fire"],
        "expires_at": now() + timedelta(minutes=10),
        "save_estimate": schedule_confirmation(user.id, task_id, values),
    }


@app.post("/api/tasks")
def create_task(body: TaskBody, user=Depends(auth)):
    values = task_values(body)
    require_schedule_confirmation(body, values, user)
    with transaction() as s:
        source = get(s, DataSource, body.source_id, for_update=True)
        if not source.connection:raise HTTPException(400,"此连接仅配置 API，扫描需要数据库连接")
        if body.enabled and not source.enabled:
            raise HTTPException(409, "数据源已停用")
        task = Task(**values)
        s.add(task)
        s.flush()
        return serialize(task)


@app.put("/api/tasks/{task_id}")
def edit_task(task_id: str, body: TaskBody, user=Depends(auth)):
    values = task_values(body)
    require_schedule_confirmation(body, values, user, task_id)
    with transaction() as s:
        task = s.execute(
            select(Task).where(Task.id == task_id).with_for_update()
        ).scalar_one_or_none()
        if not task:
            raise HTTPException(404, "任务不存在")
        source = get(s, DataSource, body.source_id, for_update=True)
        if not source.connection:raise HTTPException(400,"此连接仅配置 API，扫描需要数据库连接")
        if body.enabled and not source.enabled:
            raise HTTPException(409, "数据源已停用")
        for key, value in values.items():
            setattr(task, key, value)
        return serialize(task)


class EstimateRequest(BaseModel):
    retry_run_id: str | None = None


class ConfirmScan(BaseModel):
    estimate_id: str


@app.post("/api/tasks/{task_id}/estimate")
def estimate_task(task_id: str, body: EstimateRequest, user=Depends(auth)):
    with Session() as s:
        task = get(s, Task, task_id)
        config = scan_config(task)
        if body.retry_run_id:
            previous = get(s, Run, body.retry_run_id)
            if previous.task_id != task_id or previous.state not in [
                "failed",
                "cancelled",
            ]:
                raise HTTPException(409, "此运行不能重新扫描")
            previous_config = {"include_inactive_documents": True, **previous.config}
            config = {key: previous_config[key] for key in config}
            config["retry_of"] = previous.id
        source = get(s, DataSource, config["source_id"])
        if not source.enabled:
            raise HTTPException(409, "数据源已停用")
        if s.scalar(
            select(Run.id).where(
                Run.task_id == task_id, Run.state.in_(["queued", "running"])
            )
        ):
            raise HTTPException(409, "任务已有活动运行")
        try:
            summary = estimate(source, config, s)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except Exception:
            raise HTTPException(502, "统计失败，请检查数据库连接、权限和查询超时")
    with transaction() as s:
        preview = ScanEstimate(
            task_id=task_id,
            user_id=user.id,
            config=config,
            summary=summary,
            expires_at=now() + timedelta(minutes=10),
        )
        s.add(preview)
        s.flush()
        return {
            "estimate_id": preview.id,
            "expires_at": preview.expires_at,
            "task_name": config["task_name"],
            **summary,
        }


@app.post("/api/tasks/{task_id}/run")
def run_task(task_id: str, body: ConfirmScan, user=Depends(auth)):
    with transaction() as s:
        task = s.execute(
            select(Task).where(Task.id == task_id).with_for_update()
        ).scalar_one_or_none()
        if not task:
            raise HTTPException(404, "任务不存在")
        preview = s.execute(
            select(ScanEstimate)
            .where(ScanEstimate.id == body.estimate_id)
            .with_for_update()
        ).scalar_one_or_none()
        if not preview or preview.task_id != task_id or preview.user_id != user.id:
            raise HTTPException(400, "无效的扫描预估")
        if preview.run_id:
            return serialize(get(s, Run, preview.run_id))
        if preview.expires_at <= now():
            raise HTTPException(409, "预估已过期，请重新统计")
        preview_config = {"include_inactive_documents": True, **preview.config}
        if preview_config.get("retry_of"):
            previous = get(s, Run, preview_config["retry_of"])
            if previous.state not in ["failed", "cancelled"]:
                raise HTTPException(409, "原运行状态已变化")
        elif preview_config != scan_config(task):
            raise HTTPException(409, "任务配置已变化，请重新预估")
        if preview.summary["comparisons"] <= 0:
            raise HTTPException(400, "所选范围没有可比较的不同段落")
        try:
            run = enqueue(
                s,
                task,
                {
                    **preview_config,
                    "estimate": preview.summary,
                    "estimate_id": preview.id,
                },
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        if not run:
            raise HTTPException(409, "任务已有活动运行")
        preview.run_id = run.id
        return serialize(run)


@app.get("/api/runs")
def runs(page: int = 1, s=Depends(db), user=Depends(auth)):
    return [
        serialize(r)
        for r in s.scalars(
            select(Run)
            .order_by(Run.created_at.desc())
            .offset(max(0, page - 1) * 50)
            .limit(50)
        )
    ]


@app.delete("/api/runs/{run_id}")
def delete_run(run_id: str, user=Depends(auth)):
    with transaction() as s:
        run = get(s, Run, run_id)
        # Match scan confirmation's task -> run lock order.
        s.execute(select(Task.id).where(Task.id == run.task_id).with_for_update()).scalar_one()
        run = s.execute(select(Run).where(Run.id == run_id).with_for_update()
                        .execution_options(populate_existing=True)).scalar_one_or_none()
        if not run:
            raise HTTPException(404, "扫描记录不存在")
        if run.state not in {"succeeded", "failed", "cancelled"}:
            raise HTTPException(409, "请先取消扫描，等待任务停止后再删除")
        # Foreign keys roll back deletion if a new draft is inserted concurrently.
        pair_ids = select(Pair.id).where(Pair.run_id == run_id)
        operations = s.scalars(select(Operation).where(Operation.pair_id.in_(pair_ids))
                              .order_by(Operation.id).with_for_update()).all()
        if any(op.state in {"executing", "unknown"} for op in operations):
            raise HTTPException(409, "本次扫描有正在执行或待核实的源系统操作，请先处理后再删除")
        for op in operations:
            op.target = {**op.target, "scan_run_id": run.id, "scan_task_name": run.config.get("task_name", ""),
                         "scan_pair_id": op.pair_id}
            op.pair_id = None
        s.flush()
        # Remove consumed confirmations so they cannot recreate the deleted run.
        s.execute(delete(ScanEstimate).where(ScanEstimate.run_id == run_id))
        s.execute(delete(Decision).where(Decision.pair_id.in_(pair_ids)))
        s.execute(delete(Pair).where(Pair.run_id == run_id))
        s.execute(delete(ParagraphSnapshot).where(ParagraphSnapshot.run_id == run_id))
        s.delete(run)
    return {"ok": True}


@app.post("/api/runs/{run_id}/cancel")
def cancel(run_id: str, user=Depends(auth)):
    with transaction() as s:
        r = s.execute(
            select(Run).where(Run.id == run_id).with_for_update()
        ).scalar_one_or_none()
        if not r:
            raise HTTPException(404, "运行不存在")
        if r.state == "queued":
            r.state, r.finished_at = "cancelled", now()
        elif r.state == "running":
            r.cancel_requested = True
        else:
            raise HTTPException(409, "当前状态不能取消")
        return serialize(r)


@app.post("/api/runs/{run_id}/resume")
def resume(run_id: str, body: ConfirmScan, user=Depends(auth)):
    with Session() as s:
        previous = get(s, Run, run_id)
        preview = get(s, ScanEstimate, body.estimate_id)
        if preview.config.get("retry_of") != run_id:
            raise HTTPException(400, "请先预估此次重新扫描")
        task_id = previous.task_id
    return run_task(task_id, body, user)


@app.get("/api/runs/{run_id}/knowledge")
def run_knowledge(run_id: str, s=Depends(db), user=Depends(auth)):
    get(s, Run, run_id)
    data = ParagraphSnapshot.data
    query = (
        select(
            data["knowledge_id"].as_string().label("id"),
            data["knowledge_name"].as_string().label("name"),
            data["workspace_id"].as_string().label("workspace_id"),
        )
        .where(ParagraphSnapshot.run_id == run_id)
        .distinct()
        .order_by("name", "workspace_id", "id")
    )
    return [dict(row) for row in s.execute(query).mappings()]


@app.get("/api/runs/{run_id}/pairs")
def pairs(
    run_id: str,
    page: int = 1,
    status: str = "",
    knowledge_id: str = "",
    document: str = "",
    min_score: float = 0,
    s=Depends(db),
    user=Depends(auth),
):
    run = get(s, Run, run_id)
    q = select(Pair).where(Pair.run_id == run_id, Pair.similarity >= min_score)
    if status:
        q = q.where(Pair.status == status)
    if knowledge_id or document:
        ids = select(ParagraphSnapshot.paragraph_id).where(
            ParagraphSnapshot.run_id == run_id
        )
        if knowledge_id:
            ids = ids.where(
                ParagraphSnapshot.data["knowledge_id"].as_string() == knowledge_id
            )
        if document:
            ids = ids.where(
                ParagraphSnapshot.data["document_name"]
                .as_string()
                .icontains(document, autoescape=True)
            )
        q = q.where(Pair.left_id.in_(ids) | Pair.right_id.in_(ids))
    total = s.scalar(select(func.count()).select_from(q.subquery()))
    rows = s.scalars(
        q.order_by(Pair.similarity.desc(), Pair.id)
        .offset(max(0, page - 1) * 20)
        .limit(20)
    ).all()
    ids = {p for r in rows for p in [r.left_id, r.right_id]}
    snapshots = {
        v.paragraph_id: v.data
        for v in s.scalars(
            select(ParagraphSnapshot).where(
                ParagraphSnapshot.run_id == run_id,
                ParagraphSnapshot.paragraph_id.in_(ids),
            )
        )
    }
    return {
        "run": serialize(run),
        "total": total,
        "items": [
            {
                **serialize(r),
                "left": snapshots[r.left_id],
                "right": snapshots[r.right_id],
            }
            for r in rows
        ],
    }


class ReviewBody(BaseModel):
    status: str = Field(pattern="^(pending|ignored|resolved)$")
    note: str = Field(default="", max_length=10000)


@app.post("/api/pairs/{pair_id}/review")
def review(pair_id: str, body: ReviewBody, user=Depends(auth)):
    with transaction() as s:
        pair = get(s, Pair, pair_id)
        if get(s, Run, pair.run_id).state != "succeeded":
            raise HTTPException(409, "完整扫描成功后才能处理")
        if pair.status == "source_changed":
            raise HTTPException(409, "内容已变化，需重新扫描，不能更改旧结果的处理状态")
        pair.status = body.status
        s.add(
            Decision(
                pair_id=pair.id, user_id=user.id, status=body.status, note=body.note
            )
        )
        return serialize(pair)


def pair_target(s, pair_id, side):
    pair = get(s, Pair, pair_id)
    run = get(s, Run, pair.run_id)
    if side not in ["left", "right"]:
        raise HTTPException(400, "无效的段落方向")
    pid = pair.left_id if side == "left" else pair.right_id
    snap = s.scalar(
        select(ParagraphSnapshot).where(
            ParagraphSnapshot.run_id == run.id, ParagraphSnapshot.paragraph_id == pid
        )
    )
    source = get(s, DataSource, run.config["source_id"])
    return pair, run, source, snap.data


@app.get("/api/pairs/{pair_id}/current/{side}")
def current(pair_id: str, side: str, s=Depends(db), user=Depends(auth)):
    pair, run, source, target = pair_target(s, pair_id, side)
    try:
        if source.api_base_url and source.api_key:
            with MaxKBAPI(source) as client:
                value = client.current(target)
        else:
            value = current_paragraph(source, target)
    except Exception:
        raise HTTPException(502, "读取源系统当前文本失败")
    return {
        "api_configured": bool(source.api_base_url and source.api_key),
        "current": value,
        "changed": value is None or value["content_hash"] != target["content_hash"],
    }


class OperationBody(BaseModel):
    side: str = Field(pattern="^(left|right)$")
    kind: str = Field(pattern="^(update_paragraph|delete_paragraph|delete_document)$")
    title: str = Field(default="", max_length=256)
    content: str = Field(default="", max_length=102400)
    expected_hash: str = ""
    expected_document_hash: str = ""


@app.post("/api/pairs/{pair_id}/operations")
def draft_operation(pair_id: str, body: OperationBody, user=Depends(auth)):
    with transaction() as s:
        pair, run, source, target = pair_target(s, pair_id, body.side)
        if run.state != "succeeded":
            raise HTTPException(409, "完整扫描成功后才能处理")
        configured = bool(source.api_base_url and source.api_key)
        try:
            if configured:
                with MaxKBAPI(source) as client:
                    before = (
                        document_snapshot(target, client.document(target))
                        if body.kind == "delete_document"
                        else client.current(target)
                    )
            else:
                if body.kind == "delete_document":
                    raise HTTPException(400, "请先配置 MaxKB API")
                before = current_paragraph(source, target)
        except HTTPException:
            raise
        except APIError as exc:
            raise HTTPException(502, str(exc))
        except Exception:
            raise HTTPException(502, "读取源系统当前文本失败")
        if not before:
            raise HTTPException(409, "源段落已删除")
        if body.kind == "delete_document":
            if (
                not body.expected_document_hash
                or before["document_hash"] != body.expected_document_hash
            ):
                raise HTTPException(409, "文档段落已变化，请刷新整篇文档后重新核对")
        elif before["content_hash"] != body.expected_hash:
            raise HTTPException(409, "源内容已变化，请刷新后重新核对")
        op = Operation(
            pair_id=pair.id,
            user_id=user.id,
            kind=body.kind,
            target={
                k: target[k]
                for k in ["workspace_id", "knowledge_id", "document_id", "paragraph_id"]
            }
            | {"source_id": source.id},
            before=before,
            proposed={"title": body.title, "content": body.content}
            if body.kind == "update_paragraph"
            else None,
            state="draft" if configured else "unconfigured",
            error=None if configured else "源系统写入 API 尚未配置；仅保存草稿",
        )
        s.add(op)
        s.flush()
        return serialize(op)


@app.get("/api/operations")
def operations(page: int = 1, s=Depends(db), user=Depends(auth)):
    return [
        serialize(o)
        for o in s.scalars(
            select(Operation)
            .order_by(Operation.created_at.desc())
            .offset(max(0, page - 1) * 50)
            .limit(50)
        )
    ]


@app.get("/api/pairs/{pair_id}/history")
def history(pair_id: str, s=Depends(db), user=Depends(auth)):
    get(s, Pair, pair_id)
    return [
        serialize(d)
        for d in s.scalars(
            select(Decision)
            .where(Decision.pair_id == pair_id)
            .order_by(Decision.created_at)
        )
    ]


@app.post("/api/operations/{operation_id}/execute")
def execute_source_operation(operation_id: str, user=Depends(auth)):
    try:
        return serialize(execute_operation(operation_id, user.id))
    except APIError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/operations/{operation_id}/reconcile")
def reconcile_source_operation(operation_id: str, user=Depends(auth)):
    try:
        return serialize(execute_operation(operation_id, user.id, reconcile=True))
    except APIError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/pairs/{pair_id}/document/{side}")
def document_content(pair_id: str, side: str, s=Depends(db), user=Depends(auth)):
    pair, run, source, target = pair_target(s, pair_id, side)
    try:
        with MaxKBAPI(source) as client:
            records = client.document(target)
            return {
                **target,
                "document_hash": document_hash(records),
                "document_name": target["document_name"],
                "records": [
                    {**row, "content_hash": content_hash(row.get("title", ""), row["content"])}
                    for row in records
                ],
            }
    except APIError as exc:
        raise HTTPException(400, str(exc))


from .ingestion.integration import install as install_ingestion
app.state.ingestion_app=install_ingestion(app,auth)

@app.post('/api/ingestion/connection/{source_id}')
def reuse_ingestion_connection(source_id:str, user=Depends(auth)):
    with Session() as s:
        source=get(s,DataSource,source_id)
        if not source.enabled or not source.api_base_url or not source.api_key:
            raise HTTPException(400,'所选连接未配置可用的 MaxKB API')
        base,key=source.api_base_url,decrypt(source.api_key)
    store=app.state.ingestion_app.state.store
    config=store.settings()
    config.update(maxkb_base_url=base,maxkb_token=key)
    store.save_settings(config)
    return {'ok':True}


# Register frontend last so API endpoints always take precedence.
from .frontend import install_frontend
install_frontend(app)
