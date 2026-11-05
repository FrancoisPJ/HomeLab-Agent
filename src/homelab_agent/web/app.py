"""Web panel: FastAPI + Jinja2 + HTMX (no JavaScript build).

This module only assembles the application; pages live in ``web/routes/``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import RequestResponseEndpoint

from homelab_agent.web.auth import (
    COOKIE,
    LoginLimiter,
    PanelSecretMissing,
    is_authorized,
    require_panel_secret,
)
from homelab_agent.web.deps import HERE
from homelab_agent.web.routes import auth, chat, pages, permissions, proactive, status

if TYPE_CHECKING:
    from homelab_agent.app import Services

__all__ = ["PanelSecretMissing", "create_app", "require_panel_secret"]

PUBLIC_PATHS = {"/login", "/healthz"}
#: answered with 401 instead of a redirect to the login page
API_PATHS = ("/api/", "/metrics")


def create_app(services: Services) -> FastAPI:
    app = FastAPI(title="homelab-agent", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.services = services
    app.state.login_limiter = LoginLimiter()
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    @app.middleware("http")
    async def guard(request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        if path in PUBLIC_PATHS or path.startswith("/static/"):
            return await call_next(request)
        allowed = is_authorized(
            services.cfg,
            path=path,
            method=request.method,
            authorization=request.headers.get("authorization", ""),
            cookie=request.cookies.get(COOKIE),
        )
        if allowed:
            return await call_next(request)
        if path.startswith(API_PATHS):
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        return RedirectResponse("/login", status_code=303)

    for module in (auth, status, permissions, chat, proactive, pages):
        app.include_router(module.router)
    return app
