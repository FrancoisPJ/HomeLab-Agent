from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from homelab_agent.app import Services
from homelab_agent.config.loader import ConfigStore
from homelab_agent.web.app import PanelSecretMissing, create_app, require_panel_secret
from homelab_agent.web.auth import check_session, make_session

from .conftest import ROOT
from .harness import build_harness

PASSWORD = "correct-horse-battery"


async def services(tmp_path: Path) -> Services:
    h = await build_harness(tmp_path)
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        (ROOT / "deploy" / "config.dev.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )
    store = ConfigStore(cfg_path)
    # the harness services read the live config from this store
    h.cfg_store._config = store.config
    h.cfg_store.path = store.path
    return Services(
        store=h.cfg_store, db=h.db, backends=h.backends, health=h.router.health,
        budget=h.orchestrator.budget, router=h.router, registry=h.orchestrator.registry,
        orchestrator=h.orchestrator, ha=h.ha,
    )  # fmt: skip


def test_panel_refuses_to_start_without_password(monkeypatch: pytest.MonkeyPatch) -> None:
    from homelab_agent.config.loader import load_config

    cfg = load_config(ROOT / "deploy" / "config.dev.yaml")
    monkeypatch.delenv("PANEL_PASSWORD", raising=False)
    with pytest.raises(PanelSecretMissing):
        require_panel_secret(cfg)
    monkeypatch.setenv("PANEL_PASSWORD", "short")
    with pytest.raises(PanelSecretMissing):
        require_panel_secret(cfg)
    monkeypatch.setenv("PANEL_PASSWORD", PASSWORD)
    assert require_panel_secret(cfg) == PASSWORD


def test_session_tokens() -> None:
    tok = make_session("pw", now=1000)
    assert check_session("pw", tok, now=2000)
    assert not check_session("other", tok, now=2000)
    assert not check_session("pw", tok, now=10**10)
    assert not check_session("pw", "garbage")
    assert not check_session("pw", None)


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    import asyncio

    monkeypatch.setenv("PANEL_PASSWORD", PASSWORD)
    svc = asyncio.new_event_loop().run_until_complete(services(tmp_path))
    with TestClient(create_app(svc)) as c:
        c.svc = svc  # type: ignore[attr-defined]
        yield c


def login(c: TestClient) -> None:
    r = c.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303


def test_auth_required(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/api/status").status_code == 401
    assert client.get("/metrics").status_code == 401
    bad = client.post("/login", data={"password": "nope"})
    assert "incorrect" in bad.text
    assert (
        client.get("/api/status", headers={"Authorization": f"Bearer {PASSWORD}"}).status_code
        == 200
    )


def test_pages_render(client: TestClient) -> None:
    login(client)
    for path in ("/", "/permissions", "/budget", "/jobs", "/audit", "/config"):
        r = client.get(path)
        assert r.status_code == 200, path
    assert "light.salon" in client.get("/permissions").text
    assert client.get("/metrics").status_code == 200


def test_permission_change_applies_without_restart(client: TestClient) -> None:
    login(client)
    svc: Services = client.svc  # type: ignore[attr-defined]
    engine = svc.ha.permissions  # type: ignore[union-attr]
    assert engine.evaluate("light.salon", "control").allowed
    r = client.post("/permissions/entity", data={"entity_id": "light.salon", "mode": "deny"})
    assert r.status_code == 200 and "deny" in r.text
    assert not engine.evaluate("light.salon", "control").allowed  # live, no restart
    text = svc.store.path.read_text(encoding="utf-8")
    assert "light.salon" in text and "deny" in text  # persisted in the YAML
    client.post("/permissions/entity", data={"entity_id": "light.salon", "mode": "inherit"})
    assert engine.evaluate("light.salon", "control").allowed


def test_rules_default_and_confirm(client: TestClient) -> None:
    login(client)
    svc: Services = client.svc  # type: ignore[attr-defined]
    engine = svc.ha.permissions  # type: ignore[union-attr]
    client.post("/permissions/rule", data={"domain": "cover", "mode": "read_only"})
    assert not engine.evaluate("cover.volet_chambre", "control").allowed
    client.post("/permissions/rule/0/delete")
    assert svc.cfg.home_assistant.rules == []
    client.post("/permissions/confirm", data={"domain": "cover", "enabled": "on"})
    assert engine.evaluate("cover.volet_chambre", "control").needs_confirm
    client.post("/permissions/confirm", data={"domain": "cover", "enabled": "off"})
    assert not engine.evaluate("cover.volet_chambre", "control").needs_confirm
    client.post("/permissions/default", data={"policy": "read_only"})
    assert not engine.evaluate("light.cuisine", "control").allowed


def test_config_editor_validates(client: TestClient) -> None:
    login(client)
    svc: Services = client.svc  # type: ignore[attr-defined]
    before = svc.store.path.read_text(encoding="utf-8")
    r = client.post("/config", data={"text": "web:\n  port: 0\n"})
    assert "invalid configuration" in r.text
    assert svc.store.path.read_text(encoding="utf-8") == before
    r = client.post("/config", data={"text": before.replace("alert_at: 0.8", "alert_at: 0.5")})
    assert "Enregistré" in r.text
    assert svc.cfg.cloud.limits.alert_at == 0.5
