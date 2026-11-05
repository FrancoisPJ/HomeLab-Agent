#!/usr/bin/env python3
"""Generate the Grafana dashboard of homelab-agent (deploy/monitoring/grafana/dashboards).

    uv run python scripts/grafana_dashboard.py          # rewrite the JSON
    uv run python scripts/grafana_dashboard.py --check  # fail if the committed JSON is stale

Editing the dashboard = editing this file (reviewable diff), then regenerating.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

OUT = (
    Path(__file__).resolve().parent.parent
    / "deploy/monitoring/grafana/dashboards/homelab-agent.json"
)
DS = {"type": "prometheus", "uid": "prometheus"}

# same palette as the panel's liquid glass theme
TIER_COLORS = {"fast_path": "#22d3ee", "gpu": "#a78bfa", "cpu": "#f472b6", "cloud": "#fbbf24"}
OUTCOME_COLORS = {"ok": "#34d399", "error": "#f87171", "denied": "#fb923c", "declined": "#94a3b8",
                  "sent": "#34d399", "quiet": "#818cf8", "cooldown": "#94a3b8"}  # fmt: skip

_ids = iter(range(1, 1000))


def target(expr: str, legend: str = "", ref: str = "A", instant: bool = False) -> dict[str, Any]:
    t: dict[str, Any] = {"datasource": DS, "expr": expr, "refId": ref, "legendFormat": legend}
    if instant:
        t.update(instant=True, range=False)
    return t


def color_overrides(colors: dict[str, str]) -> list[dict[str, Any]]:
    return [
        {
            "matcher": {"id": "byName", "options": name},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": color}}],
        }
        for name, color in colors.items()
    ]


def panel(kind: str, title: str, grid: tuple[int, int, int, int], targets: list[dict[str, Any]],
          *, unit: str = "short", description: str = "", options: dict[str, Any] | None = None,
          defaults: dict[str, Any] | None = None, overrides: list[dict[str, Any]] | None = None,
          ) -> dict[str, Any]:  # fmt: skip
    x, y, w, h = grid
    field_defaults: dict[str, Any] = {"unit": unit, "color": {"mode": "palette-classic"}}
    field_defaults.update(defaults or {})
    return {
        "id": next(_ids),
        "type": kind,
        "title": title,
        "description": description,
        "datasource": DS,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "targets": targets,
        "fieldConfig": {"defaults": field_defaults, "overrides": overrides or []},
        "options": options or {},
        "transparent": False,
    }


def stat(title: str, grid: tuple[int, int, int, int], expr: str, *, unit: str = "short",
         color: str = "#a78bfa", description: str = "", decimals: int | None = None,
         mappings: list[dict[str, Any]] | None = None, thresholds: list[dict[str, Any]] | None = None,
         ) -> dict[str, Any]:  # fmt: skip
    defaults: dict[str, Any] = {
        "color": {"mode": "thresholds" if thresholds else "fixed", "fixedColor": color},
        "thresholds": {
            "mode": "absolute",
            "steps": thresholds or [{"color": color, "value": None}],
        },
        "mappings": mappings or [],
    }
    if decimals is not None:
        defaults["decimals"] = decimals
    return panel(
        "stat", title, grid, [target(expr, instant=True)], unit=unit, description=description,
        defaults=defaults,
        options={"colorMode": "background_solid" if thresholds else "value", "graphMode": "area",
                 "justifyMode": "center", "textMode": "value", "wideLayout": True,
                 "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
    )  # fmt: skip


def timeseries(title: str, grid: tuple[int, int, int, int], targets: list[dict[str, Any]], *,
               unit: str = "short", stack: bool = False, bars: bool = False,
               colors: dict[str, str] | None = None, description: str = "") -> dict[str, Any]:  # fmt: skip
    custom = {
        "drawStyle": "bars" if bars else "line",
        "lineInterpolation": "smooth",
        "lineWidth": 2,
        "fillOpacity": 35 if bars else 18,
        "gradientMode": "opacity",
        "showPoints": "never",
        "spanNulls": True,
        "barAlignment": 0,
        "stacking": {"mode": "normal" if stack else "none", "group": "A"},
        "axisBorderShow": False,
        "axisPlacement": "auto",
    }
    return panel(
        "timeseries", title, grid, targets, unit=unit, description=description,
        defaults={"custom": custom, "min": 0},
        overrides=color_overrides(colors or {}),
        options={"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                 "tooltip": {"mode": "multi", "sort": "desc"}},
    )  # fmt: skip


def build() -> dict[str, Any]:
    panels: list[dict[str, Any]] = []
    # ------------------------------------------------------------------ KPIs
    panels += [
        stat("Messages · 24 h", (0, 0, 4, 5), "sum(increase(hla_messages_total[24h]))",
             color="#a78bfa", decimals=0, description="Messages reçus (Telegram, chat web, routines)."),
        stat("Réponses / min", (4, 0, 4, 5), "sum(rate(hla_replies_total[5m])) * 60",
             color="#22d3ee", decimals=1),
        stat("Latence p95 · 15 min", (8, 0, 4, 5),
             "histogram_quantile(0.95, sum by (le) (rate(hla_latency_ms_bucket[15m])))",
             unit="ms", color="#f472b6", decimals=0,
             description="95 % des réponses arrivent plus vite que ça (tous niveaux)."),
        stat("Home Assistant", (12, 0, 4, 5), "max(hla_home_assistant_connected)",
             mappings=[{"type": "value", "options": {
                 "0": {"text": "Déconnecté", "color": "#f87171", "index": 0},
                 "1": {"text": "Connecté", "color": "#34d399", "index": 1}}}],
             thresholds=[{"color": "#f87171", "value": None}, {"color": "#34d399", "value": 1}]),
        stat("Tâches CPU en cours", (16, 0, 4, 5),
             'sum(hla_jobs{status=~"queued|running"})', color="#fbbf24", decimals=0),
        panel("gauge", "Budget cloud · jour", (20, 0, 4, 5), [target(
            '100 * sum(hla_cloud_spend_eur{period="day"}) / clamp_min(sum(hla_cloud_limit_eur{period="day"}), 0.0001)',
            instant=True)], unit="percent",
            defaults={"min": 0, "max": 100, "color": {"mode": "thresholds"},
                      "thresholds": {"mode": "absolute", "steps": [
                          {"color": "#34d399", "value": None}, {"color": "#fbbf24", "value": 80},
                          {"color": "#f87171", "value": 100}]}},
            options={"showThresholdMarkers": True, "showThresholdLabels": False,
                     "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
            description="Dépense cloud du jour, en % du plafond journalier."),
    ]  # fmt: skip
    # ------------------------------------------------------------ activity
    panels += [
        timeseries("Réponses par niveau (par minute)", (0, 5, 14, 9),
                   [target("sum by (tier) (rate(hla_replies_total[5m])) * 60", "{{tier}}")],
                   stack=True, bars=True, colors=TIER_COLORS),
        panel("piechart", "Répartition des niveaux · 24 h", (14, 5, 10, 9),
              [target("sum by (tier) (increase(hla_replies_total[24h]))", "{{tier}}", instant=True)],
              overrides=color_overrides(TIER_COLORS),
              options={"pieType": "donut", "displayLabels": ["percent"],
                       "legend": {"displayMode": "table", "placement": "right", "showLegend": True,
                                  "values": ["value", "percent"]},
                       "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                       "tooltip": {"mode": "single"}}),
        timeseries("Latence p50 / p95 par niveau", (0, 14, 14, 9), [
            target("histogram_quantile(0.95, sum by (le, tier) (rate(hla_latency_ms_bucket[5m])))",
                   "p95 {{tier}}", "A"),
            target("histogram_quantile(0.5, sum by (le, tier) (rate(hla_latency_ms_bucket[5m])))",
                   "p50 {{tier}}", "B"),
        ], unit="ms", colors={f"p95 {k}": v for k, v in TIER_COLORS.items()}
            | {f"p50 {k}": v for k, v in TIER_COLORS.items()}),
        panel("state-timeline", "Disponibilité des moteurs", (14, 14, 10, 9),
              [target("max by (backend) (hla_backend_up)", "{{backend}}")],
              defaults={"color": {"mode": "thresholds"}, "mappings": [{"type": "value", "options": {
                  "0": {"text": "indisponible", "color": "#f87171", "index": 0},
                  "1": {"text": "disponible", "color": "#34d399", "index": 1}}}],
                  "thresholds": {"mode": "absolute", "steps": [{"color": "#f87171", "value": None},
                                                                {"color": "#34d399", "value": 1}]}},
              options={"showValue": "never", "rowHeight": 0.8, "mergeValues": True,
                       "legend": {"showLegend": False}, "alignValue": "center"}),
    ]  # fmt: skip
    # --------------------------------------------------------- tools & home
    panels += [
        timeseries("Appels d'outils par résultat (par minute)", (0, 23, 9, 8),
                   [target("sum by (outcome) (rate(hla_tool_calls_total[5m])) * 60", "{{outcome}}")],
                   stack=True, bars=True, colors=OUTCOME_COLORS),
        panel("bargauge", "Outils les plus utilisés · 24 h", (9, 23, 7, 8),
              [target("topk(8, sum by (tool) (increase(hla_tool_calls_total[24h])))", "{{tool}}",
                      instant=True)],
              defaults={"color": {"mode": "continuous-BlPu"}, "decimals": 0},
              options={"displayMode": "gradient", "orientation": "horizontal", "showUnfilled": True,
                       "valueMode": "color", "namePlacement": "left",
                       "text": {"titleSize": 13, "valueSize": 20}, "maxVizHeight": 28, "minVizHeight": 16,
                       "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}),
        panel("bargauge", "Jetons par moteur · 24 h", (16, 23, 8, 8),
              [target("sum by (backend) (increase(hla_tokens_total[24h]))", "{{backend}}",
                      instant=True)],
              defaults={"color": {"mode": "continuous-purples"}, "decimals": 0},
              options={"displayMode": "lcd", "orientation": "horizontal", "showUnfilled": True,
                       "valueMode": "color", "namePlacement": "left",
                       "text": {"titleSize": 13, "valueSize": 20}, "maxVizHeight": 28, "minVizHeight": 16,
                       "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}}),
        timeseries("Alertes domotiques (par heure)", (0, 31, 12, 8),
                   [target("sum by (alert, outcome) (increase(hla_alerts_total[1h]))",
                           "{{alert}} · {{outcome}}")], bars=True),
        stat("Vocaux reçus · 24 h", (12, 31, 4, 4), "sum(increase(hla_voice_messages_total[24h]))",
             color="#22d3ee", decimals=0),
        stat("Réponses vocales · 24 h", (16, 31, 4, 4), "sum(increase(hla_voice_replies_total[24h]))",
             color="#f472b6", decimals=0),
        stat("Intrus ignorés · 24 h", (20, 31, 4, 4),
             "sum(increase(hla_telegram_rejected_total[24h]))", color="#fb923c", decimals=0,
             description="Messages Telegram d'utilisateurs hors liste blanche."),
        stat("Routines exécutées · 24 h", (12, 35, 6, 4), "sum(increase(hla_routines_total[24h]))",
             color="#a78bfa", decimals=0),
        stat("Dépense cloud · mois", (18, 35, 6, 4), 'sum(hla_cloud_spend_eur{period="month"})',
             unit="currencyEUR", color="#fbbf24", decimals=2),
    ]  # fmt: skip
    return {
        "uid": "homelab-agent",
        "title": "homelab-agent",
        "description": "Activité, latences, moteurs, outils et budget de l'agent.",
        "tags": ["homelab-agent", "llm", "home-assistant"],
        "timezone": "browser",
        "editable": False,
        "graphTooltip": 1,
        "refresh": "30s",
        "schemaVersion": 41,
        "version": 1,
        "time": {"from": "now-6h", "to": "now"},
        "timepicker": {"refresh_intervals": ["10s", "30s", "1m", "5m", "15m"]},
        "templating": {"list": []},
        "annotations": {"list": []},
        "links": [],
        "panels": panels,
    }


def render() -> str:
    return json.dumps(build(), ensure_ascii=False, indent=2) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="fail if the committed JSON is stale")
    args = ap.parse_args()
    text = render()
    if args.check:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
            print(f"{OUT} is stale: run scripts/grafana_dashboard.py", file=sys.stderr)
            return 1
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
