"""Login, logout and the unauthenticated health check."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from homelab_agent.web.auth import (
    COOKIE,
    SESSION_TTL_S,
    LoginLimiter,
    check_password,
    make_session,
    require_panel_secret,
)
from homelab_agent.web.deps import ServicesDep, render

router = APIRouter()


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> HTMLResponse:
    return render(request, "login.html", error=None)


@router.post("/login")
async def login(
    request: Request, services: ServicesDep, password: str = Form(alias="password")
) -> Response:
    limiter: LoginLimiter = request.app.state.login_limiter
    client = request.client.host if request.client else "?"
    if limiter.blocked(client):
        page = render(request, "login.html", error="Trop d'essais : réessaie dans 5 minutes.")
        page.status_code = 429
        return page
    secret = require_panel_secret(services.cfg)
    if not check_password(secret, password):
        limiter.failed(client)
        await asyncio.sleep(0.5)  # slow down guessing
        return render(request, "login.html", error="Mot de passe incorrect.")
    limiter.succeeded(client)
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(
        COOKIE, make_session(secret), httponly=True, samesite="strict", max_age=SESSION_TTL_S
    )
    return resp


@router.post("/logout")
async def logout() -> Response:
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(COOKIE)
    return resp
