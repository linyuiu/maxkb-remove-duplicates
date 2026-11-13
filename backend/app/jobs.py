from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from croniter import croniter
from sqlalchemy import select
from .db import transaction
from .models import Task, Run, DataSource, now


def next_fire(expression, tz, base=None):
    if len(expression.split()) != 5:
        raise ValueError("Cron 必须包含 5 段：分 时 日 月 星期")
    zone = ZoneInfo(tz)
    return (
        croniter(expression, (base or now()).astimezone(zone))
        .get_next(datetime)
        .astimezone(timezone.utc)
    )


def enqueue(s, task, config=None):
    from .estimation import scan_config

    if task.deleted_at is not None:
        raise ValueError("任务已删除")
    config = config or scan_config(task)
    # All entry points lock the task, including manual execution and resume.
    source = s.get(DataSource, config["source_id"])
    if not source or not source.enabled:
        raise ValueError("数据源已停用")
    if not source.connection:
        raise ValueError("扫描需要数据库连接")
    if s.scalar(
        select(Run.id).where(
            Run.task_id == task.id, Run.state.in_(["queued", "running"])
        )
    ):
        return None
    run = Run(
        task_id=task.id,
        config=config,
    )
    s.add(run)
    s.flush()
    return run


def tick():
    with transaction() as s:
        tasks = s.scalars(
            select(Task)
            .where(
                Task.deleted_at.is_(None),
                Task.enabled == True,
                Task.next_fire <= now(),
            )
            .with_for_update(skip_locked=True)
        ).all()
        for task in tasks:
            source = s.get(DataSource, task.source_id)
            if not source or not source.enabled or not source.connection or enqueue(s, task) is None:
                task.skipped_fires += 1
            task.next_fire = next_fire(task.cron, task.timezone)
