"""Home Assistant REST + WebSocket client, with a live entity cache."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import websockets

from homelab_agent.tools.homeassistant.catalog import ServiceCatalog

log = logging.getLogger(__name__)

REGISTRY_EVENTS = (
    "area_registry_updated",
    "entity_registry_updated",
    "device_registry_updated",
)

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f  ]")


def sanitize_label(text: str, max_len: int = 60) -> str:
    """Make an untrusted label (entity name...) safe to embed in a prompt as data."""
    text = unicodedata.normalize("NFKC", text)
    text = _CONTROL_CHARS.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    text = text.replace('"', "'").replace("`", "'")
    if len(text) > max_len:
        text = text[: max_len - 1] + "…"
    return text


class HAError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status  # HTTP status when Home Assistant answered with an error


@dataclass(slots=True)
class EntityInfo:
    entity_id: str
    state: str
    name: str
    area_id: str | None = None
    area_name: str | None = None
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def domain(self) -> str:
        return self.entity_id.split(".", 1)[0]

    @property
    def areas(self) -> set[str]:
        return {a for a in (self.area_id, self.area_name) if a}


class EntityCache:
    """Entities with their areas, refreshed by the WebSocket subscription."""

    def __init__(self) -> None:
        self.entities: dict[str, EntityInfo] = {}
        self.areas: dict[str, str] = {}  # area_id -> name
        self._entity_area: dict[str, str] = {}  # entity_id -> area_id
        self.updated_at: datetime | None = None
        #: called with (old, new) on every live state change (not on full loads)
        self.listeners: list[Callable[[EntityInfo | None, EntityInfo | None], None]] = []

    def load(
        self,
        states: list[dict[str, Any]],
        areas: list[dict[str, Any]] | None = None,
        entity_registry: list[dict[str, Any]] | None = None,
        device_registry: list[dict[str, Any]] | None = None,
    ) -> None:
        if areas is not None:
            self.areas = {a["area_id"]: a.get("name") or a["area_id"] for a in areas}
        if entity_registry is not None:
            device_area = {
                d["id"]: d.get("area_id") for d in device_registry or [] if d.get("area_id")
            }
            self._entity_area = {}
            for e in entity_registry:
                area = e.get("area_id") or device_area.get(e.get("device_id") or "")
                if area:
                    self._entity_area[e["entity_id"]] = area
        self.entities = {}
        for s in states:
            self.apply_state(s, notify=False)
        self.updated_at = datetime.now(UTC)

    def apply_state(
        self, s: dict[str, Any] | None, entity_id: str | None = None, *, notify: bool = True
    ) -> None:
        if s is None:  # entity removed
            if entity_id:
                old_removed = self.entities.pop(entity_id, None)
                if notify and old_removed is not None:
                    self._notify(old_removed, None)
            return
        eid = s["entity_id"]
        old = self.entities.get(eid)
        attrs = s.get("attributes") or {}
        area_id = self._entity_area.get(eid)
        self.entities[eid] = EntityInfo(
            entity_id=eid,
            state=str(s.get("state", "unknown")),
            name=str(attrs.get("friendly_name") or eid),
            area_id=area_id,
            area_name=self.areas.get(area_id) if area_id else None,
            attributes={
                k: v
                for k, v in attrs.items()
                if k
                in (
                    "unit_of_measurement", "device_class", "brightness", "current_temperature",
                    "temperature", "current_position", "hvac_action", "media_title",
                )
            },
        )  # fmt: skip
        self.updated_at = datetime.now(UTC)
        if notify:
            self._notify(old, self.entities[eid])

    def _notify(self, old: EntityInfo | None, new: EntityInfo | None) -> None:
        for listener in list(self.listeners):
            try:
                listener(old, new)
            except Exception:  # a broken listener must not break the cache
                log.exception("entity listener failed")

    def entities_in_area(self, area: str) -> list[str]:
        key = area.casefold()
        return [
            e.entity_id for e in self.entities.values() if key in {a.casefold() for a in e.areas}
        ]


def _error_text(resp: httpx.Response) -> str:
    """Home Assistant's own message (``{"message": …}``) or the start of the body."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("message"), str):
        return sanitize_label(body["message"], 300)
    return sanitize_label(resp.text, 200)


class HomeAssistantClient:
    def __init__(
        self,
        url: str,
        token: str | None,
        *,
        timeout_s: float = 10,
        verify_ssl: bool = True,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.token = token
        self.cache = EntityCache()
        self.services = ServiceCatalog()
        self.connected = False
        self._http = httpx.AsyncClient(
            base_url=self.url,
            headers={"Authorization": f"Bearer {token}"} if token else {},
            timeout=timeout_s,
            verify=verify_ssl,
            transport=transport,
        )
        self._ws_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    # ------------------------------------------------------------------ REST
    async def _request(self, method: str, path: str, **kw: Any) -> Any:
        try:
            resp = await self._http.request(method, path, **kw)
        except httpx.HTTPError as exc:
            raise HAError(f"Home Assistant unreachable: {exc.__class__.__name__}") from exc
        if resp.status_code == 401:
            raise HAError("Home Assistant rejected the token (401)", 401)
        if resp.status_code >= 400:
            raise HAError(
                f"Home Assistant HTTP {resp.status_code}: {_error_text(resp)}", resp.status_code
            )
        return resp.json() if resp.content else None

    async def ping(self) -> bool:
        try:
            await self._request("GET", "/api/")
        except HAError:
            return False
        return True

    async def get_states(self) -> list[dict[str, Any]]:
        data = await self._request("GET", "/api/states")
        return list(data or [])

    async def get_state(self, entity_id: str) -> dict[str, Any]:
        return dict(await self._request("GET", f"/api/states/{entity_id}"))

    async def call_service(
        self, domain: str, service: str, data: dict[str, Any]
    ) -> list[dict[str, Any]]:
        result = await self._request("POST", f"/api/services/{domain}/{service}", json=data)
        return list(result or []) if isinstance(result, list) else []

    async def history(self, entity_id: str, hours: int) -> list[dict[str, Any]]:
        start = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
        data = await self._request(
            "GET",
            f"/api/history/period/{start}",
            params={"filter_entity_id": entity_id, "minimal_response": "", "no_attributes": ""},
        )
        return list(data[0]) if data else []

    async def camera_snapshot(
        self, entity_id: str, *, max_bytes: int = 8_000_000, timeout_s: float | None = None
    ) -> tuple[bytes, str]:
        """Current image of a camera (``/api/camera_proxy``): (bytes, content type)."""
        timeout: Any = timeout_s if timeout_s is not None else httpx.USE_CLIENT_DEFAULT
        try:
            async with self._http.stream(
                "GET", f"/api/camera_proxy/{entity_id}", timeout=timeout
            ) as resp:
                if resp.status_code == 401:
                    raise HAError("Home Assistant rejected the token (401)")
                if resp.status_code >= 400:
                    raise HAError(f"Home Assistant HTTP {resp.status_code} for {entity_id}")
                ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                if not ctype.startswith("image/"):
                    raise HAError(f"{entity_id} did not return an image ({ctype or 'no type'})")
                declared = resp.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > max_bytes:
                    raise HAError(f"{entity_id}: image too large ({int(declared)} bytes)")
                data = bytearray()
                async for chunk in resp.aiter_bytes():
                    data += chunk
                    if len(data) > max_bytes:
                        raise HAError(f"{entity_id}: image too large (> {max_bytes} bytes)")
        except httpx.HTTPError as exc:
            raise HAError(f"Home Assistant unreachable: {exc.__class__.__name__}") from exc
        if not data:
            raise HAError(f"{entity_id}: empty image")
        return bytes(data), ctype

    async def get_services(self) -> Any:
        return await self._request("GET", "/api/services")

    async def refresh_services(self) -> None:
        """Reload the service catalogue; keep the previous one if Home Assistant fails."""
        try:
            self.services.load(await self.get_services())
        except HAError as exc:
            log.warning("home assistant services not loaded: %s", exc)
            return
        log.info("home assistant services: %d domains", len(self.services.domains))

    async def refresh_cache_rest(self) -> None:
        self.cache.load(await self.get_states())
        await self.refresh_services()

    # ------------------------------------------------------------- WebSocket
    @property
    def ws_url(self) -> str:
        base = self.url.replace("https://", "wss://").replace("http://", "ws://")
        return f"{base}/api/websocket"

    async def _ws_session(self) -> None:
        async with websockets.connect(self.ws_url, max_size=16 * 1024 * 1024) as ws:
            first = json.loads(await ws.recv())
            if first.get("type") != "auth_required":
                raise HAError("unexpected websocket handshake")
            await ws.send(json.dumps({"type": "auth", "access_token": self.token or ""}))
            auth = json.loads(await ws.recv())
            if auth.get("type") != "auth_ok":
                raise HAError("websocket authentication failed")
            msg_id = 0

            async def command(payload: dict[str, Any]) -> Any:
                nonlocal msg_id
                msg_id += 1
                await ws.send(json.dumps({"id": msg_id, **payload}))
                while True:
                    reply = json.loads(await ws.recv())
                    if reply.get("id") == msg_id and reply.get("type") == "result":
                        if not reply.get("success"):
                            raise HAError(f"websocket command failed: {reply.get('error')}")
                        return reply.get("result")

            areas = await command({"type": "config/area_registry/list"})
            entities = await command({"type": "config/entity_registry/list"})
            devices = await command({"type": "config/device_registry/list"})
            states = await command({"type": "get_states"})
            self.cache.load(states, areas, entities, devices)
            await self.refresh_services()  # integrations may have changed while disconnected
            await command({"type": "subscribe_events", "event_type": "state_changed"})
            for registry in REGISTRY_EVENTS:
                await command({"type": "subscribe_events", "event_type": registry})
            self.connected = True
            log.info("home assistant websocket connected (%d entities)", len(self.cache.entities))
            async for raw in ws:
                if self._stop.is_set():
                    break
                msg = json.loads(raw)
                if msg.get("type") == "event":
                    event = msg["event"]
                    if event.get("event_type") in REGISTRY_EVENTS:
                        # areas / entities / devices changed: reload everything
                        log.info("home assistant %s: refreshing", event.get("event_type"))
                        return
                    data = event.get("data", {})
                    self.cache.apply_state(data.get("new_state"), data.get("entity_id"))

    async def _ws_loop(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            try:
                await self._ws_session()
                delay = 0.5  # session ended on purpose (registry refresh): reconnect quickly
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                log.warning("home assistant websocket error: %s (retry in %.0fs)", exc, delay)
                with contextlib.suppress(HAError):
                    if not self.cache.entities:
                        await self.refresh_cache_rest()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            delay = min(delay * 2, 60)

    def start(self) -> None:
        if self._ws_task is None:
            self._ws_task = asyncio.create_task(self._ws_loop(), name="ha-websocket")

    async def aclose(self) -> None:
        self._stop.set()
        if self._ws_task:
            self._ws_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._ws_task
        await self._http.aclose()
