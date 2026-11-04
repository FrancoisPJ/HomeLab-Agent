from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from homelab_agent.config.models import AppConfig
from homelab_agent.fakes.ha import create_app as fake_ha_app
from homelab_agent.fakes.llm import create_app as fake_llm_app
from homelab_agent.store.db import Database

ROOT = Path(__file__).resolve().parent.parent


def base_config(**overrides: Any) -> AppConfig:
    data: dict[str, Any] = {
        "telegram": {"allowed_user_ids": [111]},
        "backends": {
            "gpu_fast": {
                "kind": "openai_compat",
                "tier": "gpu",
                "url": "http://gpu/v1",
                "model": "fast",
            },
            "cpu_heavy": {
                "kind": "openai_compat",
                "tier": "cpu",
                "url": "http://cpu/v1",
                "model": "heavy",
            },
            "cloud": {
                "kind": "openai_compat",
                "tier": "cloud",
                "url": "http://cloud/v1",
                "model": "big",
            },
        },
        "routing": {"tiers": {"gpu": ["gpu_fast"], "cpu": ["cpu_heavy"], "cloud": ["cloud"]}},
        "cloud": {
            "enabled": True,
            "pricing": {"cloud": {"in_per_mtok": 1000, "out_per_mtok": 1000}},
            "limits": {"monthly_eur": 10, "daily_eur": 10},
        },
        "home_assistant": {"url": "http://ha", "token_env": "HA_TOKEN"},
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value
    return AppConfig.model_validate(data)


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = Database(str(tmp_path / "test.db"))
    await database.init()
    yield database
    await database.close()


@pytest.fixture
def llm_transport() -> httpx.ASGITransport:
    return httpx.ASGITransport(app=fake_llm_app())


@pytest.fixture
def ha_app() -> Any:
    return fake_ha_app()


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class ServerThread:
    """Run an ASGI app with uvicorn in a background thread (for websockets / aiohttp)."""

    def __init__(self, app: Any) -> None:
        self.port = free_port()
        self.app = app
        self.server = uvicorn.Server(
            uvicorn.Config(
                app, host="127.0.0.1", port=self.port, log_level="warning", lifespan="off"
            )
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self) -> ServerThread:
        self.thread.start()
        deadline = time.time() + 10
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("server did not start")
            time.sleep(0.02)
        return self

    def __exit__(self, *exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture
def run_server() -> Iterator[Any]:
    started: list[ServerThread] = []

    def start(app: Any) -> ServerThread:
        srv = ServerThread(app).__enter__()
        started.append(srv)
        return srv

    yield start
    for srv in started:
        srv.__exit__()


async def wait_for(predicate: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met in time")
