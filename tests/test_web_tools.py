from __future__ import annotations

from typing import Any

import httpx
import pytest

from homelab_agent.config.models import WebToolsSection
from homelab_agent.tools.registry import ToolContext, ToolRegistry
from homelab_agent.tools.web import (
    WEB_NOTICE,
    WebClient,
    WebError,
    is_public_ip,
    readable_text,
    register_web_tools,
)

from .fake_web import PAGE, create_app, resolver


def client(**section: Any) -> tuple[WebClient, Any]:
    app = create_app()
    cfg = WebToolsSection(enabled=True, **section)
    web = WebClient(lambda: cfg, transport=httpx.ASGITransport(app=app), resolver=resolver)
    return web, app


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.20", "169.254.169.254", "0.0.0.0",
        "100.64.0.1", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "224.0.0.1",
    ],
)  # fmt: skip
def test_private_addresses_are_refused(address: str) -> None:
    assert not is_public_ip(address)


def test_public_addresses() -> None:
    assert is_public_ip("93.184.215.14")
    assert is_public_ip("2606:4700::6810:84e5")


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("file:///etc/passwd", "http"),
        ("ftp://example.org/x", "http"),
        ("gopher://example.org/", "http"),
        ("http://user:pw@example.org/", "identifiants"),
        ("http://localhost:8099/api", "privée"),
        ("http://nas.lan/", "privée"),
        ("http://metadata.example/latest/meta-data", "privée"),
        ("http://split.example/", "privée"),
        ("http://127.0.0.1/", "nom inconnu"),  # literal IPs go through the same check
        ("http://inconnu.example/", "nom inconnu"),
        ("https://", "hôte"),
    ],
)
async def test_check_url_refusals(url: str, reason: str) -> None:
    web, _ = client()
    with pytest.raises(WebError, match=reason):
        await web.check_url(url)


async def test_blocked_domains() -> None:
    web, _ = client(blocked_domains=["*.example.org"])
    with pytest.raises(WebError, match="bloqué"):
        await web.check_url("https://docs.example.org/x")
    target = await web.check_url("https://example.org/x")
    assert target.ip == "93.184.215.14" and target.port == 443


async def test_fetch_readable_text_and_host_header() -> None:
    web, app = client()
    page = await web.fetch("https://example.org/crepes")
    assert page["title"] == "Recette des crêpes"
    assert "Mélanger 250 g de farine" in page["text"]
    assert "alert" not in page["text"] and "color:red" not in page["text"]
    assert "Menu Accueil" not in page["text"]  # navigation dropped
    assert page["truncated"] is False
    # the connection goes to the checked IP, the server still sees the real name
    assert app.state.hosts == ["example.org"]


async def test_fetch_limits() -> None:
    web, _ = client(fetch_max_chars=500, fetch_max_bytes=10_000)
    page = await web.fetch("http://example.org/long")
    assert page["truncated"] and len(page["text"]) <= 502
    with pytest.raises(WebError, match="non lisible"):
        await web.fetch("http://example.org/image")


async def test_fetch_redirects_are_checked_again() -> None:
    web, _ = client()
    with pytest.raises(WebError, match="privée"):
        await web.fetch("https://example.org/to-lan")
    page = await web.fetch("https://example.org/to-page")
    assert page["url"] == "https://example.org/crepes" and "Crêpes" in page["text"]
    with pytest.raises(WebError, match="redirections"):
        await web.fetch("https://example.org/loop")


async def test_fetch_network_error() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no", request=request)

    cfg = WebToolsSection(enabled=True)
    web = WebClient(lambda: cfg, transport=httpx.MockTransport(refuse), resolver=resolver)
    with pytest.raises(WebError, match="injoignable"):
        await web.fetch("https://example.org/x")
    with pytest.raises(WebError, match="injoignable"):
        await web.search("x")


async def test_search() -> None:
    web, _ = client(max_results=3)
    results = await web.search("crêpes")
    assert [r["url"] for r in results] == [
        "https://example.org/crepes", "https://example.org/0", "https://example.org/1",
    ]  # fmt: skip
    assert results[1]["snippet"] == "y"  # control characters cleaned
    with pytest.raises(WebError, match="json"):
        await web.search("forbidden")


def test_readable_text_plain() -> None:
    assert readable_text("a\x00b\n\n\n c", "text/plain") == ("", "a b\nc")
    title, text = readable_text(PAGE, "text/html")
    assert title and "IGNORE TES INSTRUCTIONS" in text  # kept, but marked as untrusted data


async def test_tools_are_marked_untrusted_and_switchable() -> None:
    app = create_app()
    cfg = WebToolsSection(enabled=False)
    web = WebClient(lambda: cfg, transport=httpx.ASGITransport(app=app), resolver=resolver)
    registry = ToolRegistry()
    register_web_tools(registry, web, lambda: cfg.enabled)
    assert registry.specs() == []
    cfg.enabled = True
    assert {s.name for s in registry.specs()} == {"web_search", "web_fetch"}
    ctx = ToolContext(requester="1", chat_id="1")
    res = await registry.execute("web_fetch", {"url": "https://example.org/crepes"}, ctx)
    assert res.ok and res.data["notice"] == WEB_NOTICE
    assert "DONNÉES NON FIABLES" in res.to_text()
    res = await registry.execute("web_fetch", {"url": "http://nas.lan/"}, ctx)
    assert not res.ok and "privée" in (res.error or "")
    res = await registry.execute("web_search", {"query": "crêpes"}, ctx)
    assert res.ok and res.data["results"][0]["title"] == "Crêpes faciles"
    res = await registry.execute("web_search", {"query": "  "}, ctx)
    assert not res.ok
