"""Composed T22 entry points for the CLI, the API worker and the Temporal activity.

Callers pick a provider; everything else - authoritative selection, identity,
restart safety, persistence and the gate - stays inside the pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from sqlalchemy.orm import Session

from services.qa.final_editorial import FinalEditorialPipeline, FinalQAOptions
from services.qa.final_editorial_provider import (
    FinalEditorialProvider,
    FinalEditorialRole,
)
from services.qa.final_fake_provider import FakeEditorialDefect, FakeFinalEditorialProvider
from services.qa.final_rubric import DEFAULT_CONFIGURATION
from vidgen.contracts.final_editorial import (
    FinalEditorialResult,
    FinalQAConfiguration,
    FinalQADecision,
)
from vidgen.db.final_editorial_repository import FinalEditorialRepository
from vidgen.storage.blob import BlobStore


class FinalQAConfigurationError(RuntimeError):
    """The requested provider cannot be constructed from the environment."""


@dataclass(frozen=True, slots=True)
class FinalQACommandOptions:
    provider: str = "fake"
    idempotency_key: str | None = None
    adjudicate: bool = True
    openai_api_key: str | None = None
    first_pass_model: str | None = None
    adjudicator_model: str | None = None
    configuration: FinalQAConfiguration = DEFAULT_CONFIGURATION
    trace_context: dict[str, str] = field(default_factory=dict)
    #: Controlled fake profiles keyed by render identity, for fixtures only.
    fake_defects: dict[str, FakeEditorialDefect] = field(default_factory=dict)


def build_providers(
    options: FinalQACommandOptions,
) -> tuple[FinalEditorialProvider, FinalEditorialProvider | None]:
    """Construct the first-pass provider and, when policy allows, the adjudicator."""
    first: FinalEditorialProvider
    second: FinalEditorialProvider | None
    if options.provider == "fake":
        first = FakeFinalEditorialProvider(options.fake_defects)
        second = (
            FakeFinalEditorialProvider(
                options.fake_defects,
                model="fake-final-editorial-adjudicator-1",
                adjudicator=True,
            )
            if options.adjudicate
            else None
        )
        return first, second
    if options.provider != "openai":
        raise FinalQAConfigurationError(f"unsupported final-QA provider {options.provider!r}")
    if not options.openai_api_key:
        raise FinalQAConfigurationError(
            "the production final-QA provider requires VIDGEN_OPENAI_API_KEY"
        )
    from services.qa.final_openai_adapter import OpenAIFinalEditorialProvider

    first = OpenAIFinalEditorialProvider(
        api_key=options.openai_api_key,
        role=FinalEditorialRole.LUNA_FIRST_PASS,
        model=options.first_pass_model,
    )
    second = (
        OpenAIFinalEditorialProvider(
            api_key=options.openai_api_key,
            role=FinalEditorialRole.TERRA_ADJUDICATOR,
            model=options.adjudicator_model,
        )
        if options.adjudicate
        else None
    )
    return first, second


async def run_final_editorial_qa(
    session: Session,
    blob_store: BlobStore,
    *,
    project_id: UUID,
    options: FinalQACommandOptions | None = None,
) -> FinalEditorialResult:
    """Run or resume final editorial QA for one project's current render."""
    resolved = options or FinalQACommandOptions()
    first, second = build_providers(resolved)
    pipeline = FinalEditorialPipeline(
        session,
        blob_store,
        first,
        adjudicator=second,
        options=FinalQAOptions(
            configuration=resolved.configuration,
            trace_context=dict(resolved.trace_context),
            adjudicate=resolved.adjudicate,
        ),
    )
    key = resolved.idempotency_key or f"final-qa:{project_id}"
    try:
        return await pipeline.evaluate_project(project_id=project_id, idempotency_key=key)
    finally:
        for provider in (first, second):
            close = getattr(provider, "aclose", None)
            if close is not None:
                await close()


class FinalQABlocked(RuntimeError):
    """The assembled recap failed final QA; the project cannot complete."""

    def __init__(self, result: FinalEditorialResult) -> None:
        super().__init__(f"final editorial QA failed for project {result.project_id}")
        self.result = result


class FinalQAReviewRequired(RuntimeError):
    """A genuinely uncertain editorial question is waiting for a human."""

    def __init__(self, result: FinalEditorialResult) -> None:
        super().__init__(f"final editorial QA requires review for project {result.project_id}")
        self.result = result


async def evaluate_final_stage(
    session: Session,
    blob_store: BlobStore,
    *,
    project_id: UUID,
    options: FinalQACommandOptions | None = None,
) -> FinalEditorialResult:
    """The workflow entry point: a non-``PASS`` gate raises rather than returns.

    The parent workflow must not be able to treat ``FAIL`` or ``REVIEW`` as a
    completed stage, so the outcome is expressed as control flow rather than as
    a status string a caller could ignore.
    """
    result = await run_final_editorial_qa(
        session, blob_store, project_id=project_id, options=options
    )
    if result.decision is FinalQADecision.FAIL:
        raise FinalQABlocked(result)
    if result.decision is FinalQADecision.REVIEW:
        raise FinalQAReviewRequired(result)
    return result


def completion_allowed(
    session: Session, *, project_id: UUID, final_render_asset_id: UUID | None
) -> tuple[bool, str]:
    """Whether the project may reach its final completed state, and why not."""
    return FinalEditorialRepository(session).completion_gate(project_id, final_render_asset_id)
