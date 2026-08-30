"""Whether a project may publish, decided before any OAuth or YouTube request.

Publication is the last irreversible step in the pipeline: once a video exists
on someone's channel it cannot be un-created, only made private. So every
precondition is proven from persisted rows *first*, and a refusal is an
actionable prerequisite error rather than a fabricated render or a bypassed
gate.

The rules, in the order a user hits them:

#. the project has a selected, complete T17 render with a verification report;
#. that render's final MP4 asset exists, belongs to the project and is readable;
#. a T18 approval exists for that exact render job and has not been revoked;
#. the project's selected T22 run is for that exact render and decided ``PASS``,
   and a ``PASS`` completion gate is recorded for it;
#. nothing was invalidated after the render was produced - a later script edit,
   shot regeneration or reference rebuild makes the render stale;
#. no T21 repair is parked in ``HUMAN_REVIEW_REQUIRED``;
#. the canonical caption track belongs to that render's lineage;
#. the selected thumbnail, when one is chosen, is a project-owned image;
#. the requesting owner controls the selected YouTube connection.

A refusal never names another owner's resource: a cross-owner or cross-project
ID produces the same "not found" shape as a missing one.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.publisher import youtube as capabilities
from vidgen.contracts.publication import (
    ConnectionStatus,
    PublicationFailure,
    PublicationFailureCode,
    PublicationGate,
    PublicationWarning,
)
from vidgen.db.final_editorial_models import FinalCompletionGate, FinalEditorialRun
from vidgen.db.models import Asset, Project, RenderJob
from vidgen.db.publication_models import YouTubeConnection
from vidgen.db.render_models import CaptionTrackRecord
from vidgen.db.repair_models import RepairRun
from vidgen.db.review_models import DownstreamInvalidation, RenderApproval
from vidgen.storage.blob import BlobStore

#: The T17 terminal status a render must hold before it may be published.
COMPLETED_RENDER_STATUS = "render_complete"
#: The T22 decision that opens the gate.
PASS_DECISION = "PASS"
#: T21 states that mean a human still owes the project an answer.
BLOCKING_REPAIR_STATES = frozenset({"HUMAN_REVIEW_REQUIRED"})


@dataclass(frozen=True, slots=True)
class EligibleRender:
    """Everything a publication needs about the render it is about to upload."""

    project: Project
    render_job: RenderJob
    final_asset: Asset
    caption_asset: Asset
    final_editorial_run: FinalEditorialRun
    completion_gate: FinalCompletionGate
    approval: RenderApproval
    connection: YouTubeConnection
    thumbnail_asset: Asset | None
    render_identity: str


def _failure(
    code: PublicationFailureCode,
    summary: str,
    *,
    remediation: str = "",
    reference_id: UUID | None = None,
) -> PublicationFailure:
    return PublicationFailure(
        code=code,
        summary=summary,
        retryable=False,
        reference_id=reference_id,
        remediation=remediation,
    )


class PublicationEligibilityError(RuntimeError):
    """A refused publication. Carries the structured gate that explains it."""

    def __init__(self, gate: PublicationGate) -> None:
        first = gate.failures[0] if gate.failures else None
        super().__init__(first.summary if first else "this project may not be published")
        self.gate = gate
        self.code = first.code if first else PublicationFailureCode.NO_ELIGIBLE_RENDER


class PublicationEligibilityService:
    """Evaluates the publication gate for one project and one connection."""

    def __init__(self, session: Session, blob_store: BlobStore) -> None:
        self.session = session
        self.blob_store = blob_store

    def evaluate(
        self,
        *,
        project_id: UUID,
        owner_subject: str,
        connection_id: UUID | None,
        thumbnail_asset_id: UUID | None = None,
        now: datetime | None = None,
    ) -> tuple[PublicationGate, EligibleRender | None]:
        """Return the gate, and the resolved inputs when it allows publication."""
        moment = now or datetime.now(UTC)
        failures: list[PublicationFailure] = []
        warnings: list[PublicationWarning] = []

        project = self.session.scalar(
            select(Project).where(Project.id == project_id, Project.owner_subject == owner_subject)
        )
        if project is None:
            # Indistinguishable from a foreign project, deliberately.
            failures.append(
                _failure(
                    PublicationFailureCode.CROSS_PROJECT_REFERENCE,
                    "The requested project was not found.",
                )
            )
            return self._refused(project_id, failures, warnings, moment), None

        job = self.session.scalar(
            select(RenderJob)
            .where(RenderJob.project_id == project.id, RenderJob.selected.is_(True))
            .order_by(RenderJob.created_at.desc())
        )
        if job is None:
            failures.append(
                _failure(
                    PublicationFailureCode.RENDER_NOT_SELECTED,
                    "This project has no selected final render.",
                    remediation="Render the project and select the result before publishing.",
                )
            )
            return self._refused(project.id, failures, warnings, moment), None
        if job.status != COMPLETED_RENDER_STATUS or job.final_video_asset_id is None:
            failures.append(
                _failure(
                    PublicationFailureCode.NO_ELIGIBLE_RENDER,
                    "The selected render has not completed.",
                    remediation="Wait for the render to finish, then run final QA.",
                    reference_id=job.id,
                )
            )
            return self._refused(project.id, failures, warnings, moment), None
        if job.verification_report_asset_id is None:
            failures.append(
                _failure(
                    PublicationFailureCode.NO_ELIGIBLE_RENDER,
                    "The selected render carries no T17 verification report.",
                    remediation="Re-run the render so its output is verified.",
                    reference_id=job.id,
                )
            )
            return self._refused(project.id, failures, warnings, moment), None

        render_identity = job.render_identity or ""
        final_asset = self.session.get(Asset, job.final_video_asset_id)
        if final_asset is None or final_asset.project_id != project.id:
            failures.append(
                _failure(
                    PublicationFailureCode.MISSING_FINAL_ASSET,
                    "The final render asset is missing or belongs to another project.",
                    reference_id=job.final_video_asset_id,
                )
            )
            return self._refused(project.id, failures, warnings, moment), None
        if final_asset.byte_size <= 0 or not self.blob_store.exists(final_asset.storage_key):
            failures.append(
                _failure(
                    PublicationFailureCode.MISSING_FINAL_ASSET,
                    "The final MP4 is not readable from storage.",
                    remediation="Re-run the render; its output is no longer present.",
                    reference_id=final_asset.id,
                )
            )
            return self._refused(project.id, failures, warnings, moment), None
        if final_asset.media_type not in capabilities.ACCEPTED_VIDEO_MEDIA_TYPES:
            failures.append(
                _failure(
                    PublicationFailureCode.MISSING_FINAL_ASSET,
                    f"YouTube does not accept {final_asset.media_type} as a video upload.",
                    reference_id=final_asset.id,
                )
            )
        if final_asset.byte_size > capabilities.MAX_VIDEO_BYTES:
            failures.append(
                _failure(
                    PublicationFailureCode.MISSING_FINAL_ASSET,
                    "The final render exceeds YouTube's maximum upload size.",
                    reference_id=final_asset.id,
                )
            )

        approval = self.session.scalar(
            select(RenderApproval)
            .where(
                RenderApproval.project_id == project.id,
                RenderApproval.render_job_id == job.id,
                RenderApproval.revoked_at.is_(None),
            )
            .order_by(RenderApproval.approved_at.desc())
        )
        if approval is None:
            failures.append(
                _failure(
                    PublicationFailureCode.RENDER_NOT_APPROVED,
                    "This render has not been approved in the review UI.",
                    remediation="Approve the render on the final review page, then publish.",
                    reference_id=job.id,
                )
            )

        final_run = self.session.scalar(
            select(FinalEditorialRun)
            .where(
                FinalEditorialRun.project_id == project.id,
                FinalEditorialRun.final_render_asset_id == final_asset.id,
                FinalEditorialRun.selected.is_(True),
            )
            .order_by(FinalEditorialRun.created_at.desc())
        )
        gate_row: FinalCompletionGate | None = None
        if final_run is None:
            failures.append(
                _failure(
                    PublicationFailureCode.COMPLETION_GATE_NOT_PASSED,
                    "Final editorial QA has not run for the selected render.",
                    remediation="Run final QA, then publish.",
                    reference_id=final_asset.id,
                )
            )
        elif final_run.final_decision != PASS_DECISION:
            failures.append(
                _failure(
                    PublicationFailureCode.COMPLETION_GATE_NOT_PASSED,
                    f"Final editorial QA decided {final_run.final_decision or 'nothing'} "
                    "for this render.",
                    remediation="Resolve the findings and re-run final QA until it passes.",
                    reference_id=final_run.id,
                )
            )
        else:
            gate_row = self.session.scalar(
                select(FinalCompletionGate)
                .where(
                    FinalCompletionGate.final_editorial_run_id == final_run.id,
                    FinalCompletionGate.final_render_asset_id == final_asset.id,
                    FinalCompletionGate.decision == PASS_DECISION,
                )
                .order_by(FinalCompletionGate.created_at.desc())
            )
            if gate_row is None:
                failures.append(
                    _failure(
                        PublicationFailureCode.COMPLETION_GATE_NOT_PASSED,
                        "No passing completion gate is recorded for this render.",
                        remediation="Re-run final QA so the completion gate is recorded.",
                        reference_id=final_run.id,
                    )
                )
            elif render_identity and gate_row.render_identity != render_identity:
                failures.append(
                    _failure(
                        PublicationFailureCode.STALE_RENDER,
                        "The passing completion gate belongs to a different render.",
                        reference_id=gate_row.id,
                    )
                )

        # Anything invalidated after the render finished makes it stale: the
        # answer is a new render, never publishing the old one anyway.
        rendered_at = job.completed_at or job.created_at
        if rendered_at is not None:
            if rendered_at.tzinfo is None:
                rendered_at = rendered_at.replace(tzinfo=UTC)
            stale = self.session.scalar(
                select(DownstreamInvalidation)
                .where(
                    DownstreamInvalidation.project_id == project.id,
                    DownstreamInvalidation.created_at > rendered_at,
                )
                .order_by(DownstreamInvalidation.created_at.desc())
            )
            if stale is not None:
                failures.append(
                    _failure(
                        PublicationFailureCode.STALE_RENDER,
                        f"The project changed after this render ({stale.invalidated_type} "
                        f"was invalidated: {stale.reason}).",
                        remediation="Re-render the project, re-approve it and re-run final QA.",
                        reference_id=stale.id,
                    )
                )

        unresolved = self.session.scalar(
            select(RepairRun).where(
                RepairRun.project_id == project.id,
                RepairRun.state.in_(tuple(BLOCKING_REPAIR_STATES)),
            )
        )
        if unresolved is not None:
            failures.append(
                _failure(
                    PublicationFailureCode.UNRESOLVED_HUMAN_REVIEW,
                    "A shot repair is still waiting for a human decision.",
                    remediation="Resolve the outstanding repair review, then publish.",
                    reference_id=unresolved.id,
                )
            )

        caption_asset = self._caption_asset(job, project.id, failures)
        thumbnail_asset = self._thumbnail_asset(thumbnail_asset_id, project.id, failures, warnings)
        connection = self._connection(connection_id, owner_subject, failures)

        if failures or connection is None or final_run is None or gate_row is None:
            if caption_asset is None and not any(
                failure.code is PublicationFailureCode.MISSING_CAPTION_ASSET for failure in failures
            ):
                failures.append(
                    _failure(
                        PublicationFailureCode.MISSING_CAPTION_ASSET,
                        "The render has no canonical caption track.",
                    )
                )
            return self._refused(project.id, failures, warnings, moment), None

        assert caption_asset is not None and approval is not None
        gate = PublicationGate(
            project_id=project.id,
            allowed=True,
            final_render_asset_id=final_asset.id,
            render_job_id=job.id,
            render_identity=render_identity or None,
            final_editorial_run_id=final_run.id,
            completion_gate_id=gate_row.id,
            approval_id=approval.id,
            caption_asset_id=caption_asset.id,
            thumbnail_asset_id=thumbnail_asset.id if thumbnail_asset else None,
            gate_version=gate_row.gate_version,
            warnings=warnings,
            evaluated_at=moment,
        )
        return gate, EligibleRender(
            project=project,
            render_job=job,
            final_asset=final_asset,
            caption_asset=caption_asset,
            final_editorial_run=final_run,
            completion_gate=gate_row,
            approval=approval,
            connection=connection,
            thumbnail_asset=thumbnail_asset,
            render_identity=render_identity,
        )

    # -- helpers -------------------------------------------------------------
    def _caption_asset(
        self, job: RenderJob, project_id: UUID, failures: list[PublicationFailure]
    ) -> Asset | None:
        """The canonical SRT track produced with this render, or a refusal.

        The track is looked up through the render job, not by kind: that is what
        proves it belongs to this render's lineage rather than an older one's.
        """
        track = self.session.scalar(
            select(CaptionTrackRecord).where(CaptionTrackRecord.render_job_id == job.id)
        )
        asset_id = track.srt_asset_id if track is not None else job.srt_asset_id
        if asset_id is None:
            failures.append(
                _failure(
                    PublicationFailureCode.MISSING_CAPTION_ASSET,
                    "The render has no canonical caption track.",
                    remediation="Re-run the render so captions are produced.",
                    reference_id=job.id,
                )
            )
            return None
        asset = self.session.get(Asset, asset_id)
        if asset is None or asset.project_id != project_id:
            failures.append(
                _failure(
                    PublicationFailureCode.MISSING_CAPTION_ASSET,
                    "The caption asset is missing or belongs to another project.",
                    reference_id=asset_id,
                )
            )
            return None
        if asset.byte_size <= 0 or asset.byte_size > capabilities.MAX_CAPTION_BYTES:
            failures.append(
                _failure(
                    PublicationFailureCode.MISSING_CAPTION_ASSET,
                    "The caption asset is empty or exceeds YouTube's size limit.",
                    reference_id=asset.id,
                )
            )
            return None
        return asset

    def _thumbnail_asset(
        self,
        thumbnail_asset_id: UUID | None,
        project_id: UUID,
        failures: list[PublicationFailure],
        warnings: list[PublicationWarning],
    ) -> Asset | None:
        if thumbnail_asset_id is None:
            warnings.append(
                PublicationWarning(
                    code="thumbnail_not_selected",
                    summary="No custom thumbnail is selected; YouTube will generate one.",
                )
            )
            return None
        asset = self.session.get(Asset, thumbnail_asset_id)
        if asset is None or asset.project_id != project_id:
            failures.append(
                _failure(
                    PublicationFailureCode.INVALID_THUMBNAIL_ASSET,
                    "The selected thumbnail was not found in this project.",
                    reference_id=thumbnail_asset_id,
                )
            )
            return None
        if asset.media_type not in capabilities.ACCEPTED_THUMBNAIL_MEDIA_TYPES:
            failures.append(
                _failure(
                    PublicationFailureCode.INVALID_THUMBNAIL_ASSET,
                    f"YouTube accepts JPEG and PNG thumbnails, not {asset.media_type}.",
                    reference_id=asset.id,
                )
            )
            return None
        if asset.byte_size <= 0 or asset.byte_size > capabilities.MAX_THUMBNAIL_BYTES:
            failures.append(
                _failure(
                    PublicationFailureCode.INVALID_THUMBNAIL_ASSET,
                    "The selected thumbnail is empty or exceeds YouTube's 2 MiB limit.",
                    reference_id=asset.id,
                )
            )
            return None
        return asset

    def _connection(
        self,
        connection_id: UUID | None,
        owner_subject: str,
        failures: list[PublicationFailure],
    ) -> YouTubeConnection | None:
        if connection_id is None:
            failures.append(
                _failure(
                    PublicationFailureCode.AUTHENTICATION_REQUIRED,
                    "No YouTube channel is selected for this publication.",
                    remediation="Connect a YouTube channel, then publish.",
                )
            )
            return None
        connection = self.session.scalar(
            select(YouTubeConnection).where(
                YouTubeConnection.id == connection_id,
                YouTubeConnection.owner_subject == owner_subject,
            )
        )
        if connection is None:
            failures.append(
                _failure(
                    PublicationFailureCode.CONNECTION_NOT_OWNED,
                    "The requested YouTube connection was not found.",
                    reference_id=connection_id,
                )
            )
            return None
        if connection.status != ConnectionStatus.CONNECTED.value:
            failures.append(
                _failure(
                    PublicationFailureCode.AUTHENTICATION_REQUIRED,
                    f"This YouTube connection is {connection.status.replace('_', ' ')}.",
                    remediation="Reconnect the channel, then publish.",
                    reference_id=connection.id,
                )
            )
            return None
        missing = [
            scope
            for scope in capabilities.REQUIRED_SCOPES
            if scope not in (connection.granted_scopes or [])
        ]
        if missing:
            failures.append(
                _failure(
                    PublicationFailureCode.INSUFFICIENT_SCOPE,
                    "This connection lacks a permission publishing requires: "
                    + ", ".join(scope.rsplit("/", 1)[-1] for scope in missing),
                    remediation="Reconnect the channel and approve every requested permission.",
                    reference_id=connection.id,
                )
            )
            return None
        return connection

    @staticmethod
    def _refused(
        project_id: UUID,
        failures: list[PublicationFailure],
        warnings: list[PublicationWarning],
        moment: datetime,
    ) -> PublicationGate:
        if not failures:
            failures = [
                _failure(
                    PublicationFailureCode.NO_ELIGIBLE_RENDER,
                    "This project has no publishable render.",
                )
            ]
        return PublicationGate(
            project_id=project_id,
            allowed=False,
            failures=failures[:32],
            warnings=warnings[:32],
            evaluated_at=moment,
        )
