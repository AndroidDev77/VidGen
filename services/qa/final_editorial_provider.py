"""The provider-neutral T22 final editorial-evaluation interface.

The pipeline depends on this module only. It never touches an SDK object, a
response payload or a signed URL: an adapter turns one bounded
:class:`FinalEditorialProviderRequest` plus its sampled frames into a
:class:`FinalEditorialProviderResult`, and nothing else crosses the boundary.

Model identity is centralized here rather than scattered through the pipeline,
and follows the project's existing two-role policy:

* **Luna** performs the first-pass editorial QA on the inexpensive vision model.
* **Terra** adjudicates borderline, contradictory or low-confidence findings on
  the stronger model, and may only decide at or above the configured confidence
  floor. Below it, the disagreement becomes ``REVIEW``.

A provider score is never accepted as canonical. :func:`validate_result` rejects
a reply that answers a different attempt, scores a dimension it was not asked
about, cites a frame that was never sampled, or leaves an actionable finding
without evidence. Recomputation happens in ``final_gate``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from services.qa.final_rubric import EDITORIAL_DIMENSIONS, PROMPT_VERSION
from vidgen.contracts.final_editorial import (
    ADJUDICATION_CONFIDENCE_FLOOR,
    FinalEditorialProviderRequest,
    FinalEditorialProviderResult,
)


class FinalEditorialRole(StrEnum):
    """The two configured evaluation roles from the technical design."""

    LUNA_FIRST_PASS = "luna"
    TERRA_ADJUDICATOR = "terra"


@dataclass(frozen=True, slots=True)
class FinalEditorialModel:
    """One configured role binding: provider, model and prompt version."""

    role: FinalEditorialRole
    provider: str
    model: str
    prompt_version: str = PROMPT_VERSION


#: The default registry. ``build_registry`` replaces it from settings so a
#: deployment never has a model name compiled into the pipeline. The defaults
#: reuse the model this repository already has configured and verified for its
#: other vision agent roles; changing a production model ID requires checking
#: the provider's current official documentation first.
DEFAULT_REGISTRY: dict[FinalEditorialRole, FinalEditorialModel] = {
    FinalEditorialRole.LUNA_FIRST_PASS: FinalEditorialModel(
        role=FinalEditorialRole.LUNA_FIRST_PASS, provider="openai", model="gpt-5.6"
    ),
    FinalEditorialRole.TERRA_ADJUDICATOR: FinalEditorialModel(
        role=FinalEditorialRole.TERRA_ADJUDICATOR, provider="openai", model="gpt-5.6"
    ),
}


def build_registry(
    *, provider: str, first_pass_model: str, adjudicator_model: str
) -> dict[FinalEditorialRole, FinalEditorialModel]:
    return {
        FinalEditorialRole.LUNA_FIRST_PASS: FinalEditorialModel(
            role=FinalEditorialRole.LUNA_FIRST_PASS, provider=provider, model=first_pass_model
        ),
        FinalEditorialRole.TERRA_ADJUDICATOR: FinalEditorialModel(
            role=FinalEditorialRole.TERRA_ADJUDICATOR, provider=provider, model=adjudicator_model
        ),
    }


def role_for(attempt_type: str) -> FinalEditorialRole:
    return (
        FinalEditorialRole.LUNA_FIRST_PASS
        if attempt_type == "first_pass"
        else FinalEditorialRole.TERRA_ADJUDICATOR
    )


@dataclass(frozen=True, slots=True)
class EditorialFrame:
    """One bounded frame handed to an adapter. Bytes never enter a contract."""

    sample_id: UUID
    sequence: int
    timestamp_us: int
    content: bytes
    media_type: str = "image/png"


@dataclass(frozen=True, slots=True)
class FinalEditorialCall:
    """Exactly the minimum bounded evidence needed to evaluate one recap."""

    request: FinalEditorialProviderRequest
    frames: tuple[EditorialFrame, ...] = ()
    contact_sheet: bytes | None = None
    #: Set when an adjudicator must see the disputed first-pass result.
    first_pass: FinalEditorialProviderResult | None = None


class FinalEditorialProvider(Protocol):
    """Evaluate one assembled recap against its approved story and continuity."""

    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    async def evaluate(self, call: FinalEditorialCall) -> FinalEditorialProviderResult: ...


class FinalEditorialProviderError(RuntimeError):
    """The adapter could not produce a validatable provider result."""


def validate_result(
    result: FinalEditorialProviderResult,
    request: FinalEditorialProviderRequest,
    *,
    known_sample_ids: Sequence[UUID],
    known_shot_ids: Sequence[UUID],
) -> FinalEditorialProviderResult:
    """Reject a provider result that cannot be safely turned into a gate decision.

    Anything that fails here is a bounded, non-retryable provider-contract
    failure, never a silent pass.
    """
    if result.attempt_identity != request.attempt_identity:
        raise FinalEditorialProviderError("provider result does not answer the requested attempt")
    if result.attempt_type != request.attempt_type:
        raise FinalEditorialProviderError("provider result attempt type does not match the request")
    scored = {item.category for item in result.dimension_scores}
    expected = set(EDITORIAL_DIMENSIONS)
    if scored != expected:
        missing = sorted(item.value for item in expected - scored)
        raise FinalEditorialProviderError(f"provider result is missing dimensions: {missing}")
    samples = set(known_sample_ids)
    shots = set(known_shot_ids)
    for finding in result.findings:
        unknown_samples = [str(value) for value in finding.sample_ids if value not in samples]
        if unknown_samples:
            raise FinalEditorialProviderError(
                f"provider finding cites unsampled frames: {unknown_samples}"
            )
        unknown_shots = [str(value) for value in finding.shot_ids if value not in shots]
        if unknown_shots:
            raise FinalEditorialProviderError(
                f"provider finding cites shots outside the render: {unknown_shots}"
            )
        if finding.end_us > request.timeline_duration_us:
            raise FinalEditorialProviderError(
                "provider finding cites a timestamp beyond the final render duration"
            )
        if finding.proposed_severity.value in {"blocking", "review_required"} and not (
            finding.sample_ids or finding.shot_ids or finding.caption_cue_sequences
        ):
            raise FinalEditorialProviderError(
                "an actionable provider finding must cite a frame, shot or caption cue"
            )
    return result


def adjudicator_decided(confidence: float) -> bool:
    """Terra may only settle a disagreement at or above the confidence floor."""
    return confidence >= ADJUDICATION_CONFIDENCE_FLOOR
