"""End-to-end scenario against `docker compose --profile dev` (fake LLMs, HA and Telegram).

Run with `make e2e` (sets HLA_E2E=1). Skipped otherwise.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx
import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(os.environ.get("HLA_E2E") != "1", reason="needs the dev compose stack"),
]

TG = os.environ.get("E2E_TELEGRAM", "http://127.0.0.1:8081")
HA = os.environ.get("E2E_HA", "http://127.0.0.1:8123")
LLM_GPU = os.environ.get("E2E_LLM_GPU", "http://127.0.0.1:18080")
PANEL = os.environ.get("E2E_PANEL", "http://127.0.0.1:8099")
USER = 111


def client() -> httpx.Client:
    return httpx.Client(timeout=10)


def send(text: str, user: int = USER) -> int:
    with client() as c:
        before = len(c.get(f"{TG}/_test/sent").json())
        c.post(f"{TG}/_test/message", json={"user_id": user, "text": text}).raise_for_status()
    return before


def wait_text(pred: Any, since: int = 0, timeout: float = 30) -> list[dict[str, Any]]:
    deadline = time.time() + timeout
    with client() as c:
        while time.time() < deadline:
            sent = c.get(f"{TG}/_test/sent").json()[since:]
            if any(pred(m["text"]) for m in sent):
                return sent
            time.sleep(0.3)
    raise AssertionError(f"no matching Telegram message; got: {[m['text'] for m in sent]}")


def ha_calls() -> list[dict[str, Any]]:
    with client() as c:
        return c.get(f"{HA}/_test/calls").json()


@pytest.fixture(autouse=True, scope="module")
def stack_ready() -> None:
    deadline = time.time() + 90
    while time.time() < deadline:
        try:
            with client() as c:
                if c.get(f"{PANEL}/healthz").status_code == 200:
                    status = c.get(
                        f"{PANEL}/api/status", headers={"Authorization": "Bearer dev-password-123"}
                    ).json()
                    if status["home_assistant"]:
                        return
        except httpx.HTTPError:
            pass
        time.sleep(1)
    raise AssertionError("dev stack not ready")


def test_1_unknown_user_is_ignored() -> None:
    since = send("bonjour", user=999)
    time.sleep(2)
    with client() as c:
        assert c.get(f"{TG}/_test/sent").json()[since:] == []


def test_2_gpu_route_then_home_assistant_tool_then_answer() -> None:
    n = len(ha_calls())
    since = send("peux-tu éteindre la cuisine")
    wait_text(lambda t: "C'est fait" in t, since)
    calls = ha_calls()[n:]
    assert calls == [
        {"domain": "light", "service": "turn_off", "data": {"entity_id": ["light.cuisine"]}}
    ]


def test_3_fast_path() -> None:
    since = send("éteins le salon")
    wait_text(lambda t: t.startswith("✅ light.turn_off"), since)


def test_4_complex_message_becomes_cpu_job() -> None:
    since = send("/deep rédige un poème sur le chauffage")
    wait_text(lambda t: "Je m'en occupe" in t, since)
    wait_text(lambda t: "terminée" in t and "(heavy)" in t, since, timeout=60)


def test_5_confirmation_flow_for_locks() -> None:
    n = len(ha_calls())
    since = send(
        '[[tool:ha_call_service {"domain": "lock", "service": "unlock", "entity_id": "lock.porte_entree"}]]'
    )
    sent = wait_text(lambda t: t.startswith("Confirmer"), since)
    question = next(m for m in sent if m["text"].startswith("Confirmer"))
    yes = question["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    with client() as c:
        c.post(
            f"{TG}/_test/callback",
            json={"user_id": USER, "message_id": question["message_id"], "data": yes},
        )
    wait_text(lambda t: "C'est fait" in t, since)
    assert ha_calls()[n:][0]["service"] == "unlock"


def test_6_cloud_cap_then_fallback() -> None:
    since = send("/cloud première question cloud")
    sent = wait_text(lambda t: "(cloud)" in t or "bascule" in t, since)
    if any("(cloud)" in m["text"] for m in sent):
        # fresh stack: the first answer costs more than the cap, a budget alert follows it
        wait_text(lambda t: "Budget cloud" in t, since)
    # (on a stack that already spent its budget, the first request falls back directly)
    since = send("/cloud deuxième question cloud")
    sent = wait_text(lambda t: "bascule" in t, since)
    fallback = next(m["text"] for m in sent if "bascule" in m["text"])
    assert "plafond" in fallback


def test_7_gpu_outage_falls_back() -> None:
    with client() as c:
        c.post(f"{LLM_GPU}/_control", json={"fail": True}).raise_for_status()
    try:
        since = send("dis-moi bonjour")
        wait_text(lambda t: "tâche #" in t or "bascule" in t, since)
    finally:
        with client() as c:
            c.post(f"{LLM_GPU}/_control", json={"fail": False})


def test_8_audit_visible_in_panel() -> None:
    with client() as c:
        page = c.get(f"{PANEL}/audit", headers={"Authorization": "Bearer dev-password-123"})
    assert page.status_code == 200
    assert "ha_call_service" in page.text and "lock" in page.text


def test_9_leak_alert_with_action_button() -> None:
    with client() as c:  # make sure the sensor goes off -> on (the alert fires on the change)
        c.post(
            f"{HA}/_test/state", json={"entity_id": "binary_sensor.fuite_cuisine", "state": "off"}
        )
    time.sleep(0.5)
    n = len(ha_calls())
    with client() as c:
        before = len(c.get(f"{TG}/_test/sent").json())
        c.post(
            f"{HA}/_test/state", json={"entity_id": "binary_sensor.fuite_cuisine", "state": "on"}
        ).raise_for_status()
    sent = wait_text(lambda t: t.startswith("🚨 Fuite détectée"), before)
    alert = next(m for m in sent if m["text"].startswith("🚨 Fuite détectée"))
    assert "Fuite cuisine (Cuisine)" in alert["text"]
    button = alert["reply_markup"]["inline_keyboard"][0][0]
    assert button["text"] == "Couper la prise"
    with client() as c:
        c.post(
            f"{TG}/_test/callback",
            json={
                "user_id": USER,
                "message_id": alert["message_id"],
                "data": button["callback_data"],
            },
        )
    wait_text(lambda t: t.startswith("✅ Couper la prise"), before)
    assert ha_calls()[n:][-1] == {
        "domain": "switch",
        "service": "turn_off",
        "data": {"entity_id": ["switch.prise_bureau"]},
    }


def test_10_routines_commands() -> None:
    since = send("/routines")
    wait_text(lambda t: "soir" in t and "prochaine" in t, since)
    with client() as c:  # make sure something is on, then run the evening check now
        c.post(f"{HA}/_test/state", json={"entity_id": "light.salon", "state": "on"})
    since = send("/run soir")
    wait_text(lambda t: t.startswith("🔎 soir") and "Lumière salon" in t, since)


def test_11_voice_note_drives_the_house() -> None:
    n = len(ha_calls())
    with client() as c:
        since = len(c.get(f"{TG}/_test/sent").json())
        c.post(
            f"{TG}/_test/voice", json={"user_id": USER, "text": "Éteins le salon"}
        ).raise_for_status()
    wait_text(lambda t: t == "🎤 « Éteins le salon »", since)
    wait_text(lambda t: t.startswith("✅ light.turn_off"), since)
    assert ha_calls()[n:][0]["service"] == "turn_off"


def test_12_create_a_reminder_from_the_chat() -> None:
    since = send("/routine delete sortir_les_poubelles")  # leftover of an interrupted run
    wait_text(lambda t: "supprimée" in t or "inconnue" in t, since)
    args = '{"what": "reminder", "at": "20h", "days": "mardi", "text": "Sortir les poubelles"}'
    since = send(f"[[tool:routine_create {args}]]")
    sent = wait_text(lambda t: t.startswith("Créer la routine"), since)
    question = next(m for m in sent if m["text"].startswith("Créer la routine"))
    assert "mar à 20:00" in question["text"]
    yes = question["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    with client() as c:
        c.post(
            f"{TG}/_test/callback",
            json={"user_id": USER, "message_id": question["message_id"], "data": yes},
        )
    wait_text(lambda t: "C'est fait" in t, since)
    since = send("/routines")
    wait_text(lambda t: "sortir_les_poubelles" in t and "prochaine : mar" in t.lower(), since)
    since = send("/run sortir_les_poubelles")
    wait_text(lambda t: t == "⏰ Sortir les poubelles", since)
    since = send("/routine delete sortir_les_poubelles")
    wait_text(lambda t: "supprimée" in t, since)
