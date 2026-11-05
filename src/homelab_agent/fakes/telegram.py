"""Fake Telegram Bot API (long polling) with test hooks to inject messages and read replies."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import time
from typing import Any

from fastapi import FastAPI, Request, Response


class FakeTelegram:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.updates: list[dict[str, Any]] = []
        self.update_id = 0
        self.message_id = 1000
        self.sent: list[dict[str, Any]] = []  # every sendMessage / edit, in order
        self.messages: dict[int, dict[str, Any]] = {}
        self.callback_answers: list[dict[str, Any]] = []
        self.files: dict[str, bytes] = {}
        self.event = asyncio.Event()

    def _user(self, uid: int) -> dict[str, Any]:
        return {"id": uid, "is_bot": False, "first_name": f"user{uid}"}

    def push(self, update: dict[str, Any]) -> None:
        self.update_id += 1
        update["update_id"] = self.update_id
        self.updates.append(update)
        self.event.set()

    def inject_message(self, user_id: int, chat_id: int, text: str) -> None:
        self.message_id += 1
        self.push(
            {
                "message": {
                    "message_id": self.message_id,
                    "date": int(time.time()),
                    "chat": {"id": chat_id, "type": "private"},
                    "from": self._user(user_id),
                    "text": text,
                }
            }
        )

    def inject_voice(self, user_id: int, chat_id: int, audio: bytes, duration: int) -> None:
        self.message_id += 1
        file_id = f"voice{self.message_id}"
        self.files[file_id] = audio
        self.push(
            {
                "message": {
                    "message_id": self.message_id,
                    "date": int(time.time()),
                    "chat": {"id": chat_id, "type": "private"},
                    "from": self._user(user_id),
                    "voice": {
                        "file_id": file_id,
                        "file_unique_id": file_id,
                        "duration": duration,
                        "mime_type": "audio/ogg",
                        "file_size": len(audio),
                    },
                }
            }
        )

    def inject_callback(self, user_id: int, chat_id: int, message_id: int, data: str) -> None:
        msg = self.messages.get(message_id, {"message_id": message_id})
        self.push(
            {
                "callback_query": {
                    "id": f"cb{self.update_id + 1}",
                    "from": self._user(user_id),
                    "chat_instance": "fake",
                    "data": data,
                    "message": {
                        "message_id": message_id,
                        "date": int(time.time()),
                        "chat": {"id": chat_id, "type": "private"},
                        "text": msg.get("text", ""),
                    },
                }
            }
        )

    def store(
        self, chat_id: int, text: str, markup: Any, message_id: int | None = None
    ) -> dict[str, Any]:
        if message_id is None:
            self.message_id += 1
            message_id = self.message_id
        msg = {
            "message_id": message_id,
            "date": int(time.time()),
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": 1, "is_bot": True, "first_name": "fake_bot"},
            "text": text,
        }
        if markup:
            msg["reply_markup"] = markup
        self.messages[message_id] = msg
        return msg


async def _params(request: Request) -> dict[str, Any]:
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        data: dict[str, Any] = await request.json()
    else:
        form = await request.form()
        data = {k: v for k, v in form.items() if isinstance(v, str)}
    data.update(dict(request.query_params))
    for k in ("reply_markup",):
        if isinstance(data.get(k), str):
            with contextlib.suppress(json.JSONDecodeError):
                data[k] = json.loads(data[k])
    return data


def create_app() -> FastAPI:
    app = FastAPI(title="fake-telegram")
    tg = FakeTelegram()
    app.state.tg = tg

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.api_route("/bot{token}/{method}", methods=["GET", "POST"])
    async def bot_api(token: str, method: str, request: Request) -> dict[str, Any]:
        p = await _params(request)
        m = method.lower()
        if m == "getme":
            return {
                "ok": True,
                "result": {
                    "id": 1,
                    "is_bot": True,
                    "first_name": "fake_bot",
                    "username": "fake_bot",
                },
            }
        if m in ("deletewebhook", "close", "logout", "setmycommands"):
            return {"ok": True, "result": True}
        if m == "getupdates":
            offset = int(p.get("offset") or 0)
            timeout = min(float(p.get("timeout") or 0), 5.0)
            deadline = time.monotonic() + timeout
            while True:
                pending = [u for u in tg.updates if u["update_id"] >= offset]
                if pending or time.monotonic() >= deadline:
                    tg.updates = pending
                    return {"ok": True, "result": pending}
                tg.event.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        tg.event.wait(), timeout=max(0.05, deadline - time.monotonic())
                    )
        if m == "sendmessage":
            msg = tg.store(int(p["chat_id"]), str(p["text"]), p.get("reply_markup"))
            tg.sent.append({"op": "send", **msg})
            return {"ok": True, "result": msg}
        if m == "editmessagetext":
            msg = tg.store(
                int(p["chat_id"]), str(p["text"]), p.get("reply_markup"), int(p["message_id"])
            )
            tg.sent.append({"op": "edit", **msg})
            return {"ok": True, "result": msg}
        if m == "sendvoice":
            form = await request.form()
            upload = form.get("voice")
            if isinstance(upload, str) and upload.startswith("attach://"):
                upload = form.get(upload.removeprefix("attach://"))  # aiogram's multipart style
            audio = (
                await upload.read() if upload is not None and not isinstance(upload, str) else b""
            )
            msg = tg.store(int(str(form.get("chat_id"))), "", None)
            fid = f"sent{msg['message_id']}"
            msg["voice"] = {"file_id": fid, "file_unique_id": fid, "duration": 1}
            tg.files[msg["voice"]["file_id"]] = audio  # downloadable like any voice note
            try:
                msg["voice_text"] = audio.decode("utf-8")  # fake TTS "audio" is plain text
            except UnicodeDecodeError:
                msg["voice_text"] = None
            tg.sent.append({"op": "voice", **msg})
            return {"ok": True, "result": msg}
        if m == "getfile":
            fid = str(p["file_id"])
            if fid not in tg.files:
                return {
                    "ok": False,
                    "error_code": 400,
                    "description": "Bad Request: invalid file_id",
                }
            info = {
                "file_id": fid,
                "file_unique_id": fid,
                "file_size": len(tg.files[fid]),
                "file_path": f"voice/{fid}.oga",
            }
            return {"ok": True, "result": info}
        if m == "answercallbackquery":
            tg.callback_answers.append(p)
            return {"ok": True, "result": True}
        return {
            "ok": False,
            "error_code": 404,
            "description": f"fake: method {method} not supported",
        }

    @app.post("/_test/message")
    async def inject(request: Request) -> dict[str, bool]:
        body = await request.json()
        tg.inject_message(
            int(body["user_id"]), int(body.get("chat_id", body["user_id"])), body["text"]
        )
        return {"ok": True}

    @app.get("/file/bot{token}/voice/{name}")
    async def file_download(token: str, name: str) -> Response:
        fid = name.rsplit(".", 1)[0]
        if fid not in tg.files:
            return Response(status_code=404)
        return Response(tg.files[fid], media_type="audio/ogg")

    @app.post("/_test/voice")
    async def inject_voice(request: Request) -> dict[str, bool]:
        body = await request.json()
        uid = int(body["user_id"])
        audio = body["text"].encode() if "text" in body else base64.b64decode(body["audio_b64"])
        tg.inject_voice(uid, int(body.get("chat_id", uid)), audio, int(body.get("duration", 3)))
        return {"ok": True}

    @app.post("/_test/callback")
    async def inject_cb(request: Request) -> dict[str, bool]:
        body = await request.json()
        tg.inject_callback(int(body["user_id"]), int(body.get("chat_id", body["user_id"])),
                           int(body["message_id"]), body["data"])  # fmt: skip
        return {"ok": True}

    @app.get("/_test/sent")
    async def sent() -> list[dict[str, Any]]:
        return tg.sent

    @app.get("/_test/messages")
    async def messages() -> list[dict[str, Any]]:
        return list(tg.messages.values())

    @app.post("/_test/reset")
    async def reset() -> dict[str, bool]:
        tg.reset()
        return {"ok": True}

    return app
