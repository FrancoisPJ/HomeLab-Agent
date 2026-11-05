from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from prometheus_client.parser import text_string_to_metric_families

from homelab_agent.observability.metrics import Metrics
from homelab_agent.web.app import create_app

from .test_web import PASSWORD, services


def families(text: str) -> dict[str, Any]:
    return {f.name: f for f in text_string_to_metric_families(text)}


async def test_counters_histograms_and_gauges_parse_as_prometheus() -> None:
    m = Metrics(buckets=(100, 1000))
    m.inc("replies_total", tier="gpu")
    m.inc("replies_total", 2, tier="cpu")
    for v in (50, 150, 5000):
        m.observe("latency_ms", v, tier="gpu")

    async def gauges() -> list[tuple[str, dict[str, str], float]]:
        return [("backend_up", {"backend": 'we"ird\\name', "tier": "gpu"}, 1.0)]

    async def broken() -> list[tuple[str, dict[str, str], float]]:
        raise RuntimeError("boom")

    fams = families(await m.render([gauges, broken]))
    replies = {s.labels["tier"]: s.value for s in fams["hla_replies"].samples}
    assert replies == {"gpu": 1.0, "cpu": 2.0}
    assert fams["hla_replies"].documentation.startswith("Answers produced")
    hist = fams["hla_latency_ms"]
    assert hist.type == "histogram"
    buckets = {float(s.labels["le"]): s.value for s in hist.samples if s.name.endswith("_bucket")}
    assert buckets == {100.0: 1, 1000.0: 2, float("inf"): 3}  # cumulative
    total = next(s.value for s in hist.samples if s.name.endswith("_sum"))
    assert total == 5200
    up = fams["hla_backend_up"].samples[0]
    assert up.labels["backend"] == 'we"ird\\name' and up.value == 1.0  # escaping round-trips
    assert m.histogram_count("latency_ms", tier="gpu") == 3


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("PANEL_PASSWORD", PASSWORD)
    monkeypatch.setenv("METRICS_TOKEN", "prometheus-scrape-token-0123")
    svc = asyncio.new_event_loop().run_until_complete(services(tmp_path))
    with TestClient(create_app(svc)) as c:
        yield c


def test_metrics_endpoint_and_scrape_token(client: TestClient) -> None:
    token = {"Authorization": "Bearer prometheus-scrape-token-0123"}
    r = client.get("/metrics", headers=token)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain")
    fams = families(r.text)
    assert {
        "hla_build_info",
        "hla_backend_up",
        "hla_jobs",
        "hla_cloud_spend_eur",
        "hla_home_assistant_entities",
    } <= set(fams)
    jobs = {s.labels["status"] for s in fams["hla_jobs"].samples}
    assert jobs == {"queued", "running", "done", "failed"}
    # the scrape token grants /metrics only
    assert client.get("/api/status", headers=token).status_code == 401
    assert client.get("/", headers=token, follow_redirects=False).status_code == 303
    assert client.get("/metrics", headers={"Authorization": "Bearer nope"}).status_code == 401
