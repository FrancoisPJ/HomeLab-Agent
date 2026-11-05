"""Web chat: send a message, stream the answer (SSE), confirmations and buttons."""

from __future__ import annotations

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

from homelab_agent.commands import AgentCommands
from homelab_agent.web.chat import WEB_CHAT, inbox_events, turn_events
from homelab_agent.web.deps import ServicesDep, render

router = APIRouter(prefix="/chat")

SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
MAX_MESSAGE_CHARS = 4000


@router.get("", response_class=HTMLResponse)
async def chat_page(request: Request) -> HTMLResponse:
    return render(request, "chat.html")


@router.post("/send")
async def chat_send(services: ServicesDep, text: str = Form("")) -> JSONResponse:
    if not text.strip():
        return JSONResponse({"detail": "message vide"}, status_code=400)
    if len(text) > MAX_MESSAGE_CHARS:
        return JSONResponse({"detail": "message trop long"}, status_code=400)
    turn = services.webchat.start_turn(text, services.orchestrator.handle, services.commands)
    return JSONResponse({"turn": turn.id})


@router.get("/events/{turn_id}")
async def chat_events(services: ServicesDep, turn_id: str) -> Response:
    turn = services.webchat.turns.get(turn_id)
    if turn is None:
        raise HTTPException(404, "unknown turn")
    return StreamingResponse(turn_events(turn), media_type="text/event-stream", headers=SSE_HEADERS)


@router.get("/inbox")
async def chat_inbox(services: ServicesDep) -> Response:
    return StreamingResponse(
        inbox_events(services.webchat), media_type="text/event-stream", headers=SSE_HEADERS
    )


@router.post("/confirm/{token}")
async def chat_confirm(services: ServicesDep, token: str, yes: str = Form("0")) -> JSONResponse:
    return JSONResponse({"ok": services.webchat.answer(token, yes == "1")})


@router.post("/action/{token}")
async def chat_action(services: ServicesDep, token: str) -> JSONResponse:
    return JSONResponse({"text": await services.webchat.run_action(token)})


@router.post("/reset")
async def chat_reset(services: ServicesDep) -> JSONResponse:
    return JSONResponse({"text": await AgentCommands(services).reset(WEB_CHAT)})
