from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from homelab_agent.config.models import TelegramSection
from homelab_agent.gateway.home_remote import HomeRemote, toggle_service
from homelab_agent.gateway.telegram import TelegramFrontend, keyboard_rows
from homelab_agent.tools.homeassistant.client import EntityInfo

from .conftest import wait_for
from .harness import Harness, build_harness
from .test_telegram import TABLE, FakeBot


async def setup(
    tmp_path: Path, **overrides: Any
) -> tuple[Harness, FakeBot, HomeRemote, TelegramFrontend]:
    h = await build_harness(tmp_path, **overrides)
    bot = FakeBot()
    section = TelegramSection(allowed_user_ids=[111, 222], confirm_timeout_s=5)
    f = TelegramFrontend(
        bot, lambda: section, h.orchestrator.handle, commands=TABLE,
    )  # fmt: skip
    remote = HomeRemote(f, h.ha, lambda: section)
    remote.attach()
    return h, bot, remote, f


def last_keyboard(bot: FakeBot) -> tuple[str, list[list[tuple[str, str]]], int]:
    for op, args in reversed(bot.ops):
        if op == "send" and args[2]:
            return args[1], keyboard_rows(args[2]), bot.next_id
        if op == "edit" and len(args) == 4:
            return args[2], keyboard_rows(args[3]), args[1]
    raise AssertionError("no keyboard sent")


def press(rows: list[list[tuple[str, str]]], startswith: str) -> str:
    for row in rows:
        for label, data in row:
            if label.startswith(startswith):
                return data
    raise AssertionError(f"no button {startswith!r} in {rows}")


def test_toggle_service() -> None:
    def e(eid: str, state: str) -> EntityInfo:
        return EntityInfo(eid, state, eid, None, None, {})

    assert toggle_service(e("light.a", "on")) == "turn_off"
    assert toggle_service(e("light.a", "off")) == "turn_on"
    assert toggle_service(e("cover.a", "open")) == "close_cover"
    assert toggle_service(e("cover.a", "closed")) == "open_cover"
    assert toggle_service(e("lock.a", "locked")) == "unlock"
    assert toggle_service(e("scene.a", "scening")) == "turn_on"
    assert toggle_service(e("sensor.a", "21")) is None


async def test_browse_rooms_and_toggle_a_light(tmp_path: Path) -> None:
    h, bot, remote, f = await setup(tmp_path)
    await f.on_message(111, 111, "/maison")
    text, rows, msg_id = last_keyboard(bot)
    assert text.startswith("🏠 Maison")
    labels = [label for row in rows for label, _ in row]
    assert "Salon (2)" in labels and "Cuisine (1)" in labels and "🎬 Scènes" in labels
    # injected entity names never become buttons of their own (sensor.piege is not a device)
    assert not any("Ignore" in label for label in labels)

    await f.on_callback(111, 111, "cb1", press(rows, "Salon"), msg_id)
    text, rows, _ = last_keyboard(bot)
    assert text.startswith("🚪 Salon")
    assert "📟 Température salon : 21.5 °C" in text  # sensors shown as text
    await f.on_callback(111, 111, "cb2", press(rows, "💡 Lumière salon · allumé"), msg_id)
    assert h.ha_calls[-1] == {
        "domain": "light",
        "service": "turn_off",
        "data": {"entity_id": ["light.salon"]},
    }
    text, rows, _ = last_keyboard(bot)
    press(rows, "💡 Lumière salon · éteint")  # refreshed with the new state
    assert (await h.db.audit_entries())[0].tool == "home_remote"
    await f.on_callback(111, 111, "cb3", press(rows, "⬅️ Pièces"), msg_id)
    assert last_keyboard(bot)[0].startswith("🏠 Maison")
    await h.close()


async def test_read_only_devices_show_a_padlock(tmp_path: Path) -> None:
    h, bot, remote, f = await setup(
        tmp_path,
        home_assistant={"url": "http://ha", "rules": [{"domain": "switch", "mode": "read_only"}]},
    )
    await f.on_message(111, 111, "/maison")
    _, rows, msg_id = last_keyboard(bot)
    await f.on_callback(111, 111, "cb1", press(rows, "Salon"), msg_id)
    _, rows, _ = last_keyboard(bot)
    data = press(rows, "🔒 Prise bureau")
    await f.on_callback(111, 111, "cb2", data, msg_id)
    assert h.ha_calls == []
    assert ("answer", ("cb2", "🔒 Lecture seule (permissions)")) in bot.ops
    await h.close()


async def test_confirmation_is_asked_to_the_person_who_tapped(tmp_path: Path) -> None:
    h, bot, remote, f = await setup(
        tmp_path, home_assistant={"url": "http://ha", "confirm": [{"domain": "lock"}]}
    )
    await f.on_message(111, 111, "/maison")
    _, rows, msg_id = last_keyboard(bot)
    await f.on_callback(111, 111, "cb1", press(rows, "Entrée"), msg_id)
    _, rows, _ = last_keyboard(bot)
    task = asyncio.create_task(
        f.on_callback(111, 111, "cb2", press(rows, "🔐 Porte d'entrée · verrouillé ⚠️"), msg_id)
    )
    await wait_for(
        lambda: any(op == "send" and args[1].startswith("Confirmer") for op, args in bot.ops)
    )
    question = next(
        args for op, args in bot.ops if op == "send" and args[1].startswith("Confirmer")
    )
    yes = question[2][0][1]
    await f.on_callback(222, 111, "cb3", yes)  # someone else cannot confirm
    assert not task.done()
    await f.on_callback(111, 111, "cb4", yes)
    await task
    assert h.ha_calls[-1]["service"] == "unlock"
    await h.close()


async def test_expired_menu_and_close(tmp_path: Path) -> None:
    h, bot, remote, f = await setup(tmp_path)
    await f.on_callback(111, 111, "cb0", "hm:deadbeef0000", 5)
    assert ("answer", ("cb0", "Menu expiré : relance /maison.")) in bot.ops
    await f.on_message(111, 111, "/maison")
    _, rows, msg_id = last_keyboard(bot)
    await f.on_callback(111, 111, "cb1", press(rows, "✖️ Fermer"), msg_id)
    assert bot.texts()[-1] == "🏠 Télécommande fermée."
    await f.on_message(999, 999, "/maison")  # stranger: nothing
    assert bot.texts()[-1] == "🏠 Télécommande fermée."
    await h.close()


async def test_pagination(tmp_path: Path) -> None:
    h, bot, remote, f = await setup(tmp_path)
    cache = h.ha.client.cache
    for i in range(15):
        cache.apply_state({"entity_id": f"light.extra_{i:02d}", "state": "off",
                           "attributes": {"friendly_name": f"Extra {i:02d}"}}, notify=False)  # fmt: skip
    text, rows = remote.area_view("__none__")
    assert len(rows) == 11  # 10 devices + navigation
    assert [label for label, _ in rows[-1]] == ["⬅️ Pièces", "1/2", "▶️"]
    text, rows = remote.area_view("__none__", 1)
    assert [label for label, _ in rows[-1]] == ["⬅️ Pièces", "◀️", "2/2"]
    await h.close()
