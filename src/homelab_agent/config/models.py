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
    remote_domains: list[str] = Field(
        default_factory=lambda: [
            "light", "switch", "fan", "cover", "lock", "scene", "script", "media_player",
            "input_boolean", "button",
        ],
        description="Domains shown as buttons by /maison (the Telegram home remote).",
    )  # fmt: skip


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
    concurrency: int = Field(
        1, ge=1, le=16, description="Requests sent at once (match the model's `parallel`)."
    )


class AnthropicBackend(_BackendBase):
    """Note: the current Messages API ignores `temperature`; it is not sent."""

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
    match_names: bool = Field(
        True,
        description="Also match Home Assistant friendly names (only when exactly one entity "
        "has that name). false = aliases only.",
    )


class GpuQualitySection(_Base):
    """Step up to a bigger GPU model for medium requests, when it can be loaded.

    The quality backend is *not* a failover: if it cannot be loaded (VRAM taken by another
    process, e.g. a Jellyfin transcode), the request goes back to the fast GPU model and the
    quality model is left alone for ``retry_after_s``.
    """

    backend: str | None = Field(None, description="Backend (tier gpu) used for medium requests.")
    min_chars: int = Field(
        200, ge=0, description="Messages at least this long are medium (0 = length ignored)."
    )
    keywords: list[str] = Field(
        default_factory=lambda: ["pourquoi", "comment", "explique", "conseille"],
        description="Messages containing one of these words are medium.",
    )
    retry_after_s: int = Field(
        300, ge=10, description="Pause after a failed load before trying the quality model again."
    )


class RoutingSection(_Base):
    fast_path: FastPathSection = Field(default_factory=FastPathSection)
    gpu_quality: GpuQualitySection = Field(default_factory=GpuQualitySection)
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
    blocked_services: list[str] = Field(
        default_factory=lambda: [
            "hassio.*", "homeassistant.restart", "homeassistant.stop",
            "homeassistant.reload_all", "homeassistant.set_location", "recorder.*",
            "shell_command.*", "python_script.*", "pyscript.*", "backup.*", "update.install",
            "system_log.*", "logger.*", "persistent_notification.dismiss_all",
        ],
        description="Services the agent may never call (globs on domain.service), whatever "
        "the permissions. Unblock one with allowed_services.",
    )  # fmt: skip
    allowed_services: list[str] = Field(
        default_factory=list, description="Exceptions to blocked_services, e.g. backup.create."
    )
    untargeted_domains: list[str] = Field(
        default_factory=lambda: ["notify", "tts", "persistent_notification"],
        description="Domains whose services may be called without a target entity. Every "
        "other service needs explicit entities (so that per-entity rules always apply).",
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
    engine: Literal["gpu", "cpu", "whisper"] = Field(
        description="gpu / cpu: llama.cpp engines; whisper: speech-to-text model (voice)."
    )
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
    swap: bool = Field(
        True,
        description="One model loaded at a time (true). false keeps every model of this engine "
        "loaded together (e.g. fast + heavy on CPU in the cpu-only preset): mind the RAM.",
    )


class ModelsSection(_Base):
    dir: str = "./models"
    items: list[ModelItem] = Field(default_factory=list)
    gpu: EngineSettings = Field(default_factory=EngineSettings)
    cpu: EngineSettings = Field(default_factory=EngineSettings)
    whisper_threads: int = Field(4, ge=1, description="Threads of the speech-to-text engine.")


class StoreSection(_Base):
    path: str = "./data/agent.db"
    retention_days: int = Field(
        180,
        ge=0,
        description="Delete conversations, interactions, audit entries and finished jobs "
        "older than this (0 = keep forever). Cloud spend is kept at least 400 days.",
    )


# --------------------------------------------------------------------------- voice
class VoiceSection(_Base):
    """Telegram voice notes, transcribed locally (whisper.cpp) then handled as text."""

    enabled: bool = False
    stt_url: str = Field(
        "http://whisper:8080/v1/audio/transcriptions",
        description="OpenAI-style transcription endpoint (whisper.cpp server, speaches...).",
    )
    stt_model: str = Field(
        "whisper-1", description="Sent as the `model` field (some servers need it)."
    )
    api_key_env: str | None = None
    language: str = "fr"
    timeout_s: float = Field(120, gt=0)
    max_duration_s: int = Field(120, ge=1, description="Longer voice notes are refused.")
    echo_transcript: bool = Field(True, description="Reply with what was understood first.")


# ----------------------------------------------------------------------- proactive
_HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
Weekday = Literal["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAYS: list[Weekday] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class QuietHours(_Base):
    start: str = Field("23:00", pattern=_HHMM)
    end: str = Field("07:00", pattern=_HHMM)


class AlertAction(_Base):
    """A button under the alert message, e.g. "Close". Goes through the permission engine."""

    label: str = Field(max_length=40)
    domain: str
    service: str
    entity: str | None = Field(
        None, description="Target entity; default: the entity that triggered the alert."
    )


class AlertRule(_Base):
    name: str = Field(pattern=r"^[a-z0-9_]+$")
    entity: str = Field(description="Entity id or glob, e.g. binary_sensor.porte_*")
    state: list[str] | None = Field(
        None, description="Fires when the state is one of these values (e.g. ['on'])."
    )
    above: float | None = Field(None, description="Fires when the numeric state is above.")
    below: float | None = Field(None, description="Fires when the numeric state is below.")
    for_s: float = Field(0, ge=0, description="Condition must hold this long (seconds).")
    severity: Literal["info", "warning", "critical"] = "warning"
    message: str = Field(
        "⚠️ {name} : {state}{unit}",
        description="Placeholders: {name} {entity_id} {state} {unit} {area} {alert}",
    )
    cooldown_s: int = Field(3600, ge=0, description="Minimum delay between two notifications.")
    action: AlertAction | None = None

    @field_validator("state", mode="before")
    @classmethod
    def _one_state(cls, v: object) -> object:
        return [v] if isinstance(v, str) else v

    @model_validator(mode="after")
    def _has_condition(self) -> AlertRule:
        if self.state is None and self.above is None and self.below is None:
            raise ValueError(f"alert '{self.name}' needs state, above or below")
        return self


class RoutineCheck(_Base):
    """LLM-free report: list readable entities in one of ``states``."""

    domains: list[str] = Field(default_factory=lambda: ["light", "switch", "cover"])
    states: list[str] = Field(default_factory=lambda: ["on", "open"])
    entities: list[str] = Field(default_factory=list, description="Optional globs to restrict.")


class Routine(_Base):
    """Something to do at a local time: a check report, a prompt for the agent or a reminder.

    Repeats on ``days``; with ``date`` it runs once that day and is then removed.
    """

    name: str = Field(pattern=r"^[a-z0-9_]+$")
    at: str = Field(pattern=_HHMM, description="Local time (agent.timezone), HH:MM.")
    days: list[Weekday] = Field(default_factory=lambda: list(WEEKDAYS))
    date: str | None = Field(
        None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="YYYY-MM-DD: run once, then remove."
    )
    enabled: bool = True
    check: RoutineCheck | None = None
    prompt: str | None = Field(None, description="Request sent to the agent as if you wrote it.")
    message: str | None = Field(None, description="Reminder sent as is (no LLM).")
    tier: Tier = Field(Tier.GPU, description="Tier for prompt routines (cpu = background job).")
    only_if_found: bool = Field(True, description="check: stay silent when nothing matches.")
    chat_ids: list[int] = Field(
        default_factory=list, description="Recipients; default: proactive recipients."
    )
    created_by: str | None = Field(None, description="Set when created from a conversation.")

    @property
    def kind(self) -> str:
        return "check" if self.check else ("prompt" if self.prompt else "message")

    @model_validator(mode="after")
    def _kind(self) -> Routine:
        kinds = [k for k in (self.check, self.prompt, self.message) if k is not None]
        if len(kinds) != 1:
            raise ValueError(f"routine '{self.name}' needs exactly one of check, prompt or message")
        if not self.days and self.date is None:
            raise ValueError(f"routine '{self.name}' has no day")
        return self


class ProactiveSection(_Base):
    enabled: bool = False
    alerts_enabled: bool | None = Field(
        None, description="Override for alerts only (null = follow `enabled`)."
    )
    routines_enabled: bool | None = Field(
        None, description="Override for routines only (null = follow `enabled`)."
    )
    notify_chat_ids: list[int] = Field(
        default_factory=list, description="Recipients; default: telegram.allowed_user_ids."
    )
    quiet_hours: QuietHours | None = Field(
        None, description="During these hours only critical alerts are sent."
    )
    alerts: list[AlertRule] = Field(default_factory=list)
    routines: list[Routine] = Field(default_factory=list)
    action_ttl_s: int = Field(3600, ge=60, description="How long alert buttons stay usable.")
    chat_edit: bool = Field(
        True,
        description="Let the agent create / change routines from a conversation "
        "(always behind a Yes/No confirmation).",
    )
    max_routines: int = Field(50, ge=1, description="Upper bound on the number of routines.")
    missed_grace_s: int = Field(
        7200,
        ge=0,
        description="A one-off routine missed while the agent was down still runs, marked "
        "late, if less than this old; older ones are removed with a notice.",
    )

    @property
    def alerts_active(self) -> bool:
        return self.enabled if self.alerts_enabled is None else self.alerts_enabled

    @property
    def routines_active(self) -> bool:
        return self.enabled if self.routines_enabled is None else self.routines_enabled

    @model_validator(mode="after")
    def _unique(self) -> ProactiveSection:
        for kind, names in (
            ("alert", [a.name for a in self.alerts]),
            ("routine", [r.name for r in self.routines]),
        ):
            dupes = {n for n in names if names.count(n) > 1}
            if dupes:
                raise ValueError(f"duplicate {kind} names: {sorted(dupes)}")
        return self


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
    proactive: ProactiveSection = Field(default_factory=ProactiveSection)
    voice: VoiceSection = Field(default_factory=VoiceSection)
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
        quality = self.routing.gpu_quality.backend
        if quality is not None:
            qb = self.backends.get(quality)
            if qb is None:
                raise ValueError(f"routing.gpu_quality.backend: unknown backend '{quality}'")
            if qb.tier != Tier.GPU:
                raise ValueError(f"routing.gpu_quality.backend '{quality}' must have tier 'gpu'")
        for name in self.cloud.pricing:
            if name not in self.backends:
                raise ValueError(f"cloud.pricing references unknown backend '{name}'")
        names = [m.name for m in self.models.items]
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise ValueError(f"duplicate model names in models.items: {sorted(dupes)}")
        if len([m for m in self.models.items if m.engine == "whisper"]) > 1:
            raise ValueError("only one models.items entry may use engine 'whisper'")
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
