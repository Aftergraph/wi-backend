"""Task-queue stats stay exact under concurrency (characterization).

Honest label: the read-modify-write race on these counters could NOT be
observed under CPython's GIL at 64 threads x 500 ops, so this is not a
RED-proven regression test — it is a characterization pin: counters live
under the queue lock next to the state they count (free-threading and
refactor insurance), and this test fails if anyone moves them out again
in a way that loses updates at this volume.
"""

from __future__ import annotations

import threading
import time

from aftergraph_work_intelligence.tasks import TaskQueue


def _drained(queue: TaskQueue, total: int, timeout: float = 20.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        stats = queue.get_stats()
        if stats["completed"] + stats["failed"] >= total:
            return True
        time.sleep(0.05)
    return False


def _slow_ok():
    time.sleep(0.001)
    return "ok"


def test_stats_exact_under_concurrent_submits():
    queue = TaskQueue(max_workers=16)
    try:
        queue.register("slow", _slow_ok)
        threads, per_thread = 64, 500
        total = threads * per_thread

        def burst():
            for _ in range(per_thread):
                queue.submit("slow")

        workers = [threading.Thread(target=burst) for _ in range(threads)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=30)
        assert _drained(queue, total)
        stats = queue.get_stats()
        assert stats["submitted"] == total
        assert stats["completed"] == total
        assert stats["failed"] == 0
    finally:
        queue.shutdown()
