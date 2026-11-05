"""What every panel route needs: the running services and the template renderer."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from homelab_agent.app import Services
from homelab_agent.core.routines import fr_days

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=str(HERE / "templates"))
templates.env.filters["days_fr"] = fr_days


def get_services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


ServicesDep = Annotated[Services, Depends(get_services)]


def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
    ctx.setdefault("cfg", get_services(request).cfg)
    return templates.TemplateResponse(request, name, ctx)
