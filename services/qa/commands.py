"""Composed T20 entry points for the CLI, the API worker and T16 activities.

Callers pick a provider and a scope; everything else - authoritative selection,
identity, restart safety and persistence - stays inside the pipeline.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.animation.providers import VideoGenerationProvider
from services.animation.veo import DEFAULT_VEO_LOCATION
from services.animation.veo_adapter import AlternateVideoProvider
from services.qa.fake_visual_agent import FakeDefect, FakeVisualAgent
from services.qa.pipeline import VisualQAOptions, VisualQAPipeline
from services.qa.rubric import DETERMINISTIC_THRESHOLDS
from services.qa.repair import RepairOptions, Revalidator, VisualRepairPipeline
from services.qa.repair_policy import default_policy
from services.qa.visual_agent import VisualAgent, VisualQARole
from vidgen.contracts.repair import RepairOutcome
from vidgen.contracts.visual_qa import VisualQAResult, VisualQATargetType
from vidgen.db.storyboard_models import StoryboardRun, StoryboardShotRecord
from vidgen.db.visual_qa_repository import VisualQARepository
from vidgen.storage.blob import BlobStore


class VisualQAConfigurationError(RuntimeError):
    """The requested provider cannot be constructed from the environment."""


@dataclass(frozen=True, slots=True)
class VisualQACommandOptions:
    provider: str = "fake"
    idempotency_key: str | None = None
    targets: tuple[VisualQATargetType, ...] = (
        VisualQATargetType.KEYFRAME,
        VisualQATargetType.VIDEO,
    )
    shot_id: UUID | None = None
    expected_width: int | None = None
    expected_height: int | None = None
    adjudicate: bool = True
    openai_api_key: str | None = None
    first_pass_model: str | None = None
    adjudicator_model: str | None = None
    fake_defects: dict[UUID, FakeDefect] = field(default_factory=dict)
    ocr_threshold: float | None = None


@dataclass(frozen=True, slots=True)
class VisualQACommandResult:
    project_id: UUID
    storyboard_run_id: UUID
    results: tuple[VisualQAResult, ...]
    failures: tuple[tuple[UUID, VisualQATargetType, str], ...]

    @property
    def status(self) -> str:
        if self.failures:
            return "visual_qa_partial"
        if any(item.outcome.value == "FAIL" for item in self.results):
            return "visual_qa_blocked"
        if any(item.outcome.value == "REVIEW" for item in self.results):
            return "visual_qa_review_required"
        return "visual_qa_complete"


def build_agents(
    options: VisualQACommandOptions,
) -> tuple[VisualAgent, VisualAgent | None]:
    """Construct the first-pass agent and, when policy allows, the adjudicator."""
    first: VisualAgent
    second: VisualAgent | None
    if options.provider == "fake":
        first = FakeVisualAgent(options.fake_defects, role=VisualQARole.LUNA_FIRST_PASS)
        second = (
            FakeVisualAgent(
                options.fake_defects,
                role=VisualQARole.TERRA_ADJUDICATOR,
                model="fake-visual-qa-adjudicator/1",
            )
            if options.adjudicate
            else None
        )
        return first, second
    if options.provider == "openai":
        from services.qa.openai_adapter import OpenAIVisualAgent

        if not options.openai_api_key:
            raise VisualQAConfigurationError("the configured provider requires an API key")
        first = OpenAIVisualAgent(
            api_key=options.openai_api_key,
            role=VisualQARole.LUNA_FIRST_PASS,
            model=options.first_pass_model,
        )
        second = (
            OpenAIVisualAgent(
                api_key=options.openai_api_key,
                role=VisualQARole.TERRA_ADJUDICATOR,
                model=options.adjudicator_model,
            )
            if options.adjudicate
            else None
        )
        return first, second
    raise VisualQAConfigurationError(f"unknown visual-QA provider {options.provider!r}")


def shot_workflow_identity_resolver(
    session: Session, storyboard: StoryboardRun, shot: StoryboardShotRecord
) -> str:
    """Reuse the T16 identity helper so QA binds the real child-workflow identity."""
    from apps.api.settings import get_settings
    from services.review.shot_identity import configuration_identities, shot_workflow_identity

    settings = get_settings()
    t14, t15 = configuration_identities(
        image_provider_name=settings.image_provider_name,
        image_model=settings.image_model,
        video_provider_name=settings.video_provider_name,
        visual_capability_profile=settings.visual_capability_profile,
    )
    identity = shot_workflow_identity(
        session,
        storyboard,
        shot,
        t14_configuration_identity=t14,
        t15_capability_profile_identity=t15,
    )
    return identity.identity_hash


async def run_visual_qa(
    session: Session,
    blob_store: BlobStore,
    *,
    project_id: UUID,
    options: VisualQACommandOptions,
    identity_resolver: Callable[..., str] | None = None,
) -> VisualQACommandResult:
    """Run or resume visual QA for a whole project or one shot."""
    first, second = build_agents(options)
    pipeline = VisualQAPipeline(
        session,
        blob_store,
        first,
        adjudicator=second,
        shot_workflow_identity_resolver=identity_resolver or shot_workflow_identity_resolver,
        options=VisualQAOptions(
            expected_width=options.expected_width, expected_height=options.expected_height
        ),
    )
    storyboard = session.scalar(
        select(StoryboardRun).where(
            StoryboardRun.project_id == project_id, StoryboardRun.selected.is_(True)
        )
    )
    if storyboard is None:
        raise VisualQAConfigurationError("project has no selected T13 storyboard")
    shots = list(
        session.scalars(
            select(StoryboardShotRecord)
            .where(StoryboardShotRecord.storyboard_run_id == storyboard.id)
            .order_by(StoryboardShotRecord.global_sequence)
        )
    )
    if options.shot_id is not None:
        shots = [shot for shot in shots if options.shot_id in {shot.id, shot.stable_shot_id}]
        if not shots:
            raise VisualQAConfigurationError("shot is not part of the selected storyboard")
    results: list[VisualQAResult] = []
    failures: list[tuple[UUID, VisualQATargetType, str]] = []
    for shot in shots:
        for target in options.targets:
            # A supplied key scopes the whole request, so it is still qualified
            # per shot and target: one key reused verbatim across shots would
            # bind shot 2's inputs to shot 1's run and fail as a conflict.
            key = (
                f"{options.idempotency_key}:{shot.id}:{target.value}"
                if options.idempotency_key
                else f"visual-qa:{shot.id}:{target.value}"
            )
            try:
                results.append(
                    await pipeline.evaluate_shot(
                        project_id=project_id,
                        shot_id=shot.id,
                        target_type=target,
                        idempotency_key=key,
                    )
                )
            except Exception as error:  # a failed shot never stops its siblings
                failures.append((shot.id, target, _code(error)))
    return VisualQACommandResult(
        project_id=project_id,
        storyboard_run_id=storyboard.id,
        results=tuple(results),
        failures=tuple(failures),
    )


def _code(error: Exception) -> str:
    failure = getattr(error, "failure", None)
    if failure is not None:
        return str(getattr(failure, "code", type(error).__name__))
    return type(error).__name__


class VisualQABlocked(RuntimeError):
    """T20 blocked the shot. T21 owns the repair; T20 never regenerates."""

    def __init__(
        self, target: VisualQATargetType, qa_run_id: UUID, repair_codes: tuple[str, ...]
    ) -> None:
        super().__init__(f"{target.value} QA failed: {', '.join(repair_codes) or 'no repair code'}")
        self.target = target
        self.qa_run_id = qa_run_id
        self.repair_codes = repair_codes
        self.retryable = False


class VisualQAReviewRequired(RuntimeError):
    """T20 could not resolve the shot; a human decision is required."""

    def __init__(
        self, target: VisualQATargetType, qa_run_id: UUID, repair_codes: tuple[str, ...]
    ) -> None:
        super().__init__(f"{target.value} QA requires human review")
        self.target = target
        self.qa_run_id = qa_run_id
        self.repair_codes = repair_codes
        self.retryable = False


async def evaluate_shot_stage(
    session: Session,
    blob_store: BlobStore,
    *,
    project_id: UUID,
    shot_id: UUID,
    target_type: VisualQATargetType,
    options: VisualQACommandOptions,
    identity_resolver: Callable[..., str] | None = None,
) -> VisualQAResult:
    """Run or resume one T16 QA stage and enforce the gate.

    A completed run is reused verbatim, so a worker restart re-reads the stored
    result instead of paying for a second evaluation. A human approval recorded
    against a ``REVIEW`` result clears the gate without changing the result.
    """
    first, second = build_agents(options)
    pipeline = VisualQAPipeline(
        session,
        blob_store,
        first,
        adjudicator=second,
        shot_workflow_identity_resolver=identity_resolver or shot_workflow_identity_resolver,
        options=VisualQAOptions(
            expected_width=options.expected_width,
            expected_height=options.expected_height,
            thresholds=DETERMINISTIC_THRESHOLDS
            if options.ocr_threshold is None
            else replace(DETERMINISTIC_THRESHOLDS, ocr_confidence_warning=options.ocr_threshold),
        ),
    )
    result = await pipeline.evaluate_shot(
        project_id=project_id,
        shot_id=shot_id,
        target_type=target_type,
        idempotency_key=options.idempotency_key or f"visual-qa:{shot_id}:{target_type.value}",
    )
    # Gate on the shot the selector actually resolved, not the caller's argument:
    # the selector also accepts a stable shot ID, and gating on that form would
    # report visual_qa_missing for the shot that just passed.
    passed, reason = VisualQARepository(session).gate(result.target.storyboard_shot_id, target_type)
    if passed:
        return result
    codes = tuple(code.value for code in result.repair_codes)
    if reason == "visual_qa_review_required":
        raise VisualQAReviewRequired(target_type, result.qa_run_id, codes)
    raise VisualQABlocked(target_type, result.qa_run_id, codes)


# --- T21 repair and fallback routing -----------------------------------------
@dataclass(frozen=True, slots=True)
class VisualRepairCommandOptions:
    """Everything the CLI, the API worker and the T16 activity can choose."""

    provider: str = "fake"
    alternate_provider: str | None = "fake"
    idempotency_key: str | None = None
    max_same_provider_repairs: int = 2
    allow_parallax_fallback: bool = True
    resume: bool = False
    width: int = 1280
    height: int = 720
    frame_rate: int = 24
    per_shot_repair_cost_limit: Decimal | None = None
    per_run_repair_cost_limit: Decimal | None = None
    provider_configuration_version: str = "runway/2024-11-06"
    alternate_provider_model: str | None = None
    #: Fake alternate provider only. Deterministic fixtures use small media, and
    #: a real Veo model renders only 720p and 1080p, so tests pin the geometry
    #: here rather than pretending the model supports it.
    alternate_fake_geometry: tuple[int, int] | None = None
    qa: VisualQACommandOptions = field(default_factory=VisualQACommandOptions)
    #: Injected by tests so a repaired output can be scored deterministically.
    qa_defects_by_attempt: tuple[dict[UUID, FakeDefect], ...] = ()


def build_same_provider(options: VisualRepairCommandOptions) -> VideoGenerationProvider:
    """The T15 provider a same-provider repair reuses.

    A repair never introduces a second way to reach the animation provider: it
    builds the same provider T15 builds, from the same configuration.
    """
    from services.animation.commands import AnimationCommandOptions, build_provider

    if options.provider not in {"fake", "runway"}:
        raise VisualQAConfigurationError(f"unknown repair provider {options.provider!r}")
    return build_provider(
        AnimationCommandOptions(
            provider=options.provider,
            # The same secret T15 reads, under both the name the SDK documents
            # and the repository's own prefixed setting.
            runway_api_key=(
                os.getenv("RUNWAYML_API_SECRET") or os.getenv("VIDGEN_RUNWAY_API_SECRET")
            ),
        )
    )


def build_alternate_provider(
    options: VisualRepairCommandOptions,
) -> AlternateVideoProvider | None:
    """The single bounded alternate provider, or ``None`` to skip that route."""
    if options.alternate_provider in {None, "none"}:
        return None
    if options.alternate_provider == "fake":
        from services.animation.veo_fake import FakeVeoProvider

        geometry = options.alternate_fake_geometry
        return FakeVeoProvider(
            model=options.alternate_provider_model,
            output_width=geometry[0] if geometry else None,
            output_height=geometry[1] if geometry else None,
        )
    if options.alternate_provider in {"veo", "google_veo"}:
        from services.animation.veo_adapter import GoogleVeoProvider

        project = os.getenv("VIDGEN_GOOGLE_CLOUD_PROJECT")
        token = os.getenv("VIDGEN_GOOGLE_ACCESS_TOKEN")
        if not project or not token:
            raise VisualQAConfigurationError(
                "the Veo provider requires a Google Cloud project and an access token"
            )
        return GoogleVeoProvider(
            project=project,
            access_token=lambda: token,
            location=os.getenv("VIDGEN_VEO_LOCATION", DEFAULT_VEO_LOCATION),
            model=options.alternate_provider_model,
        )
    raise VisualQAConfigurationError(f"unknown alternate provider {options.alternate_provider!r}")


def build_revalidator(
    session: Session,
    blob_store: BlobStore,
    options: VisualRepairCommandOptions,
    *,
    identity_resolver: Callable[..., str] | None = None,
) -> Revalidator:
    """Run T20 video QA over whatever output the repair just selected.

    Every repaired or fallback result is revalidated through the real T20
    pipeline, so nothing is ever selected on the strength of the repair alone.
    """
    resolver = identity_resolver or shot_workflow_identity_resolver

    async def revalidate(
        *, project_id: UUID, shot_id: UUID, idempotency_key: str
    ) -> VisualQAResult:
        first, second = build_agents(options.qa)
        pipeline = VisualQAPipeline(
            session,
            blob_store,
            first,
            adjudicator=second,
            shot_workflow_identity_resolver=resolver,
            options=VisualQAOptions(
                expected_width=options.qa.expected_width or options.width,
                expected_height=options.qa.expected_height or options.height,
            ),
        )
        return await pipeline.evaluate_shot(
            project_id=project_id,
            shot_id=shot_id,
            target_type=VisualQATargetType.VIDEO,
            idempotency_key=idempotency_key,
        )

    return revalidate


async def run_visual_repair(
    session: Session,
    blob_store: BlobStore,
    *,
    project_id: UUID,
    shot_id: UUID,
    options: VisualRepairCommandOptions,
    identity_resolver: Callable[..., str] | None = None,
    revalidate: Revalidator | None = None,
    same_provider: VideoGenerationProvider | None = None,
) -> RepairOutcome:
    """Run or resume the bounded T21 repair for exactly one failed shot.

    ``same_provider`` lets a caller that already holds a configured T15 provider
    - the T16 worker does - hand it straight in, so a repair never builds a
    second, differently credentialed route to the same provider.
    """
    same = same_provider or build_same_provider(options)
    alternate = build_alternate_provider(options)
    policy = default_policy(
        max_same_provider_repairs=options.max_same_provider_repairs,
        allow_parallax_fallback=options.allow_parallax_fallback,
        per_shot_repair_cost_limit=options.per_shot_repair_cost_limit,
        per_run_repair_cost_limit=options.per_run_repair_cost_limit,
    )
    pipeline = VisualRepairPipeline(
        session,
        blob_store,
        same_provider=same,
        alternate_provider=alternate,
        revalidate=revalidate
        or build_revalidator(session, blob_store, options, identity_resolver=identity_resolver),
        shot_workflow_identity_resolver=identity_resolver or shot_workflow_identity_resolver,
        options=RepairOptions(
            policy=policy,
            width=options.width,
            height=options.height,
            frame_rate=options.frame_rate,
            provider_configuration_version=options.provider_configuration_version,
            alternate_provider_model=options.alternate_provider_model,
        ),
    )
    key = options.idempotency_key or f"t21-repair:{shot_id}"
    try:
        return await pipeline.repair(project_id=project_id, shot_id=shot_id, idempotency_key=key)
    finally:
        closer = getattr(alternate, "aclose", None)
        if closer is not None:
            await closer()
