"""Bounded producer/writer pipeline; one frozen, unacknowledged batch at a time."""

from contextlib import nullcontext
import queue
import sys
import threading

from .bulk_writer import RESULT_BATCH_ROWS, RESULT_BUFFER_BYTES
from .profiling import current, measure, timed

_END = object()


def row_bytes(row):
    # Deliberately count shared strings again: this is a conservative object
    # budget, with headroom for dictionary keys, sorting and COPY's small buffer.
    return sys.getsizeof(row)+sum(sys.getsizeof(value) for value in row)+256


class ResultPipeline:
    def __init__(self, writer, batch_rows=RESULT_BATCH_ROWS, buffer_bytes=RESULT_BUFFER_BYTES):
        if batch_rows<1 or buffer_bytes<1:
            raise ValueError("结果批次及缓冲预算必须大于零")
        self.writer=writer
        self.batch_rows=batch_rows
        self.limit=buffer_bytes
        self.pending={}
        self.pending_bytes=0
        self.used=0
        self.condition=threading.Condition()
        self.queue=queue.Queue(maxsize=1)
        self.completed=self.processed=0
        profile=current()
        self.profile=profile.worker() if profile else None
        self.thread=threading.Thread(target=self._write,name="scan-result-writer",daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def _check(self):
        self.writer.signal.check()

    def _put(self, value):
        with measure("queue_wait", "等待写队列空位"):
            while True:
                self._check()
                try:
                    self.queue.put(value,timeout=0.1)
                    return
                except queue.Full:
                    pass

    def _flush(self):
        if not self.pending:
            return
        # Transfer the dictionary itself, rather than duplicating its row data.
        pending,size=self.pending,self.pending_bytes
        self.pending={}
        self.pending_bytes=0
        self._put((pending,size))

    @timed("aggregate", "跨矩阵块批内合并")
    def submit(self, rows):
        for row in rows:
            self._check()
            value=(row["left_id"],row["right_id"],float(row["similarity"]),row["left_embedding_id"],row["right_embedding_id"])
            if value[0]>value[1]:
                value=(value[1],value[0],value[2],value[4],value[3])
            key=value[:2]
            old=self.pending.get(key)
            if old is not None and (old[2]>value[2] or (old[2]==value[2] and old[3:]<=value[3:])):
                continue
            size=row_bytes(value)
            old_size=row_bytes(old) if old else 0
            extra=size-old_size
            if size>self.limit:
                raise ValueError("单条结果超过写入缓冲预算")
            with self.condition:
                enough=self.used+extra<=self.limit
            if not enough:
                self._flush()
                key=value[:2]
                old_size=0
                extra=size
                with measure("buffer_wait", "等待结果缓冲预算"):
                    with self.condition:
                        while self.used+extra>self.limit:
                            self._check()
                            self.condition.wait(0.1)
            with self.condition:
                self.used+=extra
            self.pending[key]=value
            self.pending_bytes+=extra
            if len(self.pending)>=self.batch_rows:
                self._flush()

    def checkpoint(self, completed, processed):
        self._check()
        with self.condition:
            self.completed,self.processed=completed,processed

    def _write(self):
        context=self.profile.activate() if self.profile else nullcontext()
        try:
            with context:
                while True:
                    self._check()
                    with measure("queue_wait", "等待计算结果"):
                        try:
                            item=self.queue.get(timeout=0.2)
                        except queue.Empty:
                            item=None
                    if item is _END:
                        with self.condition:
                            completed,processed=self.completed,self.processed
                        self.writer.progress(completed,processed,force=True)
                        break
                    if item is not None:
                        pending,size=item
                        try:
                            self.writer.results(pending.values())
                        finally:
                            del pending,item
                            with self.condition:
                                self.used-=size
                                self.condition.notify_all()
                    with self.condition:
                        completed,processed=self.completed,self.processed
                    self.writer.progress(completed,processed)
                    self.writer.metrics(self.profile)
        except BaseException as exc:
            self.writer.signal.stop(exc)
        finally:
            if self.profile:
                self.profile.finish()
            # Release queued buffers on cancellation/failure before temp cleanup.
            with self.condition:
                self.condition.notify_all()
            while True:
                try:
                    self.queue.get_nowait()
                except queue.Empty:
                    break

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc is None:
                self._flush()
                self._put(_END)
            else:
                self.writer.signal.stop(exc)
        finally:
            while self.thread.is_alive():
                self.thread.join(timeout=0.2)
            self.pending.clear()
            with self.condition:
                self.pending_bytes=self.used=0
        if exc is None:
            self._check()
