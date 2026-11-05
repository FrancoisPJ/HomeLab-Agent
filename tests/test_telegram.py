from __future__ import annotations

import asyncio
from typing import Any

import httpx

from homelab_agent.config.models import TelegramSection, Tier
from homelab_agent.fakes.telegram import create_app as fake_tg
from homelab_agent.gateway.base import IncomingMessage, Replier
from homelab_agent.gateway.telegram import (
    HELP_TEXT,
    Button,
    StreamEditor,
    TelegramFrontend,
    split_text,
)

from .conftest import wait_for


class FakeBot:
    def __init__(self) -> None:
        self.ops: list[tuple[str, Any]] = []
        self.next_id = 100
        self.files: dict[str, bytes] = {}

    async def send_message(
        self, chat_id: str, text: str, buttons: list[Button] | None = None
    ) -> int:
        self.next_id += 1
        self.ops.append(("send", (chat_id, text, buttons)))
        return self.next_id

    async def edit_message_text(
        self, chat_id: str, message_id: int, text: str, buttons: list[Button] | None = None
    ) -> None:
        if buttons:
            self.ops.append(("edit", (chat_id, message_id, text, buttons)))
        else:
            self.ops.append(("edit", (chat_id, message_id, text)))

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.ops.append(("answer", (callback_id, text)))

    async def send_voice(self, chat_id: str, audio: bytes) -> None:
        self.ops.append(("voice", (chat_id, audio)))

    async def send_photo(self, chat_id: str, image: bytes, caption: str = "") -> None:
        self.ops.append(("photo", (chat_id, image, caption)))

    async def download_file(self, file_id: str) -> bytes:
        self.ops.append(("download", file_id))
        return self.files[file_id]

    def texts(self) -> list[str]:
        return [
            op[1][1] if op[0] == "send" else op[1][2]
            for op in self.ops
            if op[0] in ("send", "edit")
        ]


async def _text() -> str:
    return "status!"


async def _chat(chat: str) -> str:
    return f"done {chat}"


def frontend(bot: FakeBot, handler: Any = None, **cfg: Any) -> TelegramFrontend:
    section = TelegramSection(allowed_user_ids=[111], confirm_timeout_s=5, **cfg)
    received: list[IncomingMessage] = []

    async def default_handler(msg: IncomingMessage, replier: Replier) -> None:
        received.append(msg)
        await replier.send(f"echo {msg.text}")

    f = TelegramFrontend(
        bot, lambda: section, handler or default_handler,
        status_text=_text, model_text=_text, jobs_text=_chat, reset_chat=_chat,
    )  # fmt: skip
    f.received = received  # type: ignore[attr-defined]
    return f


async def test_unknown_user_is_silently_ignored() -> None:
    bot = FakeBot()
    f = frontend(bot)
    await f.on_message(999, 999, "salut")
    await f.on_message("not-a-number", 1, "salut")
    await f.on_callback(999, 999, "cb", "cf:x:y")
    assert bot.ops == []


async def test_commands() -> None:
    bot = FakeBot()
    f = frontend(bot)
    await f.on_message(111, 111, "/help")
    await f.on_message(111, 111, "/status")
    await f.on_message(111, 111, "/model@my_bot")
    await f.on_message(111, 111, "/jobs")
    await f.on_message(111, 111, "/reset")
    await f.on_message(111, 111, "/cloud")
    await f.on_message(111, 111, "/nope")
    texts = bot.texts()
    assert texts[0] == HELP_TEXT
    assert texts[1:5] == ["status!", "status!", "done 111", "done 111"]
    assert texts[5].startswith("Usage")
    assert "inconnue" in texts[6]


async def test_forced_tiers_and_plain_text() -> None:
    bot = FakeBot()
    f = frontend(bot)
    await f.on_message(111, 222, "/cloud explique la relativité")
    await f.on_message(111, 222, "/deep rédige un rapport")
    await f.on_message(111, 222, "bonjour")
    got = f.received  # type: ignore[attr-defined]
    assert [(m.text, m.forced_tier) for m in got] == [
        ("explique la relativité", Tier.CLOUD),
        ("rédige un rapport", Tier.CPU),
        ("bonjour", None),
    ]
    assert got[0].chat_id == "222"


async def test_handler_crash_is_reported() -> None:
    bot = FakeBot()

    async def boom(msg: IncomingMessage, replier: Replier) -> None:
        raise RuntimeError("x")

    await frontend(bot, boom).on_message(111, 111, "hi")
    assert "Erreur interne" in bot.texts()[0]


def test_split_text() -> None:
    text = ("paragraphe " * 50 + "\n\n") * 10
    chunks = split_text(text, 1000)
    assert all(len(c) <= 1000 for c in chunks)
    assert "".join(chunks).replace(" ", "").replace("\n", "") == text.replace(" ", "").replace(
        "\n", ""
    )
    assert split_text("x" * 2500, 1000) == ["x" * 1000, "x" * 1000, "x" * 500]


async def test_stream_editor_rate_limit_and_split() -> None:
    bot = FakeBot()
    now = [0.0]
    ed = StreamEditor(bot, "1", interval_s=1.0, max_chars=1000, clock=lambda: now[0])
    await ed.update("Bon")
    await ed.update("Bonjour")  # too soon: skipped
    now[0] = 1.5
    await ed.update("Bonjour à")
    await ed.finish("Bonjour à toi " + "y" * 1500)
    kinds = [op[0] for op in bot.ops]
    assert kinds == ["send", "edit", "edit", "send", "send"]
    assert bot.ops[0][1][1] == "Bon …"
    assert len(bot.ops[2][1][2]) <= 1000


async def test_confirmation_yes_no_and_timeout() -> None:
    bot = FakeBot()
    f = frontend(bot)
    task = asyncio.create_task(f.ask_confirmation("111", "Déverrouiller ?"))
    await wait_for(lambda: bool(bot.ops))
    _, (chat, text, buttons) = bot.ops[0]
    assert buttons and buttons[0][1].endswith(":y")
    await f.on_callback(999, 111, "cbx", buttons[0][1])  # stranger: ignored
    assert not task.done()
    await f.on_callback(111, 111, "cb1", buttons[0][1], message_id=101)
    assert await task is True
    assert ("answer", ("cb1", "Confirmé")) in bot.ops

    task = asyncio.create_task(f.ask_confirmation("111", "Encore ?"))
    await wait_for(lambda: len([o for o in bot.ops if o[0] == "send"]) == 2)
    no_data = [o for o in bot.ops if o[0] == "send"][-1][1][2][1][1]
    await f.on_callback(111, 111, "cb2", no_data)
    assert await task is False
    await f.on_callback(111, 111, "cb3", no_data)  # already answered
    assert ("answer", ("cb3", "Demande expirée.")) in bot.ops


async def test_confirmation_timeout() -> None:
    bot = FakeBot()
    section = TelegramSection(allowed_user_ids=[111], confirm_timeout_s=5)
    f = TelegramFrontend(
        bot,
        lambda: section,
        None,
        status_text=_text,
        model_text=_text,
        jobs_text=_chat,
        reset_chat=_chat,
    )  # type: ignore[arg-type]
    section_fast = section.model_copy(update={"confirm_timeout_s": 5})
    f.cfg = lambda: section_fast
    import homelab_agent.gateway.telegram as tgmod

    orig = tgmod.asyncio.wait_for

    async def instant(fut: Any, timeout: float) -> Any:
        raise TimeoutError

    tgmod.asyncio.wait_for = instant  # type: ignore[assignment]
    try:
        assert await f.ask_confirmation("111", "?") is False
    finally:
        tgmod.asyncio.wait_for = orig
    assert "Expiré" in bot.texts()[-1]


async def test_aiogram_gateway_against_fake_bot_api(run_server: Any) -> None:
    """Full Telegram path: long polling → frontend → handler → sendMessage (fake Bot API)."""
    from homelab_agent.gateway.aiogram_adapter import TelegramGateway

    app = fake_tg()
    srv = run_server(app)
    gw = TelegramGateway("123456:TEST", api_base=srv.url)
    received: list[IncomingMessage] = []

    async def handler(msg: IncomingMessage, replier: Replier) -> None:
        received.append(msg)
        if msg.text == "confirm":
            ok = await replier.confirm("Sûr ?")
            await replier.send(f"answer={ok}")
            return
        stream = replier.stream()
        await stream.update("Réponse")
        await stream.finish("Réponse complète")

    section = TelegramSection(allowed_user_ids=[111], stream_edit_interval_s=0.3)
    f = TelegramFrontend(
        gw.api,
        lambda: section,
        handler,
        status_text=_text,
        model_text=_text,
        jobs_text=_chat,
        reset_chat=_chat,
    )
    gw.attach(f)
    await gw.start()
    try:
        async with httpx.AsyncClient(base_url=srv.url) as c:
            await c.post("/_test/message", json={"user_id": 999, "text": "intrus"})
            await c.post("/_test/message", json={"user_id": 111, "text": "salut"})

            async def sent() -> list[dict[str, Any]]:
                return (await c.get("/_test/sent")).json()

            for _ in range(200):
                if any(s["text"] == "Réponse complète" for s in await sent()):
                    break
                await asyncio.sleep(0.05)
            msgs = await sent()
            assert [m.text for m in received] == ["salut"]
            assert all(m["chat"]["id"] == 111 for m in msgs)
            assert msgs[-1]["text"] == "Réponse complète"

            await c.post("/_test/message", json={"user_id": 111, "text": "confirm"})
            for _ in range(200):
                q = [m for m in await sent() if m.get("reply_markup")]
                if q:
                    break
                await asyncio.sleep(0.05)
            q_msg = q[-1]
            yes = q_msg["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
            await c.post(
                "/_test/callback",
                json={"user_id": 111, "message_id": q_msg["message_id"], "data": yes},
            )
            for _ in range(200):
                if any(s["text"] == "answer=True" for s in await sent()):
                    break
                await asyncio.sleep(0.05)
            assert any(s["text"] == "answer=True" for s in await sent())
    finally:
        await gw.stop()


async def test_extra_commands_and_help() -> None:
    bot = FakeBot()
    seen: list[tuple[str, str]] = []

    async def run(chat: str, arg: str) -> str:
        seen.append((chat, arg))
        return f"ran {arg}"

    async def boom(chat: str, arg: str) -> str:
        raise RuntimeError("x")

    section = TelegramSection(allowed_user_ids=[111])
    f = TelegramFrontend(
        bot, lambda: section, None,  # type: ignore[arg-type]
        status_text=_text, model_text=_text, jobs_text=_chat, reset_chat=_chat,
        commands={"run": ("lancer une routine", run), "boom": ("casse", boom)},
    )  # fmt: skip
    await f.on_message(111, 5, "/run soir")
    await f.on_message(111, 5, "/boom")
    await f.on_message(111, 5, "/help")
    texts = bot.texts()
    assert seen == [("5", "soir")] and texts[0] == "ran soir"
    assert "Erreur interne" in texts[1]
    assert "/run – lancer une routine" in texts[2] and texts[2].rstrip().endswith(
        "écris simplement ta demande."
    )


async def test_action_buttons_are_one_shot_and_expire() -> None:
    from homelab_agent.gateway.base import ActionButton

    bot = FakeBot()
    now = [0.0]
    section = TelegramSection(allowed_user_ids=[111])
    f = TelegramFrontend(
        bot, lambda: section, None,  # type: ignore[arg-type]
        status_text=_text, model_text=_text, jobs_text=_chat, reset_chat=_chat,
        clock=lambda: now[0],
    )  # fmt: skip
    calls: list[str] = []

    async def act(user: str, replier: Replier) -> str:
        calls.append(user)
        return "✅ fait"

    await f.notify_actions("111", "🚨 Fuite", [ActionButton("Couper", act)], ttl_s=60)
    _, (chat, text, buttons) = bot.ops[0]
    assert text == "🚨 Fuite" and buttons and buttons[0][0] == "Couper"
    data = buttons[0][1]
    await f.on_callback(999, 111, "x", data)  # stranger
    await f.on_callback(111, 222, "x", data)  # other chat
    assert calls == []
    await f.on_callback(111, 111, "cb1", data)
    assert calls == ["111"] and bot.texts()[-1] == "✅ fait"
    await f.on_callback(111, 111, "cb2", data)  # one shot
    assert calls == ["111"] and ("answer", ("cb2", "Bouton expiré.")) in bot.ops
    await f.notify_actions("111", "again", [ActionButton("Couper", act)], ttl_s=60)
    data2 = [op for op in bot.ops if op[0] == "send"][-1][1][2][0][1]
    now[0] = 61
    await f.on_callback(111, 111, "cb3", data2)
    assert calls == ["111"]
