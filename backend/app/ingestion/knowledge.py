"""First-level directory routing and durable MaxKB knowledge provisioning."""

import json
import logging
import re
import time
import uuid

import httpx
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .pipeline import MaxKBClient, PipelineError

logger = logging.getLogger(__name__)
ID = re.compile(r"[A-Za-z0-9_-]{1,200}")


class Tag(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def clean(self):
        self.key, self.value = self.key.strip(), self.value.strip()
        if not self.key or not self.value or any(ord(c) < 32 for c in self.key + self.value):
            raise ValueError("标签名称和值不能为空或含控制字符")
        return self


class DocumentTagRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paths: list[str] = Field(min_length=1, max_length=10000)
    tags: list[Tag] = Field(min_length=1, max_length=100)


class KnowledgeGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")
    folder: str = Field(default="", max_length=200)
    tags: list[Tag] = Field(default_factory=list, max_length=100)
    folder_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,200}$")
    apply_to_documents: bool | None = None


class DirectoryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root: str = Field(default="", max_length=1000)
    embedding_model_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,200}$")
    folder_id: str = Field(default="default", pattern=r"^[A-Za-z0-9_-]{1,200}$")
    groups: list[KnowledgeGroup] = Field(min_length=1, max_length=1000)
    document_rules: list[DocumentTagRule] = Field(default_factory=list, max_length=100)


def group_of(path, root):
    if root:
        if not path.startswith(root + "/"):
            raise ValueError("所选文件不在确认的根目录下")
        path = path[len(root) + 1 :]
    return path.split("/", 1)[0] if "/" in path else ""


def validate_plan(plan, paths):
    if plan is None:
        return None
    plan = DirectoryPlan.model_validate(plan).model_dump()
    root = plan["root"]
    if root and (root.startswith("/") or any(p in {"", ".", ".."} for p in root.split("/"))):
        raise ValueError("知识库分组根目录无效")
    folders = [g["folder"] for g in plan["groups"]]
    expected = {group_of(path, root) for path in paths}
    if "" not in folders:
        expected.discard("")  # Previously saved plans keep their original direct-file routing.
    if set(folders) != expected or len(folders) != len(set(folders)):
        raise ValueError("知识库计划与一级文件夹不一致，请重新确认")
    for g in plan["groups"]:
        if g["folder"] in {".", ".."} or any(c in g["folder"] for c in "/\\"):
            raise ValueError("一级文件夹名称无效")
        g["tags"] = [
            dict(key=k, value=v)
            for k, v in dict.fromkeys((t["key"], t["value"]) for t in g["tags"])
        ]
    allowed = set(paths)
    memberships = 0
    for rule in plan["document_rules"]:
        rule["paths"] = list(dict.fromkeys(rule["paths"]))
        memberships += len(rule["paths"])
        if any(group_of(path, root) not in folders for path in rule["paths"]):
            raise ValueError("文档标签规则缺少对应的知识库分组")
        if not set(rule["paths"]).issubset(allowed):
            raise ValueError("文档标签规则包含未选择的文件，请重新确认")
        if memberships > 20000:
            raise ValueError("文档标签规则包含过多文件，请合并规则")
        rule["tags"] = unique_tags(rule["tags"])
    return plan


def unique_tags(tags):
    return [dict(key=k, value=v) for k, v in dict.fromkeys((t["key"], t["value"]) for t in tags)]


def prepare_tag_assignments(plan, group, config, paths):
    """Resolve definitions once per knowledge, then use indexed per-file IDs at runtime."""
    base = group["tags"] if config.get("knowledge_tags_enabled", True) else []
    if not config.get("document_tags_enabled", True):
        return unique_tags(base), {path: [] for path in paths}, base
    inherit = group.get("apply_to_documents")
    if inherit is None:
        inherit = config.get("inherit_knowledge_tags", True)
    assignments = {path: list(base) if inherit else [] for path in paths}
    definitions = list(base)
    for rule in plan.get("document_rules", []):
        affected = set(rule["paths"]).intersection(assignments)
        if affected:
            definitions.extend(rule["tags"])
            for path in affected:
                assignments[path].extend(rule["tags"])
    return unique_tags(definitions), {p: unique_tags(t) for p, t in assignments.items()}, base


class KnowledgeStore:
    def init_knowledge(self):
        pass  # Schema is managed by parent Alembic migrations.

    def knowledge_targets(self, queue_id):
        with self.connect() as db:
            rows = [
                dict(r)
                for r in db.execute(
                    "SELECT * FROM knowledge_targets WHERE queue_id=? ORDER BY folder", (queue_id,)
                )
            ]
            saved = db.execute(
                "SELECT plan FROM knowledge_plans WHERE queue_id=?", (queue_id,)
            ).fetchone()
        plan = self.decrypt(saved[0]) if saved else {}
        destinations = {
            g["folder"]: g.get("folder_id") or plan.get("folder_id", "default")
            for g in plan.get("groups", [])
        }
        return [
            {
                **row,
                "candidates": json.loads(row["candidates"]),
                "destination_folder_id": destinations.get(row["folder"], "default"),
            }
            for row in rows
        ]

    def init_targets(self, db, queue_id, plan):
        if plan:
            db.execute("INSERT INTO knowledge_plans VALUES(?,?)", (queue_id, self.encrypt(plan)))
            db.executemany(
                "INSERT INTO knowledge_targets(queue_id,folder,name,status,updated_at) "
                "VALUES(?,?,?,'queued',?)",
                [
                    (queue_id, g["folder"], g["folder"] or "未分类", time.time())
                    for g in plan["groups"]
                ],
            )

    def target_update(self, queue_id, folder, **values):
        with self.connect() as db:
            db.execute(
                "UPDATE knowledge_targets SET "
                + ",".join(k + "=?" for k in values)
                + ",updated_at=? WHERE queue_id=? AND folder=?",
                (*values.values(), time.time(), queue_id, folder),
            )

    def tag_receipts(self, file_id):
        with self.connect() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT t.* FROM document_tag_receipts t JOIN import_attempts a "
                    "ON a.id=t.attempt_id WHERE a.file_id=? ORDER BY t.applied_at",
                    (file_id,),
                )
            ]

    def target_for_file(self, file_id):
        with self.connect() as db:
            row = db.execute(
                "SELECT f.relative_path,b.config,b.queue_id FROM files f "
                "JOIN batches b ON b.id=f.batch_id WHERE f.id=?",
                (file_id,),
            ).fetchone()
            if not row:
                return None
            plan = self.decrypt(row["config"]).get("_knowledge_plan")
            if not plan:
                return None
            folder = group_of(row["relative_path"], plan["root"])
            if not folder and not plan.get("root_knowledge"):
                return None
            target = db.execute(
                "SELECT * FROM knowledge_targets WHERE queue_id=? AND folder=?",
                (row["queue_id"], folder),
            ).fetchone()
        if not target or target["status"] != "ready":
            raise PipelineError("目标知识库尚未准备完成，禁止处理文件")
        with self.connect() as db:
            assigned = db.execute(
                "SELECT tag_ids FROM knowledge_document_tags WHERE queue_id=? AND path=?",
                (row["queue_id"], row["relative_path"]),
            ).fetchone()
        return {
            **dict(target),
            "document_tag_ids": json.loads(assigned[0] if assigned else target["tag_ids"]),
        }


class KnowledgeProvisioner:
    def __init__(self, workers):
        self.workers, self.store = workers, workers.store
        self.folder_check = None

    def recover(self):
        with self.store.connect() as db:
            db.execute(
                "UPDATE knowledge_targets SET status='unknown',error=? WHERE status='creating'",
                ("创建期间服务中断，请核对并填写已创建知识库的 ID，禁止盲目重建",),
            )
            db.execute(
                "UPDATE knowledge_targets SET status='queued' "
                "WHERE status IN ('tagging','matching')"
            )

    def loop(self):
        while not self.workers.stop.is_set():
            try:
                if self.run_one():
                    continue
            except Exception:
                logger.exception("知识库准备调度异常")
            self.workers.stop.wait(1)

    def stopped(self, target):
        with self.store.connect() as db:
            row = db.execute(
                "SELECT intake_state FROM queues WHERE id=?", (target["queue_id"],)
            ).fetchone()
        if not row or row[0] == "stopped":
            self.store.target_update(target["queue_id"], target["folder"], status="stopped")
            return True
        return False

    def run_one(self):
        with self.store.connect() as db:
            if not db.execute(
                "SELECT 1 FROM knowledge_targets t JOIN queues q ON q.id=t.queue_id "
                "WHERE t.status='queued' AND q.intake_state='ready' LIMIT 1"
            ).fetchone():
                return False
            db.execute("SELECT pg_advisory_xact_lock(762190317)")
            row = db.execute(
                "SELECT t.*,b.config,p.plan FROM knowledge_targets t "
                "JOIN queues q ON q.id=t.queue_id "
                "JOIN knowledge_plans p ON p.queue_id=q.id "
                "JOIN batches b ON b.queue_id=q.id AND b.ordinal=0 "
                "WHERE t.status='queued' AND q.intake_state='ready' "
                "ORDER BY q.created_at,t.folder LIMIT 1"
            ).fetchone()
            if not row:
                return False
            target = dict(row)
            state = "tagging" if target["knowledge_id"] else "matching"
            db.execute(
                "UPDATE knowledge_targets SET status=?,updated_at=? WHERE queue_id=? AND folder=?",
                (state, time.time(), target["queue_id"], target["folder"]),
            )
        config = self.store.decrypt(target["config"])
        plan = self.store.decrypt(target["plan"])
        group = next(g for g in plan["groups"] if g["folder"] == target["folder"])
        destination = group.get("folder_id") or plan.get("folder_id", "default")
        from .workers import settings_from

        try:
            if self.stopped(target):
                return True
            with self.workers.client_factory() as client:
                maxkb = MaxKBClient(settings_from(config), client)
                if not target["knowledge_id"]:
                    if destination != "default":
                        try:
                            if (
                                self.folder_check is None
                                or self.folder_check[0] != target["queue_id"]
                                or self.folder_check[2] < time.monotonic()
                            ):
                                allowed = {f["id"] for f in maxkb.knowledge_folders()}
                                self.folder_check = (
                                    target["queue_id"],
                                    allowed,
                                    time.monotonic() + 60,
                                )
                            if destination not in self.folder_check[1]:
                                raise PipelineError("所选目录不在根目录一级目录中或已无权限")
                        except (PipelineError, httpx.HTTPError) as exc:
                            raise KnowledgeRejected(
                                "目标目录核验失败，未发送知识库创建请求，请刷新目录后重试"
                            ) from exc
                    candidates = maxkb.matching_knowledge(target["name"], destination)
                    if self.stopped(target):
                        return True
                    if len(candidates) > 1:
                        self.store.target_update(
                            target["queue_id"],
                            target["folder"],
                            status="failed",
                            candidates=json.dumps(candidates, ensure_ascii=False),
                            error="发现多个同名知识库，请手动选择后继续；未创建新知识库",
                        )
                        return True
                    if candidates:
                        target["knowledge_id"] = maxkb.verify_knowledge_target(
                            candidates[0]["id"], target["name"], destination
                        )
                        self.store.target_update(target["queue_id"], target["folder"], reused=1)
                    else:
                        state = "creating"
                        self.store.target_update(target["queue_id"], target["folder"], status=state)
                        target["knowledge_id"] = maxkb.create_knowledge(
                            target["name"], plan["embedding_model_id"], destination
                        )
                    # Save the returned ID before any subsequent HTTP request.
                    self.store.target_update(
                        target["queue_id"],
                        target["folder"],
                        knowledge_id=target["knowledge_id"],
                        status="tagging",
                    )
                if self.stopped(target):
                    return True
                with self.store.connect() as db:
                    paths = [
                        r[0]
                        for r in db.execute(
                            "SELECT f.relative_path FROM files f JOIN batches b ON b.id=f.batch_id "
                            "WHERE b.queue_id=?",
                            (target["queue_id"],),
                        )
                        if group_of(r[0], plan["root"]) == group["folder"]
                    ]
                definitions, assignments, base = prepare_tag_assignments(plan, group, config, paths)
                ids = maxkb.ensure_tags(target["knowledge_id"], definitions)
                if self.stopped(target):
                    return True
                lookup = {
                    (t["key"], t["value"]): tag_id
                    for t, tag_id in zip(definitions, ids, strict=True)
                }
                with self.store.connect() as db:
                    db.execute("SELECT pg_advisory_xact_lock(762190317)")
                    queue = db.execute(
                        "SELECT intake_state FROM queues WHERE id=?", (target["queue_id"],)
                    ).fetchone()
                    if not queue or queue[0] == "stopped":
                        db.execute(
                            "UPDATE knowledge_targets SET status='stopped' "
                            "WHERE queue_id=? AND folder=?",
                            (target["queue_id"], target["folder"]),
                        )
                        return True
                    db.executemany(
                        "INSERT INTO knowledge_document_tags VALUES(?,?,?) ON CONFLICT(queue_id,path) DO UPDATE SET tag_ids=excluded.tag_ids",
                        [
                            (
                                target["queue_id"],
                                path,
                                json.dumps(sorted({lookup[(t["key"], t["value"])] for t in tags})),
                            )
                            for path, tags in assignments.items()
                        ],
                    )
                    db.execute(
                        "UPDATE knowledge_targets SET status='ready',tag_ids=?,"
                        "error='',updated_at=? "
                        "WHERE queue_id=? AND folder=?",
                        (
                            json.dumps([lookup[(t["key"], t["value"])] for t in base]),
                            time.time(),
                            target["queue_id"],
                            target["folder"],
                        ),
                    )
            logger.info(
                "知识库准备完成：队列 %s · %s · %s",
                target["queue_id"],
                target["folder"],
                target["knowledge_id"],
            )
            with self.store.connect() as db:
                self.store.queue_advance(db, target["queue_id"])
            self.workers.wake.set()
        except Exception as exc:
            status = "unknown" if state == "creating" and not target["knowledge_id"] else "failed"
            if self.stopped(target):
                status = "stopped"
            # Explicit business rejection is known; transport/malformed responses are not.
            if isinstance(exc, KnowledgeRejected) and status != "stopped":
                status = "failed"
            error = (
                settings_from(config).redact(str(exc))
                if isinstance(exc, PipelineError)
                else type(exc).__name__
            )
            self.store.target_update(
                target["queue_id"],
                target["folder"],
                status=status,
                error=error[:2000],
                knowledge_id=target["knowledge_id"],
            )
            logger.error(
                "知识库准备失败：队列 %s · %s · %s", target["queue_id"], target["folder"], error
            )
        return True


class KnowledgeRejected(PipelineError):
    pass


def install_knowledge_routes(app, config_model, validate_config):
    class TagsInput(BaseModel):
        model_config = ConfigDict(extra="forbid")
        tags: list[Tag] = Field(max_length=100)

    class ResolveInput(BaseModel):
        model_config = ConfigDict(extra="forbid")
        folder: str = Field(default="", max_length=200)
        knowledge_id: str = Field(default="", max_length=200)
        maxkb_token: str | None = Field(default=None, max_length=8192)

    @app.get("/api/ingestion/knowledge/tag-settings")
    def tag_settings():
        with app.state.store.connect() as db:
            row = db.execute("SELECT tags FROM knowledge_preferences WHERE id=1").fetchone()
        return {"tags": json.loads(row[0]) if row else []}

    @app.put("/api/ingestion/knowledge/tag-settings")
    def save_tags(body: TagsInput):
        tags = [t.model_dump() for t in body.tags]
        with app.state.store.connect() as db:
            db.execute(
                "INSERT INTO knowledge_preferences VALUES(1,?) ON CONFLICT(id) "
                "DO UPDATE SET tags=excluded.tags",
                (json.dumps(tags),),
            )
        return {"tags": tags}

    @app.post("/api/ingestion/knowledge/models")
    def models(body: config_model):
        from .workers import settings_from

        try:
            config = validate_config(body)
            with app.state.workers.client_factory() as client:
                return MaxKBClient(settings_from(config), client).embedding_models()
        except (PipelineError, httpx.HTTPError) as exc:
            raise HTTPException(502, "获取向量模型失败，请核对连接、权限及模型响应") from exc

    @app.post("/api/ingestion/knowledge/folders")
    def folders(body: config_model):
        from .workers import settings_from

        try:
            config = validate_config(body)
            with app.state.workers.client_factory() as client:
                return MaxKBClient(settings_from(config), client).knowledge_folders()
        except (PipelineError, httpx.HTTPError) as exc:
            raise HTTPException(502, "获取知识库目录失败，请检查连接、权限及目录响应") from exc

    @app.post("/api/ingestion/queues/{queue_id}/knowledge/retry")
    def retry_target(queue_id: uuid.UUID, body: ResolveInput):
        store = app.state.store
        targets = store.knowledge_targets(str(queue_id))
        target = next((t for t in targets if t["folder"] == body.folder), None)
        if not target or target["status"] not in {"failed", "unknown"}:
            raise HTTPException(409, "该知识库当前不可重试")
        with store.connect() as db:
            row = db.execute(
                "SELECT config FROM batches WHERE queue_id=? LIMIT 1", (str(queue_id),)
            ).fetchone()
        config = store.decrypt(row[0])
        if body.maxkb_token:
            config["maxkb_token"] = body.maxkb_token
        if body.knowledge_id:
            if not ID.fullmatch(body.knowledge_id):
                raise HTTPException(422, "知识库 ID 无效")
            from .workers import settings_from

            with app.state.workers.client_factory() as client:
                maxkb = MaxKBClient(settings_from(config), client)
                data = maxkb.knowledge_request("GET", f"knowledge/{body.knowledge_id}")["data"]
            with store.connect() as db:
                saved_plan = db.execute(
                    "SELECT plan FROM knowledge_plans WHERE queue_id=?", (str(queue_id),)
                ).fetchone()
            plan = store.decrypt(saved_plan[0])
            group = next(g for g in plan["groups"] if g["folder"] == body.folder)
            expected_folder = group.get("folder_id") or plan.get("folder_id", "default")
            if (
                not isinstance(data, dict)
                or data.get("id") != body.knowledge_id
                or data.get("name") != target["name"]
                or data.get("workspace_id") != config["workspace_id"]
                or data.get("folder_id", "default") != expected_folder
                or data.get("type", 0) not in (0, "BASE")
            ):
                raise HTTPException(409, "知识库 ID、名称、工作空间或目标目录不匹配")
        elif target["status"] == "unknown":
            raise HTTPException(409, "创建结果不明，请先在 MaxKB 核对并填写已创建知识库 ID")
        with store.connect() as db:
            changed = db.execute(
                "UPDATE knowledge_targets SET status='queued',error='',"
                "knowledge_id=?,updated_at=?, "
                "reused=CASE WHEN ?<>0 THEN 1 ELSE reused END,candidates='[]' "
                "WHERE queue_id=? AND folder=? AND status IN ('failed','unknown')",
                (
                    body.knowledge_id or target["knowledge_id"],
                    time.time(),
                    bool(body.knowledge_id),
                    str(queue_id),
                    body.folder,
                ),
            ).rowcount
            if not changed:
                raise HTTPException(409, "该知识库已有准备任务，请刷新状态")
            if body.maxkb_token:
                from .workers import settings_from

                settings_from(config)
                for batch in db.execute(
                    "SELECT id,config FROM batches WHERE queue_id=?", (str(queue_id),)
                ).fetchall():
                    updated = store.decrypt(batch["config"])
                    updated["maxkb_token"] = body.maxkb_token
                    db.execute(
                        "UPDATE batches SET config=? WHERE id=?",
                        (store.encrypt(updated), batch["id"]),
                    )
        return {"accepted": True}

    @app.post("/api/ingestion/files/{file_id}/tags/retry")
    def retry_document_tags(file_id: uuid.UUID):
        store = app.state.store
        row = store.file(str(file_id))
        if (
            not row
            or row["status"] != "import_unknown"
            or not row["error"].startswith("文档已入库但标签绑定未确认")
            or not store.target_for_file(str(file_id))
        ):
            raise HTTPException(409, "当前文件无需补充标签")
        store.update(
            str(file_id),
            status="import_monitoring",
            next_check=0,
            monitor_errors=0,
            error="",
            message="重新核验文档并补充标签",
        )
        app.state.workers.wake.set()
        return {"accepted": True}
