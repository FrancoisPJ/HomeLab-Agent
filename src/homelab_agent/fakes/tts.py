"""Fake text-to-speech server (OpenAI-style ``/v1/audio/speech``).

The "audio" it returns is the UTF-8 text itself, so that the fake speech-to-text server
transcribes it back: tests can check what would have been said.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response


def create_app() -> FastAPI:
    app = FastAPI(title="fake-tts")
    state: dict[str, Any] = {"fail": False, "requests": []}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/_control")
    async def control(request: Request) -> dict[str, Any]:
        state.update(await request.json())
        return {"fail": state["fail"]}

    @app.get("/_test/requests")
    async def requests() -> list[dict[str, Any]]:
        return list(state["requests"])

    @app.post("/v1/audio/speech")
    async def speech(request: Request) -> Response:
        if state["fail"]:
            return JSONResponse({"error": "simulated"}, status_code=500)
        body = await request.json()
        state["requests"].append(body)
        return Response(str(body.get("input", "")).encode(), media_type="audio/ogg")

    return app
