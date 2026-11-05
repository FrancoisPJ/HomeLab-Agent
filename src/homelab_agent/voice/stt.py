"""Speech-to-text client for OpenAI-style ``/v1/audio/transcriptions`` endpoints.

Works with the whisper.cpp server shipped in docker/whisper.Dockerfile (started with
``--inference-path /v1/audio/transcriptions --convert``) and with other compatible servers.
"""

from __future__ import annotations

import httpx

from homelab_agent.config.models import VoiceSection


class STTError(Exception):
    """Transcription failed (server unreachable, bad audio...)."""


class SpeechToText:
    def __init__(
        self,
        cfg: VoiceSection,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.cfg = cfg
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._client = httpx.AsyncClient(
            headers=headers, timeout=httpx.Timeout(cfg.timeout_s, connect=5.0), transport=transport
        )

    async def transcribe(
        self, audio: bytes, filename: str = "voice.ogg", mime: str = "audio/ogg"
    ) -> str:
        data = {
            "model": self.cfg.stt_model,
            "language": self.cfg.language,
            "response_format": "json",
            "temperature": "0",
        }
        try:
            resp = await self._client.post(
                self.cfg.stt_url, data=data, files={"file": (filename, audio, mime)}
            )
        except httpx.HTTPError as exc:
            raise STTError(f"speech-to-text unreachable: {exc.__class__.__name__}") from exc
        if resp.status_code >= 400:
            raise STTError(f"speech-to-text HTTP {resp.status_code}: {resp.text[:200]}")
        try:
            payload = resp.json()
        except ValueError:
            return resp.text.strip()
        text = payload.get("text") if isinstance(payload, dict) else None
        if not isinstance(text, str):
            raise STTError("speech-to-text: unexpected response")
        return " ".join(text.split())

    async def health(self) -> bool:
        base = self.cfg.stt_url.split("/v1/")[0]
        try:
            resp = await self._client.get(f"{base}/health", timeout=5.0)
        except httpx.HTTPError:
            return False
        return resp.status_code < 500

    async def aclose(self) -> None:
        await self._client.aclose()
