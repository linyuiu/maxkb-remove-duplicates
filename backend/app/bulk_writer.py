"""PostgreSQL 18 COPY writer with transaction-tail fencing and batch ACK recovery."""

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import struct
import threading
import time

import psycopg
from psycopg.types.json import Json

from . import db
from .profiling import measure, count, timed
from .scan_diagnostics import BatchTiming

_active_writer = ContextVar("scan_writer", default=None)


def current_writer():
    return _active_writer.get()


@contextmanager
def writer_scope(run_id, token, control=None):
    existing = current_writer()
    if existing is not None:
        yield existing
    else:
        with BulkWriter(run_id, token, StopSignal(control)) as writer:
            yield writer


RESULT_BATCH_ROWS = 50_000
RESULT_BUFFER_BYTES = 128 * 1024**2
PROGRESS_INTERVAL = 5.0


class Stopped(Exception):
    pass


class Shutdown(Stopped):
    pass


class RejectedBatch(Exception):
    pass


class StopSignal:
    def __init__(self, control=None):
        self.control = control
        self.event = threading.Event()
        self.error = None
        self.lock = threading.Lock()
        self.connections = set()

    @property
    def connection(self):
        return self.control.connection if self.control else None

    def check(self):
        if self.control:
            self.control.check()
        if self.event.is_set():
            raise self.error

    def register(self, connection):
        with self.lock:
            self.check()
            self.connections.add(connection)
        if self.control:
            self.control.register(connection)

    def unregister(self, connection):
        with self.lock:
            self.connections.discard(connection)
        if self.control:
            self.control.unregister(connection)

    def stop(self, error):
        with self.lock:
            if self.event.is_set():
                return
            self.error = error
            self.event.set()
            connections = list(self.connections)
        for connection in connections:
            try:
                connection.cancel_safe(timeout=2)
            except Exception:
                pass


STATE_SQL = """SELECT token,state,cancel_requested,lease_until>clock_timestamp() AS live,
    batch_seq,batch_key,batch_delta FROM scan_runs WHERE id=%s"""


def check_state(row, token):
    if not row or row["token"] != token or row["state"] != "running" or not row["live"]:
        raise Stopped("执行租约已失效")
    if row["cancel_requested"]:
        raise Stopped("用户取消")


RESULT_MERGE = """
WITH applied AS (
 INSERT INTO similarity_pairs AS p
 (id,run_id,left_id,right_id,similarity,left_embedding_id,right_embedding_id,status)
 SELECT gen_random_uuid()::text,%s,left_id,right_id,similarity,left_embedding_id,right_embedding_id,'pending'
 FROM scan_result_stage WHERE true
 ORDER BY left_id COLLATE "C",right_id COLLATE "C"
 ON CONFLICT (run_id,left_id,right_id) DO UPDATE SET
 similarity=excluded.similarity,left_embedding_id=excluded.left_embedding_id,
 right_embedding_id=excluded.right_embedding_id
 WHERE excluded.similarity>p.similarity OR
   (excluded.similarity=p.similarity AND
    ROW(excluded.left_embedding_id COLLATE "C",excluded.right_embedding_id COLLATE "C")
      < ROW(p.left_embedding_id COLLATE "C",p.right_embedding_id COLLATE "C"))
 RETURNING OLD.id IS NULL AS inserted
)
SELECT count(*) FILTER (WHERE inserted) AS inserted,
       count(*) FILTER (WHERE NOT inserted) AS updated FROM applied
"""
SNAPSHOT_MERGE = """
WITH applied AS (
 INSERT INTO paragraph_snapshots(run_id,paragraph_id,data)
 SELECT %s,paragraph_id,data FROM scan_snapshot_stage
 ON CONFLICT (run_id,paragraph_id) DO NOTHING RETURNING paragraph_id
)
SELECT count(*) AS paragraphs,coalesce(sum(s.chunks),0)::bigint AS chunks
FROM applied a JOIN scan_snapshot_stage s USING(paragraph_id)
"""
GUARD_SQL = """
UPDATE scan_runs SET pair_count=pair_count+%s,total=total+%s,
 result_submitted=result_submitted+%s,result_improved=result_improved+%s,
 result_unchanged=result_unchanged+%s,batch_seq=%s,batch_key=%s,batch_delta=%s
WHERE id=%s AND token=%s AND state='running' AND NOT cancel_requested
  AND lease_until>clock_timestamp() AND batch_seq=%s
RETURNING batch_seq
"""


class BulkWriter:
    def __init__(self, run_id, token, signal=None):
        self.run_id, self.token = run_id, token
        self.signal = signal or StopSignal()
        self.conn = self.manager = None
        self.seq = None
        self.last_progress = 0.0
        self.last_metrics = 0.0

    def __enter__(self):
        try:
            self._open()
            state = self.conn.execute(STATE_SQL, (self.run_id,)).fetchone()
            check_state(state, self.token)
            self.seq = state["batch_seq"]
            self.context_token = _active_writer.set(self)
            return self
        except BaseException:
            self._close()
            raise

    def __exit__(self, *args):
        try:
            self._close()
        finally:
            _active_writer.reset(self.context_token)

    @timed("connection", "借出扫描专用写连接")
    def _open(self):
        self.manager = db.scan_connection(work_mem=True)
        self.conn = self.manager.__enter__()
        try:
            if self.conn.info.server_version < 180000:
                raise ValueError("COPY 结果写入需要 PostgreSQL 18 或更高版本")
            self.conn.execute("""CREATE TEMP TABLE scan_result_stage (
                left_id text,right_id text,similarity float8,
                left_embedding_id text,right_embedding_id text
            ) ON COMMIT DELETE ROWS""")
            self.conn.execute("""CREATE TEMP TABLE scan_snapshot_stage (
                paragraph_id text,data json,chunks bigint
            ) ON COMMIT DELETE ROWS""")
            self.signal.register(self.conn)
        except BaseException:
            self._close()
            raise

    def _close(self, broken=False):
        if self.conn:
            self.signal.unregister(self.conn)
            if not self.conn.closed:
                try:
                    self.conn.rollback()
                    if broken:
                        self.conn.close()
                    else:
                        self.conn.execute("DROP TABLE IF EXISTS pg_temp.scan_result_stage,pg_temp.scan_snapshot_stage")
                except Exception:
                    self.conn.close()
            self.manager.__exit__(None, None, None)
        self.conn = self.manager = None

    @timed("commit", "提交批次")
    def _commit(self):
        self.conn.execute("COMMIT")

    @timed("guard", "事务末尾校验及确认批次")
    def _guard(self, seq, key, delta):
        row = self.conn.execute(GUARD_SQL, (
            delta.get("inserted",0),delta.get("chunks",0),delta.get("rows",0) if delta["kind"]=="results" else 0,
            delta.get("updated",0),delta.get("unchanged",0),seq,key,Json(delta),self.run_id,self.token,seq-1,
        )).fetchone()
        if row is None:
            raise RejectedBatch()

    def _reconcile(self, seq, key):
        state = self.conn.execute(STATE_SQL, (self.run_id,)).fetchone()
        if state and state["batch_seq"] == seq:
            if state["batch_key"] != key:
                raise Stopped("批序号对应的内容不一致，已停止写入")
            return state["batch_delta"]
        check_state(state, self.token)
        if state["batch_seq"] != seq - 1:
            raise Stopped("批序号不连续，已停止写入")
        return None

    def _copy(self, kind, rows):
        if kind == "results":
            query = "COPY scan_result_stage FROM STDIN (FORMAT BINARY)"
            types = ["text","text","float8","text","text"]
            values = rows
        else:
            query = "COPY scan_snapshot_stage FROM STDIN (FORMAT BINARY)"
            types = ["text","json","int8"]
            values = ((paragraph_id,Json(data),chunks) for paragraph_id,data,chunks in rows)
        with measure("copy", "COPY 批次到临时表"):
            with self.conn.cursor() as cursor:
                with cursor.copy(query) as copy:
                    copy.set_types(types)
                    for i, row in enumerate(values):
                        if i % 256 == 0:
                            self.signal.check()
                        copy.write_row(row)

    def _apply(self, kind, rows, key):
        with BatchTiming(self.run_id,kind,self.seq+1,len(rows)) as batch:
            delta=self._apply_batch(kind,rows,key,batch)
            batch.confirm(delta)
            return delta

    def _apply_batch(self, kind, rows, key, batch):
        self.signal.check()
        seq = self.seq + 1
        for attempt in range(3):
            batch.value["attempts"]=attempt+1
            try:
                if self.conn is None:
                    self._open()
                    acknowledged = self._reconcile(seq,key)
                    if acknowledged is not None:
                        self.seq = seq
                        batch.value["recovered"]=True
                        return acknowledged
                with measure("begin", "开始批次事务"):
                    self.conn.execute("BEGIN")
                with batch.phase("copy"):
                    self._copy(kind,rows)
                with batch.phase("merge"), measure("merge", "服务端批量合并及计数"):
                    values = self.conn.execute(RESULT_MERGE if kind=="results" else SNAPSHOT_MERGE, (self.run_id,)).fetchone()
                delta = dict(values,kind=kind,rows=len(rows))
                if kind == "results":
                    delta["unchanged"] = len(rows)-delta["inserted"]-delta["updated"]
                self.signal.check()
                self._guard(seq,key,delta)
                with batch.phase("commit"):
                    self._commit()
                self.seq = seq
                return delta
            except RejectedBatch:
                self.conn.rollback()
                acknowledged = self._reconcile(seq,key)
                if acknowledged is not None:
                    self.seq = seq
                    batch.value["recovered"]=True
                    return acknowledged
                raise Stopped("批次条件已变化，已回滚本批") from None
            except (psycopg.OperationalError, psycopg.InterfaceError):
                self._close(broken=True)
                self.signal.check()
                count("reconnect_attempts")
                if attempt == 2:
                    raise
            except BaseException:
                if self.conn and not self.conn.closed:
                    with measure("rollback", "回滚批次"):
                        self.conn.rollback()
                self.signal.check()
                raise
        raise RuntimeError("无法确认批次提交结果")

    @timed("result_write", "结果 COPY 及持久化")
    def results(self, rows):
        # Freeze a unique, ordered batch before computing its identity.
        with measure("aggregate", "批内合并最高分"):
            pending = {}
            for row in rows:
                if isinstance(row,dict):
                    value=(row["left_id"],row["right_id"],float(row["similarity"]),row["left_embedding_id"],row["right_embedding_id"])
                else:
                    value=row
                left,right,score,le,re=value
                if left>right:
                    value=(right,left,score,re,le)
                old=pending.get(value[:2])
                if old is None or value[2]>old[2] or (value[2]==old[2] and value[3:]<old[3:]):
                    pending[value[:2]]=value
        if not pending:
            return
        with measure("sort", "按段落对排序"):
            frozen=sorted(pending.values(),key=lambda row:row[:2])
        with measure("digest", "计算冻结批次摘要"):
            digest=hashlib.sha256(b"results")
            for left,right,score,le,re in frozen:
                digest.update(left.encode()+b"\0"+right.encode()+b"\0"+struct.pack("!d",score)+le.encode()+b"\0"+re.encode()+b"\0")
        delta=self._apply("results",frozen,digest.hexdigest())
        count("result_batches")
        count("submitted_pairs",delta["rows"])
        count("inserted_pairs",delta["inserted"])
        count("improved_pairs",delta["updated"])
        count("unchanged_pairs",delta["unchanged"])
        return delta

    @timed("snapshot_write", "正文快照 COPY 及保存")
    def snapshots(self, rows):
        if not rows:
            return
        digest=hashlib.sha256(b"snapshots")
        for paragraph_id,data,chunks in rows:
            digest.update(paragraph_id.encode()+json.dumps(data,sort_keys=True,ensure_ascii=True).encode()+str(chunks).encode()+b"\0")
        delta=self._apply("snapshots",rows,digest.hexdigest())
        count("snapshot_chunks",delta["chunks"])
        count("snapshot_paragraphs",delta["paragraphs"])
        return delta

    def progress(self, completed, processed, force=False):
        current=time.monotonic()
        if not force and current-self.last_progress<PROGRESS_INTERVAL:
            return False
        self.signal.check()
        with measure("progress", "轻量发布比较进度"):
            row=self.conn.execute("""UPDATE scan_runs SET comparisons_done=greatest(comparisons_done,%s),
                processed=greatest(processed,%s),cursor=greatest(cursor,%s)
                WHERE id=%s AND token=%s AND state='running' AND NOT cancel_requested
                  AND lease_until>clock_timestamp() RETURNING id""",
                (completed,processed,processed,self.run_id,self.token)).fetchone()
            if row is None:
                state=self.conn.execute(STATE_SQL,(self.run_id,)).fetchone()
                check_state(state,self.token)
                raise Stopped("进度更新条件失效")
        self.last_progress=time.monotonic()
        return True


    def metrics(self, profile, force=False):
        if profile is None or (not force and time.monotonic()-self.last_metrics<10):
            return
        root = profile.parent or profile
        with measure("timing_publish", "单独保存计时统计"):
            self.conn.execute("""INSERT INTO scan_timings(run_id,data) VALUES (%s,%s)
                ON CONFLICT (run_id) DO UPDATE SET data=excluded.data""",(self.run_id,Json(root.snapshot())))
        self.last_metrics=time.monotonic()
        root.report()

    @timed("finalize", "校验结果及完成运行")
    def finish(self, completed, chunks):
        for attempt in range(3):
            try:
                if self.conn is None:
                    self._open()
                    state=self.conn.execute("SELECT state,batch_seq,comparisons_done FROM scan_runs WHERE id=%s AND token=%s",(self.run_id,self.token)).fetchone()
                    if state and state["state"]=="succeeded" and state["batch_seq"]==self.seq and state["comparisons_done"]==completed:
                        return
                self.signal.check()
                self.conn.execute("BEGIN")
                with measure("result_count", "核对最终结果数量"):
                    count_row=self.conn.execute("SELECT count(*) AS n FROM similarity_pairs WHERE run_id=%s",(self.run_id,)).fetchone()
                row=self.conn.execute("""UPDATE scan_runs SET state='succeeded',phase='done',finished_at=clock_timestamp(),
                    comparisons_done=%s,processed=total,cursor=total
                    WHERE id=%s AND token=%s AND state='running' AND NOT cancel_requested
                    AND lease_until>clock_timestamp() AND batch_seq=%s AND snapshot_ready
                    AND comparisons_total=%s AND total=%s AND pair_count=%s RETURNING id""",
                    (completed,self.run_id,self.token,self.seq,completed,chunks,count_row["n"])).fetchone()
                if row is None:
                    self.conn.rollback()
                    state=self.conn.execute(STATE_SQL,(self.run_id,)).fetchone()
                    check_state(state,self.token)
                    raise ValueError("运行完成校验不一致")
                self._commit()
                return
            except (psycopg.OperationalError,psycopg.InterfaceError):
                self._close(broken=True)
                self.signal.check()
                if attempt==2:
                    raise
            except BaseException:
                if self.conn and not self.conn.closed:
                    self.conn.rollback()
                raise
