"""House at a glance for the status page: lights, temperature, locks, alerts."""

from __future__ import annotations

import math
from collections.abc import Iterable
from statistics import mean
from typing import Any

from homelab_agent.tools.homeassistant.client import EntityInfo

ALERT_CLASSES = {"moisture", "smoke", "gas", "carbon_monoxide", "safety", "problem", "tamper"}
OPENING_CLASSES = {"door", "window", "opening", "garage_door"}
UNLOCKED_STATES = {"unlocked", "unlocking", "open", "opening", "jammed"}


def _number(value: str) -> float | None:
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def house_summary(entities: Iterable[EntityInfo]) -> dict[str, Any]:
    lights = lights_on = 0
    temps: list[float] = []
    unlocked: list[str] = []
    alerts: list[str] = []
    opened: list[str] = []
    for e in entities:
        dc = e.attributes.get("device_class")
        if e.domain == "light":
            lights += 1
            lights_on += e.state == "on"
        elif e.domain == "lock" and e.state in UNLOCKED_STATES:
            unlocked.append(e.name)
        elif e.domain == "sensor" and dc == "temperature":
            t = _number(e.state)
            if t is not None:
                temps.append(t)
        elif e.domain == "binary_sensor" and e.state == "on":
            if dc in ALERT_CLASSES:
                alerts.append(e.name)
            elif dc in OPENING_CLASSES:
                opened.append(e.name)
    return {
        "lights": lights,
        "lights_on": lights_on,
        "temp": round(mean(temps), 1) if temps else None,
        "unlocked": sorted(unlocked),
        "alerts": sorted(alerts),
        "opened": sorted(opened),
    }
