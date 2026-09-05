"""Simple in-memory task queue for background processing."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger("aftergraph.work-intelligence.tasks")


class TaskStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class Task:
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    status: TaskStatus = TaskStatus.PENDING
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    result: Any = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    completed_at: float | None = None
    retries: int = 0
    max_retries: int = 3


class TaskQueue:
    """Simple thread-safe task queue with worker pool."""

    def __init__(self, max_workers: int = 4):
        self.max_workers = max_workers
        self._tasks: dict[str, Task] = {}
        self._queue: list[str] = []
        self._functions: dict[str, Callable] = {}
        self._lock = threading.Lock()
        self._shutdown = False
        self._workers: list[threading.Thread] = []
        self._stats: dict[str, int] = defaultdict(int)
        self._start_workers()

    def _start_workers(self) -> None:
        for i in range(self.max_workers):
            t = threading.Thread(target=self._worker_loop, daemon=True, name=f"task-worker-{i}")
            t.start()
            self._workers.append(t)

    def _worker_loop(self) -> None:
        while not self._shutdown:
            task_id = None
            with self._lock:
                if self._queue:
                    task_id = self._queue.pop(0)
            if task_id:
                self._execute_task(task_id)
            else:
                time.sleep(0.1)

    def _execute_task(self, task_id: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task or task.status != TaskStatus.PENDING:
                return
            task.status = TaskStatus.RUNNING
            task.started_at = time.time()

        func = self._functions.get(task.name)
        if not func:
            with self._lock:
                task.status = TaskStatus.FAILED
                task.error = f"No function registered for '{task.name}'"
                task.completed_at = time.time()
            self._stats["failed"] += 1
            return

        try:
            result = func(*task.args, **task.kwargs)
            with self._lock:
                task.status = TaskStatus.COMPLETED
                task.result = result
                task.completed_at = time.time()
            self._stats["completed"] += 1
            logger.info(f"Task {task.name} ({task.id[:8]}) completed")
        except Exception as e:
            with self._lock:
                if task.retries < task.max_retries:
                    task.retries += 1
                    task.status = TaskStatus.PENDING
                    task.started_at = None
                    self._queue.append(task_id)
                    logger.warning(f"Task {task.name} ({task.id[:8]}) failed, retry {task.retries}/{task.max_retries}")
                else:
                    task.status = TaskStatus.FAILED
                    task.error = str(e)
                    task.completed_at = time.time()
                    self._stats["failed"] += 1
                    logger.error(f"Task {task.name} ({task.id[:8]}) failed permanently: {e}")

    def register(self, name: str, func: Callable) -> None:
        """Register a function that can be executed as a task."""
        self._functions[name] = func

    def submit(self, name: str, *args: Any, **kwargs: Any) -> Task:
        """Submit a task for background execution."""
        task = Task(name=name, args=args, kwargs=kwargs)
        with self._lock:
            self._tasks[task.id] = task
            self._queue.append(task.id)
        self._stats["submitted"] += 1
        logger.info(f"Task {name} ({task.id[:8]}) submitted")
        return task

    def get_task(self, task_id: str) -> Task | None:
        """Get task status."""
        return self._tasks.get(task_id)

    def list_tasks(self, status: TaskStatus | None = None) -> list[Task]:
        """List tasks, optionally filtered by status."""
        with self._lock:
            tasks = list(self._tasks.values())
        if status:
            tasks = [t for t in tasks if t.status == status]
        return tasks

    def get_stats(self) -> dict:
        """Get queue statistics."""
        with self._lock:
            pending = sum(1 for t in self._tasks.values() if t.status == TaskStatus.PENDING)
            running = sum(1 for t in self._tasks.values() if t.status == TaskStatus.RUNNING)
            return {
                "submitted": self._stats["submitted"],
                "completed": self._stats["completed"],
                "failed": self._stats["failed"],
                "pending": pending,
                "running": running,
                "workers": self.max_workers,
            }

    def shutdown(self) -> None:
        """Gracefully shutdown workers."""
        self._shutdown = True
        for t in self._workers:
            t.join(timeout=5)


def create_task_queue() -> TaskQueue:
    """Create and configure the task queue with built-in tasks."""
    queue = TaskQueue(max_workers=4)

    def process_observation_async(observation_id: str, tenant_id: str) -> str:
        """Process an observation asynchronously (e.g., enrichment, dedup)."""
        return f"processed:{observation_id}"

    def publish_to_destination_async(work_item_id: str, destination: str) -> str:
        """Publish a work item to a destination asynchronously."""
        return f"published:{work_item_id}:{destination}"

    def cleanup_old_data_async(days: int = 30) -> str:
        """Clean up old data from the database."""
        return f"cleaned:{days}"

    queue.register("process_observation", process_observation_async)
    queue.register("publish_destination", publish_to_destination_async)
    queue.register("cleanup_old_data", cleanup_old_data_async)

    return queue
