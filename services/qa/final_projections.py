"""Bounded T22 API projections assembled from persisted rows and the report.

The projection lives here rather than in the route module so the route stays a
thin authorization and concurrency shell. It also keeps the layering rule the
review-UI suite enforces intact: a route may not so much as name a media tool,
and these projections carry the recorded ``ffmpeg``/``ffprobe`` versions.

Nothing here reads media or calls a provider. It reads what a completed run
already persisted: the run row, its provider attempts, its human reviews and the
immutable report asset.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.schemas.final_editorial import (
    FinalCheckProjection,
    FinalDimensionProjection,
    FinalEditorialRunDetailProjection,
    FinalEditorialRunProjection,
    FinalEvidenceProjection,
    FinalFindingProjection,
    FinalMeasurementProjection,
    FinalRemediationProjection,
)
from services.qa.final_human_review import report_payload
from vidgen.db.final_editorial_models import FinalEditorialProviderAttempt, FinalEditorialRun
from vidgen.db.final_editorial_repository import FinalEditorialRepository
from vidgen.review.versions import RowVersionService
from vidgen.storage.blob import BlobStore

#: Final QA is a property of the project's current render, so its mutations use
#: the project row version rather than a second concurrency token.
FINAL_QA_RESOURCE = "project"


def row_version(session: Session, project_id: UUID) -> int:
    return RowVersionService(session).current(project_id, FINAL_QA_RESOURCE, project_id)


def _attempts(session: Session, run: FinalEditorialRun) -> list[FinalEditorialProviderAttempt]:
    return list(
        session.scalars(
            select(FinalEditorialProviderAttempt)
            .where(FinalEditorialProviderAttempt.final_editorial_run_id == run.id)
            .order_by(FinalEditorialProviderAttempt.created_at)
        )
    )


def run_projection(
    session: Session, run: FinalEditorialRun, report: dict[str, Any]
) -> FinalEditorialRunProjection:
    attempts = _attempts(session, run)
    return FinalEditorialRunProjection(
        final_editorial_run_id=run.id,
        project_id=run.project_id,
        final_render_asset_id=run.final_render_asset_id,
        render_manifest_asset_id=run.render_manifest_asset_id,
        render_identity=run.render_identity,
        final_qa_identity=run.final_qa_identity,
        input_hash=run.input_hash,
        configuration_hash=run.configuration_hash,
        report_version=str(report.get("report_version", "")),
        status=run.status,
        phase=run.current_phase,
        decision=run.final_decision,  # type: ignore[arg-type]
        selected=bool(run.selected),
        blocking_finding_count=run.blocking_finding_count or 0,
        review_finding_count=run.review_finding_count or 0,
        warning_finding_count=run.warning_finding_count or 0,
        deterministic_failure_count=run.deterministic_failure_count or 0,
        remediation_targets=list(run.remediation_targets or []),
        provider=run.first_pass_provider or "",
        model=run.first_pass_model or "",
        adjudicated=any(attempt.phase == "ADJUDICATION" for attempt in attempts),
        cost_microusd=run.cost_microusd or 0,
        report_asset_id=run.report_asset_id,
        contact_sheet_asset_id=run.contact_sheet_asset_id,
        error_code=run.error_code,
        row_version=row_version(session, run.project_id),
        created_at=run.created_at,
        completed_at=run.completed_at,
    )


def _check_projection(payload: dict[str, Any]) -> FinalCheckProjection:
    return FinalCheckProjection(
        check_id=UUID(str(payload["check_id"])),
        check_type=str(payload.get("check_type", "")),
        code=str(payload.get("code", "")),
        status=str(payload.get("status", "")),
        blocking=bool(payload.get("blocking", False)),
        measurement=payload.get("measurement"),
        threshold=payload.get("threshold"),
        unit=str(payload.get("unit", "")),
        start_us=payload.get("start_us"),
        end_us=payload.get("end_us"),
        cue_sequence=payload.get("cue_sequence"),
        tool=str(payload.get("tool", "")),
        tool_version=str(payload.get("tool_version", "")),
        message=str(payload.get("message", "")),
    )


def _finding_projection(
    payload: dict[str, Any], resolved: frozenset[UUID]
) -> FinalFindingProjection:
    finding_id = UUID(str(payload["finding_id"]))
    return FinalFindingProjection(
        finding_id=finding_id,
        category=str(payload.get("category", "")),
        severity=str(payload.get("severity", "")),
        blocking=bool(payload.get("blocking", False)),
        confidence=float(payload.get("confidence", 0.0)),
        issue_code=str(payload.get("issue_code", "")),
        summary=str(payload.get("summary", "")),
        start_us=int(payload.get("start_us", 0)),
        end_us=int(payload.get("end_us", 0)),
        shot_ids=[UUID(str(item)) for item in payload.get("shot_ids", [])],
        caption_cue_sequences=[int(item) for item in payload.get("caption_cue_sequences", [])],
        narration_segment_ids=[
            UUID(str(item)) for item in payload.get("narration_segment_ids", [])
        ],
        evidence=[
            FinalEvidenceProjection(
                evidence_id=UUID(str(item["evidence_id"])),
                evidence_type=str(item.get("evidence_type", "")),
                start_us=int(item.get("start_us", 0)),
                end_us=int(item.get("end_us", 0)),
                frame_asset_id=_optional_uuid(item.get("frame_asset_id")),
                sample_id=_optional_uuid(item.get("sample_id")),
                contact_sheet_asset_id=_optional_uuid(item.get("contact_sheet_asset_id")),
                contact_sheet_position=item.get("contact_sheet_position"),
                caption_cue_sequence=item.get("caption_cue_sequence"),
                shot_id=_optional_uuid(item.get("shot_id")),
                measurement=item.get("measurement"),
                threshold=item.get("threshold"),
                explanation=str(item.get("explanation", "")),
            )
            for item in payload.get("evidence", [])
        ],
        expected_behavior=str(payload.get("expected_behavior", "")),
        observed_behavior=str(payload.get("observed_behavior", "")),
        remediation_target=str(payload.get("remediation_target", "NONE")),
        provenance=str(payload.get("provenance", "deterministic")),
        resolved_by_review=finding_id in resolved,
    )


def _optional_uuid(value: Any) -> UUID | None:
    return None if value in (None, "") else UUID(str(value))


def detail_projection(
    session: Session, blob: BlobStore, run: FinalEditorialRun
) -> FinalEditorialRunDetailProjection:
    report = report_payload(blob, session, run)
    resolved = FinalEditorialRepository(session).resolved_finding_ids(run.id)
    measurements = report.get("measurements")
    gate = report.get("gate", {})
    adjudication = report.get("adjudication") or {}
    inputs = report.get("inputs", {})
    base = run_projection(session, run, report)
    return FinalEditorialRunDetailProjection(
        **base.model_dump(),
        measurements=(
            FinalMeasurementProjection(
                container_format=str(measurements.get("container_format", "")),
                byte_size=int(measurements.get("byte_size", 0)),
                video_codec=str(measurements.get("video_codec", "")),
                audio_codec=str(measurements.get("audio_codec", "")),
                width=measurements.get("width"),
                height=measurements.get("height"),
                pixel_format=str(measurements.get("pixel_format", "")),
                frame_rate=str(measurements.get("frame_rate", "")),
                container_duration_us=measurements.get("container_duration_us"),
                video_duration_us=measurements.get("video_duration_us"),
                audio_duration_us=measurements.get("audio_duration_us"),
                sample_rate_hz=measurements.get("sample_rate_hz"),
                channels=measurements.get("channels"),
                integrated_lufs=measurements.get("integrated_lufs"),
                true_peak_dbtp=measurements.get("true_peak_dbtp"),
                clipping_ratio=measurements.get("clipping_ratio"),
                video_decoded=bool(measurements.get("video_decoded", False)),
                audio_decoded=bool(measurements.get("audio_decoded", False)),
                black_interval_count=len(measurements.get("black_intervals", [])),
                freeze_interval_count=len(measurements.get("freeze_intervals", [])),
                silence_interval_count=len(measurements.get("silence_intervals", [])),
                ffmpeg_version=str(measurements.get("ffmpeg_version", "")),
                ffprobe_version=str(measurements.get("ffprobe_version", "")),
            )
            if isinstance(measurements, dict)
            else None
        ),
        media_checks=[_check_projection(item) for item in report.get("deterministic_checks", [])],
        audio_checks=[_check_projection(item) for item in report.get("audio_checks", [])],
        caption_checks=[_check_projection(item) for item in report.get("caption_checks", [])],
        dimensions=[
            FinalDimensionProjection(
                category=str(item.get("category", "")),
                applicable=bool(item.get("applicable", True)),
                score=float(item.get("score", 0.0)),
                confidence=float(item.get("confidence", 0.0)),
                blocking_finding_count=int(item.get("blocking_finding_count", 0)),
                review_finding_count=int(item.get("review_finding_count", 0)),
                warning_finding_count=int(item.get("warning_finding_count", 0)),
                summary=str(item.get("summary", "")),
            )
            for item in report.get("dimensions", [])
        ],
        findings=[_finding_projection(item, resolved) for item in report.get("findings", [])],
        remediation_routes=[
            FinalRemediationProjection(
                target=str(item.get("target", "NONE")),
                finding_ids=[UUID(str(value)) for value in item.get("finding_ids", [])],
                shot_ids=[UUID(str(value)) for value in item.get("shot_ids", [])],
                caption_cue_sequences=[
                    int(value) for value in item.get("caption_cue_sequences", [])
                ],
                reason=str(item.get("reason", "")),
                requires_new_render=bool(item.get("requires_new_render", True)),
            )
            for item in report.get("remediation_routes", [])
        ],
        adjudication_confidence=adjudication.get("confidence"),
        adjudication_decided=bool(adjudication.get("decided", False)),
        gate_reasons=[str(reason) for reason in gate.get("reasons", [])],
        timeline_duration_us=int(inputs.get("timeline_duration_us", 0)),
    )
