"""Background jobs for work that cannot finish inside one tool call.

Copilot Studio gives a tool call 120 seconds. Scanning hundreds of Dataverse
environments takes longer than that, so the tool starts a job, waits a little,
and hands back a job id; the agent polls `job_status` until it is done.

Jobs live in memory on the instance that started them. That is fine for the
single-instance App Service the template deploys; if you scale out, a poll
can land on another instance and report "unknown job". Jobs are visible only
to the user who started them and are forgotten after JOB_RETENTION_HOURS.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..auth.entra import Principal, set_principal
from ..config import settings
from . import budget

log = logging.getLogger("megamcp.jobs")

TERMINAL = ("succeeded", "failed", "cancelled")


@dataclass
class Job:
    id: str
    owner: str
    kind: str
    title: str
    status: str = "queued"
    created: float = field(default_factory=time.time)
    started: float = 0.0
    finished: float = 0.0
    progress: dict[str, Any] = field(default_factory=lambda: {"done": 0, "total": 0})
    result: dict[str, Any] | None = None
    error: str = ""
    cancel_requested: bool = False
    task: asyncio.Task | None = None

    @property
    def done(self) -> bool:
        return self.status in TERMINAL

    def summary(self) -> dict[str, Any]:
        now = time.time()
        elapsed = (self.finished or now) - (self.started or self.created)
        p = dict(self.progress)
        if p.get("total"):
            p["percent"] = round(100 * p.get("done", 0) / p["total"], 1)
        return {"job_id": self.id, "kind": self.kind, "title": self.title, "status": self.status, "progress": p,
                "elapsed_seconds": round(elapsed, 1), "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.created)),
                **({"error": self.error} if self.error else {})}


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}

    def _prune(self) -> None:
        cutoff = time.time() - settings.job_retention_hours * 3600
        for jid in [j.id for j in self._jobs.values() if j.done and j.finished and j.finished < cutoff]:
            self._jobs.pop(jid, None)

    def running_for(self, owner: str) -> list[Job]:
        return [j for j in self._jobs.values() if j.owner == owner and not j.done]

    def start(self, principal: Principal, kind: str, title: str, work: Callable[[Job], Awaitable[dict[str, Any]]], total: int = 0) -> Job:
        """Start `work(job)` in the background as `principal`. Raises RuntimeError when the user already runs too many jobs."""
        self._prune()
        owner = principal.cache_key
        if len(self.running_for(owner)) >= settings.job_max_running_per_user:
            raise RuntimeError(f"You already have {settings.job_max_running_per_user} jobs running. Wait for one to finish (job_status) or cancel one.")
        job = Job(id=uuid.uuid4().hex[:12], owner=owner, kind=kind, title=title)
        job.progress["total"] = total

        async def runner() -> None:
            # The task inherits the request's context; give it its own identity and a longer time budget.
            set_principal(principal)
            budget.start(settings.job_time_budget_seconds)
            job.status, job.started = "running", time.time()
            try:
                job.result = await work(job)
                job.status = "cancelled" if job.cancel_requested else "succeeded"
            except asyncio.CancelledError:
                job.status = "cancelled"
            except Exception as e:  # noqa: BLE001
                log.exception("job %s failed", job.id)
                job.status, job.error = "failed", f"{type(e).__name__}: {str(e)[:500]}"
            finally:
                job.finished = time.time()

        def finalize(t: asyncio.Task) -> None:
            # A task cancelled before it ever ran never reaches runner's finally block.
            if not job.done:
                job.status = "cancelled" if t.cancelled() or job.cancel_requested else "failed"
                job.finished = job.finished or time.time()

        job.task = asyncio.get_running_loop().create_task(runner(), name=f"megamcp-job-{job.id}")
        job.task.add_done_callback(finalize)
        self._jobs[job.id] = job
        return job

    def get(self, job_id: str, owner: str) -> Job | None:
        j = self._jobs.get((job_id or "").strip())
        return j if j and j.owner == owner else None

    def list_for(self, owner: str) -> list[Job]:
        self._prune()
        return sorted((j for j in self._jobs.values() if j.owner == owner), key=lambda j: -j.created)

    def cancel(self, job: Job) -> None:
        job.cancel_requested = True
        if job.task and not job.task.done():
            job.task.cancel()

    async def wait(self, job: Job, seconds: float) -> bool:
        """Wait up to `seconds` for the job; True when it finished."""
        if job.done or not job.task:
            return job.done
        try:
            await asyncio.wait_for(asyncio.shield(job.task), timeout=max(0.0, seconds))
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        return job.done


jobs = JobStore()
