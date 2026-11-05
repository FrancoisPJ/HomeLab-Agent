"""Catalogue of the Home Assistant services (``GET /api/services``).

Used to show the model which services and fields exist, and to refuse an invented call
(``light.dim``, ``light.turn_off`` with ``brightness``…) before Home Assistant is called, with
an error that lists what is possible so that the model can fix its call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_NAME_RE = re.compile(r"^[a-z0-9_]+$")

#: fields shown first when present: the ones a home assistant needs most
PRIORITY_FIELDS = (
    "brightness_pct", "color_name", "color_temp_kelvin", "rgb_color", "temperature",
    "target_temp_low", "target_temp_high", "hvac_mode", "preset_mode", "position",
    "tilt_position", "percentage", "volume_level", "media_content_id", "media_content_type",
    "option", "value", "message", "title", "code", "duration",
)  # fmt: skip


@dataclass(slots=True)
class ServiceInfo:
    fields: list[str] = field(default_factory=list)  # most useful first
    required: set[str] = field(default_factory=set)

    @property
    def declares_fields(self) -> bool:
        return bool(self.fields)


def _flatten_fields(raw: dict[str, Any]) -> tuple[list[str], set[str]]:
    """Field names of one service, sections (``advanced_fields``…) flattened."""
    names: list[str] = []
    required: set[str] = set()
    for key, spec in raw.items():
        if isinstance(spec, dict) and isinstance(spec.get("fields"), dict):  # a section
            sub, sub_req = _flatten_fields(spec["fields"])
            names += sub
            required |= sub_req
            continue
        if not _NAME_RE.match(str(key)):
            continue
        names.append(str(key))
        if isinstance(spec, dict) and spec.get("required"):
            required.add(str(key))
    return names, required


def _order(names: list[str], required: set[str]) -> list[str]:
    rank = {n: i for i, n in enumerate(PRIORITY_FIELDS)}
    return sorted(
        dict.fromkeys(names),
        key=lambda n: (n not in required, rank.get(n, len(rank)), names.index(n)),
    )


class ServiceCatalog:
    def __init__(self) -> None:
        self.domains: dict[str, dict[str, ServiceInfo]] = {}

    @property
    def loaded(self) -> bool:
        return bool(self.domains)

    def load(self, raw: Any) -> None:
        """Accept the REST shape (list of {domain, services}) or the WebSocket one (dict)."""
        items: list[tuple[str, Any]]
        if isinstance(raw, dict):
            items = list(raw.items())
        elif isinstance(raw, list):
            items = [
                (str(d.get("domain", "")), d.get("services")) for d in raw if isinstance(d, dict)
            ]
        else:
            return
        domains: dict[str, dict[str, ServiceInfo]] = {}
        for domain, services in items:
            if not _NAME_RE.match(domain) or not isinstance(services, dict):
                continue
            out: dict[str, ServiceInfo] = {}
            for name, spec in services.items():
                if not _NAME_RE.match(str(name)):
                    continue
                fields = spec.get("fields") if isinstance(spec, dict) else None
                names, required = _flatten_fields(fields if isinstance(fields, dict) else {})
                out[str(name)] = ServiceInfo(_order(names, required), required)
            domains[domain] = out
        self.domains = domains

    def get(self, domain: str, service: str) -> ServiceInfo | None:
        return self.domains.get(domain, {}).get(service)

    # ------------------------------------------------------------- text
    def describe(self, domain: str, *, max_fields: int = 8, max_services: int = 15) -> str:
        """``turn_on (brightness_pct, color_name, …), turn_off, toggle``."""
        services = self.domains.get(domain, {})
        parts = []
        for name in list(services)[:max_services]:
            info = services[name]
            if info.fields:
                shown = info.fields[:max_fields]
                more = ", …" if len(info.fields) > max_fields else ""
                parts.append(f"{name} ({', '.join(shown)}{more})")
            else:
                parts.append(name)
        if len(services) > max_services:
            parts.append("…")
        return ", ".join(parts)

    def hint(self, domain: str) -> str:
        if domain not in self.domains:
            known = sorted(self.domains)
            more = ", …" if len(known) > 30 else ""
            return f"domaines connus : {', '.join(known[:30])}{more}"
        return f"services possibles pour {domain} : {self.describe(domain)}"

    def check(self, domain: str, service: str, data: dict[str, Any], *, fields: bool) -> str | None:
        """Why this call cannot work (French, for the model), or None."""
        if not self.loaded:
            return None  # catalogue not loaded yet: let Home Assistant decide
        if domain not in self.domains:
            return f"le domaine '{domain}' n'existe pas ; {self.hint(domain)}"
        info = self.get(domain, service)
        if info is None:
            return f"{domain}.{service} n'existe pas ; {self.hint(domain)}"
        if fields and info.declares_fields:
            unknown = sorted(k for k in data if k not in info.fields)
            if unknown:
                return (
                    f"{domain}.{service} n'accepte pas {', '.join(unknown)} ; "
                    f"champs possibles : {', '.join(info.fields)} ; {self.hint(domain)}"
                )
        return None

    def summary(self, domains: list[str], max_chars: int) -> str:
        """One line per domain, within ``max_chars``: ``light: turn_on (…), turn_off``."""
        lines: list[str] = []
        size = 0
        for domain in domains:
            if domain not in self.domains:
                continue
            line = f"{domain}: {self.describe(domain, max_fields=4, max_services=8)}"
            if size + len(line) + 1 > max_chars:
                break
            lines.append(line)
            size += len(line) + 1
        return "\n".join(lines)
