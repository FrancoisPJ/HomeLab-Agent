"""Tool registry: tools are declared with a JSON Schema and an async handler."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from homelab_agent.llm.base import ToolSpec

log = logging.getLogger(__name__)

ConfirmFn = Callable[[str], Awaitable[bool]]


async def _never_confirm(_: str) -> bool:
    return False


@dataclass(slots=True)
class ToolContext:
    requester: str
    chat_id: str
    tier: str = ""
    model: str = ""
    confirm: ConfirmFn = _never_confirm
    escalation: dict[str, str] | None = None  # set by the `escalate` tool


@dataclass(slots=True)
class ToolResult:
    ok: bool
    data: Any = None
    error: str | None = None
    outcome: str = ""  # ok | error | denied | declined (audit); derived from ok when empty

    def __post_init__(self) -> None:
        if not self.outcome:
            self.outcome = "ok" if self.ok else "error"

    def to_text(self) -> str:
        if self.ok:
            payload: Any = {"ok": True, "result": self.data}
        else:
            payload = {"ok": False, "error": self.error}
        return json.dumps(payload, ensure_ascii=False, default=str)


Handler = Callable[[dict[str, Any], ToolContext], Awaitable[ToolResult]]


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    tags: set[str] = field(default_factory=set)

    @property
    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.parameters)


def _validate(schema: dict[str, Any], args: dict[str, Any]) -> str | None:
    """Minimal JSON-Schema check (required + primitive types + enum)."""
    if not isinstance(args, dict):
        return "arguments must be an object"
    props: dict[str, Any] = schema.get("properties", {})
    for req in schema.get("required", []):
        if req not in args:
            return f"missing required argument '{req}'"
    types = {
        "string": str,
        "integer": int,
        "number": (int, float),
        "boolean": bool,
        "object": dict,
        "array": list,
    }
    for key, value in args.items():
        spec = props.get(key)
        if spec is None:
            if schema.get("additionalProperties") is False:
                return f"unknown argument '{key}'"
            continue
        expected = spec.get("type")
        py = types.get(expected) if isinstance(expected, str) else None
        if py is not None and not isinstance(value, py):  # type: ignore[arg-type]
            return f"argument '{key}' must be of type {expected}"
        if "enum" in spec and value not in spec["enum"]:
            return f"argument '{key}' must be one of {spec['enum']}"
    return None


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"tool '{tool.name}' already registered")
        self._tools[tool.name] = tool

    def names(self) -> list[str]:
        return sorted(self._tools)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def specs(self, *, exclude_tags: set[str] | None = None) -> list[ToolSpec]:
        exclude_tags = exclude_tags or set()
        return [t.spec for t in self._tools.values() if not (t.tags & exclude_tags)]

    async def execute(self, name: str, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        tool = self._tools.get(name)
        if tool is None:
            return ToolResult(False, error=f"unknown tool '{name}'")
        problem = _validate(tool.parameters, args)
        if problem:
            return ToolResult(False, error=problem)
        try:
            return await tool.handler(args, ctx)
        except Exception as exc:  # a broken tool must not crash the agent loop
            log.exception("tool %s failed", name)
            return ToolResult(False, error=f"{exc.__class__.__name__}: {exc}")


ESCALATE_TOOL_NAME = "escalate"


def escalate_tool() -> Tool:
    async def handler(args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        ctx.escalation = {"target": str(args["target"]), "reason": str(args.get("reason", ""))}
        return ToolResult(True, data="escalation requested")

    return Tool(
        name=ESCALATE_TOOL_NAME,
        description=(
            "Hand the request over to a stronger model when it is too complex for you. "
            "target=cpu for long, non-urgent work (answer arrives later); "
            "target=cloud for very complex or urgent requests."
        ),
        parameters={
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Why you escalate, in a few words."},
                "target": {"type": "string", "enum": ["cpu", "cloud"]},
            },
            "required": ["target"],
            "additionalProperties": False,
        },
        handler=handler,
        tags={"internal"},
    )
