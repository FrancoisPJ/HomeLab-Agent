from __future__ import annotations

from typing import Any

import httpx
import pytest

from homelab_agent.fakes.ha import create_app
from homelab_agent.tools.homeassistant.client import HAError, HomeAssistantClient

from .conftest import wait_for


async def test_rest_calls(ha_app: Any) -> None:
    c = HomeAssistantClient("http://ha", "tok", transport=httpx.ASGITransport(app=ha_app))
    assert await c.ping()
    states = await c.get_states()
    assert any(s["entity_id"] == "light.salon" for s in states)
    assert (await c.get_state("sensor.temperature_salon"))["state"] == "21.5"
    changed = await c.call_service("light", "turn_off", {"entity_id": ["light.salon"]})
    assert changed[0]["state"] == "off"
    assert (await c.history("light.salon", 2))[0]["state"] == "off"
    await c.refresh_cache_rest()
    assert "light.salon" in c.cache.entities
    await c.aclose()


async def test_bad_token_and_unreachable(ha_app: Any) -> None:
    c = HomeAssistantClient("http://ha", None, transport=httpx.ASGITransport(app=ha_app))
    with pytest.raises(HAError, match="401"):
        await c.get_states()
    assert not await c.ping()

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no", request=request)

    c2 = HomeAssistantClient("http://ha", "t", transport=httpx.MockTransport(refuse))
    with pytest.raises(HAError, match="unreachable"):
        await c2.get_states()


async def test_websocket_cache_and_live_updates(run_server: Any) -> None:
    app = create_app()
    srv = run_server(app)
    c = HomeAssistantClient(srv.url, "tok")
    c.start()
    await wait_for(lambda: c.connected, timeout=10)
    salon = c.cache.entities["light.salon"]
    assert salon.area_name == "Salon" and salon.state == "on"
    assert set(c.cache.entities_in_area("salon")) >= {"light.salon", "sensor.temperature_salon"}
    await c.call_service("light", "turn_off", {"entity_id": "light.salon"})
    c.cache.entities["light.salon"].state = "stale"  # prove the websocket event refreshes it
    await c.call_service("light", "turn_on", {"entity_id": "light.salon"})
    await wait_for(lambda: c.cache.entities["light.salon"].state == "on", timeout=5)
    await c.aclose()


async def test_websocket_rejects_bad_token(run_server: Any) -> None:
    srv = run_server(create_app())
    c = HomeAssistantClient(srv.url, "")
    with pytest.raises(HAError):
        await c._ws_session()
    await c.aclose()
