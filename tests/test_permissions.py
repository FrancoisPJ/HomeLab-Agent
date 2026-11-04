from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from homelab_agent.config.models import HomeAssistantSection, PermissionRule, Policy, Selector
from homelab_agent.tools.homeassistant.permissions import PermissionEngine


def engine(**kw: object) -> PermissionEngine:
    section = HomeAssistantSection.model_validate(kw)
    return PermissionEngine(lambda: section)


def test_default_allows_everything() -> None:
    e = engine()
    d = e.evaluate("lock.porte", "control")
    assert d.allowed and not d.needs_confirm


def test_default_read_only_and_deny() -> None:
    ro = engine(default_policy="read_only")
    assert ro.evaluate("light.a", "read").allowed
    assert not ro.evaluate("light.a", "control").allowed
    deny = engine(default_policy="deny")
    assert not deny.evaluate("light.a", "read").allowed


def test_domain_area_entity_specificity() -> None:
    e = engine(
        rules=[
            {"domain": "light", "mode": "deny"},
            {"area": "salon", "mode": "read_only"},
            {"entity": "light.salon", "mode": "allow"},
            {"entity": "switch.serveur_*", "mode": "deny"},
        ]
    )
    assert e.evaluate("light.salon", "control", areas={"salon"}).allowed  # exact entity wins
    assert not e.evaluate("light.lampe", "read").allowed  # domain deny
    assert not e.evaluate("switch.tv", "control", areas={"Salon"}).allowed  # area read_only
    assert e.evaluate("switch.tv", "read", areas={"Salon"}).allowed
    assert not e.evaluate("switch.serveur_nas", "read").allowed  # glob
    assert e.evaluate("switch.autre", "control").allowed  # default


def test_tie_breaks_to_most_restrictive() -> None:
    e = engine(rules=[{"domain": "lock", "mode": "allow"}, {"domain": "lock", "mode": "deny"}])
    assert not e.evaluate("lock.porte", "read").allowed


def test_confirmation_by_rule_and_selector() -> None:
    e = engine(
        rules=[{"domain": "cover", "mode": "allow", "confirm": True}],
        confirm=[{"domain": "lock"}],
    )
    assert e.evaluate("cover.volet", "control").needs_confirm
    assert e.evaluate("lock.porte", "control").needs_confirm
    assert not e.evaluate("lock.porte", "read").needs_confirm
    assert not e.evaluate("light.a", "control").needs_confirm


def test_domain_level_check_without_entity() -> None:
    e = engine(rules=[{"domain": "notify", "mode": "deny"}])
    assert not e.evaluate(None, "control", domain="notify").allowed
    assert e.evaluate(None, "control", domain="script").allowed


# ----------------------------------------------------------------- properties
domains = st.sampled_from(["light", "lock", "switch", "cover"])
areas = st.sampled_from(["salon", "cuisine", "garage"])
modes = st.sampled_from(list(Policy))


@st.composite
def rules(draw: st.DrawFn) -> PermissionRule:
    kind = draw(st.sampled_from(["domain", "area", "entity", "glob", "combo"]))
    mode = draw(modes)
    confirm = draw(st.booleans())
    if kind == "domain":
        return PermissionRule(domain=draw(domains), mode=mode, confirm=confirm)
    if kind == "area":
        return PermissionRule(area=draw(areas), mode=mode, confirm=confirm)
    if kind == "entity":
        return PermissionRule(entity=f"{draw(domains)}.{draw(areas)}", mode=mode, confirm=confirm)
    if kind == "glob":
        return PermissionRule(entity=f"{draw(domains)}.*", mode=mode, confirm=confirm)
    return PermissionRule(domain=draw(domains), area=draw(areas), mode=mode, confirm=confirm)


@settings(max_examples=200, deadline=None)
@given(st.lists(rules(), max_size=8), st.data(), domains, areas, modes)
def test_rule_order_never_matters(rule_list, data, domain, area, default) -> None:  # type: ignore[no-untyped-def]
    shuffled = data.draw(st.permutations(rule_list))
    a = HomeAssistantSection(default_policy=default, rules=rule_list)
    b = HomeAssistantSection(default_policy=default, rules=shuffled)
    eid = f"{domain}.{area}"
    for action in ("read", "control"):
        da = PermissionEngine(lambda: a).evaluate(eid, action, areas={area})
        db = PermissionEngine(lambda: b).evaluate(eid, action, areas={area})
        assert (da.allowed, da.needs_confirm, da.mode) == (db.allowed, db.needs_confirm, db.mode)


@settings(max_examples=200, deadline=None)
@given(st.lists(rules(), max_size=8), domains, areas, modes)
def test_control_implies_read(rule_list, domain, area, default) -> None:  # type: ignore[no-untyped-def]
    section = HomeAssistantSection(default_policy=default, rules=rule_list)
    e = PermissionEngine(lambda: section)
    eid = f"{domain}.{area}"
    if e.evaluate(eid, "control", areas={area}).allowed:
        assert e.evaluate(eid, "read", areas={area}).allowed


@settings(max_examples=100, deadline=None)
@given(domains, areas)
def test_read_only_and_deny_rules_never_allow_control(domain: str, area: str) -> None:
    for mode in (Policy.READ_ONLY, Policy.DENY):
        section = HomeAssistantSection(rules=[PermissionRule(entity=f"{domain}.{area}", mode=mode)])
        assert not PermissionEngine(lambda: section).evaluate(f"{domain}.{area}", "control").allowed  # noqa: B023


@settings(max_examples=100, deadline=None)
@given(domains, areas)
def test_confirm_selector_always_requires_confirmation(domain: str, area: str) -> None:
    section = HomeAssistantSection(confirm=[Selector(domain=domain)])
    d = PermissionEngine(lambda: section).evaluate(f"{domain}.{area}", "control")
    assert d.allowed and d.needs_confirm
