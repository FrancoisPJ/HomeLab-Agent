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


def test_simple_commands_with_filler_words_still_match() -> None:
    c = cache()
    sec = FastPathSection(aliases={"salon": "light.salon", "chambre": "light.chambre"})
    for text in (
        "éteins la lumière du salon",
        "Allume le salon stp",
        "allume la lampe de la chambre",
        "éteins la lumière dans la chambre",
        "allume la chambre s'il te plaît",
    ):
        assert match(text, sec, c) is not None, text


def test_extra_details_go_to_the_llm() -> None:
    """A detail the fast path cannot apply (brightness, colour, delay…) must not be dropped."""
    c = cache()
    sec = FastPathSection(aliases={"salon": "light.salon", "chambre": "light.chambre"})
    for text in (
        "je veux que la lumière de la chambre soit allumé seulement à 1%",
        "allume la chambre à 30 %",
        "allume la chambre en rouge",
        "allume le salon dans 10 minutes",
        "éteins le salon demain matin",
        "allume la chambre doucement",
        "allume la lumière cuisine à fond",
    ):
        assert match(text, sec, c) is None, text
