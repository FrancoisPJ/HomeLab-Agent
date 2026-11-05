"""Read-mostly pages: budget, CPU jobs, audit log, configuration editor."""

from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse

from homelab_agent.config.loader import ConfigError
from homelab_agent.web.deps import ServicesDep, render

router = APIRouter()


@router.get("/budget", response_class=HTMLResponse)
async def budget(request: Request, services: ServicesDep) -> HTMLResponse:
    return render(request, "budget.html", status=await services.budget.status())


@router.get("/jobs", response_class=HTMLResponse)
async def jobs(request: Request, services: ServicesDep) -> HTMLResponse:
    return render(request, "jobs.html", jobs=await services.db.list_jobs(limit=100))


@router.get("/audit", response_class=HTMLResponse)
async def audit(request: Request, services: ServicesDep) -> HTMLResponse:
    return render(request, "audit.html", entries=await services.db.audit_entries(200))


@router.get("/config", response_class=HTMLResponse)
async def config_page(request: Request, services: ServicesDep) -> HTMLResponse:
    return render(request, "config.html", text=services.store.raw_text(), error=None, saved=False)


@router.post("/config", response_class=HTMLResponse)
async def config_save(
    request: Request, services: ServicesDep, text: str = Form(...)
) -> HTMLResponse:
    try:
        services.store.replace_text(text)
    except ConfigError as exc:
        return render(request, "config.html", text=text, error=str(exc), saved=False)
    return render(request, "config.html", text=services.store.raw_text(), error=None, saved=True)
