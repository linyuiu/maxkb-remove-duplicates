"""Bounded per-batch wall times and database-wide index counter snapshots."""

from contextlib import contextmanager
import time

from psycopg.pq import TransactionStatus

from .profiling import current, measure


class BatchTiming:
    def __init__(self, run_id, kind, seq, rows):
        profile = current()
        self.profile = (profile.parent or profile) if profile else None
        self.clock = self.profile.clock if self.profile else time.perf_counter
        self.started = self.clock()
        self.value = dict(run_id=run_id,kind=kind,batch_seq=seq,rows=rows,
                          started_seconds=self.started-self.profile.started if self.profile else 0,
                          attempts=0,recovered=False,copy_seconds=0.0,merge_seconds=0.0,
                          commit_seconds=0.0,status="unconfirmed")

    def __enter__(self):
        return self

    @contextmanager
    def phase(self, name):
        started = self.clock()
        try:
            yield
        finally:
            self.value[name+"_seconds"] += self.clock()-started

    def confirm(self, delta):
        self.value["status"] = "committed"
        for key in ("inserted","updated","unchanged","chunks","paragraphs"):
            if key in delta:
                self.value[key] = delta[key]

    def __exit__(self, exc_type, exc, tb):
        finished = self.clock()
        self.value["seconds"] = finished-self.started
        self.value["finished_seconds"] = finished-self.profile.started if self.profile else self.value["seconds"]
        if exc_type:
            self.value["error_type"] = exc_type.__name__
        if self.profile and self.profile.run_id == self.value["run_id"]:
            self.profile.record_batch(self.value)


INDEX_SQL = """
SELECT statement_timestamp() AS captured_at,
       pg_postmaster_start_time() AS server_started,
       (SELECT stats_reset FROM pg_stat_database WHERE datname=current_database()) AS stats_reset,
       current_setting('block_size')::int AS block_size,
       current_setting('shared_buffers') AS shared_buffers,
       current_setting('work_mem') AS work_mem,
       current_setting('track_counts')::boolean AS track_counts,
       i.indrelid::bigint AS table_oid,i.indexrelid::bigint AS index_oid,
       s.indexrelname AS index_name,pg_relation_size(i.indexrelid) AS size_bytes,
       s.idx_blks_read,s.idx_blks_hit
FROM pg_index i JOIN pg_statio_user_indexes s ON s.indexrelid=i.indexrelid
WHERE i.indrelid='similarity_pairs'::regclass
ORDER BY s.indexrelname
"""


def index_delta(before, after):
    result = dict(scope="comparison_writes_before_final_count",includes_other_sessions=True,
                  read_includes_os_cache=True,indexes=[])
    if not before or before.get("status")!="ok" or after.get("status")!="ok":
        return dict(result,status="unavailable",reason="missing_snapshot")
    if before.get("track_counts") is False or after.get("track_counts") is False:
        return dict(result,status="unavailable",reason="track_counts_disabled")
    for key in ("server_started","stats_reset","table_oid","block_size"):
        if before[key]!=after[key]:
            return dict(result,status="invalid",reason=key+"_changed")
    result["own_stats_flushed"]=bool(before.get("flush_forced") and after.get("flush_forced"))
    old={row["index_oid"]:row for row in before["indexes"]}
    new={row["index_oid"]:row for row in after["indexes"]}
    for oid in sorted(old.keys() | new.keys()):
        left,right=old.get(oid),new.get(oid)
        row=dict(index_oid=oid,index_name=(right or left)["index_name"])
        if not left or not right:
            row.update(status="invalid",reason="index_added_or_removed")
        elif right["idx_blks_read"]<left["idx_blks_read"] or right["idx_blks_hit"]<left["idx_blks_hit"]:
            row.update(status="invalid",reason="counter_reset")
        else:
            reads=right["idx_blks_read"]-left["idx_blks_read"]
            hits=right["idx_blks_hit"]-left["idx_blks_hit"]
            row.update(status="ok",idx_blks_read=reads,idx_blks_hit=hits,
                       read_bytes=reads*after["block_size"],
                       size_before_bytes=left["size_bytes"],size_after_bytes=right["size_bytes"])
        result["indexes"].append(row)
    return dict(result,status="ok" if all(row["status"]=="ok" for row in result["indexes"]) else "partial")


def capture_index_stats(writer, phase):
    profile=current()
    if profile is None:
        return
    owner=profile.parent or profile
    snapshot=dict(status="unavailable",elapsed_seconds=owner.clock()-owner.started)
    conn=writer.conn
    try:
        if conn is None or conn.closed or conn.info.transaction_status!=TransactionStatus.IDLE:
            snapshot["reason"]="connection_unavailable"
        else:
            with measure("index_stats", "采集比较阶段索引计数"):
                # Flush this backend's pending counters at a transaction boundary.
                # Never reset shared statistics or wait for other sessions to flush.
                try:
                    with conn.transaction():
                        conn.execute("SELECT set_config('statement_timeout','2s',true),set_config('lock_timeout','500ms',true)")
                        conn.execute("SELECT pg_stat_force_next_flush()")
                    snapshot["flush_forced"]=True
                except Exception as exc:
                    snapshot.update(flush_forced=False,flush_error=type(exc).__name__)
                with conn.transaction():
                    conn.execute("SELECT set_config('statement_timeout','2s',true),set_config('lock_timeout','500ms',true)")
                    conn.execute("SELECT pg_stat_clear_snapshot()")
                    rows=conn.execute(INDEX_SQL).fetchall()
                if not rows:
                    snapshot["reason"]="no_indexes"
                else:
                    head=rows[0]
                    snapshot.update(status="ok",block_size=head["block_size"],table_oid=head["table_oid"],
                                    shared_buffers=head["shared_buffers"],work_mem=head["work_mem"],
                                    track_counts=head["track_counts"])
                    for key in ("captured_at","server_started","stats_reset"):
                        snapshot[key]=head[key].isoformat() if head[key] else None
                    snapshot["indexes"]=[{key:row[key] for key in
                        ("index_oid","index_name","size_bytes","idx_blks_read","idx_blks_hit")} for row in rows]
    except Exception as exc:
        snapshot.update(status="unavailable",reason=type(exc).__name__)
    owner.record_index_snapshot(phase,snapshot)
