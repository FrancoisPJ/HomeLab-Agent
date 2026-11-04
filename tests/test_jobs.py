from __future__ import annotations

from pathlib import Path

from homelab_agent.core.jobs import JobQueue
from homelab_agent.gateway.base import Replier
from homelab_agent.llm.base import BackendUnavailable
from homelab_agent.store.db import Database

from .conftest import wait_for
from .harness import RecordingNotifier


async def test_job_survives_restart(tmp_path: Path) -> None:
    path = str(tmp_path / "jobs.db")
    db = Database(path)
    await db.init()
    job_id = await db.enqueue_job("c1", "u1", "long task")
    claimed = await db.claim_next_job()
    assert claimed is not None and claimed.id == job_id and claimed.status == "running"
    await db.close()  # simulated crash while running

    db2 = Database(path)
    await db2.init()
    done: list[str] = []

    async def run(chat: str, user: str, prompt: str, replier: Replier) -> str:
        done.append(prompt)
        return "résultat"

    notifier = RecordingNotifier()
    q = JobQueue(db2, run, notifier, poll_interval_s=0.05)
    await q.start()
    await wait_for(lambda: bool(notifier.sent))
    assert done == ["long task"]
    job = await db2.get_job(job_id)
    assert (
        job is not None and job.status == "done" and job.result == "résultat" and job.attempts == 2
    )
    assert "Tâche #1 terminée" in notifier.sent[0][1]
    await q.stop()
    await db2.close()


async def test_only_one_job_at_a_time(db: Database) -> None:
    import asyncio

    running = 0
    peak = 0

    async def run(chat: str, user: str, prompt: str, replier: Replier) -> str:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return prompt

    notifier = RecordingNotifier()
    q = JobQueue(db, run, notifier, max_concurrent=1, poll_interval_s=0.02)
    await q.start()
    for i in range(3):
        await q.enqueue("c", "u", f"p{i}")
    await wait_for(lambda: len(notifier.sent) == 3)
    assert peak == 1
    await q.stop()


async def test_retry_then_fail(db: Database) -> None:
    calls = 0

    async def run(chat: str, user: str, prompt: str, replier: Replier) -> str:
        nonlocal calls
        calls += 1
        raise BackendUnavailable("cpu down")

    notifier = RecordingNotifier()
    q = JobQueue(db, run, notifier, max_attempts=2, poll_interval_s=0.02, retry_delay_s=0.01)
    await q.start()
    await q.enqueue("c", "u", "p")
    await wait_for(lambda: bool(notifier.sent))
    assert calls == 2
    assert "abandonnée" in notifier.sent[0][1]
    assert (await db.list_jobs())[0].status == "failed"
    await q.stop()


async def test_unexpected_error_marks_failed(db: Database) -> None:
    async def run(chat: str, user: str, prompt: str, replier: Replier) -> str:
        raise RuntimeError("boom")

    notifier = RecordingNotifier()
    q = JobQueue(db, run, notifier, poll_interval_s=0.02)
    await q.start()
    await q.enqueue("c", "u", "p")
    await wait_for(lambda: bool(notifier.sent))
    assert "échec" in notifier.sent[0][1]
    await q.stop()
