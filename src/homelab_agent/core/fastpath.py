"""LLM-free fast path for trivial home commands ("éteins la lumière du salon")."""

from __future__ import annotations

import re
import unicodedata
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


@dataclass(frozen=True, slots=True)
class FastPathMatch:
    domain: str
    service: str
    entity_ids: list[str]
    names: list[str]


def match(text: str, section: FastPathSection, cache: EntityCache) -> FastPathMatch | None:
    """Return a command only when the *user text* is short and unambiguous."""
    if not section.enabled or len(text) > MAX_LEN or "?" in text:
        return None
    norm = f" {normalize(text)} "
    actions = {action for pat, action in _VERBS if pat.search(norm)}
    if len(actions) != 1:
        return None
    action = actions.pop()

    candidates: dict[str, str] = {}
    for spoken, eid in section.aliases.items():
        if f" {normalize(spoken)} " in norm:
            candidates[eid] = spoken
    if not candidates:
        for e in cache.entities.values():
            name = normalize(e.name)
            if len(name) >= 3 and f" {name} " in norm:
                candidates[e.entity_id] = e.name
    # keep the longest names only (e.g. "lumiere salon" beats "salon")
    if not candidates:
        return None
    longest = max(len(v) for v in candidates.values())
    picked = {k: v for k, v in candidates.items() if len(v) == longest}
    domains = {eid.split(".", 1)[0] for eid in picked}
    if len(domains) != 1:
        return None
    domain = domains.pop()
    service = _SERVICES.get((domain, action))
    if service is None:
        return None
    return FastPathMatch(domain, service, sorted(picked), [picked[k] for k in sorted(picked)])
