from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from homelab_agent.core.budget import BudgetManager, cost_eur
from homelab_agent.store.db import Database

from .conftest import base_config


async def test_disabled_cloud_is_blocked(db: Database) -> None:
    cfg = base_config(cloud={"enabled": False})
    check = await BudgetManager(lambda: cfg, db).check("cloud")
    assert not check.allowed and "désactivé" in check.reason


async def test_zero_cap_blocks(db: Database) -> None:
    cfg = base_config(cloud={"enabled": True, "limits": {"monthly_eur": 0, "daily_eur": 0}})
    assert not (await BudgetManager(lambda: cfg, db).check("cloud")).allowed


async def test_cap_reached_then_blocked_and_alerts_once(db: Database) -> None:
    cfg = base_config(
        cloud={
            "enabled": True,
            "pricing": {"cloud": {"in_per_mtok": 1000, "out_per_mtok": 0}},
            "limits": {"monthly_eur": 10, "daily_eur": 1, "alert_at": 0.8},
        }
    )
    bm = BudgetManager(lambda: cfg, db)
    assert (await bm.check("cloud")).allowed
    cost, alerts = await bm.record("cloud", 500, 0)  # 0.50 €
    assert cost == 0.5 and alerts == []
    _, alerts = await bm.record("cloud", 350, 0)  # 0.85 € → 80 % daily alert
    assert len(alerts) == 1 and "80 %" in alerts[0]
    _, alerts = await bm.record("cloud", 10, 0)  # no duplicate alert
    assert alerts == []
    _, alerts = await bm.record("cloud", 200, 0)  # 1.06 € → 100 %
    assert any("100 %" in a for a in alerts)
    check = await bm.check("cloud")
    assert not check.allowed and "journalier" in check.reason


async def test_per_provider_cap(db: Database) -> None:
    cfg = base_config(
        cloud={
            "enabled": True,
            "pricing": {"cloud": {"in_per_mtok": 1000, "out_per_mtok": 1000}},
            "limits": {
                "monthly_eur": 100,
                "daily_eur": 100,
                "per_provider": {"cloud": {"monthly_eur": 0.1, "daily_eur": 0.1}},
            },
        }
    )
    bm = BudgetManager(lambda: cfg, db)
    await bm.record("cloud", 100, 0)
    check = await bm.check("cloud")
    assert not check.allowed and "cloud" in check.reason


async def test_new_day_resets_daily_cap(db: Database) -> None:
    cfg = base_config(
        cloud={"enabled": True, "limits": {"monthly_eur": 100, "daily_eur": 1}},
    )
    now = datetime(2026, 3, 10, 12, tzinfo=UTC)
    bm = BudgetManager(lambda: cfg, db, clock=lambda: now)
    await db.add_spend("cloud", 0, 0, 2.0, at=now.replace(tzinfo=None))
    assert not (await bm.check("cloud")).allowed
    later = now + timedelta(days=1)
    bm2 = BudgetManager(lambda: cfg, db, clock=lambda: later)
    assert (await bm2.check("cloud")).allowed


async def test_status_and_totals(db: Database) -> None:
    cfg = base_config()
    bm = BudgetManager(lambda: cfg, db)
    await bm.record("cloud", 1000, 1000)
    day, month = await bm.totals()
    assert round(day, 6) == 2.0 and round(month, 6) == 2.0
    status = {s.provider: s for s in await bm.status()}
    assert round(status["cloud"].day_eur, 6) == 2.0


@given(st.integers(0, 10**7), st.integers(0, 10**7), st.integers(0, 10**7))
def test_cost_is_non_negative_and_monotonic(a: int, b: int, extra: int) -> None:
    cfg = base_config()
    c1 = cost_eur(cfg, "cloud", a, b)
    c2 = cost_eur(cfg, "cloud", a + extra, b)
    assert 0 <= c1 <= c2
    assert cost_eur(cfg, "unknown", a, b) == 0
