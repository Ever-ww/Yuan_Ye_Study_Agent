"""Canonical FINALIZING v2 identities, requirements, and durable evidence."""

from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from Agent.state import AgentState, PersistenceContract
from gateway.audit import AuditSanitizer


FINALIZE_PROTOCOL_VERSION = 2


class FinalizeStep(str, Enum):
    MEMORY = "memory"
    SESSION_INDEX = "session_index"
    AUDIT = "audit"
    INBOX = "inbox"


class FinalizeRequirement(str, Enum):
    REQUIRED = "required"
    NOT_APPLICABLE = "not_applicable"


class FinalizeEvidenceReferences(BaseModel):
    """Small, typed links between finalize artifacts; never a generic side channel."""

    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    segments: tuple[str, ...] = ()
    first_record_id: str | None = None
    last_record_id: str | None = None
    final_record_id: str | None = None
    record_count: int | None = Field(default=None, ge=0)
    memory_attempt_id: str | None = None
    memory_result_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    session_index_attempt_id: str | None = None
    session_index_result_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    receipt_id: str | None = None
    inbox_item_id: str | None = None


class VerifiedArtifactEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    kind: Literal["verified"] = "verified"
    protocol_version: Literal[2] = FINALIZE_PROTOCOL_VERSION
    step: FinalizeStep
    run_id: str = Field(min_length=1)
    generation: int = Field(ge=1)
    operation_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    artifact_kind: str = Field(min_length=1)
    artifact_id: str = Field(min_length=1)
    artifact_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    verification_method: str = Field(min_length=1)
    references: FinalizeEvidenceReferences = Field(default_factory=FinalizeEvidenceReferences)


class NotApplicableEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    kind: Literal["not_applicable"] = "not_applicable"
    protocol_version: Literal[2] = FINALIZE_PROTOCOL_VERSION
    step: FinalizeStep
    run_id: str = Field(min_length=1)
    generation: int = Field(ge=1)
    operation_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    workload_kind: str = Field(min_length=1)
    persistence_contract: PersistenceContract
    policy_reason: str = Field(min_length=1)


FinalizeStepEvidence = Annotated[
    VerifiedArtifactEvidence | NotApplicableEvidence,
    Field(discriminator="kind"),
]
_EVIDENCE_ADAPTER = TypeAdapter(FinalizeStepEvidence)


class EncodedFinalizeEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    value: FinalizeStepEvidence
    serialized: str
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class FinalizeOperationIdentity(BaseModel):
    model_config = ConfigDict(frozen=True, strict=True, extra="forbid")

    run_id: str
    generation: int = Field(ge=1)
    step: FinalizeStep
    stable_key: str
    operation_id: str
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class FinalizeEvidenceCodec:
    """Validate, sanitize, and deterministically serialize evidence before hashing."""

    @staticmethod
    def encode(value: FinalizeStepEvidence | dict[str, object]) -> EncodedFinalizeEvidence:
        validated = _EVIDENCE_ADAPTER.validate_python(value, strict=True)
        # Sanitize the JSON representation, then validate it through Pydantic's
        # JSON path.  This preserves strict enum/tuple contracts without asking
        # the sanitizer to retain Python implementation types.
        sanitized = AuditSanitizer.sanitize(
            validated.model_dump(mode="json", exclude_none=True),
        )
        sanitized_json = json.dumps(
            sanitized,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        final = _EVIDENCE_ADAPTER.validate_json(sanitized_json, strict=True)
        canonical = json.dumps(
            final.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
        return EncodedFinalizeEvidence(
            value=final,
            serialized=canonical,
            result_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )

    @staticmethod
    def decode(value: str) -> FinalizeStepEvidence:
        return _EVIDENCE_ADAPTER.validate_json(value, strict=True)

    @classmethod
    def verify(cls, value: str, result_hash: str) -> FinalizeStepEvidence:
        encoded = cls.encode(cls.decode(value))
        if encoded.result_hash != result_hash:
            raise ValueError("Finalize Evidence result_hash mismatch")
        return encoded.value


class FinalizeIdentity:
    @staticmethod
    def for_step(state: AgentState, generation: int, step: FinalizeStep) -> FinalizeOperationIdentity:
        stable_key = f"finalize:v{FINALIZE_PROTOCOL_VERSION}:g{generation}:{step.value}"
        operation_id = hashlib.sha256(
            f"{state.run_id}:{stable_key}".encode("utf-8"),
        ).hexdigest()
        request = {
            "protocol_version": FINALIZE_PROTOCOL_VERSION,
            "run_id": state.run_id,
            "generation": generation,
            "step": step.value,
            "workload_kind": state.workload_kind.value,
            "persistence_contract": state.persistence_contract.value,
            "project_id": state.project_id,
            "session_id": state.session_id,
            "turn_id": state.turn_id,
            "terminal_target": state.terminal_target.value if state.terminal_target else None,
        }
        canonical = json.dumps(
            request, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        )
        return FinalizeOperationIdentity(
            run_id=state.run_id,
            generation=generation,
            step=step,
            stable_key=stable_key,
            operation_id=operation_id,
            request_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        )

    @staticmethod
    def command_id(run_id: str, generation: int, step: FinalizeStep, action: str) -> str:
        return f"finalize:v{FINALIZE_PROTOCOL_VERSION}:{run_id}:g{generation}:{step.value}:{action}"


class FinalizeRequirementPolicy:
    """Derive requirements only from durable Run intent, never caller-provided booleans."""

    @staticmethod
    def requirements(state: AgentState) -> dict[FinalizeStep, FinalizeRequirement]:
        session_required = state.persistence_contract in {
            PersistenceContract.CONVERSATION_SESSION,
            PersistenceContract.SESSION_BACKED_WORKLOAD,
        }
        return {
            FinalizeStep.MEMORY: (
                FinalizeRequirement.REQUIRED if session_required else FinalizeRequirement.NOT_APPLICABLE
            ),
            FinalizeStep.SESSION_INDEX: (
                FinalizeRequirement.REQUIRED if session_required else FinalizeRequirement.NOT_APPLICABLE
            ),
            FinalizeStep.AUDIT: FinalizeRequirement.REQUIRED,
            FinalizeStep.INBOX: FinalizeRequirement.REQUIRED,
        }

    @classmethod
    def validate(cls, state: AgentState, evidence: FinalizeStepEvidence) -> None:
        expected = cls.requirements(state)[evidence.step]
        if expected is FinalizeRequirement.REQUIRED and isinstance(evidence, NotApplicableEvidence):
            raise ValueError(f"Finalize step {evidence.step.value} is required")
        if expected is FinalizeRequirement.NOT_APPLICABLE and not isinstance(
            evidence, NotApplicableEvidence,
        ):
            raise ValueError(f"Finalize step {evidence.step.value} must be not_applicable")
        if isinstance(evidence, NotApplicableEvidence):
            if evidence.persistence_contract is not state.persistence_contract:
                raise ValueError("Finalize N/A persistence contract mismatch")
            if evidence.workload_kind != state.workload_kind.value:
                raise ValueError("Finalize N/A workload kind mismatch")


__all__ = [
    "EncodedFinalizeEvidence",
    "FINALIZE_PROTOCOL_VERSION",
    "FinalizeEvidenceCodec",
    "FinalizeEvidenceReferences",
    "FinalizeIdentity",
    "FinalizeOperationIdentity",
    "FinalizeRequirement",
    "FinalizeRequirementPolicy",
    "FinalizeStep",
    "FinalizeStepEvidence",
    "NotApplicableEvidence",
    "VerifiedArtifactEvidence",
]
