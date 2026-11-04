"""Cloud spend accounting and caps. Prices come from the config, never from code."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from homelab_agent.config.models import AppConfig
from homelab_agent.store.db import Database


@dataclass(slots=True)
class BudgetCheck:
    allowed: bool
    reason: str = ""


@dataclass(slots=True)
class BudgetStatus:
    provider: str
    day_eur: float
    month_eur: float
    daily_limit: float
    monthly_limit: float


Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def cost_eur(cfg: AppConfig, provider: str, tokens_in: int, tokens_out: int) -> float:
    price = cfg.cloud.pricing.get(provider)
    if price is None:
        return 0.0
    return (tokens_in * price.in_per_mtok + tokens_out * price.out_per_mtok) / 1_000_000


class BudgetManager:
    def __init__(self, get_config: Callable[[], AppConfig], db: Database, clock: Clock = _utc_now):
        self._cfg = get_config
        self.db = db
        self.clock = clock

    @property
    def cfg(self) -> AppConfig:
        return self._cfg()

    def _period_starts(self) -> tuple[datetime, datetime, str, str]:
        tz = ZoneInfo(self.cfg.agent.timezone)
        local = self.clock().astimezone(tz)
        day = local.replace(hour=0, minute=0, second=0, microsecond=0)
        month = day.replace(day=1)

        def to_utc(d: datetime) -> datetime:
            return d.astimezone(UTC).replace(tzinfo=None)

        return to_utc(day), to_utc(month), day.strftime("%Y-%m-%d"), month.strftime("%Y-%m")

    def _limits(self, provider: str) -> list[tuple[str, float, bool]]:
        """(scope, limit, per_provider) tuples to check."""
        lim = self.cfg.cloud.limits
        out = [("daily", lim.daily_eur, False), ("monthly", lim.monthly_eur, False)]
        pp = lim.per_provider.get(provider)
        if pp:
            out += [("daily", pp.daily_eur, True), ("monthly", pp.monthly_eur, True)]
        return out

    async def check(self, provider: str) -> BudgetCheck:
        if not self.cfg.cloud.enabled:
            return BudgetCheck(False, "le cloud est désactivé (cloud.enabled: false)")
        day, month, _, _ = self._period_starts()
        for scope, limit, per_provider in self._limits(provider):
            since = day if scope == "daily" else month
            spent = await self.db.spend_since(since, provider if per_provider else None)
            if spent >= limit:
                who = provider if per_provider else "total"
                label = "journalier" if scope == "daily" else "mensuel"
                return BudgetCheck(
                    False, f"plafond {label} atteint ({who} : {spent:.2f} € / {limit:.2f} €)"
                )
        return BudgetCheck(True)

    async def record(
        self, provider: str, tokens_in: int, tokens_out: int
    ) -> tuple[float, list[str]]:
        """Store a spend and return (cost, alert messages crossing the alert threshold)."""
        cost = cost_eur(self.cfg, provider, tokens_in, tokens_out)
        await self.db.add_spend(provider, tokens_in, tokens_out, cost)
        alerts: list[str] = []
        day, month, day_key, month_key = self._period_starts()
        threshold = self.cfg.cloud.limits.alert_at
        for scope, limit, per_provider in self._limits(provider):
            if limit <= 0:
                continue
            since, key = (day, day_key) if scope == "daily" else (month, month_key)
            spent = await self.db.spend_since(since, provider if per_provider else None)
            ratio = spent / limit
            who = provider if per_provider else "total"
            for level, label in ((threshold, f"{int(threshold * 100)} %"), (1.0, "100 %")):
                if ratio >= level and await self.db.alert_once(f"{scope}:{who}:{key}:{level}"):
                    period = "jour" if scope == "daily" else "mois"
                    alerts.append(
                        f"⚠️ Budget cloud ({who}, {period}) : {label} du plafond atteint "
                        f"({spent:.2f} € / {limit:.2f} €)."
                    )
        return cost, alerts

    async def status(self) -> list[BudgetStatus]:
        day, month, _, _ = self._period_starts()
        lim = self.cfg.cloud.limits
        providers = sorted(set(self.cfg.cloud.pricing) | set(self.cfg.backends_for_cloud()))
        out = []
        for p in providers:
            pp = lim.per_provider.get(p)
            out.append(
                BudgetStatus(
                    p,
                    await self.db.spend_since(day, p),
                    await self.db.spend_since(month, p),
                    pp.daily_eur if pp else lim.daily_eur,
                    pp.monthly_eur if pp else lim.monthly_eur,
                )
            )
        return out

    async def totals(self) -> tuple[float, float]:
        day, month, _, _ = self._period_starts()
        return await self.db.spend_since(day), await self.db.spend_since(month)
