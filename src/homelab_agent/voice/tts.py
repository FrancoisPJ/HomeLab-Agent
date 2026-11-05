"""Text-to-speech client (OpenAI-style ``POST /v1/audio/speech``), for voice replies.

Served by docker/tts (Piper + ffmpeg, OGG/Opus output) or any compatible server.
"""

from __future__ import annotations

import re
import unicodedata

import httpx

from homelab_agent.config.models import VoiceSection


class TTSError(Exception):
    pass


SPOKEN_SERVICES = {
    "turn_off": "éteint", "turn_on": "allumé", "toggle": "basculé", "open_cover": "ouvert",
    "close_cover": "fermé", "lock": "verrouillé", "unlock": "déverrouillé", "press": "appuyé",
    "trigger": "déclenché",
}  # fmt: skip
_ACTION_RE = re.compile(r"\b([a-z_]+)\.([a-z_]+) → (.+)$", re.M)


def _spoken_action(m: re.Match[str]) -> str:
    # tested with the real Piper + whisper pair: the quotes add a short pause that keeps the
    # device name intelligible ("salon : éteint." was heard as "Salon est un")
    domain, service, names = m.group(1), m.group(2), m.group(3)
    if domain == "scene":
        return f"C'est fait. J'ai activé la scène « {names} »."
    word = SPOKEN_SERVICES.get(service, service.replace("_", " "))
    return f"C'est fait. J'ai {word} « {names} »."


def speakable(text: str, max_chars: int) -> str:
    """Turn a chat answer into something pleasant to hear.

    Drops emoji and symbols, markdown marks, code and URLs; keeps whole sentences up to
    ``max_chars``.
    """
    text = re.sub(r"```.*?```", " ", text, flags=re.S)  # code blocks are not read aloud
    text = _ACTION_RE.sub(_spoken_action, text)  # "light.turn_off → salon" -> "salon : éteint."
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"\[actions effectuées : .*?\]", " ", text)
    text = "".join(" " if unicodedata.category(c) in ("So", "Sk", "Cs", "Co") else c for c in text)
    text = re.sub(r"[*_`#>|~]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    return (cut[: end + 1] if end > max_chars // 3 else cut.rsplit(" ", 1)[0] + "…").strip()


class TextToSpeech:
    def __init__(
        self,
        cfg: VoiceSection,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.cfg = cfg
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(cfg.tts_timeout_s, connect=5.0),
            transport=transport,
        )

    async def synthesize(self, text: str) -> bytes:
        """OGG/Opus audio of ``text`` (already made speakable)."""
        if not self.cfg.tts_url:
            raise TTSError("voice.tts_url is not set")
        payload = {
            "input": text,
            "model": "tts-1",
            "voice": self.cfg.tts_voice or "default",
            "response_format": "opus",
            "speed": self.cfg.tts_speed,
        }
        try:
            resp = await self._client.post(self.cfg.tts_url, json=payload)
        except httpx.HTTPError as exc:
            raise TTSError(f"text-to-speech unreachable: {exc.__class__.__name__}") from exc
        if resp.status_code >= 400 or not resp.content:
            raise TTSError(f"text-to-speech HTTP {resp.status_code}")
        return resp.content

    async def aclose(self) -> None:
        await self._client.aclose()
