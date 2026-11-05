import threading
import pytest
from app import runtime
from app.scanning import ScanControl, Shutdown


def test_api_background_threads_run_queue_and_schedule_then_stop(monkeypatch):
    claimed = threading.Event()
    scheduled = threading.Event()
    entered = threading.Event()
    released = []

    def claim():
        if not claimed.is_set():
            claimed.set()
            return ("run", "token")
        return None

    def execute(run_id, token, control=None):
        assert (run_id, token) == ("run", "token")
        entered.set()
        assert control.stopped.wait(3)

    monkeypatch.setattr(runtime.scanning, "claim", claim)
    monkeypatch.setattr(runtime.scanning, "execute", execute)
    monkeypatch.setattr(
        runtime.scanning, "release_for_restart", lambda *job: released.append(job)
    )
    monkeypatch.setattr(runtime.jobs, "tick", scheduled.set)
    runner = runtime.BackgroundRuntime()
    runner.start()
    try:
        assert entered.wait(2) and scheduled.wait(2)
        assert len(runner.threads) == 2
    finally:
        runner.stop()
    assert all(not t.is_alive() for t in runner.threads)
    assert ("run", "token") in released


def test_shutdown_cancels_source_query_and_prevents_reuse():
    calls = []

    class Connection:
        def cancel_safe(self, timeout):
            calls.append(timeout)

    control = ScanControl()
    control.attach(Connection())
    control.request_stop()
    assert calls == [2]
    with pytest.raises(Shutdown):
        control.check()
    control.detach()
    with pytest.raises(Shutdown):
        control.attach(Connection())
