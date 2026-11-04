"""Pydantic models describing ``config.yaml``.

Secrets are never stored in the config itself: fields ending in ``_env`` hold the
*name* of an environment variable that contains the secret.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _Base(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


# --------------------------------------------------------------------------- agent
class AgentSection(_Base):
    language: str = Field("fr", description="Reply language (ISO code).")
    timezone: str = Field("Europe/Paris", description="IANA time zone used in prompts and logs.")
    log_level: Literal["debug", "info", "warning", "error"] = "info"
    log_format: Literal["json", "text"] = "json"
    system_prompt: str | None = Field(
        None, description="Optional extra instructions appended to the built-in system prompt."
    )
    history_messages: int = Field(
        12, ge=0, le=200, description="Number of past messages kept as conversation context."
    )
    max_tool_rounds: int = Field(6, ge=1, le=20, description="Max LLM/tool round trips per reply.")


# ------------------------------------------------------------------------ telegram
class TelegramSection(_Base):
    enabled: bool = True
    token_env: str = "TELEGRAM_BOT_TOKEN"
    allowed_user_ids: list[int] = Field(
        default_factory=list,
        description="Numeric Telegram user ids allowed to talk to the bot. Others are ignored.",
    )
    api_base: str | None = Field(
        None, description="Override of the Bot API URL (tests / self-hosted Bot API server)."
    )
    stream_edit_interval_s: float = Field(
        1.2, ge=0.3, description="Minimum delay between two edits of a streamed message."
    )
    max_message_chars: int = Field(4000, ge=500, le=4096)
    confirm_timeout_s: int = Field(60, ge=5, description="Delay before a Yes/No prompt expires.")


# ------------------------------------------------------------------------ backends
class Tier(StrEnum):
    GPU = "gpu"
    CPU = "cpu"
    CLOUD = "cloud"


class _BackendBase(_Base):
    tier: Tier
    model: str = Field(description="Model identifier sent to the backend.")
    timeout_s: float = Field(60, gt=0)
    max_output_tokens: int = Field(1024, ge=16)
    temperature: float = Field(0.3, ge=0, le=2)


class OpenAICompatBackend(_BackendBase):
    """Any server exposing ``/v1/chat/completions`` (llama-server, llama-swap...)."""

    kind: Literal["openai_compat"] = "openai_compat"
    url: str = Field(description="Base URL including /v1, e.g. http://llama-gpu:8080/v1")
    api_key_env: str | None = None
    max_context: int = Field(4096, ge=512)
    concurrency: int = Field(1, ge=1, le=16)


class AnthropicBackend(_BackendBase):
    kind: Literal["anthropic"] = "anthropic"
    tier: Tier = Tier.CLOUD
    api_key_env: str = "ANTHROPIC_API_KEY"
    base_url: str | None = None


class GeminiBackend(_BackendBase):
    kind: Literal["gemini"] = "gemini"
    tier: Tier = Tier.CLOUD
    api_key_env: str = "GEMINI_API_KEY"


Backend = Annotated[
    OpenAICompatBackend | AnthropicBackend | GeminiBackend, Field(discriminator="kind")
]


# ------------------------------------------------------------------------- routing
class EscalationSection(_Base):
    to_cpu_keywords: list[str] = Field(
        default_factory=lambda: ["analyse", "résume", "rédige", "compare", "explique en détail"]
    )
    to_cloud_keywords: list[str] = Field(default_factory=lambda: ["urgent", "très complexe"])
    long_message_chars: int = Field(
        600, ge=0, description="Messages longer than this go to the CPU tier (0 = disabled)."
    )
    max_tool_failures: int = Field(2, ge=1)
    allow_model_escalation: bool = Field(
        True, description="Expose the internal `escalate` tool to the fast model."
    )


class FastPathSection(_Base):
    enabled: bool = True
    aliases: dict[str, str] = Field(
        default_factory=dict,
        description="Spoken name -> entity_id, e.g. {'salon': 'light.salon'}.",
    )


class RoutingSection(_Base):
    fast_path: FastPathSection = Field(default_factory=FastPathSection)
    tiers: dict[Tier, list[str]] = Field(
        default_factory=lambda: {
            Tier.GPU: ["gpu_fast"],
            Tier.CPU: ["cpu_heavy"],
            Tier.CLOUD: ["claude", "gemini"],
        },
        description="Backends of each tier, in order of preference.",
    )
    escalation: EscalationSection = Field(default_factory=EscalationSection)
    fallback_order: list[Tier] = Field(
        default_factory=lambda: [Tier.GPU, Tier.CPU, Tier.CLOUD],
        description="Order in which tiers are tried when the chosen one is unavailable.",
    )
    health_interval_s: int = Field(30, ge=5)
    unhealthy_cooldown_s: int = Field(30, ge=1)

    @field_validator("fast_path", mode="before")
    @classmethod
    def _bool_fast_path(cls, v: object) -> object:
        return {"enabled": v} if isinstance(v, bool) else v


class JobsSection(_Base):
    max_concurrent: int = Field(1, ge=1, le=8, description="Heavy CPU jobs running at once.")
    poll_interval_s: float = Field(2.0, gt=0)
    max_attempts: int = Field(2, ge=1)


# --------------------------------------------------------------------------- cloud
class Pricing(_Base):
    in_per_mtok: float = Field(0, ge=0, description="Price in EUR per million input tokens.")
    out_per_mtok: float = Field(0, ge=0, description="Price in EUR per million output tokens.")


class Limits(_Base):
    monthly_eur: float = Field(0, ge=0)
    daily_eur: float = Field(0, ge=0)


class CloudLimits(Limits):
    alert_at: float = Field(0.8, gt=0, le=1, description="Fraction of a limit that sends an alert.")
    per_provider: dict[str, Limits] = Field(default_factory=dict)


class CloudSection(_Base):
    enabled: bool = False
    pricing: dict[str, Pricing] = Field(default_factory=dict, description="Keyed by backend name.")
    limits: CloudLimits = Field(default_factory=CloudLimits)


# -------------------------------------------------------------------- home assistant
class Policy(StrEnum):
    ALLOW = "allow"
    READ_ONLY = "read_only"
    DENY = "deny"


class Selector(_Base):
    """Matches entities. Every field set must match (AND). ``entity`` accepts globs."""

    domain: str | None = None
    area: str | None = None
    entity: str | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> Selector:
        if not (self.domain or self.area or self.entity):
            raise ValueError("a selector needs at least one of domain, area, entity")
        return self


class PermissionRule(Selector):
    mode: Policy
    confirm: bool = False


class HomeAssistantSection(_Base):
    enabled: bool = True
    url: str = "http://homeassistant.local:8123"
    token_env: str = "HA_TOKEN"
    verify_ssl: bool = True
    default_policy: Policy = Policy.ALLOW
    rules: list[PermissionRule] = Field(default_factory=list)
    confirm: list[Selector] = Field(
        default_factory=list, description="Entities whose control requires a Yes/No in Telegram."
    )
    context_max_chars: int = Field(3000, ge=200, description="Size limit of the entity summary.")
    context_domains: list[str] = Field(
        default_factory=lambda: [
            "light", "switch", "cover", "climate", "scene", "script", "lock",
            "fan", "media_player", "sensor", "binary_sensor", "alarm_control_panel",
        ]
    )  # fmt: skip
    request_timeout_s: float = Field(10, gt=0)


# ----------------------------------------------------------------------------- web
class WebSection(_Base):
    enabled: bool = True
    bind: str = "127.0.0.1"
    port: int = Field(8099, ge=1, le=65535)
    password_env: str = "PANEL_PASSWORD"


# -------------------------------------------------------------------------- models
class ModelItem(_Base):
    """A GGUF model served by llama.cpp. Rendered into llama-swap configs."""

    name: str = Field(description="Name used by backends (`model:` field) and llama-swap.")
    engine: Literal["gpu", "cpu"]
    repo: str = Field(description="Hugging Face repository, e.g. Qwen/Qwen3-4B-GGUF")
    file: str = Field(description="GGUF file in the repository.")
    sha256: str | None = None
    ctx_size: int = Field(4096, ge=512)
    n_gpu_layers: int = Field(99, ge=0, description="Layers offloaded to the GPU (gpu engine).")
    threads: int | None = Field(None, ge=1, description="CPU threads (default: engine setting).")
    flash_attn: bool = False
    cache_type_k: str | None = Field(None, description="e.g. q8_0 to save VRAM")
    cache_type_v: str | None = None
    parallel: int = Field(1, ge=1, le=16)
    mlock: bool = False
    ttl_s: int = Field(
        0, ge=0, description="Unload after this many idle seconds (0 = keep loaded)."
    )
    extra_args: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not v.replace("-", "").replace("_", "").replace(".", "").isalnum():
            raise ValueError("model name may only contain letters, digits, '-', '_' and '.'")
        return v


class EngineSettings(_Base):
    default_threads: int = Field(8, ge=1)
    health_timeout_s: int = Field(300, ge=10)


class ModelsSection(_Base):
    dir: str = "./models"
    items: list[ModelItem] = Field(default_factory=list)
    gpu: EngineSettings = Field(default_factory=EngineSettings)
    cpu: EngineSettings = Field(default_factory=EngineSettings)


class StoreSection(_Base):
    path: str = "./data/agent.db"


# -------------------------------------------------------------------------- root
class AppConfig(_Base):
    """Root of ``config.yaml``."""

    agent: AgentSection = Field(default_factory=AgentSection)
    telegram: TelegramSection = Field(default_factory=TelegramSection)
    backends: dict[str, Backend] = Field(default_factory=dict)
    routing: RoutingSection = Field(default_factory=RoutingSection)
    jobs: JobsSection = Field(default_factory=JobsSection)
    cloud: CloudSection = Field(default_factory=CloudSection)
    home_assistant: HomeAssistantSection = Field(default_factory=HomeAssistantSection)
    web: WebSection = Field(default_factory=WebSection)
    models: ModelsSection = Field(default_factory=ModelsSection)
    store: StoreSection = Field(default_factory=StoreSection)

    @model_validator(mode="after")
    def _cross_checks(self) -> AppConfig:
        for tier, names in self.routing.tiers.items():
            for name in names:
                backend = self.backends.get(name)
                if backend is None:
                    raise ValueError(
                        f"routing.tiers.{tier.value} references unknown backend '{name}'"
                    )
                if backend.tier != tier:
                    raise ValueError(
                        f"backend '{name}' has tier '{backend.tier.value}' but is listed "
                        f"under routing.tiers.{tier.value}"
                    )
        for name in self.cloud.pricing:
            if name not in self.backends:
                raise ValueError(f"cloud.pricing references unknown backend '{name}'")
        names = [m.name for m in self.models.items]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate model names in models.items: {sorted(dupes)}")
        return self

    # -- helpers -----------------------------------------------------------
    def backends_for(self, tier: Tier) -> list[str]:
        return list(self.routing.tiers.get(tier, []))

    def backends_for_cloud(self) -> list[str]:
        return [n for n, b in self.backends.items() if b.tier == Tier.CLOUD]

    @staticmethod
    def secret(env_name: str | None) -> str | None:
        """Read a secret from the environment (never stored in config)."""
        if not env_name:
            return None
        value = os.environ.get(env_name)
        return value or None
