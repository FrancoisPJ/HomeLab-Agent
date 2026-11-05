"""LLM-free fast path for trivial home commands ("éteins la lumière du salon")."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass

from homelab_agent.config.models import FastPathSection
from homelab_agent.tools.homeassistant.client import EntityCache


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9_. ]+", " ", text).strip()


# verb -> abstract action
_VERBS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b(allume|allumer|turn on|switch on)\b"), "on"),
    (re.compile(r"\b(eteins|eteint|eteindre|coupe|couper|turn off|switch off)\b"), "off"),
    (re.compile(r"\b(ouvre|ouvrir|open)\b"), "open"),
    (re.compile(r"\b(ferme|fermer|close)\b"), "close"),
    (re.compile(r"\b(active|activer|lance|lancer|activate)\b"), "activate"),
]

_SERVICES: dict[tuple[str, str], str] = {
    ("light", "on"): "turn_on",
    ("light", "off"): "turn_off",
    ("switch", "on"): "turn_on",
    ("switch", "off"): "turn_off",
    ("fan", "on"): "turn_on",
    ("fan", "off"): "turn_off",
    ("media_player", "on"): "turn_on",
    ("media_player", "off"): "turn_off",
    ("cover", "open"): "open_cover",
    ("cover", "close"): "close_cover",
    ("scene", "activate"): "turn_on",
    ("scene", "on"): "turn_on",
    ("script", "activate"): "turn_on",
    ("script", "on"): "turn_on",
}

MAX_LEN = 80
MAX_NAME_LEN = 40

# Words allowed around the verb and the entity name. Anything else ("1%", "en rouge",
# "dans 10 minutes", "seulement"…) carries a detail the fast path cannot apply: the LLM takes it.
_FILLER = frozenset(
    {
        "la", "le", "les", "l", "du", "de", "des", "d", "the", "a", "an",
        "lumiere", "lumieres", "lampe", "lampes", "light", "lights", "lamp", "lamps",
        "scene", "scenes", "script",
        "dans", "in", "stp", "svp", "please", "merci", "s", "il", "te", "plait", "vous",
    }
)  # fmt: skip


@dataclass(frozen=True, slots=True)
class FastPathMatch:
    domain: str
    service: str
    entity_ids: list[str]
    names: list[str]


def _only_filler_left(norm: str, names: Iterable[str]) -> bool:
    """True when the text is just verb + entity name + filler words."""
    rest = norm
    for pat, _ in _VERBS:
        rest = pat.sub(" ", rest)
    for name in names:
        rest = rest.replace(f" {normalize(name)} ", " ")
    return all(word in _FILLER for word in rest.split())


def _candidates(
    norm: str, section: FastPathSection, cache: EntityCache
) -> tuple[dict[str, str], bool]:
    """Entities named in the text: aliases first, else Home Assistant names.

    Returns {entity_id: spoken name} and whether the names came from Home Assistant.
    """
    candidates: dict[str, str] = {}
    for spoken, eid in section.aliases.items():
        if f" {normalize(spoken)} " in norm:
            candidates[eid] = spoken
    if candidates or not section.match_names:
        return candidates, False
    for e in cache.entities.values():
        name = normalize(e.name)
        # names come from Home Assistant (untrusted): ignore odd ones
        if not 3 <= len(name) <= MAX_NAME_LEN or "\n" in e.name:
            continue
        if f" {name} " in norm:
            candidates[e.entity_id] = e.name
    return candidates, True


def match(text: str, section: FastPathSection, cache: EntityCache) -> FastPathMatch | None:
    """Return a command only when the *user text* is short and unambiguous."""
    if not section.enabled or len(text) > MAX_LEN or "?" in text:
        return None
    norm = f" {normalize(text)} "
    actions = {action for pat, action in _VERBS if pat.search(norm)}
    if len(actions) != 1:
        return None
    action = actions.pop()

    candidates, from_names = _candidates(norm, section, cache)
    # keep the longest names only (e.g. "lumiere salon" beats "salon")
    if not candidates:
        return None
    longest = max(len(v) for v in candidates.values())
    picked = {k: v for k, v in candidates.items() if len(v) == longest}
    if from_names and len(picked) != 1:
        return None  # two entities share that name: ambiguous (or a look-alike), ask the LLM
    domains = {eid.split(".", 1)[0] for eid in picked}
    if len(domains) != 1:
        return None
    domain = domains.pop()
    service = _SERVICES.get((domain, action))
    if service is None:
        return None
    if not _only_filler_left(norm, picked.values()):
        return None
    return FastPathMatch(domain, service, sorted(picked), [picked[k] for k in sorted(picked)])
