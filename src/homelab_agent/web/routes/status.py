"""Home page, JSON status and Prometheus metrics."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from homelab_agent.app import Services
from homelab_agent.config.models import Tier
from homelab_agent.core.router import TIER_LABELS
from homelab_agent.observability.metrics import METRICS
from homelab_agent.web.dashboard import WINDOW, build_dashboard
from homelab_agent.web.deps import ServicesDep, render
from homelab_agent.web.summary import house_summary

router = APIRouter()


def backend_rows(services: Services) -> list[dict[str, Any]]:
    rows = []
    for tier in Tier:
        for name in services.cfg.backends_for(tier):
            b = services.backends.get(name)
            rows.append(
                {
                    "tier": TIER_LABELS[tier],
                    "name": name,
                    "model": b.model if b else "?",
                    "up": services.health.available(name),
                    "probed": name in services.health.last_probe,
                }
            )
    return rows


async def dashboard_data(services: Services, day: float) -> dict[str, Any]:
    now = datetime.now(UTC)
    ha = services.ha
    return build_dashboard(
        await services.db.interactions_since(now - WINDOW),
        await services.db.spend_rows_since(now - WINDOW),
        now=now,
        tz=ZoneInfo(services.cfg.agent.timezone),
        day_spend=day,
        daily_limit=services.cfg.cloud.limits.daily_eur,
        house=house_summary(ha.client.cache.entities.values()) if ha is not None else None,
    )


@router.get("/", response_class=HTMLResponse)
async def index(request: Request, services: ServicesDep) -> HTMLResponse:
    day, month = await services.budget.totals()
    return render(
        request,
        "index.html",
        backends=backend_rows(services),
        ha=services.ha,
        day=day,
        month=month,
        interactions=await services.db.recent_interactions(12),
        dash=await dashboard_data(services, day),
    )


@router.get("/api/status")
async def api_status(services: ServicesDep) -> dict[str, Any]:
    day, month = await services.budget.totals()
    return {
        "backends": backend_rows(services),
        "home_assistant": bool(services.ha and services.ha.client.connected),
        "cloud": {"enabled": services.cfg.cloud.enabled, "day_eur": day, "month_eur": month},
    }


@router.get("/metrics")
async def metrics(services: ServicesDep) -> PlainTextResponse:
    return PlainTextResponse(
        await METRICS.render([services.metrics_samples]),
        media_type="text/plain; version=0.0.4",
    )
