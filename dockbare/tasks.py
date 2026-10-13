"""Background tasks with progress and per-task log."""
from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

log = logging.getLogger("dockbare.tasks")


class Task:
    def __init__(self, kind: str, title: str, *, volume: str | None = None, meta: dict | None = None):
        self.id = uuid.uuid4().hex[:16]
        self.kind = kind
        self.title = title
        self.volume = volume
        self.meta = meta or {}
        self.status = "queued"  # queued | running | success | failure | cancelled
        self.created = time.time()
        self.started: float | None = None
        self.finished: float | None = None
        self.progress: dict[str, Any] = {}
        self.log: list[str] = []
        self.result: Any = None
        self.error: str | None = None
        self.cancel_requested = False
        self._lock = threading.Lock()

    def add_log(self, line: str) -> None:
        with self._lock:
            self.log.append(f"[{time.strftime('%H:%M:%S')}] {line}")
            if len(self.log) > 500:
                self.log = self.log[-500:]

    def set_progress(self, **kw) -> None:
        with self._lock:
            self.progress.update(kw)

    def check_cancel(self) -> None:
        if self.cancel_requested:
            raise TaskCancelled()

    def to_dict(self, include_log: bool = False) -> dict:
        with self._lock:
            d = {
                "id": self.id, "kind": self.kind, "title": self.title, "volume": self.volume,
                "status": self.status, "created": self.created, "started": self.started,
                "finished": self.finished, "progress": dict(self.progress), "error": self.error,
                "result": self.result if isinstance(self.result, (dict, list, str, int, float, bool, type(None))) else str(self.result),
                "meta": self.meta,
            }
            if include_log:
                d["log"] = list(self.log)
            return d


class TaskCancelled(Exception):
    pass


class TaskManager:
    def __init__(self, workers: int = 4, keep_seconds: int = 3600):
        self._tasks: dict[str, Task] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dockbare-task")
        self._keep = keep_seconds

    def submit(self, kind: str, title: str, fn: Callable[[Task], Any], *, volume: str | None = None,
               meta: dict | None = None, on_done: Callable[[Task], None] | None = None) -> Task:
        task = Task(kind, title, volume=volume, meta=meta)
        with self._lock:
            self._tasks[task.id] = task
        self._cleanup()

        def run() -> None:
            task.status = "running"
            task.started = time.time()
            try:
                task.result = fn(task)
                task.status = "success"
            except TaskCancelled:
                task.status = "cancelled"
                task.error = "Cancelled"
            except Exception as e:  # noqa: BLE001
                task.status = "failure"
                task.error = str(e) or e.__class__.__name__
                task.add_log(f"ERROR: {task.error}")
                log.error("Task %s (%s) failed: %s\n%s", task.id, task.title, e, traceback.format_exc())
            finally:
                task.finished = time.time()
                if on_done:
                    try:
                        on_done(task)
                    except Exception:  # noqa: BLE001
                        log.exception("on_done handler failed for task %s", task.id)

        self._pool.submit(run)
        return task

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self, active_only: bool = False) -> list[Task]:
        with self._lock:
            tasks = list(self._tasks.values())
        if active_only:
            tasks = [t for t in tasks if t.status in ("queued", "running")]
        return sorted(tasks, key=lambda t: t.created, reverse=True)

    def active_for_volume(self, volume: str) -> list[Task]:
        return [t for t in self.list(active_only=True) if t.volume == volume]

    def cancel(self, task_id: str) -> bool:
        t = self.get(task_id)
        if not t or t.status not in ("queued", "running"):
            return False
        t.cancel_requested = True
        return True

    def _cleanup(self) -> None:
        cutoff = time.time() - self._keep
        with self._lock:
            for tid in [k for k, t in self._tasks.items() if t.finished and t.finished < cutoff]:
                del self._tasks[tid]

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
