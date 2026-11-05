"""Status page charts, house summary, liquid glass shell, Grafana dashboard consistency."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import pydantic
import pytest
from fastapi.testclient import TestClient

from homelab_agent.app import Services
from homelab_agent.observability.metrics import HELP
from homelab_agent.tools.homeassistant.client import EntityInfo
from homelab_agent.web.charts import donut, percentile, sparkline
from homelab_agent.web.summary import house_summary

from .conftest import ROOT
from .test_web import client, login  # noqa: F401  (pytest fixture)

STATIC = ROOT / "src" / "homelab_agent" / "web" / "static"


# ------------------------------------------------------------------- charts
def test_sparkline_shapes_and_escaping() -> None:
    flat = str(sparkline([]))
    assert flat.startswith('<svg class="spark"') and flat.endswith("</svg>")
    one = str(sparkline([5]))
    assert "NaN" not in one and "inf" not in one
    hostile = str(sparkline([1, 3, 2], color='"><script>alert(1)</script>'))
    assert "<script>" not in hostile and "&lt;script&gt;" in hostile
    # every chart gets its own gradient id (several sparklines on one page)
    ids = re.findall(
        r'linearGradient id="(sg\d+)"', str(sparkline([1, 2])) + str(sparkline([1, 2]))
    )
    assert len(ids) == 2 and ids[0] != ids[1]
    # negative values are clamped instead of drawing under the baseline
    paths = re.findall(r' d="([^"]+)"', str(sparkline([-5, -1], width=100, height=44)))
    coords = [float(x) for d in paths for x in re.findall(r"-?\d+(?:\.\d+)?", d)]
    assert coords and all(0 <= c <= 100 for c in coords)


def test_donut_segments_and_labels() -> None:
    svg = str(
        donut([("a", 3, "#fff"), ("vide", 0, "#000"), ("<b>", 1, "#123456")], center_label="x")
    )
    assert svg.count("<title>") == 2  # zero segment skipped
    assert "&lt;b&gt;" in svg and "<b>" not in svg
    assert ">4</text>" in svg
    empty = str(donut([], center_label="rien"))
    assert ">0</text>" in empty and "<title>" not in empty


def test_percentile() -> None:
    assert percentile([], 0.95) == 0.0
    assert percentile([7], 0.95) == 7
    assert percentile([1, 2, 3, 4], 0.5) == 2.5
    assert percentile(list(range(101)), 0.95) == pytest.approx(95)


# -------------------------------------------------------------------- house
def _e(eid: str, state: str, name: str = "", **attrs: Any) -> EntityInfo:
    return EntityInfo(entity_id=eid, state=state, name=name or eid, attributes=attrs)


def test_house_summary() -> None:
    h = house_summary([
        _e("light.a", "on"), _e("light.b", "off"), _e("light.c", "unavailable"),
        _e("sensor.t1", "20", device_class="temperature"),
        _e("sensor.t2", "22.5", device_class="temperature"),
        _e("sensor.t3", "unknown", device_class="temperature"),
        _e("sensor.t4", "nan", device_class="temperature"),
        _e("sensor.humidity", "55", device_class="humidity"),
        _e("lock.porte", "unlocked", "Porte"), _e("lock.garage", "locked", "Garage"),
        _e("binary_sensor.fuite", "on", "Fuite cuisine", device_class="moisture"),
        _e("binary_sensor.fenetre", "on", "Fenêtre", device_class="window"),
        _e("binary_sensor.mouvement", "on", "Couloir", device_class="motion"),
        _e("binary_sensor.fumee", "off", "Fumée", device_class="smoke"),
    ])  # fmt: skip
    assert (h["lights_on"], h["lights"]) == (1, 3)
    assert h["temp"] == 21.2  # mean of the numeric temperatures only
    assert h["unlocked"] == ["Porte"]
    assert h["alerts"] == ["Fuite cuisine"]
    assert h["opened"] == ["Fenêtre"]
    assert house_summary([])["temp"] is None


# --------------------------------------------------------------- status page
def test_status_page_dashboard(client: TestClient) -> None:  # noqa: F811
    import asyncio

    svc: Services = client.svc  # type: ignore[attr-defined]
    now = datetime.now(UTC).replace(tzinfo=None)

    async def seed() -> None:
        for i, (tier, ms) in enumerate(
            [("fast_path", 12), ("gpu", 340), ("gpu", 410), ("cpu", 90_000)]
        ):
            await svc.db.record_interaction(
                chat_id="1", tier=tier, backend="b", model="m", latency_ms=ms,
                created_at=now - timedelta(minutes=10 * i),
            )  # fmt: skip
        await svc.db.add_spend("cloud", 10, 10, 1.0)

    asyncio.new_event_loop().run_until_complete(seed())
    assert svc.ha is not None
    svc.ha.client.cache.entities["binary_sensor.fuite_cuisine"].state = "on"
    login(client)
    html = client.get("/").text
    assert html.count('<svg class="spark"') == 3
    assert 'class="donut"' in html and "réponses 24 h" in html
    assert re.search(r"<span data-count>4</span>", html)  # four replies in 24 h
    # p95 of the immediate answers only (the 90 s CPU job is excluded)
    assert re.search(r"<span data-count>40\d</span><small>ms</small>", html)
    # real budget ratio, not capped at 100 %, and the meter is marked as over
    pct = round(100 * 1.0 / svc.cfg.cloud.limits.daily_eur)
    assert f"{pct} % du plafond" in html and "dépassé" in html and 'class="meter over"' in html
    assert "⚠️ Fuite cuisine" in html and "💡" in html and "🔒 verrouillé" in html


def test_shell_palette_and_grafana_link(client: TestClient) -> None:  # noqa: F811
    svc: Services = client.svc  # type: ignore[attr-defined]
    login(client)
    html = client.get("/").text
    assert '<dialog id="palette"' in html and 'src="/static/ui.js"' in html
    assert 'filter id="liquid"' in html
    assert "Grafana" not in html
    svc.cfg.web.grafana_url = "http://127.0.0.1:3000"
    assert 'href="http://127.0.0.1:3000"' in client.get("/").text
    with pytest.raises(pydantic.ValidationError):
        svc.cfg.web.grafana_url = "javascript:alert(1)"
    # static assets stay public (login page needs them) and carry no secrets
    assert client.get("/static/ui.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


def test_ui_script_never_injects_html() -> None:
    raw = (STATIC / "ui.js").read_text(encoding="utf-8")
    js = re.sub(r"//[^\n]*", "", raw)  # comments may mention the rule itself
    assert "innerHTML" not in js and "insertAdjacentHTML" not in js and "eval(" not in js
    chat = (ROOT / "src" / "homelab_agent" / "web" / "templates" / "chat.html").read_text("utf-8")
    # the palette only pre-fills the chat: nothing is sent without the user pressing Enter
    prefill = chat[chat.index("const prefill") :]
    assert "post(" not in prefill.split("input.focus()")[0]


# ---------------------------------------------------------------- grafana
def test_grafana_dashboard_is_up_to_date() -> None:
    r = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "grafana_dashboard.py"), "--check"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert r.returncode == 0, r.stderr


def test_grafana_queries_use_existing_metrics() -> None:
    path = ROOT / "deploy" / "monitoring" / "grafana" / "dashboards" / "homelab-agent.json"
    dash = json.loads(path.read_text(encoding="utf-8"))
    exprs = [t["expr"] for p in dash["panels"] for t in p.get("targets", [])]
    assert len(exprs) >= 15
    used = {m for e in exprs for m in re.findall(r"\bhla_([a-z_]+)", e)}
    known = set(HELP) | {f"{n}_{s}" for n in HELP for s in ("bucket", "count", "sum")}
    assert used and used <= known, used - known
    uids = {
        t.get("datasource", {}).get("uid") for p in dash["panels"] for t in p.get("targets", [])
    }
    assert uids <= {"prometheus"}


# ----------------------------------------------------------- dashboard maths
def test_build_dashboard_is_pure_and_correct() -> None:
    from dataclasses import dataclass
    from zoneinfo import ZoneInfo

    from homelab_agent.web.dashboard import build_dashboard, greeting, latency_label

    @dataclass
    class Row:
        created_at: datetime
        tier: str
        latency_ms: int

    now = datetime(2026, 11, 5, 9, 30, tzinfo=UTC)
    naive = now.replace(tzinfo=None)
    rows = [
        Row(naive - timedelta(minutes=5), "gpu", 300),
        Row(naive - timedelta(minutes=50), "fast_path", 10),
        Row(naive - timedelta(hours=3), "cpu", 120_000),  # excluded from latency
        Row(naive - timedelta(hours=30), "gpu", 999),  # older than 24 h: clamped to hour 0
    ]
    spend = [(naive - timedelta(hours=2), 0.02), (naive - timedelta(minutes=1), 0.01)]
    d = build_dashboard(rows, spend, now=now, tz=ZoneInfo("Europe/Paris"), day_spend=0.03,
                        daily_limit=0.02, house=None)  # fmt: skip
    assert d["messages"] == 4
    assert d["greeting"] == "Bonjour" and d["date"] == "05/11/2026 · 10:30"  # local time
    assert [s[0] for s in d["segments"]] == ["chemin rapide", "rapide", "lourd"]  # tier order
    assert (d["p95_label"], d["p95_unit"]) == ("929", "ms")  # 300 + 0.9 × (999 − 300)
    assert d["budget_pct"] == 150  # not capped
    assert build_dashboard([], [], now=now, tz=UTC, day_spend=1, daily_limit=0,
                           house=None)["budget_pct"] == 0  # fmt: skip
    assert [greeting(h) for h in (2, 8, 15, 21)] == ["Bonne nuit", "Bonjour", "Bon après-midi",
                                                       "Bonsoir"]  # fmt: skip
    assert latency_label(1530) == ("1.5", "s") and latency_label(42) == ("42", "ms")


def test_login_limiter_forgets_old_failures() -> None:
    from homelab_agent.web.auth import LoginLimiter

    now = [0.0]
    lim = LoginLimiter(max_failures=2, window_s=10, clock=lambda: now[0])
    lim.failed("a")
    lim.failed("a")
    lim.failed("b")
    assert lim.blocked("a") and not lim.blocked("b")
    now[0] = 11
    assert not lim.blocked("a")
    assert lim._failures == {}  # stale clients are dropped, the dict does not grow forever
    lim.failed("a")
    lim.succeeded("a")
    assert not lim.blocked("a")


def test_invalid_form_values_are_client_errors(client: TestClient) -> None:  # noqa: F811
    login(client)
    assert client.post("/permissions/default", data={"policy": "bogus"}).status_code == 400
    r = client.post("/permissions/entity", data={"entity_id": "light.salon", "mode": "x"})
    assert r.status_code == 400
    assert client.post("/permissions/rule", data={"mode": "nope"}).status_code == 400
    assert client.post("/proactive/routine/soir/explode").status_code == 422
    assert client.post("/proactive/run/inconnue").status_code == 404
