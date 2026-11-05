"""Wire ``TelegramFrontend`` to the Telegram Bot API with aiogram 3 (long polling)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from aiogram.types import Message as TgMessage

from homelab_agent.gateway.base import Gateway
from homelab_agent.gateway.telegram import Buttons, TelegramFrontend, keyboard_rows

log = logging.getLogger(__name__)


def _markup(buttons: Buttons | None) -> InlineKeyboardMarkup | None:
    rows = keyboard_rows(buttons)
    if not rows:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows
        ]
    )


def _attachment(message: TgMessage) -> tuple[str | None, str | None]:
    """(kind, file name) of a photo or file the agent cannot read yet; (None, None) else."""
    if message.photo:
        return "photo", None
    if message.document is not None:
        return "document", message.document.file_name
    for kind in ("video", "audio", "animation", "video_note"):
        media = getattr(message, kind, None)
        if media is not None:
            return kind, getattr(media, "file_name", None)
    return None, None


class AiogramBotApi:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def _retry(self, coro_fn: Any) -> Any:
        for _ in range(3):
            try:
                return await coro_fn()
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after)
        return await coro_fn()

    async def send_message(self, chat_id: str, text: str, buttons: Buttons | None = None) -> int:
        msg = await self._retry(
            lambda: self.bot.send_message(int(chat_id), text, reply_markup=_markup(buttons))
        )
        return int(msg.message_id)

    async def edit_message_text(
        self, chat_id: str, message_id: int, text: str, buttons: Buttons | None = None
    ) -> None:
        try:
            await self._retry(
                lambda: self.bot.edit_message_text(
                    text=text,
                    chat_id=int(chat_id),
                    message_id=message_id,
                    reply_markup=_markup(buttons),
                )
            )
        except TelegramBadRequest as exc:
            if "not modified" not in str(exc):
                raise

    async def send_voice(self, chat_id: str, audio: bytes) -> None:
        await self._retry(
            lambda: self.bot.send_voice(int(chat_id), BufferedInputFile(audio, "reponse.ogg"))
        )

    async def send_photo(self, chat_id: str, image: bytes, caption: str = "") -> None:
        await self._retry(
            lambda: self.bot.send_photo(
                int(chat_id), BufferedInputFile(image, "camera.jpg"), caption=caption or None
            )
        )

    async def download_file(self, file_id: str) -> bytes:
        buf = await self.bot.download(file_id)
        if buf is None:
            raise RuntimeError("empty download")
        return buf.read()

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        with contextlib.suppress(TelegramBadRequest):
            await self.bot.answer_callback_query(callback_id, text=text or None)


class TelegramGateway(Gateway):
    name = "telegram"

    def __init__(self, token: str, api_base: str | None = None) -> None:
        session = None
        if api_base:
            session = AiohttpSession(api=TelegramAPIServer.from_base(api_base))
        self.bot = Bot(token=token, session=session)
        self.api = AiogramBotApi(self.bot)
        self.dp = Dispatcher()
        self.frontend: TelegramFrontend | None = None
        self._task: asyncio.Task[None] | None = None

    def attach(self, frontend: TelegramFrontend) -> None:
        self.frontend = frontend
        front = frontend

        @self.dp.message()
        async def on_message(message: TgMessage) -> None:
            if message.from_user is None:
                return
            if message.voice is not None:
                v = message.voice
                await front.on_voice(
                    message.from_user.id, message.chat.id, v.file_id, int(v.duration or 0),
                    v.mime_type or "audio/ogg",
                )  # fmt: skip
                return
            if message.text is not None:
                await front.on_message(message.from_user.id, message.chat.id, message.text)
                return
            kind, name = _attachment(message)
            if kind is not None:
                await front.on_attachment(
                    message.from_user.id, message.chat.id, kind, message.caption or "", name
                )

        @self.dp.callback_query()
        async def on_callback(query: CallbackQuery) -> None:
            chat_id = query.message.chat.id if query.message else query.from_user.id
            msg_id = query.message.message_id if query.message else None
            await front.on_callback(query.from_user.id, chat_id, query.id, query.data or "", msg_id)

    async def start(self) -> None:
        if self.frontend is None:
            raise RuntimeError("attach() a frontend before start()")
        self._task = asyncio.create_task(
            self.dp.start_polling(self.bot, handle_signals=False, close_bot_session=False),
            name="telegram-polling",
        )

    async def stop(self) -> None:
        with contextlib.suppress(Exception):
            await self.dp.stop_polling()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task
        await self.bot.session.close()
