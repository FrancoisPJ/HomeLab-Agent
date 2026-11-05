from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from homelab_agent.config.models import Routine, Tier
from homelab_agent.core.routines import describe, parse_date, parse_days, parse_time, slug
from homelab_agent.gateway.base import BufferedReplier, IncomingMessage

from .harness import Harness, build_harness


def test_parse_days() -> None:
    assert parse_days(None) == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    assert parse_days("tous les jours") == parse_days(None)
    assert parse_days("semaine") == ["mon", "tue", "wed", "thu", "fri"]
    assert parse_days("Week-end") == ["sat", "sun"]
    assert parse_days("mardi") == ["tue"]
    assert parse_days("lun, mer et ven") == ["mon", "wed", "fri"]
    assert parse_days(["sun", "Samedi"]) == ["sat", "sun"]
    with pytest.raises(ValueError, match="jour inconnu"):
        parse_days("bientôt")


def test_parse_time_and_date() -> None:
    assert parse_time("23:00") == parse_time("23h") == "23:00"
    assert parse_time("7h30") == parse_time("07.30") == parse_time("0730") == "07:30"
    with pytest.raises(ValueError):
        parse_time("25h")
    with pytest.raises(ValueError):
        parse_time("midi")
    today = date(2026, 11, 5)
    assert parse_date("demain", today) == "2026-11-06"
    assert parse_date("aujourd'hui", today) == "2026-11-05"
    assert parse_date("2026-12-24", today) == "2026-12-24"
    assert parse_date(None, today) is None
    with pytest.raises(ValueError):
        parse_date("noël", today)


def test_slug_and_describe() -> None:
    assert slug("Sortir les poubelles !") == "sortir_les_poubelles"
    assert slug("!!!", "rappel") == "rappel"
    r = Routine(name="poubelles", at="20:00", days=["tue"], message="Sortir les poubelles")
    assert describe(r) == "poubelles : rappel « Sortir les poubelles », mar à 20:00"
    once = Routine(name="rdv", at="08:00", date="2026-11-06", message="Dentiste")
    assert "le 2026-11-06 à 08:00" in describe(once)


def msg(text: str) -> IncomingMessage:
    return IncomingMessage(user_id="111", chat_id="111", text=text)


def tool(args: dict[str, object], name: str = "routine_create") -> str:
    return f"[[tool:{name} {json.dumps(args, ensure_ascii=False)}]]"


async def harness(tmp_path: Path) -> Harness:
    return await build_harness(tmp_path, routing={
        "tiers": {"gpu": ["gpu_fast"], "cpu": ["cpu_heavy"], "cloud": ["cloud"]}, "fast_path": False,
    })  # fmt: skip


async def test_create_reminder_from_chat_with_confirmation(tmp_path: Path) -> None:
    h = await harness(tmp_path)
    r = BufferedReplier(auto_confirm=True)
    args = {"what": "reminder", "at": "20h", "days": "mardi", "text": "Sortir les poubelles"}
    await h.orchestrator.handle(msg(tool(args)), r)
    assert r.questions == [
        "Créer la routine sortir_les_poubelles : rappel « Sortir les poubelles », mar à 20:00 ?"
    ]
    routine = h.routines.get("sortir_les_poubelles")
    assert routine is not None and routine.message == "Sortir les poubelles"
    assert routine.chat_ids == [111] and routine.created_by == "111"
    cfg = h.cfg_store.config
    assert cfg.proactive.enabled  # turned on so that the routine actually runs
    text = h.cfg_store.path.read_text(encoding="utf-8")
    assert "sortir_les_poubelles" in text and "Sortir les poubelles" in text  # persisted
    assert "C'est fait" in r.messages[-1]
    await h.close()


async def test_declined_creation_saves_nothing(tmp_path: Path) -> None:
    h = await harness(tmp_path)
    r = BufferedReplier(auto_confirm=False)
    await h.orchestrator.handle(msg(tool({"what": "check", "at": "23:00"})), r)
    assert r.questions and h.routines.routines == []
    assert (await h.db.audit_entries())[0].outcome == "declined"
    await h.close()


async def test_invalid_request_is_reported_to_the_model(tmp_path: Path) -> None:
    h = await harness(tmp_path)
    r = BufferedReplier(auto_confirm=True)
    await h.orchestrator.handle(msg(tool({"what": "reminder", "at": "26:00", "text": "x"})), r)
    assert r.questions == [] and h.routines.routines == []
    assert "heure invalide" in r.messages[-1]
    await h.close()


async def test_names_are_unique_and_limit_is_enforced(tmp_path: Path) -> None:
    h = await build_harness(tmp_path, proactive={"max_routines": 2})
    for _ in range(2):
        rt = h.routines.build(
            {"what": "reminder", "at": "08:00", "text": "Eau"}, chat_id="111", requester="111"
        )
        h.routines.add(rt)
    assert [r.name for r in h.routines.routines] == ["eau", "eau_2"]
    from homelab_agent.core.routines import RoutineError

    with pytest.raises(RoutineError, match="maximal"):
        h.routines.add(
            h.routines.build(
                {"what": "reminder", "at": "08:00", "text": "Eau"}, chat_id="1", requester="1"
            )
        )
    await h.close()


async def test_routine_prompts_cannot_edit_routines(tmp_path: Path) -> None:
    h = await harness(tmp_path)
    r = BufferedReplier(auto_confirm=True)
    sneaky = IncomingMessage(
        "111", "111", tool({"what": "reminder", "at": "03:00", "text": "boucle"}), origin="routine"
    )
    await h.orchestrator.handle(sneaky, r)
    assert r.questions == [] and h.routines.routines == []
    await h.close()


async def test_chat_edit_can_be_disabled(tmp_path: Path) -> None:
    h = await build_harness(tmp_path, proactive={"chat_edit": False}, routing={
        "tiers": {"gpu": ["gpu_fast"], "cpu": ["cpu_heavy"], "cloud": ["cloud"]}, "fast_path": False,
    })  # fmt: skip
    r = BufferedReplier(auto_confirm=True)
    await h.orchestrator.handle(msg(tool({"what": "check", "at": "23:00"})), r)
    assert h.routines.routines == []
    await h.close()


async def test_list_toggle_delete_through_tools(tmp_path: Path) -> None:
    h = await harness(tmp_path)
    h.routines.add(
        h.routines.build(
            {"what": "check", "at": "23:00", "name": "soir"}, chat_id="111", requester="111"
        )
    )
    r = BufferedReplier(auto_confirm=True)
    await h.orchestrator.handle(
        msg(tool({"name": "soir", "enabled": False}, "routine_set_enabled")), r
    )
    assert h.routines.get("soir").enabled is False  # type: ignore[union-attr]
    await h.orchestrator.handle(msg(tool({"name": "nope"}, "routine_delete")), r)
    assert "unknown routine" in r.messages[-1]
    await h.orchestrator.handle(msg(tool({"name": "soir"}, "routine_delete")), r)
    assert h.routines.routines == []
    await h.close()


async def test_telegram_routine_command(tmp_path: Path) -> None:
    h = await harness(tmp_path)
    h.routines.add(
        h.routines.build(
            {"what": "check", "at": "23:00", "name": "soir"}, chat_id="111", requester="111"
        )
    )
    assert "désactivée" in await h.routines.command("111", "off soir")
    assert h.routines.get("soir").enabled is False  # type: ignore[union-attr]
    assert "activée" in await h.routines.command("111", "on soir")
    assert "inconnue" in await h.routines.command("111", "off nope")
    assert "Usage" in await h.routines.command("111", "")
    assert "supprimée" in await h.routines.command("111", "delete soir")
    assert h.routines.routines == []
    await h.close()


async def test_reminder_and_one_off_routines_run(tmp_path: Path) -> None:
    h = await harness(tmp_path)
    now = datetime(2026, 11, 5, 10, 0, tzinfo=UTC)
    h.scheduler.clock = lambda: now
    h.routines.clock = lambda: now
    rt = h.routines.build(
        {"what": "reminder", "at": "08:00", "date": "demain", "text": "Dentiste"},
        chat_id="42",
        requester="42",
    )
    h.routines.add(rt)
    assert rt.date == "2026-11-06"
    assert h.scheduler.next_run(rt) == datetime(2026, 11, 6, 7, 0, tzinfo=UTC)
    due = h.scheduler.due(datetime(2026, 11, 6, 7, 0, 30, tzinfo=UTC), now)
    assert [r.name for r in due] == ["dentiste"]
    sent = await h.scheduler.run(due[0])
    assert sent == ["⏰ Dentiste"] and h.notifier.sent == [("42", "⏰ Dentiste")]  # its own chat
    assert h.routines.routines == []  # one-off removed after running
    assert (
        h.scheduler.next_run(Routine(name="past", at="08:00", date="2026-11-01", message="x"))
        is None
    )
    await h.close()


async def test_ask_routine_runs_through_the_agent(tmp_path: Path) -> None:
    h = await harness(tmp_path)
    rt = h.routines.build(
        {"what": "ask", "at": "07:30", "days": "semaine", "text": "Fais le point", "tier": "gpu"},
        chat_id="111",
        requester="111",
    )
    h.routines.add(rt)
    assert rt.prompt == "Fais le point" and rt.tier == Tier.GPU
    await h.scheduler.run(rt)
    assert any("Réponse simulée (fast) : Fais le point" in m for m in h.notifier.replies)
    await h.close()
