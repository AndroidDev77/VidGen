"""Gather each stage's progress inputs from its durable rows.

Every loader answers one question, "where is this stage for this project?",
from the rows the stage's pipeline commits as it works, and returns ``None``
when the stage has not started. ``load_stage_progress`` asks all of them and
reports the stage that moved most recently, which is the one the owner is
waiting on: stages that never write ``project.status`` (evidence, references,
rendering, the final check) still surface because their own rows are newer
than anything the earlier stages wrote.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, Table, func, select
from sqlalchemy.orm import Session

from services.progress import specs
from services.progress.engine import StageProgress, StageSpec, calculate_progress, latest
from vidgen.contracts.final_editorial import PHASE_ORDER
from vidgen.db.animation_models import AnimationGeneratedVideo, AnimationItem, AnimationRun
from vidgen.db.continuity_models import (
    character_identity_versions,
    character_reference_candidates,
    character_reference_sets,
    location_identity_versions,
    location_reference_candidates,
    location_reference_sets,
    shot_reference_bindings,
)
from vidgen.db.episode_analysis_models import EpisodeAnalysisRun, SceneAnalysisCheckpoint
from vidgen.db.final_editorial_models import FinalEditorialRun
from vidgen.db.image_generation_models import (
    GeneratedKeyframeImage,
    ImageGenerationItem,
    ImageGenerationRun,
)
from vidgen.db.models import Asset, Project, RenderJob, Scene
from vidgen.db.narration_models import NarrationRun, NarrationSegment
from vidgen.db.script_models import Script, ScriptGenerationRun, ScriptReview, ScriptSegment
from vidgen.db.storyboard_models import (
    StoryboardRun,
    StoryboardSegmentCheckpoint,
    StoryboardShotRecord,
)
from vidgen.db.subtitle_models import SubtitleRun
from vidgen.db.transcription_models import TranscriptionChunk, TranscriptionRun
from vidgen.db.visual_qa_models import VisualQARun
from vidgen.db.workflow_models import EvidencePackageRecord, SceneEvidenceRecord

Loader = Callable[[Session, Project], StageProgress | None]


def _utc(value: datetime | None) -> datetime | None:
    """SQLite hands back naive timestamps; give them the zone they were written in."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _count(session: Session, statement: Select[tuple[int]]) -> int:
    return int(session.scalar(statement) or 0)


def _status_for(spec: StageSpec, project: Project, run_status: str) -> str:
    """Prefer the project status when it belongs to this stage.

    Some pipelines write finer-grained phases to ``project.status`` than to
    their run row (narration's aligning and validating, the storyboard's
    retiming); when the project status is one of this stage's, it is the
    most recent word.
    """
    return project.status if project.status in spec.status_phases else run_status


def _newest_run(session: Session, model: Any, project_id: UUID) -> Any:
    """The project's most recent run of ``model``: the one the owner waits on."""
    return session.scalar(
        select(model)
        .where(model.project_id == project_id)
        .order_by(model.created_at.desc(), model.id.desc())
    )


def _selected_storyboard(session: Session, project_id: UUID) -> StoryboardRun | None:
    return session.scalar(
        select(StoryboardRun).where(
            StoryboardRun.project_id == project_id, StoryboardRun.selected.is_(True)
        )
    )


def _segment_total(session: Session, script_id: UUID) -> int:
    return _count(
        session,
        select(func.count()).select_from(ScriptSegment).where(ScriptSegment.script_id == script_id),
    )


# --------------------------------------------------------------------------
# Stage loaders
# --------------------------------------------------------------------------


def media_processing(session: Session, project: Project) -> StageProgress | None:
    """Media processing writes only ``project.status``, so it reports while
    that status is one of its own and not afterwards."""
    spec = specs.MEDIA_PROCESSING
    if project.status not in spec.status_phases:
        return None
    total = _count(
        session, select(func.count()).select_from(Scene).where(Scene.project_id == project.id)
    )
    done = _count(
        session,
        select(func.count())
        .select_from(Asset)
        .where(Asset.project_id == project.id, Asset.kind == "frame"),
    )
    return calculate_progress(
        spec,
        status=project.status,
        completed_count=done,
        total_count=total,
        updated_at=project.updated_at,
    )


def transcription(session: Session, project: Project) -> StageProgress | None:
    """Either acquisition path: subtitles first, audio transcription if they
    fall through. The newer run is the one still doing the work."""
    spec = specs.TRANSCRIPTION
    subtitle = _newest_run(session, SubtitleRun, project.id)
    audio = _newest_run(session, TranscriptionRun, project.id)
    if subtitle is None and audio is None:
        return None
    assert isinstance(subtitle, SubtitleRun | None)
    assert isinstance(audio, TranscriptionRun | None)
    use_audio = audio is not None and (subtitle is None or audio.created_at >= subtitle.created_at)
    if use_audio:
        assert audio is not None
        chunks = list(
            session.scalars(select(TranscriptionChunk).where(TranscriptionChunk.run_id == audio.id))
        )
        return calculate_progress(
            spec,
            status=_status_for(spec, project, audio.status),
            completed_count=sum(1 for chunk in chunks if chunk.status == "complete"),
            total_count=len(chunks),
            error_code=audio.error_code,
            updated_at=latest(audio.updated_at, *(chunk.updated_at for chunk in chunks)),
        )
    assert subtitle is not None
    return calculate_progress(
        spec,
        status=_status_for(spec, project, subtitle.status),
        completed_count=0,
        total_count=0,
        error_code=subtitle.error_code,
        updated_at=subtitle.updated_at,
    )


def evidence(session: Session, project: Project) -> StageProgress | None:
    """Evidence is assembled in one transaction, so it is either pending or
    ready; the scene count from media processing is the total either way."""
    spec = specs.EVIDENCE
    package = session.scalar(
        select(EvidencePackageRecord).where(
            EvidencePackageRecord.project_id == project.id,
            EvidencePackageRecord.selected.is_(True),
        )
    )
    scenes = _count(
        session, select(func.count()).select_from(Scene).where(Scene.project_id == project.id)
    )
    if package is not None:
        with_evidence = _count(
            session,
            select(func.count())
            .select_from(SceneEvidenceRecord)
            .where(SceneEvidenceRecord.evidence_package_id == package.id),
        )
        return calculate_progress(
            spec,
            status="complete",
            completed_count=with_evidence,
            total_count=max(scenes, with_evidence),
            updated_at=package.created_at,
        )
    if project.status != "transcribed":
        return None
    return calculate_progress(
        spec,
        status="pending",
        completed_count=0,
        total_count=scenes,
        updated_at=project.updated_at,
    )


def episode_analysis(session: Session, project: Project) -> StageProgress | None:
    """The newest analysis run and its per-scene checkpoints. Before the run
    has written checkpoints the total comes from the evidence package."""
    spec = specs.EPISODE_ANALYSIS
    run = _newest_run(session, EpisodeAnalysisRun, project.id)
    if run is None:
        return None
    assert isinstance(run, EpisodeAnalysisRun)
    checkpoints = list(
        session.scalars(
            select(SceneAnalysisCheckpoint).where(SceneAnalysisCheckpoint.analysis_run_id == run.id)
        )
    )
    total = len(checkpoints) or _count(
        session,
        select(func.count())
        .select_from(SceneEvidenceRecord)
        .where(SceneEvidenceRecord.evidence_package_id == run.evidence_package_id),
    )
    return calculate_progress(
        spec,
        status=run.status,
        # ``invalid`` and ``failed`` checkpoints are retried, so still in flight.
        completed_count=sum(1 for item in checkpoints if item.status == "succeeded"),
        total_count=total,
        error_code=run.error_code,
        updated_at=latest(run.updated_at, *(item.updated_at for item in checkpoints)),
    )


def script_generation(session: Session, project: Project) -> StageProgress | None:
    """Script generation has no per-item rows; its countable work is the
    bounded editorial passes, one review row each."""
    spec = specs.SCRIPT_GENERATION
    run = _newest_run(session, ScriptGenerationRun, project.id)
    if run is None:
        return None
    assert isinstance(run, ScriptGenerationRun)
    scripts = list(session.scalars(select(Script).where(Script.generation_run_id == run.id)))
    reviews = (
        list(
            session.scalars(
                select(ScriptReview).where(
                    ScriptReview.script_id.in_([script.id for script in scripts])
                )
            )
        )
        if scripts
        else []
    )
    return calculate_progress(
        spec,
        status=_status_for(spec, project, run.status),
        completed_count=len(reviews),
        total_count=specs.SCRIPT_EDITORIAL_PASSES,
        error_code=run.error_code,
        updated_at=latest(
            run.updated_at,
            *(script.updated_at for script in scripts),
            *(review.created_at for review in reviews),
        ),
    )


def narration(session: Session, project: Project) -> StageProgress | None:
    """Segments are created lazily as they are narrated, so the total is the
    approved script's segment count rather than the rows written so far."""
    spec = specs.NARRATION
    run = _newest_run(session, NarrationRun, project.id)
    if run is None:
        return None
    assert isinstance(run, NarrationRun)
    segments = list(
        session.scalars(select(NarrationSegment).where(NarrationSegment.narration_run_id == run.id))
    )
    return calculate_progress(
        spec,
        status=_status_for(spec, project, run.status),
        completed_count=sum(1 for item in segments if item.status == "complete"),
        total_count=_segment_total(session, run.script_id),
        error_code=run.error_code,
        updated_at=latest(run.updated_at, *(item.updated_at for item in segments)),
    )


def storyboard(session: Session, project: Project) -> StageProgress | None:
    spec = specs.STORYBOARD
    run = _newest_run(session, StoryboardRun, project.id)
    if run is None:
        return None
    assert isinstance(run, StoryboardRun)
    checkpoints = list(
        session.scalars(
            select(StoryboardSegmentCheckpoint).where(
                StoryboardSegmentCheckpoint.storyboard_run_id == run.id
            )
        )
    )
    return calculate_progress(
        spec,
        status=_status_for(spec, project, run.status),
        completed_count=sum(1 for item in checkpoints if item.status == "complete"),
        total_count=_segment_total(session, run.script_id),
        error_code=run.error_code,
        updated_at=latest(run.updated_at, *(item.updated_at for item in checkpoints)),
    )


def _reference_counts(
    session: Session, project_id: UUID, versions: Table, candidates: Table, sheets: Table
) -> tuple[int, int, int, datetime | None]:
    """(entities needing a sheet, sheets generated, sheets approved, last change)."""
    required = _count(
        session,
        select(func.count(func.distinct(candidates.c.identity_version_id)))
        .select_from(candidates)
        .join(versions, candidates.c.identity_version_id == versions.c.id)
        .where(versions.c.project_id == project_id),
    )
    rows = list(
        session.execute(
            select(sheets.c.status, sheets.c.updated_at).where(sheets.c.project_id == project_id)
        )
    )
    generated = sum(1 for status, _ in rows if status in {"draft", "approved"})
    approved = sum(1 for status, _ in rows if status == "approved")
    changed = latest(
        session.scalar(
            select(func.max(versions.c.updated_at)).where(versions.c.project_id == project_id)
        ),
        *(updated for _, updated in rows),
    )
    return required, generated, approved, changed


def references(session: Session, project: Project) -> StageProgress | None:
    """Reference sheets have no run row: the stage is read from the identity
    versions, the sheets generated for them, and the bindings to shots."""
    spec = specs.REFERENCES
    versions = _count(
        session,
        select(func.count())
        .select_from(character_identity_versions)
        .where(character_identity_versions.c.project_id == project.id),
    ) + _count(
        session,
        select(func.count())
        .select_from(location_identity_versions)
        .where(location_identity_versions.c.project_id == project.id),
    )
    if versions == 0:
        return None
    characters = _reference_counts(
        session,
        project.id,
        character_identity_versions,
        character_reference_candidates,
        character_reference_sets,
    )
    locations = _reference_counts(
        session,
        project.id,
        location_identity_versions,
        location_reference_candidates,
        location_reference_sets,
    )
    required = characters[0] + locations[0]
    generated = min(characters[1] + locations[1], required)
    approved = min(characters[2] + locations[2], required)
    storyboard_run = _selected_storyboard(session, project.id)
    bindings = (
        _count(
            session,
            select(func.count())
            .select_from(shot_reference_bindings)
            .where(shot_reference_bindings.c.storyboard_id == storyboard_run.id),
        )
        if storyboard_run is not None
        else 0
    )
    bound_at = session.scalar(
        select(func.max(shot_reference_bindings.c.updated_at)).where(
            shot_reference_bindings.c.project_id == project.id
        )
    )
    shots = storyboard_run.shot_count if storyboard_run is not None else 0
    if generated < required:
        status, done = "generating", generated
    elif approved < required:
        status, done = "awaiting_approval", approved
    elif bindings < shots:
        status, done = "binding", approved
    else:
        status, done = "complete", approved
    return calculate_progress(
        spec,
        status=status,
        completed_count=done,
        total_count=required,
        updated_at=latest(characters[3], locations[3], bound_at),
    )


#: ``project.status`` values the per-shot pipelines write while shots are in
#: flight, grouped by the phase they belong to.
_KEYFRAME_STATUSES = frozenset(
    {"keyframes_queued", "keyframes_compiling", "keyframes_generating", "keyframes_persisting"}
)
_ANIMATION_STATUSES = frozenset(
    {
        "animation_queued",
        "animation_submitting",
        "animation_polling",
        "animation_downloading",
        "animation_trimming",
    }
)
_AMBIGUOUS_SHOT_STATUSES = frozenset(
    {
        "keyframes_complete",
        "keyframes_failed",
        "animation_complete",
        "animation_failed",
        "shot_generation_running",
        "shot_generation_retrying",
    }
)
_TERMINAL_SHOT_STATUSES = frozenset(
    {
        "shot_generation_queued",
        "shot_generation_complete",
        "shot_generation_partial",
        "shot_generation_failed",
        "shot_generation_cancelled",
    }
)


def shot_generation(session: Session, project: Project) -> StageProgress | None:
    """Keyframes, animation and quality review, one shot at a time.

    The per-shot workflows run concurrently, so the bar blends the three
    counts and the message names whichever step the pipelines are on. Once
    every shot is animated the remaining work is review, which the heading
    says out loud.
    """
    spec = specs.SHOT_GENERATION
    storyboard_run = _selected_storyboard(session, project.id)
    if storyboard_run is None:
        return None
    shot_ids = select(StoryboardShotRecord.id).where(
        StoryboardShotRecord.storyboard_run_id == storyboard_run.id
    )
    total = storyboard_run.shot_count
    keyframed = _count(
        session,
        select(func.count(func.distinct(GeneratedKeyframeImage.shot_id))).where(
            GeneratedKeyframeImage.shot_id.in_(shot_ids),
            GeneratedKeyframeImage.keyframe_role == "FIRST_FRAME",
            GeneratedKeyframeImage.selected.is_(True),
        ),
    )
    animated = _count(
        session,
        select(func.count(func.distinct(AnimationGeneratedVideo.shot_id))).where(
            AnimationGeneratedVideo.shot_id.in_(shot_ids),
            AnimationGeneratedVideo.selected.is_(True),
        ),
    )
    reviewed = _count(
        session,
        select(func.count(func.distinct(VisualQARun.shot_id))).where(
            VisualQARun.shot_id.in_(shot_ids),
            VisualQARun.target_type == "video",
            VisualQARun.status == "visual_qa_complete",
        ),
    )
    status = project.status
    in_flight = status in _KEYFRAME_STATUSES | _ANIMATION_STATUSES | _AMBIGUOUS_SHOT_STATUSES
    if not in_flight and status not in _TERMINAL_SHOT_STATUSES:
        if keyframed + animated + reviewed == 0:
            return None
        status = "shot_generation_running"
        in_flight = True
    if status in _KEYFRAME_STATUSES:
        phase, done = "keyframes", keyframed
    elif status in _ANIMATION_STATUSES:
        phase, done = "animating", animated
    elif in_flight or (status == "shot_generation_queued" and keyframed + animated + reviewed):
        if animated >= total and total:
            phase, done = "reviewing", reviewed
        elif keyframed >= total and total:
            phase, done = "animating", animated
        else:
            phase, done = "keyframes", keyframed
    else:
        phase, done = status, animated
    changed = latest(
        session.scalar(
            select(func.max(ImageGenerationItem.updated_at))
            .join(ImageGenerationRun, ImageGenerationRun.id == ImageGenerationItem.run_id)
            .where(ImageGenerationRun.project_id == project.id)
        ),
        session.scalar(
            select(func.max(AnimationItem.updated_at))
            .join(AnimationRun, AnimationRun.id == AnimationItem.run_id)
            .where(AnimationRun.project_id == project.id)
        ),
        session.scalar(
            select(func.max(VisualQARun.updated_at)).where(VisualQARun.project_id == project.id)
        ),
        project.updated_at if in_flight else None,
    )
    progress = calculate_progress(
        spec,
        status=phase,
        completed_count=done,
        total_count=total,
        updated_at=changed,
        share=(keyframed + animated + reviewed) / (3 * total) if total else 0.0,
    )
    if phase == "reviewing":
        progress = replace(progress, label=specs.QUALITY_REVIEW_LABEL)
    return progress


def rendering(session: Session, project: Project) -> StageProgress | None:
    """The render job keeps its own durable percentage and checkpoint."""
    spec = specs.RENDERING
    job = _newest_run(session, RenderJob, project.id)
    if job is None:
        return None
    assert isinstance(job, RenderJob)
    return calculate_progress(
        spec,
        status=job.status,
        completed_count=0,
        total_count=0,
        error_code=job.error_code,
        # Claims and heartbeats update the row without touching ``updated_at``.
        updated_at=latest(job.updated_at, job.heartbeat_at, job.completed_at),
        share=job.progress_percent / 100,
    )


def final_qa(session: Session, project: Project) -> StageProgress | None:
    """The final check records the phases it has completed on its run row."""
    spec = specs.FINAL_QA
    run = _newest_run(session, FinalEditorialRun, project.id)
    if run is None:
        return None
    assert isinstance(run, FinalEditorialRun)
    return calculate_progress(
        spec,
        status=run.status,
        completed_count=len(run.completed_phases or []),
        total_count=len(PHASE_ORDER),
        error_code=run.error_code,
        updated_at=latest(run.updated_at, run.completed_at),
    )


#: Every loader, in pipeline order.
LOADERS: tuple[Loader, ...] = (
    media_processing,
    transcription,
    evidence,
    episode_analysis,
    script_generation,
    narration,
    storyboard,
    references,
    shot_generation,
    rendering,
    final_qa,
)


def load_stage_progress(session: Session, project: Project) -> StageProgress | None:
    """The progress of whichever stage moved most recently.

    Later stages win ties, so a stage that starts in the same second its
    predecessor finished is the one reported.
    """
    candidates: list[StageProgress] = []
    for loader in LOADERS:
        progress = loader(session, project)
        if progress is not None:
            candidates.append(replace(progress, updated_at=_utc(progress.updated_at)))
    if not candidates:
        return None
    floor = datetime.min.replace(tzinfo=UTC)
    return max(
        candidates,
        key=lambda item: (item.updated_at or floor, specs.STAGE_ORDER.index(item.stage)),
    )
