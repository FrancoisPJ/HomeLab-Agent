"""Telegram front-end logic (allowlist, commands, streaming, confirmations).

Kept independent from the Telegram library through the small ``BotApi`` protocol, so that
it is unit-testable; ``aiogram_adapter`` wires it to the real Bot API (long polling).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Protocol

from homelab_agent.config.models import TelegramSection, Tier, VoiceSection
from homelab_agent.gateway.base import ActionButton, IncomingMessage, Replier, StreamHandle
from homelab_agent.observability.metrics import METRICS
from homelab_agent.voice.stt import SpeechToText, STTError
from homelab_agent.voice.tts import TextToSpeech, TTSError, speakable

log = logging.getLogger(__name__)

Button = tuple[str, str]  # (label, callback data)
#: one row of buttons, or several rows
Buttons = list[Button] | list[list[Button]]


def keyboard_rows(buttons: Buttons | None) -> list[list[Button]]:
    if not buttons:
        return []
    if isinstance(buttons[0], list):
        return [list(row) for row in buttons]
    return [list(buttons)]


class BotApi(Protocol):
    async def send_message(
        self, chat_id: str, text: str, buttons: Buttons | None = None
    ) -> int: ...

    async def edit_message_text(
        self, chat_id: str, message_id: int, text: str, buttons: Buttons | None = None
    ) -> None: ...

    async def answer_callback(self, callback_id: str, text: str = "") -> None: ...

    async def download_file(self, file_id: str) -> bytes: ...

    async def send_voice(self, chat_id: str, audio: bytes) -> None: ...


def split_text(text: str, limit: int) -> list[str]:
    """Split on paragraph/line boundaries when possible, never above ``limit``."""
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = rest.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest:
        chunks.append(rest)
    return chunks


HELP_TEXT = """Commandes :
/status – état des moteurs et du budget
/model – moteurs et modèles configurés
/cloud <texte> – forcer le cloud
/deep <texte> – forcer le CPU (réponse différée)
/jobs – tâches en cours
/reset – oublier la conversation
/help – cette aide
Sinon, écris simplement ta demande."""


class StreamEditor:
    """Streams an answer by editing one message, respecting a minimum edit interval."""

    def __init__(
        self,
        api: BotApi,
        chat_id: str,
        *,
        interval_s: float,
        max_chars: int,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api = api
        self.chat_id = chat_id
        self.interval_s = interval_s
        self.max_chars = max_chars
        self.clock = clock
        self.message_id: int | None = None
        self._last_edit = 0.0
        self._shown = ""

    async def _show(self, text: str) -> None:
        text = text[: self.max_chars]
        if text == self._shown or not text.strip():
            return
        if self.message_id is None:
            self.message_id = await self.api.send_message(self.chat_id, text)
        else:
            await self.api.edit_message_text(self.chat_id, self.message_id, text)
        self._shown = text
        self._last_edit = self.clock()

    async def update(self, text: str) -> None:
        if self.message_id is not None and self.clock() - self._last_edit < self.interval_s:
            return
        preview = text if len(text) <= self.max_chars - 2 else text[: self.max_chars - 2]
        await self._show(preview + " …")

    async def finish(self, text: str) -> None:
        chunks = split_text(text, self.max_chars)
        await self._show(chunks[0])
        for chunk in chunks[1:]:
            await self.api.send_message(self.chat_id, chunk)


class _CapturingReplier:
    """Wraps a replier and remembers the final answers (for the voice reply)."""

    def __init__(self, inner: Replier) -> None:
        self.inner = inner
        self.answers: list[str] = []

    async def send(self, text: str) -> None:
        self.answers.append(text)
        await self.inner.send(text)

    def stream(self) -> StreamHandle:
        handle = self.inner.stream()
        outer = self

        class _H:
            async def update(self, text: str) -> None:
                await handle.update(text)

            async def finish(self, text: str) -> None:
                outer.answers.append(text)
                await handle.finish(text)

        return _H()

    async def confirm(self, question: str) -> bool:
        return await self.inner.confirm(question)

    async def send_actions(self, text: str, actions: list[ActionButton]) -> None:
        self.answers.append(text)
        send_actions = getattr(self.inner, "send_actions", None)
        if send_actions is not None:
            await send_actions(text, actions)
        else:
            await self.inner.send(text)

    def last_answer(self) -> str:
        skip = ("🎤", "↗️", "ℹ️", "⏳", "⚠️ Budget")
        for text in reversed(self.answers):
            if not text.startswith(skip):
                return text
        return ""


class TelegramReplier:
    def __init__(
        self, frontend: TelegramFrontend, chat_id: str, user_id: str | None = None
    ) -> None:
        self.f = frontend
        self.chat_id = chat_id
        self.user_id = user_id  # who must answer confirmations (None: anyone allowed)

    async def send(self, text: str) -> None:
        for chunk in split_text(text, self.f.cfg().max_message_chars):
            await self.f.api.send_message(self.chat_id, chunk)

    def stream(self) -> StreamHandle:
        cfg = self.f.cfg()
        return StreamEditor(
            self.f.api,
            self.chat_id,
            interval_s=cfg.stream_edit_interval_s,
            max_chars=cfg.max_message_chars,
            clock=self.f.clock,
        )

    async def confirm(self, question: str) -> bool:
        return await self.f.ask_confirmation(self.chat_id, question, self.user_id)

    async def send_actions(self, text: str, actions: list[ActionButton]) -> None:
        await self.f.notify_actions(self.chat_id, text, actions, ttl_s=3600)


Handler = Callable[[IncomingMessage, Replier], Awaitable[None]]
TextProvider = Callable[[], Awaitable[str]]
ChatAction = Callable[[str], Awaitable[str]]
#: extra command: (description, fn(chat_id, argument) -> reply text)
#: (returning None means the command already replied by itself)
ChatCommand = tuple[str, Callable[[str, str], Awaitable[str | None]]]
#: callback handler: (user_id, chat_id, message_id, payload, callback_id)
CallbackHandler = Callable[[str, str, int | None, str, str], Awaitable[None]]


class TelegramFrontend:
    def __init__(
        self,
        api: BotApi,
        get_section: Callable[[], TelegramSection],
        handler: Handler,
        *,
        status_text: TextProvider,
        model_text: TextProvider,
        jobs_text: ChatAction,
        reset_chat: ChatAction,
        commands: dict[str, ChatCommand] | None = None,
        voice: Callable[[], VoiceSection] | None = None,
        stt: SpeechToText | None = None,
        tts: TextToSpeech | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api = api
        self.cfg = get_section
        self.handler = handler
        self.status_text = status_text
        self.model_text = model_text
        self.jobs_text = jobs_text
        self.reset_chat = reset_chat
        self.clock = clock
        self.commands = commands or {}
        self.voice = voice
        self.stt = stt
        self.tts = tts
        self._pending: dict[str, tuple[str, asyncio.Future[bool], str | None]] = {}
        self._actions: dict[str, tuple[str, ActionButton, float]] = {}
        self.callbacks: dict[str, CallbackHandler] = {}  # prefix -> handler (e.g. "hm")

    def help_text(self) -> str:
        extra = "".join(f"\n/{name} – {desc}" for name, (desc, _) in sorted(self.commands.items()))
        head, _, tail = HELP_TEXT.partition("\n/help")
        return head + extra + "\n/help" + tail

    # ------------------------------------------------------------- notifier
    async def notify(self, chat_id: str, text: str) -> None:
        await TelegramReplier(self, chat_id).send(text)

    def replier_for(self, chat_id: str) -> Replier:
        return TelegramReplier(self, chat_id)

    async def notify_actions(
        self, chat_id: str, text: str, actions: list[ActionButton], ttl_s: float
    ) -> None:
        now = self.clock()
        self._actions = {k: v for k, v in self._actions.items() if v[2] > now}
        buttons: list[Button] = []
        for action in actions:
            token = uuid.uuid4().hex[:12]
            self._actions[token] = (chat_id, action, now + ttl_s)
            buttons.append((action.label, f"ac:{token}"))
        await self.api.send_message(chat_id, text[: self.cfg().max_message_chars], buttons=buttons)

    # ------------------------------------------------------------ allowlist
    def is_allowed(self, user_id: int | str) -> bool:
        try:
            uid = int(user_id)
        except (TypeError, ValueError):
            return False
        return uid in self.cfg().allowed_user_ids

    # ------------------------------------------------------------- messages
    async def on_message(self, user_id: int | str, chat_id: int | str, text: str) -> None:
        if not self.is_allowed(user_id):
            METRICS.inc("telegram_rejected_total")
            log.warning("ignored message from non-allowlisted user %s", user_id)
            return  # silent refusal
        chat = str(chat_id)
        replier = TelegramReplier(self, chat, str(user_id))
        text = (text or "").strip()
        if not text:
            return
        if text.startswith("/"):
            cmd, _, arg = text.partition(" ")
            cmd = cmd.split("@", 1)[0].lower()
            arg = arg.strip()
            if cmd in ("/start", "/help"):
                await replier.send(self.help_text())
            elif cmd == "/status":
                await replier.send(await self.status_text())
            elif cmd == "/model":
                await replier.send(await self.model_text())
            elif cmd == "/jobs":
                await replier.send(await self.jobs_text(chat))
            elif cmd == "/reset":
                await replier.send(await self.reset_chat(chat))
            elif cmd in ("/cloud", "/deep"):
                if not arg:
                    await replier.send(f"Usage : {cmd} <ta demande>")
                    return
                tier = Tier.CLOUD if cmd == "/cloud" else Tier.CPU
                await self._run(IncomingMessage(str(user_id), chat, arg, tier), replier)
            elif cmd[1:] in self.commands:
                try:
                    reply = await self.commands[cmd[1:]][1](chat, arg)
                    if reply is not None:
                        await replier.send(reply)
                except Exception:
                    log.exception("command %s failed", cmd)
                    await replier.send("❌ Erreur interne, voir les journaux.")
            else:
                await replier.send("Commande inconnue. /help pour la liste.")
            return
        await self._run_and_speak(IncomingMessage(str(user_id), chat, text), replier, spoke=False)

    async def on_voice(
        self,
        user_id: int | str,
        chat_id: int | str,
        file_id: str,
        duration_s: int,
        mime: str = "audio/ogg",
    ) -> None:
        """Voice note: transcribe locally, show what was understood, then handle as text."""
        if not self.is_allowed(user_id):
            METRICS.inc("telegram_rejected_total")
            log.warning("ignored voice message from non-allowlisted user %s", user_id)
            return
        replier = TelegramReplier(self, str(chat_id), str(user_id))
        section = self.voice() if self.voice else None
        if section is None or not section.enabled or self.stt is None:
            await replier.send("🎤 Les messages vocaux ne sont pas activés (voice.enabled).")
            return
        if duration_s > section.max_duration_s:
            await replier.send(
                f"🎤 Message trop long ({duration_s} s, maximum {section.max_duration_s} s)."
            )
            return
        METRICS.inc("voice_messages_total")
        try:
            audio = await self.api.download_file(file_id)
            text = await self.stt.transcribe(audio, "voice.ogg", mime)
        except STTError as exc:
            log.warning("transcription failed: %s", exc)
            await replier.send("❌ Transcription impossible (moteur vocal indisponible).")
            return
        except Exception:
            log.exception("voice message failed")
            await replier.send("❌ Impossible de récupérer le message vocal.")
            return
        text = text.strip().lstrip("/")
        if not text:
            await replier.send("🎤 Je n'ai rien compris.")
            return
        if section.echo_transcript:
            await replier.send(f"🎤 « {text} »")
        await self._run_and_speak(
            IncomingMessage(str(user_id), str(chat_id), text), replier, spoke=True
        )

    async def _run_and_speak(self, msg: IncomingMessage, replier: Replier, spoke: bool) -> None:
        """Handle a message; answer with a voice note too when voice.reply_voice asks for it."""
        section = self.voice() if self.voice else None
        mode = section.reply_voice if section is not None and section.enabled else "never"
        if self.tts is None or mode == "never" or (mode == "when_voice" and not spoke):
            await self._run(msg, replier)
            return
        assert section is not None
        capture = _CapturingReplier(replier)
        await self._run(msg, capture)
        answer = capture.last_answer()
        if not answer:
            return
        text = speakable(answer, section.tts_max_chars)
        if not text:
            return
        try:
            audio = await self.tts.synthesize(text)
            await self.api.send_voice(msg.chat_id, audio)
            METRICS.inc("voice_replies_total")
        except TTSError as exc:
            log.warning("voice reply failed: %s", exc)

    async def _run(self, msg: IncomingMessage, replier: Replier) -> None:
        try:
            await self.handler(msg, replier)
        except Exception:
            log.exception("error while handling a message")
            await replier.send("❌ Erreur interne, voir les journaux.")

    # -------------------------------------------------------- confirmations
    async def ask_confirmation(
        self, chat_id: str, question: str, user_id: str | None = None
    ) -> bool:
        token = uuid.uuid4().hex[:12]
        fut: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._pending[token] = (chat_id, fut, user_id)
        msg_id = await self.api.send_message(
            chat_id, question, buttons=[("✅ Oui", f"cf:{token}:y"), ("❌ Non", f"cf:{token}:n")]
        )
        try:
            answer = await asyncio.wait_for(fut, timeout=self.cfg().confirm_timeout_s)
        except TimeoutError:
            answer = False
            await self.api.edit_message_text(chat_id, msg_id, question + "\n⌛ Expiré.")
        finally:
            self._pending.pop(token, None)
        return answer

    async def on_callback(
        self,
        user_id: int | str,
        chat_id: int | str,
        callback_id: str,
        data: str,
        message_id: int | None = None,
    ) -> None:
        if not self.is_allowed(user_id):
            log.warning("ignored callback from non-allowlisted user %s", user_id)
            return
        prefix, _, payload = data.partition(":")
        if prefix in self.callbacks:
            await self.callbacks[prefix](
                str(user_id), str(chat_id), message_id, payload, callback_id
            )
            return
        parts = data.split(":")
        if len(parts) == 2 and parts[0] == "ac":
            await self._run_action(str(user_id), str(chat_id), callback_id, parts[1], message_id)
            return
        if len(parts) != 3 or parts[0] != "cf":
            await self.api.answer_callback(callback_id)
            return
        pending = self._pending.get(parts[1])
        if pending is None or pending[0] != str(chat_id) or pending[1].done():
            await self.api.answer_callback(callback_id, "Demande expirée.")
            return
        if pending[2] is not None and pending[2] != str(user_id):
            await self.api.answer_callback(
                callback_id, "Seul l'auteur de la demande peut répondre."
            )
            return
        yes = parts[2] == "y"
        pending[1].set_result(yes)
        await self.api.answer_callback(callback_id, "Confirmé" if yes else "Annulé")
        if message_id is not None:
            await self.api.edit_message_text(
                str(chat_id), message_id, "✅ Confirmé." if yes else "❌ Annulé."
            )

    async def _run_action(
        self, user_id: str, chat_id: str, callback_id: str, token: str, message_id: int | None
    ) -> None:
        entry = self._actions.get(token)
        if entry is not None and entry[2] < self.clock():
            del self._actions[token]
            entry = None
        if entry is None or entry[0] != chat_id:
            await self.api.answer_callback(callback_id, "Bouton expiré.")
            return
        del self._actions[token]  # one shot
        await self.api.answer_callback(callback_id, "En cours…")
        try:
            result = await entry[1].run(user_id, TelegramReplier(self, chat_id))
        except Exception:
            log.exception("alert action failed")
            result = "❌ Erreur interne."
        await self.api.send_message(chat_id, result)
