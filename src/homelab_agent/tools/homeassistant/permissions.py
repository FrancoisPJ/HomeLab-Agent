"""Home Assistant permission engine. Enforced in code, never delegated to the prompt.

Resolution: the most specific matching rule wins (exact entity > entity glob > area > domain;
fields add up when combined). On a tie the most restrictive mode (then confirmation)
wins, so the result never depends on the order of rules in the file. With no matching rule,
``default_policy`` applies.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Literal

from homelab_agent.config.models import HomeAssistantSection, PermissionRule, Policy, Selector

Action = Literal["read", "control"]

_RESTRICTIVENESS = {Policy.DENY: 2, Policy.READ_ONLY: 1, Policy.ALLOW: 0}


@dataclass(frozen=True, slots=True)
class Decision:
    allowed: bool
    needs_confirm: bool
    mode: Policy
    reason: str


def _domain(entity_id: str | None) -> str | None:
    if not entity_id or "." not in entity_id:
        return None
    return entity_id.split(".", 1)[0]


def selector_specificity(
    sel: Selector, entity_id: str | None, domain: str | None, areas: set[str]
) -> int | None:
    """Return the specificity score if ``sel`` matches, else None."""
    score = 0
    if sel.entity is not None:
        if entity_id is None:
            return None
        if any(ch in sel.entity for ch in "*?["):
            if not fnmatchcase(entity_id, sel.entity):
                return None
            score += 100
        else:
            if entity_id != sel.entity:
                return None
            score += 1000
    if sel.area is not None:
        if sel.area.casefold() not in areas:
            return None
        score += 10
    if sel.domain is not None:
        if domain != sel.domain:
            return None
        score += 1
    return score


def allows(mode: Policy, action: Action) -> bool:
    if mode == Policy.ALLOW:
        return True
    if mode == Policy.READ_ONLY:
        return action == "read"
    return False


class PermissionEngine:
    def __init__(self, get_section: Callable[[], HomeAssistantSection]) -> None:
        self._section = get_section

    def evaluate(
        self,
        entity_id: str | None,
        action: Action,
        *,
        domain: str | None = None,
        areas: set[str] | None = None,
    ) -> Decision:
        section = self._section()
        domain = domain or _domain(entity_id)
        area_keys = {a.casefold() for a in (areas or set()) if a}
        best: tuple[tuple[int, int, bool], PermissionRule] | None = None
        for rule in section.rules:
            score = selector_specificity(rule, entity_id, domain, area_keys)
            if score is None:
                continue
            # ties: stricter mode first, then "confirm" over "no confirm"
            key = (score, _RESTRICTIVENESS[rule.mode], rule.confirm)
            if best is None or key > best[0]:
                best = (key, rule)
        if best is None:
            mode = section.default_policy
            reason = f"default policy '{mode.value}'"
            rule_confirm = False
        else:
            rule = best[1]
            mode = rule.mode
            target = rule.entity or rule.area or rule.domain
            reason = f"rule '{mode.value}' on {target}"
            rule_confirm = rule.confirm
        allowed = allows(mode, action)
        needs_confirm = False
        if allowed and action == "control":
            needs_confirm = rule_confirm or any(
                selector_specificity(sel, entity_id, domain, area_keys) is not None
                for sel in section.confirm
            )
        return Decision(allowed, needs_confirm, mode, reason)
