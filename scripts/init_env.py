"""Fill the secrets of a fresh .env that can be generated locally (stdlib only).

Used by `make init` right after copying .env.example: placeholders and empty values of
PANEL_PASSWORD, METRICS_TOKEN and GRAFANA_ADMIN_PASSWORD become random strings. Values
the user already set are never touched. Tokens given by other services (Telegram, Home
Assistant, cloud APIs) cannot be generated and are left as they are.
"""

from __future__ import annotations

import re
import secrets
import sys
from pathlib import Path

GENERATED = {
    "PANEL_PASSWORD": "change-me-please",
    "METRICS_TOKEN": "",
    "GRAFANA_ADMIN_PASSWORD": "change-me-grafana",
}


def fill(text: str) -> tuple[str, list[str]]:
    done: list[str] = []

    def repl(m: re.Match[str]) -> str:
        key, value = m.group(1), m.group(2).strip()
        if key in GENERATED and value in ("", GENERATED[key]):
            done.append(key)
            return f"{key}={secrets.token_urlsafe(18)}"
        return m.group(0)

    return re.sub(r"(?m)^([A-Z_]+)=(.*)$", repl, text), done


def main(path: str) -> int:
    env = Path(path)
    text, done = fill(env.read_text(encoding="utf-8"))
    env.write_text(text, encoding="utf-8")
    env.chmod(0o600)
    if done:
        print(f"generated in {env}: {', '.join(done)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else ".env"))
