"""Proactive page: alerts, their recent firings, and routines."""

from __future__ import annotations

from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from homelab_agent.core.routines import fr_when
from homelab_agent.web.deps import ServicesDep, render

router = APIRouter(prefix="/proactive")


@router.get("", response_class=HTMLResponse)
async def proactive(request: Request, services: ServicesDep) -> HTMLResponse:
    tz = ZoneInfo(services.cfg.agent.timezone)
    routines = []
    for r in services.cfg.proactive.routines:
        nxt = services.routines.next_run(r) if services.routines else None
        when = fr_when(nxt.astimezone(tz)) if nxt and r.enabled else None
        routines.append({"r": r, "next": when})
    events = list(reversed(services.alerts.events[-50:])) if services.alerts else []
    return render(request, "proactive.html", routines=routines, events=events, tz=tz, ran=None)


@router.post("/routine/{name}/{action}")
async def proactive_routine(
    services: ServicesDep, name: str, action: Literal["on", "off", "delete"]
) -> Response:
    manager = services.routine_manager
    if manager is None or manager.get(name) is None:
        raise HTTPException(404, "unknown routine")
    await manager.command("", f"{action} {name}")
    return RedirectResponse("/proactive", status_code=303)


@router.post("/run/{name}")
async def proactive_run(services: ServicesDep, name: str) -> Response:
    routine = services.routines.get(name) if services.routines is not None else None
    if services.routines is None or routine is None:
        raise HTTPException(404, "unknown routine")
    await services.routines.run(routine)
    return RedirectResponse("/proactive", status_code=303)
