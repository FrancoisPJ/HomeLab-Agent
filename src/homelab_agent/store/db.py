"""SQLite persistence (SQLAlchemy 2 async): history, interactions, jobs, spend, audit."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, ClassVar

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    delete,
    func,
    insert,
    inspect,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    type_annotation_map: ClassVar[dict[Any, Any]] = {dict[str, Any]: JSON}


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    #: actions really executed in this assistant turn: [{name, arguments, result}]
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Interaction(Base):
    __tablename__ = "interactions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(64), index=True)
    tier: Mapped[str] = mapped_column(String(16))
    backend: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(128))
    latency_ms: Mapped[int] = mapped_column(Integer)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)
    cost_eur: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[str] = mapped_column(String(64))
    prompt: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), index=True, default="queued")
    result: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class Spend(Base):
    __tablename__ = "spend"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), index=True)
    tokens_in: Mapped[int] = mapped_column(Integer)
    tokens_out: Mapped[int] = mapped_column(Integer)
    cost_eur: Mapped[float] = mapped_column(Float)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class BudgetAlert(Base):
    __tablename__ = "budget_alerts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Memory(Base):
    __tablename__ = "memories"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(64), index=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AlertState(Base):
    """Last notification time of an (alert, entity) pair: cooldowns survive restarts."""

    __tablename__ = "alert_state"
    key: Mapped[str] = mapped_column(String(256), primary_key=True)
    last_sent: Mapped[datetime] = mapped_column(DateTime)


class AuditEntry(Base):
    __tablename__ = "audit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    requester: Mapped[str] = mapped_column(String(64))
    tier: Mapped[str] = mapped_column(String(16))
    model: Mapped[str] = mapped_column(String(128))
    tool: Mapped[str] = mapped_column(String(64))
    params: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(String(32))
    detail: Mapped[str] = mapped_column(Text, default="")


class SchemaVersion(Base):
    __tablename__ = "schema_version"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)


# Simple forward-only migrations: index i upgrades the schema to version i + 1.
# Version 1 is the initial ``create_all``; append callables for later changes.
Migration = Callable[[Any], None]


def _chat_tool_calls(conn: Any) -> None:
    """v2: actions stored next to the assistant text (replayed as real tool calls)."""
    columns = {c["name"] for c in inspect(conn).get_columns("chat_messages")}
    if "tool_calls" not in columns:
        conn.execute(text("ALTER TABLE chat_messages ADD COLUMN tool_calls JSON"))


MIGRATIONS: list[Migration] = [_chat_tool_calls]
SCHEMA_VERSION = 1 + len(MIGRATIONS)


class Database:
    def __init__(self, url_or_path: str) -> None:
        if "://" in url_or_path:
            url = url_or_path
        else:
            if url_or_path != ":memory:":
                Path(url_or_path).parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite+aiosqlite:///{url_or_path}"
        self.engine: AsyncEngine = create_async_engine(url)
        self.session: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False
        )

    async def init(self) -> None:
        async with self.engine.begin() as conn:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.run_sync(Base.metadata.create_all)
            current = (await conn.execute(select(func.max(SchemaVersion.version)))).scalar()
            if current is None:
                await conn.execute(insert(SchemaVersion).values(version=SCHEMA_VERSION))
                return
            for idx in range(current - 1, len(MIGRATIONS)):
                await conn.run_sync(MIGRATIONS[idx])
                await conn.execute(insert(SchemaVersion).values(version=idx + 2))

    async def close(self) -> None:
        await self.engine.dispose()

    # ------------------------------------------------------------ memory
    async def add_memory(self, chat_id: str, text: str) -> int:
        async with self.session() as s, s.begin():
            row = Memory(chat_id=chat_id, text=text)
            s.add(row)
            await s.flush()
            return row.id

    async def list_memories(self, chat_id: str) -> list[Memory]:
        async with self.session() as s:
            q = select(Memory).where(Memory.chat_id == chat_id).order_by(Memory.id)
            return list((await s.execute(q)).scalars())

    async def delete_memory(self, chat_id: str, memory_id: int) -> bool:
        async with self.session() as s, s.begin():
            res = await s.execute(
                delete(Memory).where(Memory.chat_id == chat_id, Memory.id == memory_id)
            )
            return bool(getattr(res, "rowcount", 0))

    async def clear_memories(self, chat_id: str) -> int:
        async with self.session() as s, s.begin():
            res = await s.execute(delete(Memory).where(Memory.chat_id == chat_id))
            return int(getattr(res, "rowcount", 0) or 0)

    # ------------------------------------------------------------ retention
    async def purge(self, older_than_days: int, now: datetime | None = None) -> dict[str, int]:
        """Delete old rows; cloud spend is kept at least 400 days (monthly budgets)."""
        now = (now or datetime.now(UTC)).astimezone(UTC).replace(tzinfo=None)
        cutoff = now - timedelta(days=older_than_days)
        spend_cutoff = now - timedelta(days=max(older_than_days, 400))
        counts: dict[str, int] = {}
        async with self.session() as s, s.begin():
            for name, stmt in (
                ("chat_messages", delete(ChatMessage).where(ChatMessage.created_at < cutoff)),
                ("interactions", delete(Interaction).where(Interaction.created_at < cutoff)),
                ("audit", delete(AuditEntry).where(AuditEntry.created_at < cutoff)),
                (
                    "jobs",
                    delete(Job).where(Job.updated_at < cutoff, Job.status.in_(["done", "failed"])),
                ),
                ("spend", delete(Spend).where(Spend.created_at < spend_cutoff)),
            ):
                res = await s.execute(stmt)
                counts[name] = int(getattr(res, "rowcount", 0) or 0)
        return counts

    # ------------------------------------------------------------ history
    async def add_message(
        self,
        chat_id: str,
        role: str,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> None:
        async with self.session() as s, s.begin():
            s.add(
                ChatMessage(
                    chat_id=chat_id, role=role, content=content, tool_calls=tool_calls or None
                )
            )

    async def history(self, chat_id: str, limit: int) -> list[ChatMessage]:
        if limit <= 0:
            return []
        async with self.session() as s:
            rows = (
                await s.execute(
                    select(ChatMessage)
                    .where(ChatMessage.chat_id == chat_id)
                    .order_by(ChatMessage.id.desc())
                    .limit(limit)
                )
            ).scalars()
            return list(reversed(list(rows)))

    async def clear_history(self, chat_id: str) -> None:
        async with self.session() as s, s.begin():
            await s.execute(delete(ChatMessage).where(ChatMessage.chat_id == chat_id))

    # ------------------------------------------------------------ interactions
    async def record_interaction(self, **fields: Any) -> None:
        async with self.session() as s, s.begin():
            s.add(Interaction(**fields))

    async def interactions_since(self, since: datetime, limit: int = 20_000) -> list[Interaction]:
        async with self.session() as s:
            q = (
                select(Interaction)
                .where(Interaction.created_at >= since.astimezone(UTC).replace(tzinfo=None))
                .order_by(Interaction.id)
                .limit(limit)
            )
            return list((await s.execute(q)).scalars())

    async def spend_rows_since(self, since: datetime) -> list[tuple[datetime, float]]:
        async with self.session() as s:
            q = select(Spend.created_at, Spend.cost_eur).where(
                Spend.created_at >= since.astimezone(UTC).replace(tzinfo=None)
            )
            return [(t, float(c)) for t, c in (await s.execute(q)).all()]

    async def recent_interactions(self, limit: int = 50) -> list[Interaction]:
        async with self.session() as s:
            q = select(Interaction).order_by(Interaction.id.desc()).limit(limit)
            return list((await s.execute(q)).scalars())

    # ------------------------------------------------------------ audit
    async def audit(
        self,
        *,
        requester: str,
        tier: str,
        model: str,
        tool: str,
        params: dict[str, Any],
        outcome: str,
        detail: str = "",
    ) -> None:
        async with self.session() as s, s.begin():
            s.add(
                AuditEntry(
                    requester=requester,
                    tier=tier,
                    model=model,
                    tool=tool,
                    params=json.dumps(params, ensure_ascii=False, default=str),
                    outcome=outcome,
                    detail=detail[:2000],
                )
            )

    async def audit_entries(self, limit: int = 100) -> list[AuditEntry]:
        async with self.session() as s:
            q = select(AuditEntry).order_by(AuditEntry.id.desc()).limit(limit)
            return list((await s.execute(q)).scalars())

    # ------------------------------------------------------------ jobs
    async def enqueue_job(self, chat_id: str, user_id: str, prompt: str) -> int:
        async with self.session() as s, s.begin():
            job = Job(chat_id=chat_id, user_id=user_id, prompt=prompt)
            s.add(job)
            await s.flush()
            return job.id

    async def claim_next_job(self) -> Job | None:
        async with self.session() as s, s.begin():
            job = (
                await s.execute(select(Job).where(Job.status == "queued").order_by(Job.id).limit(1))
            ).scalar_one_or_none()
            if job is None:
                return None
            job.status = "running"
            job.attempts += 1
            job.updated_at = utcnow()
            return job

    async def finish_job(
        self, job_id: int, *, status: str, result: str | None = None, error: str | None = None
    ) -> None:
        async with self.session() as s, s.begin():
            await s.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(status=status, result=result, error=error, updated_at=utcnow())
            )

    async def requeue_running_jobs(self) -> int:
        """Called at startup: jobs interrupted by a restart go back to the queue."""
        async with self.session() as s, s.begin():
            res = await s.execute(
                update(Job).where(Job.status == "running").values(status="queued")
            )
            return int(getattr(res, "rowcount", 0) or 0)

    async def count_jobs(self) -> dict[str, int]:
        async with self.session() as s:
            q = select(Job.status, func.count()).group_by(Job.status)
            return {status: int(n) for status, n in (await s.execute(q)).all()}

    async def get_job(self, job_id: int) -> Job | None:
        async with self.session() as s:
            return await s.get(Job, job_id)

    async def list_jobs(self, *, active_only: bool = False, limit: int = 50) -> list[Job]:
        async with self.session() as s:
            q = select(Job).order_by(Job.id.desc()).limit(limit)
            if active_only:
                q = q.where(Job.status.in_(["queued", "running"]))
            return list((await s.execute(q)).scalars())

    # ------------------------------------------------------------ spend
    async def add_spend(
        self,
        provider: str,
        tokens_in: int,
        tokens_out: int,
        cost: float,
        at: datetime | None = None,
    ) -> None:
        async with self.session() as s, s.begin():
            s.add(
                Spend(
                    provider=provider,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_eur=cost,
                    created_at=at or utcnow(),
                )
            )

    async def spend_since(self, since: datetime, provider: str | None = None) -> float:
        async with self.session() as s:
            q = select(func.coalesce(func.sum(Spend.cost_eur), 0.0)).where(
                Spend.created_at >= since
            )
            if provider:
                q = q.where(Spend.provider == provider)
            return float((await s.execute(q)).scalar() or 0.0)

    async def spend_by_provider(self, since: datetime) -> dict[str, float]:
        async with self.session() as s:
            q = (
                select(Spend.provider, func.sum(Spend.cost_eur))
                .where(Spend.created_at >= since)
                .group_by(Spend.provider)
            )
            return {p: float(c or 0) for p, c in (await s.execute(q)).all()}

    async def alert_last_sent(self, key: str) -> datetime | None:
        async with self.session() as s:
            row = await s.get(AlertState, key)
            return row.last_sent.replace(tzinfo=UTC) if row else None

    async def set_alert_sent(self, key: str, at: datetime) -> None:
        naive = at.astimezone(UTC).replace(tzinfo=None)
        async with self.session() as s, s.begin():
            row = await s.get(AlertState, key)
            if row is None:
                s.add(AlertState(key=key, last_sent=naive))
            else:
                row.last_sent = naive

    async def alert_once(self, key: str) -> bool:
        """Return True the first time ``key`` is seen (used to send each alert once)."""
        async with self.session() as s, s.begin():
            exists = (
                await s.execute(select(BudgetAlert).where(BudgetAlert.key == key))
            ).scalar_one_or_none()
            if exists:
                return False
            s.add(BudgetAlert(key=key))
            return True
