from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from homelab_agent.web.app import create_app
from homelab_agent.web.chat import RoutingNotifier, WebChatHub

from .harness import RecordingNotifier
from .test_web import PASSWORD, login, services


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("PANEL_PASSWORD", PASSWORD)
    svc = asyncio.new_event_loop().run_until_complete(services(tmp_path))
    with TestClient(create_app(svc)) as c:
        c.svc = svc  # type: ignore[attr-defined]
        login(c)
        yield c


def events(c: TestClient, turn: str, until: str = "done") -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    with c.stream("GET", f"/chat/events/{turn}") as r:
        assert r.headers["content-type"].startswith("text/event-stream")
        for line in r.iter_lines():
            if not line.startswith("data: "):
                continue
            ev = json.loads(line[6:])
            out.append(ev)
            if ev["type"] == until:
                break
    return out


def test_web_chat_has_the_same_commands_as_telegram(client: TestClient) -> None:
    def ask(text: str) -> list[str]:
        turn = client.post("/chat/send", data={"text": text}).json()["turn"]
        return [e.get("text", "") for e in events(client, turn) if e["type"] == "message"]

    assert "/routines" in ask("/help")[0] and "/maison" not in ask("/help")[0]  # Telegram only
    assert ask("/status")[0].startswith("État des moteurs")
    assert ask("/inconnue") == ["Commande inconnue. /help pour la liste."]
    assert ask("/cloud") == ["Usage : /cloud <ta demande>"]
    assert ask("/reset") == ["🧹 Contexte effacé."]
    # forced tier: the argument goes to the agent, on the CPU queue for /deep
    assert any("tâche #" in t for t in ask("/deep  rédige un poème"))


def test_chat_page_requires_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PANEL_PASSWORD", PASSWORD)
    svc = asyncio.new_event_loop().run_until_complete(services(tmp_path))
    with TestClient(create_app(svc)) as c:
        assert c.get("/chat", follow_redirects=False).status_code == 303
        assert c.post("/chat/send", data={"text": "x"}, follow_redirects=False).status_code == 303


def test_chat_streams_an_answer(client: TestClient) -> None:
    page = client.get("/chat")
    assert page.status_code == 200 and "textContent" in page.text  # untrusted text never as HTML
    r = client.post("/chat/send", data={"text": "bonjour le panneau"})
    turn = r.json()["turn"]
    evs = events(client, turn)
    texts = [e.get("text", "") for e in evs]
    assert any("Réponse simulée (fast) : bonjour le panneau" in t for t in texts)
    assert evs[-1]["type"] == "done"
    assert client.post("/chat/send", data={"text": "  "}).status_code == 400
    assert client.get("/chat/events/nope").status_code == 404


def test_chat_confirmation_buttons(client: TestClient) -> None:
    directive = '[[tool:ha_call_service {"domain": "lock", "service": "unlock", "entity_id": "lock.porte_entree"}]]'
    turn = client.post("/chat/send", data={"text": directive}).json()["turn"]
    first = events(client, turn, until="confirm")
    question = first[-1]
    assert question["type"] == "confirm" and "Porte d'entrée" in question["question"]
    assert client.post(f"/chat/confirm/{question['token']}", data={"yes": "1"}).json() == {
        "ok": True
    }
    rest = events(client, turn)
    assert any("C'est fait" in e.get("text", "") for e in rest)
    assert client.post(f"/chat/confirm/{question['token']}", data={"yes": "1"}).json() == {
        "ok": False
    }


def test_chat_reset(client: TestClient) -> None:
    assert "Contexte effacé" in client.post("/chat/reset").json()["text"]


async def test_routing_notifier_and_inbox() -> None:
    hub = WebChatHub()
    telegram = RecordingNotifier()
    notifier = RoutingNotifier(telegram, hub)
    await notifier.notify("111", "pour telegram")
    await notifier.notify("web:panel", "pour le web")
    assert telegram.sent == [("111", "pour telegram")]
    assert hub.inbox_backlog[-1]["text"] == "pour le web"
    replier = notifier.replier_for("web:panel")
    await replier.send("via replier")
    assert hub.inbox_backlog[-1]["text"] == "via replier"

    from homelab_agent.gateway.base import ActionButton

    async def act(user: str, r: Any) -> str:
        return f"fait par {user}"

    await notifier.notify_actions("web:panel", "alerte", [ActionButton("Agir", act)], 60)
    token = hub.inbox_backlog[-1]["actions"][0]["token"]
    assert await hub.run_action(token) == "fait par panel"
    assert await hub.run_action(token) == "Bouton expiré."
