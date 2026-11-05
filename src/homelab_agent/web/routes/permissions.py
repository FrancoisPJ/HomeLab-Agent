"""Home Assistant permissions: default policy, per-entity modes, rules, confirmations."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from homelab_agent.config.loader import ConfigError
from homelab_agent.config.models import Policy
from homelab_agent.services import Services
from homelab_agent.web.deps import ServicesDep, render

router = APIRouter(prefix="/permissions")

SUGGESTED_CONFIRM_DOMAINS = ["lock", "alarm_control_panel", "cover"]
INHERIT = "inherit"
POLICIES = [p.value for p in Policy]


def policy_or_400(value: str) -> Policy:
    try:
        return Policy(value)
    except ValueError:
        raise HTTPException(400, f"unknown policy {value!r}") from None


def entity_rows(services: Services) -> list[dict[str, Any]]:
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
                "effective": ctrl.mode.value if ctrl.allowed or not read.allowed else "read_only",
                "confirm": ctrl.needs_confirm,
                "reason": ctrl.reason,
                "own": own.mode.value if own else INHERIT,
                "own_confirm": bool(own and own.confirm),
            }
        )
    return rows


def grouped(services: Services) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in entity_rows(services):
        groups.setdefault(row["e"].domain, []).append(row)
    return dict(sorted(groups.items()))


def save_ha(services: Services, key: str, value: Any) -> None:
    try:
        services.store.update_section(["home_assistant", key], value)
    except ConfigError as exc:
        raise HTTPException(400, str(exc)) from exc


def rules_as_dicts(services: Services) -> list[dict[str, Any]]:
    return [
        r.model_dump(exclude_none=True, exclude_defaults=False, mode="json")
        for r in services.cfg.home_assistant.rules
    ]


def back() -> Response:
    return RedirectResponse("/permissions", status_code=303)


@router.get("", response_class=HTMLResponse)
async def permissions(request: Request, services: ServicesDep) -> HTMLResponse:
    return render(
        request,
        "permissions.html",
        groups=grouped(services),
        ha=services.ha,
        rules=services.cfg.home_assistant.rules,
        confirm=services.cfg.home_assistant.confirm,
        policies=POLICIES,
        suggested=SUGGESTED_CONFIRM_DOMAINS,
    )


@router.post("/default")
async def set_default(services: ServicesDep, policy: str = Form(...)) -> Response:
    save_ha(services, "default_policy", policy_or_400(policy).value)
    return back()


@router.post("/entity", response_class=HTMLResponse)
async def set_entity(
    request: Request,
    services: ServicesDep,
    entity_id: str = Form(...),
    mode: str = Form(...),
    confirm: str | None = Form(None),
) -> HTMLResponse:
    # only the entity's own exact rule is replaced; domain / area rules stay
    rules = [
        r
        for r in rules_as_dicts(services)
        if r.get("entity") != entity_id or r.get("domain") or r.get("area")
    ]
    if mode != INHERIT:
        rules.append(
            {"entity": entity_id, "mode": policy_or_400(mode).value, "confirm": confirm == "on"}
        )
    save_ha(services, "rules", rules)
    row = next((r for r in entity_rows(services) if r["e"].entity_id == entity_id), None)
    if row is None:
        return HTMLResponse("")
    return render(request, "_entity_row.html", row=row, policies=POLICIES)


@router.post("/rule")
async def add_rule(
    services: ServicesDep,
    mode: str = Form(...),
    domain: str = Form(""),
    area: str = Form(""),
    entity: str = Form(""),
    confirm: str | None = Form(None),
) -> Response:
    rule: dict[str, Any] = {"mode": policy_or_400(mode).value, "confirm": confirm == "on"}
    for key, value in (("domain", domain), ("area", area), ("entity", entity)):
        if value.strip():
            rule[key] = value.strip()
    save_ha(services, "rules", [*rules_as_dicts(services), rule])
    return back()


@router.post("/rule/{idx}/delete")
async def delete_rule(services: ServicesDep, idx: int) -> Response:
    rules = rules_as_dicts(services)
    if 0 <= idx < len(rules):
        rules.pop(idx)
        save_ha(services, "rules", rules)
    return back()


@router.post("/confirm")
async def toggle_confirm_domain(
    services: ServicesDep, domain: str = Form(...), enabled: str = Form("on")
) -> Response:
    current = [s.model_dump(exclude_none=True) for s in services.cfg.home_assistant.confirm]
    current = [s for s in current if s != {"domain": domain}]
    if enabled == "on":
        current.append({"domain": domain})
    save_ha(services, "confirm", current)
    return back()
