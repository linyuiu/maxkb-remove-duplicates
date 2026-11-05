"""Scanning and scheduling live in threads owned by the API lifespan."""

import logging
import threading
import time
from . import scanning, jobs

log = logging.getLogger(__name__)


class BackgroundRuntime:
    def __init__(self):
        self.stopped = threading.Event()
        self.control = scanning.ScanControl()
        self.lock = threading.Lock()
        self.active_job = None
        self.threads = []

    def start(self):
        if self.threads:
            raise RuntimeError("后台执行器已经启动")
        for name, target in [
            ("scan-queue", self.scan_loop),
            ("scan-schedule", self.schedule_loop),
        ]:
            thread = threading.Thread(name=name, target=target, daemon=True)
            self.threads.append(thread)
            thread.start()

    def scan_loop(self):
        while not self.stopped.is_set():
            try:
                job = scanning.claim()
                if job:
                    with self.lock:
                        self.active_job = job
                    try:
                        if not self.stopped.is_set():
                            scanning.execute(*job, control=self.control)
                    finally:
                        try:
                            if self.stopped.is_set():
                                scanning.release_for_restart(*job)
                        finally:
                            with self.lock:
                                self.active_job = None
                else:
                    self.stopped.wait(2)
            except Exception:
                log.exception("后台扫描循环异常")
                self.stopped.wait(5)

    def schedule_loop(self):
        while not self.stopped.is_set():
            try:
                jobs.tick()
            except Exception:
                log.exception("后台调度循环异常")
            self.stopped.wait(10)

    def stop(self):
        self.stopped.set()
        self.control.request_stop()
        # Release the lease immediately even if an I/O operation is still unwinding.
        with self.lock:
            job = self.active_job
        if job:
            try:
                scanning.release_for_restart(*job)
            except Exception:
                log.exception("关闭时释放扫描租约失败，租约到期后恢复")
        deadline = time.monotonic() + 10
        for thread in self.threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
        if any(t.is_alive() for t in self.threads):
            log.warning("后台线程仍在结束 I/O，持久化任务将在租约到期后重新领取")
