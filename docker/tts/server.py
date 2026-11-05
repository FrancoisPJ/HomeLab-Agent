"""Text-to-speech gateway for homelab-agent (standard library only).

* starts the official Piper HTTP server (GPL-3.0, a separate program in this image) on
  127.0.0.1:5000 and keeps the voice loaded,
* exposes an OpenAI-style ``POST /v1/audio/speech`` endpoint
  (``{"input": "...", "response_format": "opus" | "wav", "speed": 1.0}``),
* converts Piper's WAV into OGG/Opus with ffmpeg: the format Telegram plays as a voice note.

This file does not import Piper: the two programs only talk over HTTP.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PIPER_PORT = 5000
PORT = int(os.environ.get("TTS_PORT", "8080"))
MODELS = os.environ.get("TTS_MODELS_DIR", "/models")
VOICE = os.environ.get("TTS_VOICE", "fr_FR-siwis-medium")
MAX_CHARS = int(os.environ.get("TTS_MAX_CHARS", "1500"))
log = logging.getLogger("tts")


def start_piper() -> subprocess.Popen[bytes]:
    cmd = [
        sys.executable, "-m", "piper.http_server",
        "--host", "127.0.0.1", "--port", str(PIPER_PORT),
        "-m", f"{MODELS}/{VOICE}.onnx", "--data-dir", MODELS,
    ]  # fmt: skip
    log.info("starting piper: %s", " ".join(cmd))
    return subprocess.Popen(cmd)


def piper_ready() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PIPER_PORT}/info", timeout=2):
            return True
    except (urllib.error.URLError, OSError):
        return False


def synthesize_wav(text: str, speed: float) -> bytes:
    body = json.dumps({"text": text, "length_scale": 1.0 / max(0.5, min(speed, 2.0))}).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{PIPER_PORT}/synthesize",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return bytes(resp.read())


def wav_to_opus(wav: bytes) -> bytes:
    proc = subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-i",
            "pipe:0",
            "-c:a",
            "libopus",
            "-b:a",
            "32k",
            "-application",
            "voip",
            "-f",
            "ogg",
            "pipe:1",
        ],  # fmt: skip
        input=wav,
        capture_output=True,
        timeout=60,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.decode(errors="replace")[:300])
    return proc.stdout


class Handler(BaseHTTPRequestHandler):
    server_version = "homelab-tts/1"

    def _send(self, status: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str) -> None:
        self._send(status, json.dumps({"error": message}).encode())

    def do_GET(self) -> None:
        if self.path == "/health":
            ok = piper_ready()
            self._send(
                200 if ok else 503, json.dumps({"status": "ok" if ok else "loading"}).encode()
            )
        else:
            self._error(404, "not found")

    def do_POST(self) -> None:
        if self.path not in ("/v1/audio/speech", "/speech"):
            self._error(404, "not found")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(min(length, 64_000)) or b"{}")
            text = str(payload.get("input", "")).strip()[:MAX_CHARS]
            fmt = str(payload.get("response_format", "opus"))
            speed = float(payload.get("speed", 1.0))
        except (ValueError, TypeError):
            self._error(400, "invalid JSON body")
            return
        if not text:
            self._error(400, "empty input")
            return
        try:
            wav = synthesize_wav(text, speed)
            if fmt == "wav":
                self._send(200, wav, "audio/wav")
            else:
                self._send(200, wav_to_opus(wav), "audio/ogg")
        except Exception as exc:
            log.exception("synthesis failed")
            self._error(500, f"synthesis failed: {exc.__class__.__name__}")

    def log_message(self, fmt: str, *args: object) -> None:
        log.info("%s %s", self.address_string(), fmt % args)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    piper = start_piper()

    def watch() -> None:  # the container must stop if piper dies (restart policy)
        piper.wait()
        log.error("piper exited with %s", piper.returncode)
        os._exit(1)

    threading.Thread(target=watch, daemon=True).start()
    for _ in range(120):
        if piper_ready():
            break
        time.sleep(0.5)
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info("tts listening on :%d (voice %s)", PORT, VOICE)
    server.serve_forever()


if __name__ == "__main__":
    main()
