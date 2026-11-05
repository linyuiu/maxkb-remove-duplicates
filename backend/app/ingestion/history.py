"""Read-only, bounded views of persisted tasks and files across all uploads."""

from __future__ import annotations

import time

GROUPS = {
    "pending": ("pending_upload", "waiting_wave", "queued", "split_queued", "import_queued"),
    "active": (
        "uploading",
        "parsing",
        "downloading",
        "packaging",
        "splitting",
        "importing",
        "import_monitoring",
        "import_checking",
        "reconciling",
        "cleanup_pending",
        "cleanup_running",
    ),
    "review": ("review",),
    "imported": ("imported",),
    "importing": ("import_queued", "importing", "import_monitoring", "import_checking"),
    "import_failed": ("import_failed", "import_rejected"),
    "uncertain": ("import_unknown", "preparation_unknown", "cleanup_failed", "reconciling"),
    "failed": (
        "stopped",
        "failed",
        "import_failed",
        "import_rejected",
        "import_unknown",
        "cleanup_failed",
        "preparation_unknown",
    ),
}
TASKS = """WITH tasks AS (
    SELECT id,name,created_at,'queue' AS kind FROM queues
    UNION ALL
    SELECT id,name,created_at,'batch' AS kind FROM batches WHERE queue_id IS NULL
) """


def add_timings(db, items, view, now):
    """Aggregate only the selected page; never load paragraphs or audit payloads.

    Elapsed time includes intake, queueing and manual review. Stage sums represent
    each file's latest recorded execution, not a sum of every historical retry.
    Missing legacy end timestamps remain unknown instead of using updated_at,
    which can move when someone previews a completed file.
    """
    if not items:
        return
    from .store import FINISHED

    terminal = ",".join(f"'{status}'" for status in sorted(FINISHED))
    scope = {"tasks": "COALESCE(b.queue_id,b.id)", "batches": "b.id", "files": "f.id"}[view]
    marks = ",".join("?" for _ in items)
    ids = [item["id"] for item in items]
    condition = f"{scope} IN ({marks})"
    if view == "tasks":
        # Preserve indexed queue/batch lookup instead of filtering on COALESCE.
        condition = f"(b.queue_id IN ({marks}) OR b.id IN ({marks}))"
        ids *= 2
    rows = db.execute(
        f"""WITH clock AS (SELECT ? AS now), timings AS (
            SELECT {scope} AS scope,f.status,
                CASE WHEN f.status IN ({terminal}) THEN COALESCE(f.finished_at,
                    CASE WHEN f.status IN ('imported','import_failed','import_rejected')
                        THEN f.import_finished_at END) END AS ended,
                COALESCE(f.parse_seconds,CASE
                    WHEN f.kind='pdf' AND f.parse_started_at IS NOT NULL
                        AND f.parse_state IN ('uploading','waiting','running')
                    THEN GREATEST(0,clock.now-f.parse_started_at) END) AS parse_seconds,
                COALESCE(f.split_seconds,CASE WHEN f.status='splitting'
                    AND f.split_started_at IS NOT NULL
                    THEN GREATEST(0,clock.now-f.split_started_at) END) AS split_seconds,
                CASE WHEN a.id IS NOT NULL THEN GREATEST(0,COALESCE(
                    CASE WHEN f.status NOT IN ('importing','import_monitoring',
                        'import_checking','cleanup_pending','cleanup_running','reconciling')
                        THEN COALESCE(a.finished_at,f.import_finished_at,f.finished_at) END,
                    clock.now)-a.created_at)
                    ELSE f.import_seconds END AS import_seconds
            FROM files f JOIN batches b ON b.id=f.batch_id
            LEFT JOIN import_attempts a ON a.id=f.attempt_id CROSS JOIN clock
            WHERE {condition}
        ) SELECT scope,
            SUM(status NOT IN ({terminal})) AS unfinished,
            SUM(status IN ({terminal}) AND ended IS NULL) AS missing_end,
            MAX(ended) AS ended,SUM(parse_seconds) AS parse_seconds,
            SUM(split_seconds) AS split_seconds,SUM(import_seconds) AS import_seconds
        FROM timings GROUP BY scope""",
        [now, *ids],
    )
    snapshots = {row["scope"]: dict(row) for row in rows}
    for item in items:
        row = snapshots.get(item["id"], {})
        running = bool(row.get("unfinished"))
        end = now if running else None if row.get("missing_end") else row.get("ended")
        item["timing"] = {
            "elapsed_seconds": max(0, end - item["created_at"]) if end is not None else None,
            "running": running,
            **{key: row.get(key) for key in ("parse_seconds", "split_seconds", "import_seconds")},
        }


def read_history(store, *, view="tasks", state="all", search="", offset=0, limit=25, batch_id=None):
    from .store import public_file

    pattern = "%" + search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    now = time.time()
    with store.connect() as db:
        db.execute("BEGIN")
        counts = dict(db.execute("SELECT status,COUNT(*) FROM files GROUP BY status").fetchall())
        summary = {key: sum(counts.get(s, 0) for s in states) for key, states in GROUPS.items()}
        summary["files"] = sum(counts.values())
        summary["tasks"] = db.execute(TASKS + "SELECT COUNT(*) FROM tasks").fetchone()[0]
        summary["batches"] = db.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
        if view == "tasks":
            where, params = "", []
            if search:
                where = """ WHERE (name ILIKE ? ESCAPE '\\' OR EXISTS (
                    SELECT 1 FROM batches b JOIN files f ON f.batch_id=b.id
                    WHERE (b.queue_id=tasks.id OR (tasks.kind='batch' AND b.id=tasks.id))
                    AND (f.name ILIKE ? ESCAPE '\\' OR f.relative_path ILIKE ? ESCAPE '\\')))"""
                params = [pattern] * 3
            if state != "all":
                where += (" AND " if where else " WHERE ") + (
                    "EXISTS (SELECT 1 FROM batches b JOIN files f ON f.batch_id=b.id "
                    "WHERE (b.queue_id=tasks.id OR (tasks.kind='batch' AND b.id=tasks.id)) "
                    f"AND f.status IN ({','.join('?' for _ in GROUPS[state])}))"
                )
                params.extend(GROUPS[state])
            total = db.execute(TASKS + "SELECT COUNT(*) FROM tasks" + where, params).fetchone()[0]
            items = [
                dict(r)
                for r in db.execute(
                    TASKS
                    + "SELECT * FROM tasks"
                    + where
                    + " ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                    [*params, limit, offset],
                )
            ]
            if items:
                ids = [item["id"] for item in items]
                placeholders = ",".join("?" for _ in ids)
                aggregates = {
                    r["task_id"]: dict(r)
                    for r in db.execute(
                        f"""
                    SELECT COALESCE(b.queue_id,b.id) AS task_id,COUNT(f.id) AS files,
                        COUNT(DISTINCT b.id) AS batches,
                        SUM(f.status='imported') AS imported,SUM(f.status='review') AS review,
                        SUM(f.status IN ({",".join("?" for _ in GROUPS["failed"])})) AS failed
                    FROM batches b LEFT JOIN files f ON f.batch_id=b.id
                    WHERE b.queue_id IN ({placeholders}) OR b.id IN ({placeholders})
                    GROUP BY COALESCE(b.queue_id,b.id)
                """,
                        [*GROUPS["failed"], *ids, *ids],
                    )
                }
                first_batches = {}
                for row in db.execute(
                    "SELECT COALESCE(queue_id,id) AS task_id,id FROM batches "
                    f"WHERE queue_id IN ({placeholders}) OR id IN ({placeholders}) "
                    "ORDER BY (wave_state='active') DESC,ordinal,id",
                    [*ids, *ids],
                ):
                    first_batches.setdefault(row["task_id"], row["id"])
                for item in items:
                    item.update(aggregates.get(item["id"], {}))
                    # Keep each queue once; its waves are available in the workbench.
                    item["batch_id"] = first_batches.get(item["id"])
        elif view == "batches":
            base = " FROM batches b LEFT JOIN queues q ON q.id=b.queue_id"
            where, params = "", []
            if search:
                where = """ WHERE (b.name ILIKE ? ESCAPE '\\' OR q.name ILIKE ? ESCAPE '\\'
                    OR EXISTS (SELECT 1 FROM files f WHERE f.batch_id=b.id AND
                    (f.name ILIKE ? ESCAPE '\\' OR f.relative_path ILIKE ? ESCAPE '\\')))"""
                params = [pattern] * 4
            if state != "all":
                where += (" AND " if where else " WHERE ") + (
                    "EXISTS (SELECT 1 FROM files f WHERE f.batch_id=b.id "
                    f"AND f.status IN ({','.join('?' for _ in GROUPS[state])}))"
                )
                params.extend(GROUPS[state])
            total = db.execute("SELECT COUNT(*)" + base + where, params).fetchone()[0]
            items = [
                dict(row)
                for row in db.execute(
                    "WITH selected AS (SELECT b.id,b.name,b.created_at,b.queue_id,b.ordinal,"
                    "b.wave_state,q.name AS queue_name"
                    + base
                    + where
                    + " ORDER BY b.created_at DESC,b.ordinal,b.id DESC LIMIT ? OFFSET ?) "
                    "SELECT s.*,s.id AS batch_id,COUNT(f.id) AS files,"
                    "SUM(f.status='imported') AS imported,SUM(f.status='review') AS review,"
                    f"SUM(f.status IN ({','.join('?' for _ in GROUPS['failed'])})) AS failed "
                    "FROM selected s LEFT JOIN files f ON f.batch_id=s.id GROUP BY s.id,s.name,s.created_at,s.queue_id,s.ordinal,s.wave_state,s.queue_name "
                    "ORDER BY s.created_at DESC,s.ordinal,s.id DESC",
                    [*params, limit, offset, *GROUPS["failed"]],
                )
            ]
        else:
            where, params = " WHERE 1=1", []
            if batch_id:
                where += " AND f.batch_id=?"
                params.append(batch_id)
            if state != "all":
                states = GROUPS[state]
                where += " AND f.status IN (" + ",".join("?" for _ in states) + ")"
                params.extend(states)
            if search:
                where += " AND (f.name ILIKE ? ESCAPE '\\' OR f.relative_path ILIKE ? ESCAPE '\\')"
                params.extend([pattern, pattern])
            total = db.execute("SELECT COUNT(*) FROM files f" + where, params).fetchone()[0]
            items = []
            for row in db.execute(
                "SELECT f.*,b.name AS batch_name,b.created_at AS batch_created_at,"
                "b.ordinal AS batch_ordinal,b.queue_id,q.name AS queue_name "
                "FROM files f JOIN batches b ON b.id=f.batch_id "
                "LEFT JOIN queues q ON q.id=b.queue_id"
                + where
                + " ORDER BY b.created_at DESC,b.ordinal,b.id DESC,f.created_at,f.id "
                "LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ):
                item = public_file(row)
                item.update(
                    {
                        key: row[key]
                        for key in (
                            "batch_name",
                            "batch_created_at",
                            "batch_ordinal",
                            "queue_id",
                            "queue_name",
                        )
                    }
                )
                items.append(item)
        add_timings(db, items, view, now)
        return {
            "as_of": now,
            "view": view,
            "items": items,
            "total": total,
            "offset": offset,
            "limit": limit,
            "summary": summary,
        }
