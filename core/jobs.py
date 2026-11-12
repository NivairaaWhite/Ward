"""
jobs.py
-------
A deliberately simple background job runner: a small thread pool plus an
in-memory status dict. This is what lets Ward's upload endpoint return
immediately with a job_id instead of blocking the request while an image
gets processed -- the same problem Celery+Redis solves, at a scale where
you don't need Celery+Redis yet.

WORKER COUNT: scales with the machine's own CPU count rather than a fixed
number. This is a real (if modest) way to use more of a multi-core
machine for batch uploads: watermark.py/cloak.py's hot paths are numpy/
scipy calls (DCT, matrix multiplies), and those release Python's GIL
while the actual C-level math runs -- so several images really can crunch
on separate cores at once via ordinary threads here, no multiprocessing
needed. Capped at 4: past that, per-image memory (a large image holds
several full-resolution float64 arrays at once) starts to matter more
than added concurrency, and this hasn't been measured on a real many-core
machine to justify going higher by default -- raise MAX_WORKERS if you have measured a benefit on your own hardware.

Swap-out note: if you outgrow a single process (multiple Ward instances,
need job status to survive a restart, want retries/scheduling), this
module is the one piece designed to be replaced by Celery -- submit()'s
signature is intentionally close to `task.delay()`, and get() to
`AsyncResult`.
"""
from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from typing import Callable

MAX_WORKERS = max(2, min(os.cpu_count() or 2, 4))
_executor = ThreadPoolExecutor(max_workers=MAX_WORKERS, thread_name_prefix="ward-worker")
_jobs: dict[str, dict] = {}
_lock = Lock()


def submit(fn: Callable, *args, **kwargs) -> str:
    """Run fn(job_id, *args, **kwargs) in the background. fn should call
    set_message(job_id, ...) to report progress and return a JSON-able
    result dict on success (or raise on failure)."""
    job_id = uuid.uuid4().hex
    with _lock:
        _jobs[job_id] = {"state": "queued", "message": "Queued", "result": None, "error": None}

    def runner():
        with _lock:
            _jobs[job_id]["state"] = "processing"
        try:
            result = fn(job_id, *args, **kwargs)
            with _lock:
                _jobs[job_id]["state"] = "done"
                _jobs[job_id]["result"] = result
        except Exception as e:  # noqa: BLE001 - deliberately broad: report to the client
            with _lock:
                _jobs[job_id]["state"] = "failed"
                _jobs[job_id]["error"] = str(e)

    _executor.submit(runner)
    return job_id


def set_message(job_id: str, message: str) -> None:
    with _lock:
        if job_id in _jobs:
            _jobs[job_id]["message"] = message


def get(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return dict(job) if job is not None else None
