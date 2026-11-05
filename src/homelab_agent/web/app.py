"""Web panel: FastAPI + Jinja2 + HTMX (no JavaScript build)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from homelab_agent.config.loader import ConfigError
from homelab_agent.config.models import AppConfig, Policy, Tier
from homelab_agent.core.router import TIER_LABELS
from homelab_agent.core.routines import fr_when
from homelab_agent.observability.metrics import METRICS
from homelab_agent.web.auth import COOKIE, check_password, check_session, make_session

if TYPE_CHECKING:
    from homelab_agent.app import Services

HERE = Path(__file__).parent
SUGGESTED_CONFIRM_DOMAINS = ["lock", "alarm_control_panel", "cover"]


class PanelSecretMissing(SystemExit):
    pass


def require_panel_secret(cfg: AppConfig) -> str:
    secret = AppConfig.secret(cfg.web.password_env)
    if not secret or len(secret) < 8:
        raise PanelSecretMissing(
            f"Refusing to start the web panel: set {cfg.web.password_env} "
            "(at least 8 characters) in .env, or disable it with web.enabled: false"
        )
    return secret


def create_app(services: Services) -> FastAPI:
    app = FastAPI(title="homelab-agent", docs_url=None, redoc_url=None, openapi_url=None)
    templates = Jinja2Templates(directory=str(HERE / "templates"))
    app.mount("/static", StaticFiles(directory=str(HERE / "static")), name="static")

    def password() -> str:
        return require_panel_secret(services.cfg)

    def authed(request: Request) -> bool:
        pw = password()
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer ") and check_password(pw, auth[7:].strip()):
            return True
        return check_session(pw, request.cookies.get(COOKIE))

    @app.middleware("http")
    async def guard(request: Request, call_next: Any) -> Response:
        path = request.url.path
        if path in ("/login", "/healthz") or path.startswith("/static/"):
            return await call_next(request)  # type: ignore[no-any-return]
        if not authed(request):
            if path.startswith("/api/") or path == "/metrics":
                return JSONResponse({"detail": "unauthorized"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        response: Response = await call_next(request)
        return response

    def render(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        ctx.setdefault("cfg", services.cfg)
        return templates.TemplateResponse(request, name, ctx)

    # ------------------------------------------------------------------ auth
    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request) -> HTMLResponse:
        return render(request, "login.html", error=None)

    @app.post("/login")
    async def login(request: Request, password_field: str = Form(alias="password")) -> Response:
        if not check_password(password(), password_field):
            return render(request, "login.html", error="Mot de passe incorrect.")
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            COOKIE, make_session(password()), httponly=True, samesite="strict", max_age=7 * 86400
        )
        return resp

    @app.post("/logout")
    async def logout() -> Response:
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie(COOKIE)
        return resp

    # ---------------------------------------------------------------- status
    async def backend_rows() -> list[dict[str, Any]]:
        rows = []
        for tier in Tier:
            for name in services.cfg.backends_for(tier):
                b = services.backends.get(name)
                rows.append(
                    {
                        "tier": TIER_LABELS[tier],
                        "name": name,
                        "model": b.model if b else "?",
                        "up": services.health.is_up(name)
                        and services.health.last_probe.get(name, True),
                        "probed": name in services.health.last_probe,
                    }
                )
        return rows

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request) -> HTMLResponse:
        day, month = await services.budget.totals()
        return render(
            request,
            "index.html",
            backends=await backend_rows(),
            ha=services.ha,
            day=day,
            month=month,
            interactions=await services.db.recent_interactions(20),
        )

    @app.get("/api/status")
    async def api_status() -> dict[str, Any]:
        day, month = await services.budget.totals()
        return {
            "backends": await backend_rows(),
            "home_assistant": bool(services.ha and services.ha.client.connected),
            "cloud": {"enabled": services.cfg.cloud.enabled, "day_eur": day, "month_eur": month},
        }

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        return PlainTextResponse(METRICS.render())

    # ----------------------------------------------------------- permissions
    def entity_rows() -> list[dict[str, Any]]:
        if services.ha is None:
            return []
        engine = services.ha.permissions
        rules = services.cfg.home_assistant.rules
        exact = {r.entity: r for r in rules if r.entity and not any(c in r.entity for c in "*?[")}
        rows = []
        for e in sorted(services.ha.client.cache.entities.values(), key=lambda x: x.entity_id):
            read = engine.evaluate(e.entity_id, "read", areas=e.areas)
            ctrl = engine.evaluate(e.entity_id, "control", areas=e.areas)
            own = exact.get(e.entity_id)
            rows.append(
                {
                    "e": e,
                    "effective": ctrl.mode.value
                    if ctrl.allowed or not read.allowed
                    else "read_only",
                    "confirm": ctrl.needs_confirm,
                    "reason": ctrl.reason,
                    "own": own.mode.value if own else "inherit",
                    "own_confirm": bool(own and own.confirm),
                }
            )
        return rows

    def grouped() -> dict[str, list[dict[str, Any]]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in entity_rows():
            groups.setdefault(row["e"].domain, []).append(row)
        return dict(sorted(groups.items()))

    def save_ha(key: str, value: Any) -> None:
        try:
            services.store.update_section(["home_assistant", key], value)
        except ConfigError as exc:
            raise HTTPException(400, str(exc)) from exc

    def rules_as_dicts() -> list[dict[str, Any]]:
        return [
            r.model_dump(exclude_none=True, exclude_defaults=False, mode="json")
            for r in services.cfg.home_assistant.rules
        ]

    @app.get("/permissions", response_class=HTMLResponse)
    async def permissions(request: Request) -> HTMLResponse:
        return render(
            request,
            "permissions.html",
            groups=grouped(),
            ha=services.ha,
            rules=services.cfg.home_assistant.rules,
            confirm=services.cfg.home_assistant.confirm,
            policies=[p.value for p in Policy],
            suggested=SUGGESTED_CONFIRM_DOMAINS,
        )

    @app.post("/permissions/default")
    async def set_default(policy: str = Form(...)) -> Response:
        Policy(policy)
        save_ha("default_policy", policy)
        return RedirectResponse("/permissions", status_code=303)

    @app.post("/permissions/entity", response_class=HTMLResponse)
    async def set_entity(
        request: Request,
        entity_id: str = Form(...),
        mode: str = Form(...),
        confirm: str | None = Form(None),
    ) -> HTMLResponse:
        rules = [
            r
            for r in rules_as_dicts()
            if r.get("entity") != entity_id or r.get("domain") or r.get("area")
        ]
        if mode != "inherit":
            Policy(mode)
            rules.append({"entity": entity_id, "mode": mode, "confirm": confirm == "on"})
        save_ha("rules", rules)
        row = next((r for r in entity_rows() if r["e"].entity_id == entity_id), None)
        if row is None:
            return HTMLResponse("")
        return render(request, "_entity_row.html", row=row, policies=[p.value for p in Policy])

    @app.post("/permissions/rule")
    async def add_rule(
        mode: str = Form(...),
        domain: str = Form(""),
        area: str = Form(""),
        entity: str = Form(""),
        confirm: str | None = Form(None),
    ) -> Response:
        rule: dict[str, Any] = {"mode": Policy(mode).value, "confirm": confirm == "on"}
        for k, v in (("domain", domain), ("area", area), ("entity", entity)):
            if v.strip():
                rule[k] = v.strip()
        save_ha("rules", [*rules_as_dicts(), rule])
        return RedirectResponse("/permissions", status_code=303)

    @app.post("/permissions/rule/{idx}/delete")
    async def delete_rule(idx: int) -> Response:
        rules = rules_as_dicts()
        if 0 <= idx < len(rules):
            rules.pop(idx)
            save_ha("rules", rules)
        return RedirectResponse("/permissions", status_code=303)

    @app.post("/permissions/confirm")
    async def toggle_confirm_domain(domain: str = Form(...), enabled: str = Form("on")) -> Response:
        current = [s.model_dump(exclude_none=True) for s in services.cfg.home_assistant.confirm]
        current = [s for s in current if s != {"domain": domain}]
        if enabled == "on":
            current.append({"domain": domain})
        save_ha("confirm", current)
        return RedirectResponse("/permissions", status_code=303)

    # ------------------------------------------------------------- proactive
    @app.get("/proactive", response_class=HTMLResponse)
    async def proactive(request: Request) -> HTMLResponse:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(services.cfg.agent.timezone)
        routines = []
        for r in services.cfg.proactive.routines:
            nxt = services.routines.next_run(r) if services.routines else None
            when = fr_when(nxt.astimezone(tz)) if nxt and r.enabled else None
            routines.append({"r": r, "next": when})
        events = list(reversed(services.alerts.events[-50:])) if services.alerts else []
        return render(request, "proactive.html", routines=routines, events=events, tz=tz, ran=None)

    @app.post("/proactive/routine/{name}/{action}")
    async def proactive_routine(name: str, action: str) -> Response:
        manager = services.routine_manager
        if manager is None or manager.get(name) is None or action not in ("on", "off", "delete"):
            raise HTTPException(404, "unknown routine or action")
        await manager.command("", f"{action} {name}")
        return RedirectResponse("/proactive", status_code=303)

    @app.post("/proactive/run/{name}", response_class=HTMLResponse)
    async def proactive_run(request: Request, name: str) -> Response:
        if services.routines is None or services.routines.get(name) is None:
            raise HTTPException(404, "unknown routine")
        routine = services.routines.get(name)
        assert routine is not None
        await services.routines.run(routine)
        return RedirectResponse("/proactive", status_code=303)

    # ---------------------------------------------------------------- budget
    @app.get("/budget", response_class=HTMLResponse)
    async def budget(request: Request) -> HTMLResponse:
        return render(request, "budget.html", status=await services.budget.status())

    # ------------------------------------------------------------------ jobs
    @app.get("/jobs", response_class=HTMLResponse)
    async def jobs(request: Request) -> HTMLResponse:
        return render(request, "jobs.html", jobs=await services.db.list_jobs(limit=100))

    # ----------------------------------------------------------------- audit
    @app.get("/audit", response_class=HTMLResponse)
    async def audit(request: Request) -> HTMLResponse:
        return render(request, "audit.html", entries=await services.db.audit_entries(200))

    # ---------------------------------------------------------------- config
    @app.get("/config", response_class=HTMLResponse)
    async def config_page(request: Request) -> HTMLResponse:
        return render(
            request, "config.html", text=services.store.raw_text(), error=None, saved=False
        )

    @app.post("/config", response_class=HTMLResponse)
    async def config_save(request: Request, text: str = Form(...)) -> HTMLResponse:
        try:
            services.store.replace_text(text)
        except ConfigError as exc:
            return render(request, "config.html", text=text, error=str(exc), saved=False)
        return render(
            request, "config.html", text=services.store.raw_text(), error=None, saved=True
        )

    return app
