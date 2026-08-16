"""Provider request budgeting and conservative usage calibration."""

from __future__ import annotations

import json
import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from Agent.config import RuntimeConfig
from Agent.models.errors import ModelServiceError


ContextBudgetDecision = Literal["proceed", "compress", "reject"]


class ContextBudgetExceeded(RuntimeError):
    """The immutable request tail cannot fit inside the configured context window."""

    def __init__(self, message: str, *, estimate: "ContextBudgetEstimate | None" = None) -> None:
        super().__init__(message)
        self.estimate = estimate


class ContextCompressionPolicy(BaseModel):
    """Frozen policy used by preflight, manual compression and overflow recovery."""

    model_config = ConfigDict(frozen=True, strict=True)

    context_window_tokens: int = Field(ge=1024)
    threshold_tokens: int = Field(ge=0)
    output_reserve_tokens: int = Field(ge=0)
    safety_margin_tokens: int = Field(ge=0)
    protect_last_n: int = Field(ge=0)
    target_ratio: float = Field(gt=0.0, lt=1.0)
    hygiene_message_limit: int = Field(ge=1)
    micro_compact: bool = False

    @classmethod
    def from_config(cls, config: RuntimeConfig) -> "ContextCompressionPolicy":
        return cls(
            context_window_tokens=config.model_context_window_tokens,
            threshold_tokens=config.compression_threshold_tokens,
            output_reserve_tokens=config.compression_output_reserve_tokens,
            safety_margin_tokens=config.compression_safety_margin_tokens,
            protect_last_n=config.compression_protect_last_n,
            target_ratio=config.compression_target_ratio,
            hygiene_message_limit=config.compression_hygiene_message_limit,
            micro_compact=config.compression_micro_compact,
        )

    @property
    def hard_limit_tokens(self) -> int:
        return self.context_window_tokens - self.safety_margin_tokens

    @property
    def trigger_limit_tokens(self) -> int:
        if self.threshold_tokens <= 0:
            return self.hard_limit_tokens
        return min(self.threshold_tokens, self.hard_limit_tokens)


class ContextBudgetEstimate(BaseModel):
    """A content-free control-plane description of one request projection."""

    model_config = ConfigDict(frozen=True, strict=True)

    estimated_input_tokens: int = Field(ge=0)
    calibrated_input_tokens: int = Field(ge=0)
    tool_schema_tokens: int = Field(ge=0)
    ephemeral_context_tokens: int = Field(ge=0)
    projected_tool_output_tokens: int = Field(default=0, ge=0)
    output_reserve_tokens: int = Field(ge=0)
    projected_total_tokens: int = Field(ge=0)
    trigger_limit_tokens: int = Field(ge=1)
    hard_limit_tokens: int = Field(ge=1)
    pressure_ratio: float = Field(ge=0.0)
    message_count: int = Field(ge=0)
    decision: ContextBudgetDecision
    reason: str


class ContextUsageCalibration(BaseModel):
    """Runtime-local feedback; canonical conversation state never depends on it."""

    model_config = ConfigDict(frozen=True, strict=True)

    samples: int = Field(default=0, ge=0)
    conservative_ratio: float = Field(default=1.0, ge=1.0, le=8.0)
    last_estimated_input_tokens: int | None = Field(default=None, ge=0)
    last_provider_input_tokens: int | None = Field(default=None, ge=0)
    last_error_ratio: float | None = Field(default=None, ge=0.0)


class ContextBudgetController:
    """Forecast complete provider requests and calibrate estimates from provider usage."""

    def __init__(self, config: RuntimeConfig) -> None:
        self.policy = ContextCompressionPolicy.from_config(config)
        self._calibration: dict[str, ContextUsageCalibration] = {}
        self._last_estimate: dict[str, ContextBudgetEstimate] = {}

    def forecast(
        self,
        session_id: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        ephemeral_preview: str | None = None,
    ) -> ContextBudgetEstimate:
        projected_messages = [dict(message) for message in messages]
        ephemeral_tokens = 0
        if ephemeral_preview is not None and projected_messages:
            original = projected_messages[-1].get("content")
            if projected_messages[-1].get("role") == "user" and isinstance(original, str):
                projected_messages[-1]["content"] = ephemeral_preview
                ephemeral_tokens = max(0, estimate_tokens(ephemeral_preview) - estimate_tokens(original))
        schemas = sorted((dict(tool) for tool in tools), key=lambda value: str(value.get("name", "")))
        tool_tokens = estimate_tokens(canonical_json(schemas))
        estimated = estimate_tokens(canonical_json({"messages": projected_messages, "tools": schemas}))
        calibration = self._calibration.get(session_id, ContextUsageCalibration())
        calibrated = max(estimated, math.ceil(estimated * calibration.conservative_ratio))
        projected_total = calibrated + self.policy.output_reserve_tokens
        hard = self.policy.hard_limit_tokens
        trigger = self.policy.trigger_limit_tokens
        if projected_total >= hard:
            decision: ContextBudgetDecision = "reject"
            reason = "hard_limit"
        elif len(projected_messages) >= self.policy.hygiene_message_limit:
            decision = "compress"
            reason = "message_hygiene"
        elif self.policy.threshold_tokens > 0 and projected_total >= trigger:
            decision = "compress"
            reason = "projected_total"
        else:
            decision = "proceed"
            reason = "within_budget"
        result = ContextBudgetEstimate(
            estimated_input_tokens=estimated,
            calibrated_input_tokens=calibrated,
            tool_schema_tokens=tool_tokens,
            ephemeral_context_tokens=ephemeral_tokens,
            projected_tool_output_tokens=0,
            output_reserve_tokens=self.policy.output_reserve_tokens,
            projected_total_tokens=projected_total,
            trigger_limit_tokens=trigger,
            hard_limit_tokens=hard,
            pressure_ratio=projected_total / hard,
            message_count=len(projected_messages),
            decision=decision,
            reason=reason,
        )
        self._last_estimate[session_id] = result
        return result

    def update_from_provider_usage(
        self,
        session_id: str,
        *,
        estimated_input_tokens: int,
        provider_input_tokens: int | None,
    ) -> ContextUsageCalibration:
        current = self._calibration.get(session_id, ContextUsageCalibration())
        if provider_input_tokens is None or estimated_input_tokens <= 0:
            return current
        observed = provider_input_tokens / estimated_input_tokens
        # Never calibrate below the local estimate. Decay old spikes slowly so a
        # single under-estimate remains protective for subsequent requests.
        ratio = min(8.0, max(1.0, observed, current.conservative_ratio * 0.95))
        updated = ContextUsageCalibration(
            samples=current.samples + 1,
            conservative_ratio=ratio,
            last_estimated_input_tokens=estimated_input_tokens,
            last_provider_input_tokens=provider_input_tokens,
            last_error_ratio=abs(provider_input_tokens - estimated_input_tokens) / provider_input_tokens
            if provider_input_tokens > 0 else 0.0,
        )
        self._calibration[session_id] = updated
        return updated

    def calibration(self, session_id: str) -> ContextUsageCalibration:
        return self._calibration.get(session_id, ContextUsageCalibration())

    def last_estimate(self, session_id: str) -> ContextBudgetEstimate | None:
        return self._last_estimate.get(session_id)


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def estimate_tokens(value: str) -> int:
    if not value:
        return 0
    cjk = sum(1 for char in value if "\u3400" <= char <= "\u9fff")
    return cjk + math.ceil((len(value) - cjk) / 4)


def is_context_length_error(error: BaseException) -> bool:
    """Recognise explicit provider context-window rejection without treating every 400 as overflow."""
    text = (str(error) or type(error).__name__).lower()
    markers = (
        "context length", "context_length", "context window", "maximum context",
        "max context", "too many tokens", "token limit", "prompt is too long",
        "request too large", "上下文长度", "超过上下文", "令牌数过多",
    )
    status = getattr(error, "status_code", None)
    return any(marker in text for marker in markers) and (
        not isinstance(error, ModelServiceError) or status in {400, 413, 422}
    )
