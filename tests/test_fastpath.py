from __future__ import annotations

from homelab_agent.config.models import FastPathSection
from homelab_agent.core.fastpath import match, normalize
from homelab_agent.fakes.ha import AREAS, FakeHA
from homelab_agent.tools.homeassistant.client import EntityCache


def cache() -> EntityCache:
    fake = FakeHA()
    c = EntityCache()
    c.load(list(fake.states.values()), AREAS, fake.entity_registry(), [])
    return c


def test_normalize() -> None:
    assert normalize("Éteins la LUMIÈRE !") == "eteins la lumiere"


def test_alias_and_friendly_name() -> None:
    sec = FastPathSection(aliases={"salon": "light.salon"})
    m = match("Éteins le salon", sec, cache())
    assert m and (m.domain, m.service, m.entity_ids) == ("light", "turn_off", ["light.salon"])
    m = match("allume la lumière cuisine", FastPathSection(), cache())
    assert m and m.entity_ids == ["light.cuisine"] and m.service == "turn_on"
    m = match("ferme le volet chambre", FastPathSection(), cache())
    assert m and m.service == "close_cover"
    m = match("active la scène soirée", FastPathSection(), cache())
    assert m and m.domain == "scene" and m.service == "turn_on"


def test_no_match_cases() -> None:
    c = cache()
    sec = FastPathSection(aliases={"salon": "light.salon"})
    assert match("est-ce que le salon est allumé ?", sec, c) is None  # question
    assert match("allume et éteins le salon", sec, c) is None  # ambiguous verbs
    assert match("bonjour", sec, c) is None
    assert match("allume " + "x" * 100, sec, c) is None  # too long
    assert (
        match(
            "éteins le salon", FastPathSection(enabled=False, aliases={"salon": "light.salon"}), c
        )
        is None
    )
    assert match("ouvre la porte d'entrée", FastPathSection(), c) is None  # lock: unsupported verb
