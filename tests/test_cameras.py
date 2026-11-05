"""Camera snapshots: HA client, room resolution, privacy rules, /camera and the agent tool."""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from homelab_agent.config.models import TelegramSection
from homelab_agent.fakes.camera_images import FRAMES
from homelab_agent.fakes.ha import create_app as fake_ha_app
from homelab_agent.fakes.telegram import create_app as fake_tg
from homelab_agent.gateway.base import IncomingMessage, Replier, StreamHandle
from homelab_agent.gateway.telegram import TelegramFrontend
from homelab_agent.llm.base import Message
from homelab_agent.tools.homeassistant.cameras import (
    AUDIT_TOOL,
    CameraService,
    register_camera_tools,
)
from homelab_agent.tools.homeassistant.client import HAError, HomeAssistantClient

from .harness import Harness, build_harness
from .test_telegram import FakeBot

JPEG = b"\xff\xd8\xff"
ON = {"cameras": {"enabled": True}}


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


async def harness(tmp_path: Path, **ha: Any) -> tuple[Harness, CameraService, Clock]:
    h = await build_harness(tmp_path, home_assistant=ha)
    clock = Clock()
    cams = CameraService(h.ha, lambda: h.cfg_store.config, lambda: h.notifier, clock=clock)
    register_camera_tools(h.orchestrator.registry, cams)
    return h, cams, clock


# ------------------------------------------------------------------ client
def _client(handler: Any) -> HomeAssistantClient:
    return HomeAssistantClient("http://ha", "t", transport=httpx.MockTransport(handler))


async def test_client_snapshot_from_fake_ha() -> None:
    app = fake_ha_app()
    client = HomeAssistantClient("http://ha", "t", transport=httpx.ASGITransport(app=app))
    image, ctype = await client.camera_snapshot("camera.salon")
    assert image == FRAMES["camera.salon"] and image.startswith(JPEG) and ctype == "image/jpeg"
    assert app.state.ha.snapshots == ["camera.salon"]
    with pytest.raises(HAError, match="404"):
        await client.camera_snapshot("camera.garage")
    await client.aclose()


@pytest.mark.parametrize(
    ("response", "match"),
    [
        (httpx.Response(200, content=b"<html>", headers={"content-type": "text/html"}), "image"),
        (httpx.Response(200, content=b"", headers={"content-type": "image/jpeg"}), "empty"),
        (httpx.Response(401), "401"),
        (httpx.Response(500), "500"),
        (httpx.Response(200, content=b"x" * 50, headers={"content-type": "image/jpeg"}), "large"),
        (
            httpx.Response(
                200, content=b"x", headers={"content-type": "image/jpeg", "content-length": "999"}
            ),
            "large",
        ),
    ],
)
async def test_client_snapshot_rejects_bad_answers(response: httpx.Response, match: str) -> None:
    client = _client(lambda request: response)
    with pytest.raises(HAError, match=match):
        await client.camera_snapshot("camera.salon", max_bytes=10)
    await client.aclose()


async def test_client_snapshot_unreachable() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = _client(boom)
    with pytest.raises(HAError, match="unreachable"):
        await client.camera_snapshot("camera.salon")
    await client.aclose()


# -------------------------------------------------------------- resolution
async def test_resolve_by_room_name_or_entity(tmp_path: Path) -> None:
    h, cams, _ = await harness(tmp_path, **ON)
    ids = lambda q: [c.entity_id for c in cams.resolve(q)]  # noqa: E731
    assert ids("salon") == ids("Salon") == ids("la caméra du salon") == ["camera.salon"]
    assert ids("l'entrée") == ids("ENTREE") == ids("camera.entree") == ["camera.entree"]
    assert ids("montre moi le salon") == ["camera.salon"]
    assert ids("toutes") == ids("") == ids(None) == ids("tout l'appart") == [
        "camera.entree", "camera.salon",
    ]  # fmt: skip
    assert ids("garage") == []
    assert [label for label, _ in cams.rooms()] == ["Entrée", "Salon"]
    await h.close()


# ----------------------------------------------------------------- privacy
async def test_cameras_are_off_by_default(tmp_path: Path) -> None:
    h, cams, _ = await harness(tmp_path)
    assert not cams.section.enabled
    out = await cams.send("salon", chat_id="111", requester="111")
    assert not out.ok and out.denied and "désactivées" in (out.error or "")
    assert "ha_camera_snapshot" not in [t.name for t in h.orchestrator.registry.specs()]
    assert h.notifier.photos == [] and h.ha_app.state.ha.snapshots == []
    await h.close()


async def test_a_denied_camera_is_invisible(tmp_path: Path) -> None:
    rules = [{"entity": "camera.salon", "mode": "deny"}]
    h, cams, _ = await harness(tmp_path, rules=rules, **ON)
    assert [c.entity_id for c in cams.cameras()] == ["camera.entree"]
    out = await cams.send("camera.salon", chat_id="111", requester="111")
    assert out.denied and "permission" in (out.error or "")
    out = await cams.send("salon", chat_id="111", requester="111")
    assert not out.ok and "Entrée" in (out.error or "")  # lists the rooms one may see
    assert h.ha_app.state.ha.snapshots == []
    await h.close()


async def test_rate_limit_per_chat(tmp_path: Path) -> None:
    h, cams, clock = await harness(tmp_path, cameras={"enabled": True, "max_per_minute": 3})
    out = await cams.send("toutes", chat_id="111", requester="111")
    assert out.sent == ["Caméra entrée", "Caméra salon"]
    out = await cams.send("toutes", chat_id="111", requester="111")
    assert out.sent == ["Caméra entrée"] and "limite" in out.problems[0]
    out = await cams.send("salon", chat_id="111", requester="111")
    assert not out.ok and "minute" in (out.error or "")
    assert (await cams.send("salon", chat_id="222", requester="222")).ok  # other chat
    clock.t += 61
    assert (await cams.send("salon", chat_id="111", requester="111")).ok
    await h.close()


async def test_gateways_without_photos_get_a_text(tmp_path: Path) -> None:
    class TextOnly:
        async def send(self, text: str) -> None:
            """Text only: no send_photo."""

        def stream(self) -> StreamHandle:
            raise NotImplementedError

        async def confirm(self, question: str) -> bool:
            return False

    h, cams, _ = await harness(tmp_path, **ON)
    out = await cams.send("salon", chat_id="web:panel", requester="panel", replier=TextOnly())
    assert not out.ok and "Telegram" in (out.error or "")
    assert h.ha_app.state.ha.snapshots == []  # nothing fetched for nothing
    await h.close()


async def test_every_snapshot_is_audited(tmp_path: Path) -> None:
    h, cams, _ = await harness(tmp_path, **ON)
    h.ha_app.state.ha.states.pop("camera.entree")  # HA lost this camera: 404 on the proxy
    out = await cams.send("toutes", chat_id="111", requester="111")
    assert out.sent == ["Caméra salon"] and "Caméra entrée" in out.problems[0]
    entries = [e for e in await h.db.audit_entries() if e.tool == AUDIT_TOOL]
    got = sorted((json.loads(e.params)["entity_id"], e.outcome) for e in entries)
    assert got == [("camera.entree", "error"), ("camera.salon", "ok")]
    assert all(json.loads(e.params)["chat_id"] == "111" for e in entries)
    await h.close()


# ------------------------------------------------------------- /camera
def frontend(bot: FakeBot, cams: CameraService) -> TelegramFrontend:
    async def handler(msg: IncomingMessage, replier: Replier) -> None:
        await replier.send("agent")

    f = TelegramFrontend(bot, lambda: TelegramSection(allowed_user_ids=[111]), handler)
    f.commands.add("camera", "photo des caméras d'une pièce", cams.command)
    return f


async def test_camera_command_sends_photos_with_a_caption(tmp_path: Path) -> None:
    h, _, clock = await harness(tmp_path, **ON)
    bot = FakeBot()
    cams = CameraService(h.ha, lambda: h.cfg_store.config, lambda: f, clock=clock)
    f = frontend(bot, cams)
    await f.on_message(111, 111, "/camera salon")
    photos = [op[1] for op in bot.ops if op[0] == "photo"]
    assert len(photos) == 1
    chat, image, caption = photos[0]
    assert chat == "111" and image.startswith(JPEG) and caption.startswith("📷 Caméra salon · ")
    assert [op for op in bot.ops if op[0] == "send"] == []  # the photo is the answer
    await f.on_message(999, 999, "/camera salon")  # not allowlisted: silently ignored
    assert len([op for op in bot.ops if op[0] == "photo"]) == 1
    await h.close()


async def test_camera_command_without_room_offers_buttons(tmp_path: Path) -> None:
    h, _, clock = await harness(tmp_path, **ON)
    bot = FakeBot()
    cams = CameraService(h.ha, lambda: h.cfg_store.config, lambda: f, clock=clock)
    f = frontend(bot, cams)
    await f.on_message(111, 111, "/camera")
    send = next(op[1] for op in bot.ops if op[0] == "send")
    assert send[1] == "📷 Quelle pièce ?"
    labels = dict(send[2])
    assert set(labels) == {"📷 Entrée", "📷 Salon", "📷 Toutes"}
    await f.on_callback(111, 111, "cb1", labels["📷 Entrée"], message_id=1)
    photos = [op[1] for op in bot.ops if op[0] == "photo"]
    assert len(photos) == 1 and photos[0][2].startswith("📷 Caméra entrée")
    await h.close()


async def test_camera_command_when_disabled(tmp_path: Path) -> None:
    h, _, clock = await harness(tmp_path)
    bot = FakeBot()
    cams = CameraService(h.ha, lambda: h.cfg_store.config, lambda: f, clock=clock)
    f = frontend(bot, cams)
    await f.on_message(111, 111, "/camera salon")
    assert "désactivées" in bot.texts()[-1]
    assert not any(op[0] == "photo" for op in bot.ops)
    await h.close()


# --------------------------------------------------------------- agent tool
async def test_agent_tool_sends_the_photo_but_never_shows_it_to_the_model(
    tmp_path: Path,
) -> None:
    h, _, _ = await harness(tmp_path, **ON)
    backend = h.backends["gpu_fast"]
    seen: list[list[Message]] = []
    original = backend.stream

    def spy(messages: list[Message], tools: Any = None, **kw: Any) -> Any:
        seen.append(list(messages))
        return original(messages, tools, **kw)

    backend.stream = spy  # type: ignore[method-assign]
    replier = h.notifier.replier_for("111")
    msg = IncomingMessage("111", "111", '[[tool:ha_camera_snapshot {"room": "salon"}]]')
    await h.orchestrator.handle(msg, replier)
    assert [(c, cap.split(" · ")[0]) for c, _, cap in h.notifier.photos] == [
        ("111", "📷 Caméra salon")
    ]
    assert h.notifier.replies[-1] == "C'est fait (fast)."
    everything = json.dumps([[m.content for m in batch] for batch in seen], ensure_ascii=False)
    b64 = base64.b64encode(FRAMES["camera.salon"]).decode()
    assert b64[:40] not in everything and "\\u00ff\\u00d8" not in everything
    tool_msgs = [m for m in seen[-1] if m.role == "tool"]
    assert len(tool_msgs) == 1 and "photos_sent_to_user" in tool_msgs[0].content
    assert len(tool_msgs[0].content) < 600
    assert "ha_camera_snapshot" in seen[0][0].content  # system prompt explains the tool
    entries = [e for e in await h.db.audit_entries() if e.tool == AUDIT_TOOL]
    assert len(entries) == 1 and entries[0].tier == "gpu" and entries[0].model == "fast"
    await h.close()


async def test_agent_tool_absent_when_cameras_are_off(tmp_path: Path) -> None:
    h, _, _ = await harness(tmp_path)
    replier = h.notifier.replier_for("111")
    msg = IncomingMessage("111", "111", '[[tool:ha_camera_snapshot {"room": "salon"}]]')
    await h.orchestrator.handle(msg, replier)
    assert h.notifier.photos == [] and h.ha_app.state.ha.snapshots == []
    assert h.notifier.replies[-1].startswith("Réponse simulée")
    await h.close()


async def test_model_tool_switch(tmp_path: Path) -> None:
    h, _, _ = await harness(tmp_path, cameras={"enabled": True, "model_tool": False})
    assert "ha_camera_snapshot" not in [t.name for t in h.orchestrator.registry.specs()]
    await h.close()


# ------------------------------------------------------------- aiogram
async def test_aiogram_send_photo(run_server: Any) -> None:
    from homelab_agent.gateway.aiogram_adapter import TelegramGateway

    tg = run_server(fake_tg())
    gw = TelegramGateway("123456:TEST", api_base=tg.url)
    try:
        await gw.api.send_photo("111", FRAMES["camera.salon"], "📷 Caméra salon · 12:00")
        async with httpx.AsyncClient(base_url=tg.url) as c:
            sent = (await c.get("/_test/sent")).json()
        assert sent[-1]["op"] == "photo" and sent[-1]["caption"] == "📷 Caméra salon · 12:00"
        assert sent[-1]["photo_is_jpeg"] and sent[-1]["photo_size"] == len(FRAMES["camera.salon"])
    finally:
        await gw.bot.session.close()
