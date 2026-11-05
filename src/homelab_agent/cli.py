"""Command line entry point: ``homelab-agent <command>``."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

from homelab_agent.config.loader import ConfigError, load_config
from homelab_agent.config.models import AppConfig

DEFAULT_CONFIG = os.environ.get("HLA_CONFIG", "config.yaml")


def schema_json() -> str:
    schema = AppConfig.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "homelab-agent configuration"
    return json.dumps(schema, indent=2, ensure_ascii=False) + "\n"


def healthcheck(cfg: AppConfig, max_age_s: float = 90) -> int:
    """Web panel answering /healthz, or (panel disabled) a fresh heartbeat file."""
    import time
    import urllib.request

    if cfg.web.enabled:
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{cfg.web.port}/healthz", timeout=4
            ) as resp:
                return 0 if resp.status == 200 else 1
        except OSError:
            return 1
    from homelab_agent.app import HEARTBEAT

    try:
        return 0 if time.time() - HEARTBEAT.stat().st_mtime < max_age_s else 1
    except OSError:
        return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="homelab-agent")
    parser.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="path to config.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="start the agent (Telegram + jobs + web panel)")
    sub.add_parser("check-config", help="validate the configuration and exit")
    p_schema = sub.add_parser("schema", help="print the JSON schema of config.yaml")
    p_schema.add_argument("-o", "--output", help="write to this file instead of stdout")
    p_eng = sub.add_parser("render-engines", help="write llama-swap configs from models:")
    p_eng.add_argument("-o", "--output", default="engines", help="output directory")
    sub.add_parser("models", help="list models to download (TSV: repo, file, sha256)")
    sub.add_parser("healthcheck", help="exit 0 when the running agent is healthy (Docker)")
    p_fake = sub.add_parser("fake", help="run a fake service (tests / dev)")
    p_fake.add_argument("service", choices=["llm", "ha", "telegram", "stt"])
    p_fake.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)

    if args.cmd == "schema":
        text = schema_json()
        if args.output:
            with open(args.output, "w", encoding="utf-8") as fh:
                fh.write(text)
        else:
            sys.stdout.write(text)
        return 0

    if args.cmd == "fake":
        import uvicorn

        from homelab_agent.fakes import create_fake

        uvicorn.run(create_fake(args.service), host="0.0.0.0", port=args.port, log_level="warning")
        return 0

    template = os.environ.get("HLA_CONFIG_TEMPLATE")
    if template and not os.path.exists(args.config) and os.path.exists(template):
        import shutil

        shutil.copyfile(template, args.config)  # first start: writable copy of the template

    try:
        cfg = load_config(args.config)
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.cmd == "check-config":
        missing = [
            env
            for env in (
                cfg.telegram.token_env if cfg.telegram.enabled else None,
                cfg.home_assistant.token_env if cfg.home_assistant.enabled else None,
                cfg.web.password_env if cfg.web.enabled else None,
            )
            if env and not os.environ.get(env)
        ]
        print(f"configuration OK: {args.config}")
        for env in missing:
            print(f"warning: environment variable {env} is not set")
        return 0
    if args.cmd == "healthcheck":
        return healthcheck(cfg)
    if args.cmd == "render-engines":
        from homelab_agent.engines import write_engine_configs

        for p in write_engine_configs(cfg, args.output):
            print(f"wrote {p}")
        return 0
    if args.cmd == "models":
        from homelab_agent.engines import models_tsv

        sys.stdout.write(models_tsv(cfg))
        return 0
    if args.cmd == "run":
        from homelab_agent.app import run

        asyncio.run(run(args.config))
        return 0
    return 1  # pragma: no cover


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
