"""The provider-neutral T20 semantic visual-evaluation interface.

The pipeline depends on this module only. It never touches an OpenAI response
object, an SDK type, or a signed URL: an adapter turns a
:class:`~vidgen.contracts.visual_qa.VisualQAProviderRequest` into a
:class:`~vidgen.contracts.visual_qa.VisualQAProviderResult` and nothing else
crosses the boundary.

Model identity is centralized here rather than scattered through the pipeline.
The design names two roles - *Luna* for the first pass and *Terra* for required
adjudication - and this registry binds each role to a configured provider and
model. The role separation is a policy separation: an independent attempt, a
different prompt, and a higher confidence bar for a decision. Changing a
production model ID requires checking the provider's current official
documentation first; the defaults here reuse the model this repository already
has configured and verified for its other agent roles.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from services.qa.rubric import PROMPT_VERSION
from vidgen.contracts.visual_qa import (
    VisualQAAttemptType,
    VisualQADimension,
    VisualQAProviderRequest,
    VisualQAProviderResult,
)


class VisualQARole(StrEnum):
    """The two configured evaluation roles from the technical design."""

    LUNA_FIRST_PASS = "luna"
    TERRA_ADJUDICATOR = "terra"


@dataclass(frozen=True, slots=True)
class VisualAgentModel:
    """One configured role binding: provider, model and prompt version."""

    role: VisualQARole
    provider: str
    model: str
    prompt_version: str = PROMPT_VERSION


#: The default registry. ``configure_registry`` replaces it from APISettings so a
#: deployment never has a model name compiled into the pipeline.
DEFAULT_REGISTRY: dict[VisualQARole, VisualAgentModel] = {
    VisualQARole.LUNA_FIRST_PASS: VisualAgentModel(
        role=VisualQARole.LUNA_FIRST_PASS, provider="openai", model="gpt-5.6"
    ),
    VisualQARole.TERRA_ADJUDICATOR: VisualAgentModel(
        role=VisualQARole.TERRA_ADJUDICATOR, provider="openai", model="gpt-5.6"
    ),
}


def build_registry(
    *,
    provider: str,
    first_pass_model: str,
    adjudicator_model: str,
) -> dict[VisualQARole, VisualAgentModel]:
    return {
        VisualQARole.LUNA_FIRST_PASS: VisualAgentModel(
            role=VisualQARole.LUNA_FIRST_PASS, provider=provider, model=first_pass_model
        ),
        VisualQARole.TERRA_ADJUDICATOR: VisualAgentModel(
            role=VisualQARole.TERRA_ADJUDICATOR, provider=provider, model=adjudicator_model
        ),
    }


def role_for(attempt_type: VisualQAAttemptType) -> VisualQARole:
    return (
        VisualQARole.LUNA_FIRST_PASS
        if attempt_type is VisualQAAttemptType.FIRST_PASS
        else VisualQARole.TERRA_ADJUDICATOR
    )


@dataclass(frozen=True, slots=True)
class EvidenceFrame:
    """One bounded frame handed to an adapter. Bytes never enter a contract."""

    sample_id: UUID
    sequence: int
    shot_relative_timestamp_us: int
    content: bytes
    media_type: str = "image/png"


@dataclass(frozen=True, slots=True)
class ReferenceImage:
    """One approved T19 reference image handed to an adapter for comparison."""

    asset_id: UUID
    role: str
    content: bytes
    media_type: str = "image/png"


@dataclass(frozen=True, slots=True)
class VisualAgentCall:
    """Exactly the minimum bounded evidence needed to evaluate one shot."""

    request: VisualQAProviderRequest
    frames: tuple[EvidenceFrame, ...]
    references: tuple[ReferenceImage, ...]
    contact_sheet: bytes | None = None
    #: Set when an adjudicator must see the disputed first-pass result.
    first_pass: VisualQAProviderResult | None = None


class VisualAgent(Protocol):
    """Evaluate one shot's bounded evidence against its structured intent."""

    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    async def evaluate(self, call: VisualAgentCall) -> VisualQAProviderResult: ...


class VisualAgentError(RuntimeError):
    """The adapter could not produce a validatable provider result."""


def validate_result(
    result: VisualQAProviderResult,
    request: VisualQAProviderRequest,
    *,
    known_sample_ids: Sequence[UUID],
) -> VisualQAProviderResult:
    """Reject a provider result that cannot be safely adjudicated.

    A model may not score an identity it was not asked about, cite a frame that
    was never sampled, or answer a different attempt. Anything that fails here is
    a bounded, non-retryable provider-contract failure, never a silent pass.
    """
    if result.qa_attempt_identity != request.qa_attempt_identity:
        raise VisualAgentError("provider result does not answer the requested QA attempt")
    if result.attempt_type is not request.attempt_type:
        raise VisualAgentError("provider result attempt type does not match the request")
    scored = {item.dimension for item in result.dimension_scores}
    expected = set(VisualQADimension)
    if scored != expected:
        missing = sorted(item.value for item in expected - scored)
        raise VisualAgentError(f"provider result is missing rubric dimensions: {missing}")
    allowed = set(known_sample_ids)
    for finding in result.findings:
        unknown = [str(value) for value in finding.sample_ids if value not in allowed]
        if unknown:
            raise VisualAgentError(f"provider finding cites unsampled frames: {unknown}")
        if finding.severity != "info" and not finding.sample_ids:
            raise VisualAgentError("an actionable provider finding must cite a sampled frame")
    return result
