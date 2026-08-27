"""Authoritative T11-T16 database selection before manifest immutability."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from vidgen.db.animation_models import AnimationGeneratedVideo, AnimationRun
from vidgen.db.models import Asset, Project
from vidgen.db.narration_models import NarrationRun, NarrationSegment
from vidgen.db.script_models import Script
from vidgen.db.storyboard_models import StoryboardRun, StoryboardShotRecord


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


@dataclass(frozen=True, slots=True)
class AuthoritativeRenderSelection:
    project: Project
    script: Script
    narration: NarrationRun
    narration_segments: tuple[NarrationSegment, ...]
    storyboard: StoryboardRun
    timing_manifest_asset: Asset
    shots: tuple[SelectedShotInput, ...]


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
        run = session.scalar(
            select(AnimationRun)
            .join_from(
                AnimationRun,
                AnimationGeneratedVideo,
                AnimationGeneratedVideo.project_id == AnimationRun.project_id,
            )
            .where(
                AnimationGeneratedVideo.id == video.id, AnimationRun.storyboard_id == storyboard.id
            )
        )
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
        selected.append(SelectedShotInput(shot=shot, animation_run=run, video=video, asset=asset))
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
        project=project,
        script=script,
        narration=narration,
        narration_segments=segments,
        storyboard=storyboard,
        timing_manifest_asset=timing_asset,
        shots=tuple(selected),
    )
