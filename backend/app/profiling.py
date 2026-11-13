"""Bounded, per-run wall-clock spans. Parent totals include their children."""

from collections import deque
from copy import deepcopy
from contextlib import contextmanager, nullcontext
from contextvars import ContextVar
from functools import wraps
import json
import logging
import time
import threading

_active = ContextVar("scan_profile", default=None)
log = logging.getLogger("uvicorn.error")
PUBLISH_SECONDS = 10
BATCH_SERIES_LIMIT = 4096


class RunTiming:
    def __init__(self, run_id, clock=time.perf_counter):
        self.run_id, self.clock = run_id, clock
        self.started = clock()
        self.finished = None
        self.last_published = self.started
        self.records = {}
        self.stack = []
        self.counters = {}
        self.lock = threading.RLock()
        self.workers = []
        self.parent = None
        self.batch_head = []
        self.batch_tail = deque(maxlen=BATCH_SERIES_LIMIT // 2)
        self.batch_counts = {}
        self.index_io = {}

    @contextmanager
    def activate(self):
        token = _active.set(self)
        try:
            yield self
        finally:
            _active.reset(token)

    @contextmanager
    def measure(self, key, label):
        with self.lock:
            parent = self.stack[-1] if self.stack else None
            path = f"{parent['id']}/{key}" if parent else key
            record = self.records.setdefault(path, dict(
                id=path, key=key, parent_id=parent["id"] if parent else None,
                label=label, seconds=0.0, self_seconds=0.0, calls=0,
                max_seconds=0.0, errors=0,
            ))
            record["calls"] += 1
            frame = dict(id=path, start=self.clock(), child_seconds=0.0)
            self.stack.append(frame)
        try:
            yield
        except BaseException:
            with self.lock:
                record["errors"] += 1
            raise
        finally:
            with self.lock:
                elapsed = self.clock() - frame["start"]
                record["seconds"] += elapsed
                record["self_seconds"] += max(0.0, elapsed - frame["child_seconds"])
                record["max_seconds"] = max(record["max_seconds"], elapsed)
                self.stack.pop()
                if parent:
                    parent["child_seconds"] += elapsed

    def due(self):
        return self.clock() - self.last_published >= PUBLISH_SECONDS

    def worker(self):
        worker = RunTiming(self.run_id, self.clock)
        worker.parent = self
        with self.lock:
            self.workers.append(worker)
        return worker

    def record_batch(self, value):
        owner = self.parent or self
        if owner is not self:
            return owner.record_batch(value)
        with self.lock:
            value = dict(value)
            kind = value["kind"]
            self.batch_counts[kind] = self.batch_counts.get(kind,0)+1
            value["batch_no"] = self.batch_counts[kind]
            if len(self.batch_head)<BATCH_SERIES_LIMIT // 2:
                self.batch_head.append(value)
            else:
                self.batch_tail.append(value)
        # The bounded export keeps the first and latest batches; logs keep every batch.
        log.info("scan_batch_timing %s",json.dumps(value,ensure_ascii=False))

    def record_index_snapshot(self, phase, snapshot):
        owner = self.parent or self
        with owner.lock:
            owner.index_io[phase] = snapshot
            if phase == "after":
                from .scan_diagnostics import index_delta
                owner.index_io["delta"] = index_delta(owner.index_io.get("before"),snapshot)

    def snapshot(self):
        with self.lock:
            data = self._snapshot()
            workers = list(self.workers)
        if not workers:
            return data
        stages = []
        for label, prefix, item in [("计算及源库读取线程", "compute", data)] + [
            ("结果写入线程", f"writer{i}", worker.snapshot()) for i, worker in enumerate(workers)
        ]:
            stages.append(dict(id=prefix,key="thread",parent_id=None,label=label,
                               seconds=item["elapsed_seconds"],self_seconds=item["unmeasured_seconds"],
                               calls=1,max_seconds=item["elapsed_seconds"],errors=0))
            for row in item["stages"]:
                stages.append({**row,"id":prefix+"/"+row["id"],
                               "parent_id":prefix+"/"+row["parent_id"] if row["parent_id"] else prefix})
            if prefix != "compute":
                for key,value in item["counters"].items():
                    data["counters"][key] = data["counters"].get(key,0)+value
        data["stages"] = stages
        data["parallel"] = True
        return data

    def _snapshot(self):
        current = self.finished if self.finished is not None else self.clock()
        records = {key: dict(value) for key, value in self.records.items()}
        for i, frame in enumerate(self.stack):
            elapsed = current - frame["start"]
            active_child = current - self.stack[i + 1]["start"] if i + 1 < len(self.stack) else 0
            record = records[frame["id"]]
            record["seconds"] += elapsed
            record["self_seconds"] += max(0.0, elapsed - frame["child_seconds"] - active_child)
            record["max_seconds"] = max(record["max_seconds"], elapsed)
            record["active"] = True
        elapsed = max(0.0, current - self.started)
        measured = sum(r["seconds"] for r in records.values() if r["parent_id"] is None)
        return dict(
            version=1, run_id=self.run_id, final=self.finished is not None,
            elapsed_seconds=elapsed, unmeasured_seconds=max(0.0, elapsed - measured),
            counters=dict(self.counters), stages=list(records.values()),
            batch_series=[dict(row) for row in self.batch_head]+[dict(row) for row in self.batch_tail],
            batch_series_total=sum(self.batch_counts.values()),
            batch_series_omitted=max(0,sum(self.batch_counts.values())-BATCH_SERIES_LIMIT),
            index_io=deepcopy(self.index_io),
        )

    def finish(self):
        with self.lock:
            self.finished = self.clock()

    def report(self):
        data = self.snapshot()
        log.info("scan_timing %s", json.dumps(dict(
            run_id=self.run_id, final=data["final"], elapsed_seconds=round(data["elapsed_seconds"], 3),
            counters=data["counters"],
            stages={row["id"]: dict(seconds=round(row["seconds"], 3),
                                   self_seconds=round(row["self_seconds"], 3), calls=row["calls"])
                    for row in data["stages"]},
        ), ensure_ascii=False))


def current():
    return _active.get()


def measure(key, label):
    profile = current()
    return profile.measure(key, label) if profile else nullcontext()


def timed(key, label):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            if current() is None:
                return function(*args, **kwargs)
            with measure(key, label):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def count(key, amount=1):
    if profile := current():
        with profile.lock:
            profile.counters[key] = profile.counters.get(key, 0) + amount


def publish(run, force=False):
    profile = current()
    if profile and (force or profile.due()):
        from sqlalchemy.orm import object_session
        from sqlalchemy.dialects.postgresql import insert
        from .models import ScanTiming
        session = object_session(run)
        statement = insert(ScanTiming).values(run_id=run.id,data=profile.snapshot())
        session.execute(statement.on_conflict_do_update(index_elements=["run_id"],set_={"data":statement.excluded.data}))
        profile.last_published = profile.clock()
        profile.report()


def timed_rows(iterator, key, label):
    try:
        while True:
            with measure(key, label):
                try:
                    row = next(iterator)
                except StopIteration:
                    return
            yield row
            del row
    finally:
        with measure(key, label):
            iterator.close()
