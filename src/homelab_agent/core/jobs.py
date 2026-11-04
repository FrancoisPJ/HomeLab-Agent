"""Persistent queue for heavy CPU jobs. Survives restarts (state lives in SQLite)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable

from homelab_agent.gateway.base import Notifier, Replier
from homelab_agent.llm.base import BackendUnavailable
from homelab_agent.store.db import Database, Job

log = logging.getLogger(__name__)

RunFn = Callable[[str, str, str, Replier], Awaitable[str]]


class JobQueue:
    def __init__(
        self,
        db: Database,
        run: RunFn,
        notifier: Notifier,
        *,
        max_concurrent: int = 1,
        poll_interval_s: float = 2.0,
        max_attempts: int = 2,
        retry_delay_s: float = 10.0,
    ) -> None:
        self.db = db
        self.run = run
        self.notifier = notifier
        self.max_concurrent = max_concurrent
        self.poll_interval_s = poll_interval_s
        self.max_attempts = max_attempts
        self.retry_delay_s = retry_delay_s
        self._wake = asyncio.Event()
        self._workers: list[asyncio.Task[None]] = []

    async def enqueue(self, chat_id: str, user_id: str, prompt: str) -> int:
        job_id = await self.db.enqueue_job(chat_id, user_id, prompt)
        self._wake.set()
        return job_id

    async def start(self) -> None:
        resumed = await self.db.requeue_running_jobs()
        if resumed:
            log.info("resumed %d interrupted job(s)", resumed)
        for i in range(self.max_concurrent):
            self._workers.append(asyncio.create_task(self._worker(), name=f"job-worker-{i}"))

    async def stop(self) -> None:
        for t in self._workers:
            t.cancel()
        for t in self._workers:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self._workers.clear()

    async def _worker(self) -> None:
        while True:
            job = await self.db.claim_next_job()
            if job is None:
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval_s)
                continue
            await self.process(job)

    async def process(self, job: Job) -> None:
        replier = self.notifier.replier_for(job.chat_id)
        try:
            text = await self.run(job.chat_id, job.user_id, job.prompt, replier)
        except asyncio.CancelledError:
            raise  # stays 'running' → re-queued at next start
        except BackendUnavailable as exc:
            if job.attempts < self.max_attempts:
                log.warning("job %s: backend unavailable, retrying later", job.id)
                await asyncio.sleep(self.retry_delay_s)
                await self.db.finish_job(job.id, status="queued", error=str(exc))
                self._wake.set()
                return
            await self.db.finish_job(job.id, status="failed", error=str(exc))
            await self.notifier.notify(job.chat_id, f"❌ Tâche #{job.id} abandonnée : {exc}")
            return
        except Exception as exc:
            log.exception("job %s failed", job.id)
            await self.db.finish_job(job.id, status="failed", error=repr(exc))
            await self.notifier.notify(job.chat_id, f"❌ Tâche #{job.id} en échec.")
            return
        await self.db.finish_job(job.id, status="done", result=text)
        await self.notifier.notify(job.chat_id, f"✅ Tâche #{job.id} terminée :\n\n{text}")
