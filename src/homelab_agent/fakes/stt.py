"""Fake speech-to-text server (OpenAI-style ``/v1/audio/transcriptions`` + ``/inference``).

The "audio" is expected to be UTF-8 text in tests: it is returned as the transcription.
Real audio (undecodable bytes) gives a fixed sentence.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


def create_app() -> FastAPI:
    app = FastAPI(title="fake-stt")
    state: dict[str, Any] = {"fail": False, "requests": []}

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/_control")
    async def control(request: Request) -> dict[str, Any]:
        state.update(await request.json())
        return {"fail": state["fail"]}

    async def transcribe(request: Request) -> Any:
        if state["fail"]:
            return JSONResponse({"error": "simulated"}, status_code=500)
        form = await request.form()
        upload = form.get("file")
        if upload is None or isinstance(upload, str):
            return JSONResponse({"error": "no file"}, status_code=400)
        audio = await upload.read()
        state["requests"].append({"language": form.get("language"), "size": len(audio)})
        try:
            text = audio.decode("utf-8")
        except UnicodeDecodeError:
            text = "transcription simulée"
        return {"text": f" {text} "}

    app.add_api_route("/v1/audio/transcriptions", transcribe, methods=["POST"])
    app.add_api_route("/inference", transcribe, methods=["POST"])
    return app
