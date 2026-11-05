from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from homelab_agent.config.models import TelegramSection, VoiceSection
from homelab_agent.engines import render_whisper_env
from homelab_agent.fakes.stt import create_app as fake_stt
from homelab_agent.fakes.telegram import create_app as fake_tg
from homelab_agent.gateway.base import IncomingMessage, Replier
from homelab_agent.gateway.telegram import TelegramFrontend
from homelab_agent.voice.stt import SpeechToText, STTError

from .conftest import base_config
from .test_telegram import FakeBot, _chat, _text

VOICE = VoiceSection(enabled=True, stt_url="http://stt/v1/audio/transcriptions", max_duration_s=60)


def stt_client(app: Any = None, cfg: VoiceSection = VOICE) -> SpeechToText:
    return SpeechToText(cfg, transport=httpx.ASGITransport(app=app or fake_stt()))


async def test_transcribe_and_health() -> None:
    stt = stt_client()
    assert await stt.transcribe("  éteins   le salon ".encode()) == "éteins le salon"
    assert await stt.transcribe(b"\xff\xfe\x00binary") == "transcription simulée"
    assert await stt.health()


async def test_transcription_errors() -> None:
    app = fake_stt()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://x") as c:
        await c.post("/_control", json={"fail": True})
    with pytest.raises(STTError, match="HTTP 500"):
        await stt_client(app).transcribe(b"x")

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no", request=request)

    down = SpeechToText(VOICE, transport=httpx.MockTransport(refuse))
    with pytest.raises(STTError, match="unreachable"):
        await down.transcribe(b"x")
    assert not await down.health()


def frontend(
    bot: FakeBot, voice: VoiceSection | None = VOICE, stt: SpeechToText | None = None
) -> tuple[TelegramFrontend, list[IncomingMessage]]:
    got: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage, replier: Replier) -> None:
        got.append(msg)
        await replier.send(f"ok {msg.text}")

    section = TelegramSection(allowed_user_ids=[111])
    f = TelegramFrontend(
        bot, lambda: section, handler, status_text=_text, model_text=_text,
        jobs_text=_chat, reset_chat=_chat,
        voice=(lambda: voice) if voice else None, stt=stt if stt is not None else stt_client(),
    )  # fmt: skip
    return f, got


async def test_voice_note_is_transcribed_then_handled() -> None:
    bot = FakeBot()
    bot.files["v1"] = "Éteins la lumière du salon".encode()
    f, got = frontend(bot)
    await f.on_voice(111, 111, "v1", 3)
    assert [m.text for m in got] == ["Éteins la lumière du salon"]
    assert bot.texts() == ["🎤 « Éteins la lumière du salon »", "ok Éteins la lumière du salon"]


async def test_voice_refusals() -> None:
    bot = FakeBot()
    bot.files["v1"] = b"bonjour"
    f, got = frontend(bot)
    await f.on_voice(999, 999, "v1", 3)  # stranger: silently ignored, nothing downloaded
    assert bot.ops == []
    await f.on_voice(111, 111, "v1", 600)
    assert "trop long" in bot.texts()[-1] and ("download", "v1") not in bot.ops
    f2, _ = frontend(bot, voice=VoiceSection(enabled=False))
    await f2.on_voice(111, 111, "v1", 3)
    assert "pas activés" in bot.texts()[-1]
    bot.files["empty"] = b"   "
    await f.on_voice(111, 111, "empty", 2)
    assert bot.texts()[-1] == "🎤 Je n'ai rien compris."
    assert got == []


async def test_voice_engine_down() -> None:
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no", request=request)

    bot = FakeBot()
    bot.files["v1"] = b"bonjour"
    f, got = frontend(bot, stt=SpeechToText(VOICE, transport=httpx.MockTransport(refuse)))
    await f.on_voice(111, 111, "v1", 3)
    assert "Transcription impossible" in bot.texts()[-1] and got == []


async def test_voice_without_echo() -> None:
    bot = FakeBot()
    bot.files["v1"] = b"bonjour"
    f, _ = frontend(bot, voice=VOICE.model_copy(update={"echo_transcript": False}))
    await f.on_voice(111, 111, "v1", 3)
    assert bot.texts() == ["ok bonjour"]


def test_whisper_env_rendering() -> None:
    cfg = base_config(
        voice={"enabled": True, "language": "fr"},
        models={
            "items": [
                {
                    "name": "stt",
                    "engine": "whisper",
                    "repo": "ggerganov/whisper.cpp",
                    "file": "ggml-small.bin",
                    "threads": 6,
                }
            ]
        },
    )
    env = render_whisper_env(cfg)
    assert (
        "WHISPER_MODEL=ggml-small.bin" in env
        and "WHISPER_THREADS=6" in env
        and "WHISPER_LANGUAGE=fr" in env
    )


async def test_aiogram_voice_end_to_end(run_server: Any) -> None:
    """Real aiogram download path: getFile + /file/bot<token>/... on the fake Bot API."""
    from homelab_agent.gateway.aiogram_adapter import TelegramGateway

    tg = run_server(fake_tg())
    stt_srv = run_server(fake_stt())
    gw = TelegramGateway("123456:TEST", api_base=tg.url)
    got: list[str] = []

    async def handler(msg: IncomingMessage, replier: Replier) -> None:
        got.append(msg.text)

    voice = VoiceSection(enabled=True, stt_url=f"{stt_srv.url}/v1/audio/transcriptions")
    section = TelegramSection(allowed_user_ids=[111])
    f = TelegramFrontend(
        gw.api, lambda: section, handler, status_text=_text, model_text=_text,
        jobs_text=_chat, reset_chat=_chat, voice=lambda: voice, stt=SpeechToText(voice),
    )  # fmt: skip
    gw.attach(f)
    await gw.start()
    try:
        async with httpx.AsyncClient(base_url=tg.url) as c:
            await c.post("/_test/voice", json={"user_id": 111, "text": "allume la cuisine"})
            for _ in range(200):
                if got:
                    break
                await asyncio.sleep(0.05)
            sent = (await c.get("/_test/sent")).json()
        assert got == ["allume la cuisine"]
        assert sent[0]["text"] == "🎤 « allume la cuisine »"
    finally:
        await gw.stop()
