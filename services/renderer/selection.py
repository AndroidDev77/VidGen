"""Authoritative T11-T16 database selection before manifest immutability."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from vidgen.contracts.render import CaptionWord
from vidgen.contracts.visual_qa import VisualQATargetType
from vidgen.db.animation_models import AnimationGeneratedVideo, AnimationItem, AnimationRun
from vidgen.db.models import Asset, Project
from vidgen.db.narration_models import NarrationRun, NarrationSegment
from vidgen.db.script_models import Script
from vidgen.db.storyboard_models import StoryboardRun, StoryboardShotRecord
from vidgen.db.visual_qa_models import VisualQARun
from vidgen.db.visual_qa_repository import VisualQARepository

#: The render-eligibility policy this selection enforces. It is recorded in the
#: manifest provenance so an existing render stays readable and historical: a
#: manifest written before T20 simply carries no visual-QA provenance, and its
#: identity semantics are unchanged.
VISUAL_QA_POLICY_VERSION = "visual-qa-render-policy/1.0"


class RenderLineageError(ValueError):
    def __init__(self, code: str, message: str, *, reference_id: UUID | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.reference_id = reference_id
        self.retryable = False


@dataclass(frozen=True, slots=True)
class SelectedShotInput:
    shot: StoryboardShotRecord
    animation_run: AnimationRun
    video: AnimationGeneratedVideo
    asset: Asset
    #: The canonical passing T20 video-QA result, when the project is governed.
    visual_qa_run_id: UUID | None = None
    visual_qa_result_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class AuthoritativeRenderSelection:
    project: Project
    script: Script
    narration: NarrationRun
    narration_segments: tuple[NarrationSegment, ...]
    storyboard: StoryboardRun
    timing_manifest_asset: Asset
    shots: tuple[SelectedShotInput, ...]
    visual_qa_policy_version: str = VISUAL_QA_POLICY_VERSION
    #: Empty for a legacy project that predates the T20 policy.
    visual_qa_result_ids: tuple[UUID, ...] = ()


def animation_run_for_video(
    session: Session, video: AnimationGeneratedVideo, storyboard_id: UUID
) -> AnimationRun | None:
    """Resolve a clip through its owning item, never through project coincidence."""
    return session.scalar(
        select(AnimationRun)
        .join(AnimationItem, AnimationItem.run_id == AnimationRun.id)
        .where(
            AnimationItem.id == video.animation_item_id,
            AnimationRun.storyboard_id == storyboard_id,
        )
    )


def select_authoritative_inputs(session: Session, project_id: UUID) -> AuthoritativeRenderSelection:
    project = session.get(Project, project_id)
    if project is None:
        raise RenderLineageError(
            "project_not_found", "requested project does not exist", reference_id=project_id
        )
    script = session.scalar(
        select(Script).where(Script.project_id == project_id, Script.selected.is_(True))
    )
    if script is None or script.status != "approved":
        raise RenderLineageError(
            "script_not_approved", "project has no selected approved T11 script"
        )
    narration = session.scalar(
        select(NarrationRun).where(
            NarrationRun.project_id == project_id, NarrationRun.selected.is_(True)
        )
    )
    if narration is None or narration.status != "completed":
        raise RenderLineageError(
            "narration_not_complete", "selected T12 narration is missing or incomplete"
        )
    if (narration.script_id, narration.script_version) != (script.id, script.version):
        raise RenderLineageError(
            "narration_script_mismatch",
            "selected narration was generated from a different script version",
            reference_id=narration.id,
        )
    segments = tuple(
        session.scalars(
            select(NarrationSegment)
            .where(NarrationSegment.narration_run_id == narration.id)
            .order_by(NarrationSegment.sequence)
        ).all()
    )
    if not segments or any(
        segment.status != "completed"
        or not segment.word_timings
        or not segment.duration_seconds
        or segment.normalized_asset_id is None
        for segment in segments
    ):
        raise RenderLineageError(
            "narration_alignment_missing",
            "every narration segment requires completed audio, measured duration, and word timings",
        )
    storyboard = session.scalar(
        select(StoryboardRun).where(
            StoryboardRun.project_id == project_id, StoryboardRun.selected.is_(True)
        )
    )
    if storyboard is None or storyboard.status != "completed":
        raise RenderLineageError(
            "storyboard_not_complete", "selected T13 storyboard is missing or incomplete"
        )
    if (storyboard.script_id, storyboard.script_version, storyboard.narration_run_id) != (
        script.id,
        script.version,
        narration.id,
    ):
        raise RenderLineageError(
            "storyboard_upstream_mismatch",
            "selected storyboard was generated from different narration or script lineage",
            reference_id=storyboard.id,
        )
    timing_asset = session.get(Asset, storyboard.timing_manifest_asset_id)
    if timing_asset is None or timing_asset.project_id != project_id:
        raise RenderLineageError(
            "timing_manifest_missing",
            "selected storyboard timing manifest asset is missing or cross-project",
        )
    shot_rows = tuple(
        session.scalars(
            select(StoryboardShotRecord)
            .where(StoryboardShotRecord.storyboard_run_id == storyboard.id)
            .order_by(StoryboardShotRecord.global_sequence)
        ).all()
    )
    if len(shot_rows) != storyboard.shot_count or not shot_rows:
        raise RenderLineageError(
            "shot_coverage_missing", "storyboard shot count does not match canonical ordered shots"
        )
    # A project is governed by the T20 render policy once any visual-QA run
    # exists for it. Legacy projects that predate T20 keep rendering unchanged,
    # and their historical renders stay readable.
    qa = VisualQARepository(session)
    governed = (
        session.scalar(select(VisualQARun.id).where(VisualQARun.project_id == project_id))
        is not None
    )
    selected: list[SelectedShotInput] = []
    expected_start = 0
    for sequence, shot in enumerate(shot_rows):
        if shot.global_sequence != sequence or shot.global_start_us != expected_start:
            raise RenderLineageError(
                "shot_timing_gap",
                "ordered shot coverage contains a gap, overlap, or non-dense sequence",
                reference_id=shot.id,
            )
        expected_start = shot.global_end_us
        video = session.scalar(
            select(AnimationGeneratedVideo).where(
                AnimationGeneratedVideo.shot_id == shot.id,
                AnimationGeneratedVideo.selected.is_(True),
            )
        )
        if video is None:
            raise RenderLineageError(
                "shot_not_locked",
                "required T16 shot has no selected canonical T15 video",
                reference_id=shot.id,
            )
        run = animation_run_for_video(session, video, storyboard.id)
        if run is None or run.status != "completed":
            raise RenderLineageError(
                "animation_not_complete",
                "selected clip does not belong to a completed compatible T15 run",
                reference_id=video.id,
            )
        asset = session.get(Asset, video.canonical_asset_id)
        if asset is None or asset.project_id != project_id or asset.sha256 != video.sha256:
            raise RenderLineageError(
                "video_asset_mismatch",
                "selected video asset ownership or persisted SHA-256 does not match",
                reference_id=video.id,
            )
        if round(video.canonical_duration * 1_000_000) != shot.usable_duration_us:
            raise RenderLineageError(
                "stale_clip_duration",
                "selected clip duration is incompatible with T13 canonical timing",
                reference_id=video.id,
            )
        qa_run_id: UUID | None = None
        qa_result_id: UUID | None = None
        if governed:
            qa_run_id, qa_result_id = _require_visual_qa(qa, shot.id)
        selected.append(
            SelectedShotInput(
                shot=shot,
                animation_run=run,
                video=video,
                asset=asset,
                visual_qa_run_id=qa_run_id,
                visual_qa_result_id=qa_result_id,
            )
        )
    if (
        expected_start != storyboard.total_duration_us
        or narration.total_duration_seconds is None
        or abs(expected_start - round(narration.total_duration_seconds * 1_000_000)) > 40_000
    ):
        raise RenderLineageError(
            "duration_mismatch",
            "visual, timing-manifest, and measured narration durations exceed tolerance",
        )
    return AuthoritativeRenderSelection(
        visual_qa_result_ids=tuple(
            item.visual_qa_result_id for item in selected if item.visual_qa_result_id is not None
        ),
        project=project,
        script=script,
        narration=narration,
        narration_segments=segments,
        storyboard=storyboard,
        timing_manifest_asset=timing_asset,
        shots=tuple(selected),
    )


def _require_visual_qa(
    repository: VisualQARepository, shot_id: UUID
) -> tuple[UUID | None, UUID | None]:
    """Block render eligibility unless this shot has a passing canonical video QA.

    A hard failure blocks outright. A ``REVIEW`` result blocks automatic render
    completion until a human resolves it. Both refusals are structured and
    non-retryable, and neither deletes or rewrites an existing render.
    """
    passed, reason = repository.gate(shot_id, VisualQATargetType.VIDEO)
    if not passed:
        raise RenderLineageError(reason, _QA_MESSAGES[reason], reference_id=shot_id)
    run = repository.canonical_run(shot_id, VisualQATargetType.VIDEO)
    if run is None:  # pragma: no cover - gate already proved the run exists
        raise RenderLineageError(
            "visual_qa_missing", _QA_MESSAGES["visual_qa_missing"], reference_id=shot_id
        )
    result = repository.canonical_result(run.id)
    return run.id, result.id if result is not None else None


_QA_MESSAGES = {
    "visual_qa_missing": "shot has no completed T20 video-QA result",
    "visual_qa_failed": "shot is blocked by a failing T20 video-QA result",
    "visual_qa_review_required": (
        "shot has a T20 video-QA result awaiting human review; automatic render "
        "completion is blocked until it is resolved"
    ),
}


def visual_qa_provenance(selection: AuthoritativeRenderSelection) -> dict[str, object]:
    """Manifest provenance naming the applicable QA results and policy version.

    Recording this in ``provenance`` keeps existing manifest identity semantics
    intact: a pre-T20 manifest simply has no ``visual_qa`` key.
    """
    if not selection.visual_qa_result_ids:
        return {}
    return {
        "visual_qa": {
            "policy_version": selection.visual_qa_policy_version,
            "result_ids": [str(value) for value in selection.visual_qa_result_ids],
            "run_ids": [
                str(item.visual_qa_run_id)
                for item in selection.shots
                if item.visual_qa_run_id is not None
            ],
        }
    }


def project_narration_words(
    session: Session, *, storyboard_run_id: UUID, narration_run_id: UUID
) -> tuple[tuple[tuple[UUID, int, int], ...], tuple[CaptionWord, ...]]:
    """Project approved T12 word timings onto the global storyboard timeline.

    This is the single definition of "the approved words, in order, at their
    measured global times". T17b builds the deliverable caption track from it
    and T22 independently reconstructs captions from the same projection, so a
    disagreement between the two is a real finding rather than two different
    interpretations of the same rows.
    """
    shots = list(
        session.scalars(
            select(StoryboardShotRecord)
            .where(StoryboardShotRecord.storyboard_run_id == storyboard_run_id)
            .order_by(StoryboardShotRecord.global_sequence)
        )
    )
    spans: dict[UUID, list[int]] = {}
    for shot in shots:
        span = spans.setdefault(
            shot.narration_segment_id, [shot.global_start_us, shot.global_end_us]
        )
        span[0] = min(span[0], shot.global_start_us)
        span[1] = max(span[1], shot.global_end_us)
    segments = {
        segment.id: segment
        for segment in session.scalars(
            select(NarrationSegment)
            .where(NarrationSegment.narration_run_id == narration_run_id)
            .order_by(NarrationSegment.sequence)
        )
    }
    intervals = tuple(
        (segment_id, span[0], span[1])
        for segment_id, span in sorted(spans.items(), key=lambda item: item[1][0])
    )
    words: list[CaptionWord] = []
    for segment_id, start, _end in intervals:
        segment = segments.get(segment_id)
        for timing in list(segment.word_timings or []) if segment is not None else []:
            text = f"{timing.get('word', '')}{timing.get('punctuation', '')}".strip()
            word_start = start + round(float(timing.get("start_seconds", 0.0)) * 1_000_000)
            word_end = start + round(float(timing.get("end_seconds", 0.0)) * 1_000_000)
            if not text or word_end <= word_start:
                continue
            words.append(
                CaptionWord(
                    sequence=len(words), text=text[:128], start_us=word_start, end_us=word_end
                )
            )
    return intervals, tuple(words)
