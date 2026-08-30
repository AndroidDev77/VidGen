"""A deterministic, publishable project for the T25 tests.

Builds on the T18 project graph and adds exactly what publication requires: a
real MP4-shaped final asset of a known byte length, a parseable SRT caption
track, a decodable PNG thumbnail, a T18 approval for the selected render, and a
selected T22 run with a ``PASS`` completion gate.

Nothing here calls a provider, touches the network or needs FFmpeg: the "video"
is deterministic bytes, because T25 uploads bytes and never decodes them.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from uuid import UUID

from PIL import Image
from sqlalchemy.orm import Session

from services.publisher.credentials import Keyring, SecretValue, development_keyring
from services.publisher.fake_youtube import (
    FakeYouTubeProvider,
    FakeYouTubeState,
    reset_shared_state,
)
from services.publisher.oauth import OAuthSettings, YouTubeOAuthService
from tests.review_fixtures import ProjectGraph, build_project_graph, digest
from vidgen.db.final_editorial_models import FinalCompletionGate, FinalEditorialRun
from vidgen.db.models import Asset, Project, RenderJob
from vidgen.db.publication_models import YouTubeConnection
from vidgen.db.publication_repository import PublicationRepository
from vidgen.db.review_models import RenderApproval
from vidgen.storage.blob import FilesystemBlobStore

#: Big enough to need several 256 KiB chunks, small enough to stay fast.
FINAL_VIDEO_BYTES = 5 * 256 * 1024 + 4096
GATE_VERSION = "final-gate/1.0"

OAUTH_SETTINGS = OAuthSettings(
    client_id="test-client-id.apps.googleusercontent.com",
    redirect_uri="http://localhost:8000/api/v1/youtube/oauth:callback",
    allowed_redirect_targets=("/", "/projects"),
)


@dataclass(slots=True)
class PublishableProject:
    """Every identifier the T25 tests assert against."""

    graph: ProjectGraph
    project_id: UUID
    owner_subject: str
    render_job_id: UUID
    final_asset_id: UUID
    caption_asset_id: UUID
    thumbnail_asset_id: UUID
    approval_id: UUID
    final_editorial_run_id: UUID
    completion_gate_id: UUID
    total_bytes: int


def synthetic_mp4(byte_size: int = FINAL_VIDEO_BYTES) -> bytes:
    """Deterministic bytes with an MP4-shaped header.

    T25 never decodes the video - it uploads it - so a real encode would only
    make the tests slower and dependent on FFmpeg.
    """
    header = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom"
    body = bytearray()
    while len(header) + len(body) < byte_size:
        body.extend(hashlib.sha256(bytes([len(body) % 251])).digest())
    return bytes(header + bytes(body))[:byte_size]


def synthetic_srt(cues: int = 3) -> bytes:
    """A small, genuinely parseable SRT track."""
    lines: list[str] = []
    for index in range(1, cues + 1):
        start = (index - 1) * 2
        lines.append(str(index))
        lines.append(f"00:00:{start:02d},000 --> 00:00:{start + 2:02d},000")
        lines.append(f"Recap line {index}.")
        lines.append("")
    return "\n".join(lines).encode("utf-8")


def synthetic_thumbnail(width: int = 1280, height: int = 720) -> bytes:
    """A decodable 16:9 JPEG at YouTube's recommended dimensions."""
    buffer = BytesIO()
    Image.new("RGB", (width, height), (32, 64, 128)).save(buffer, format="JPEG", quality=70)
    return buffer.getvalue()


def _store_asset(
    session: Session,
    store: FilesystemBlobStore,
    *,
    project_id: UUID,
    kind: str,
    media_type: str,
    content: bytes,
    name: str,
) -> Asset:
    digest_value = hashlib.sha256(content).hexdigest()
    key = f"blobs/{digest_value[:2]}/{digest_value}"
    store.put_if_absent(key, content)
    asset = Asset(
        project_id=project_id,
        kind=kind,
        sha256=digest_value,
        byte_size=len(content),
        media_type=media_type,
        storage_key=key,
        extra_metadata={"fixture": name},
    )
    session.add(asset)
    session.flush()
    return asset


def build_publishable_project(
    session: Session,
    store: FilesystemBlobStore,
    *,
    owner_subject: str = "local-user",
    name: str = "Season 3 Episode 4",
    video_bytes: int = FINAL_VIDEO_BYTES,
    with_thumbnail: bool = True,
    approved: bool = True,
    gate_decision: str = "PASS",
    with_gate: bool = True,
) -> PublishableProject:
    """One project that may be published, or that may deliberately not be."""
    graph = build_project_graph(
        session, owner_subject=owner_subject, name=name, blob_root=Path(store.root)
    )
    assert graph.render_job_id is not None
    job = session.get(RenderJob, graph.render_job_id)
    assert job is not None
    project = session.get(Project, graph.project_id)
    assert project is not None

    final = _store_asset(
        session,
        store,
        project_id=project.id,
        kind="render",
        media_type="video/mp4",
        content=synthetic_mp4(video_bytes),
        name="final.mp4",
    )
    caption = _store_asset(
        session,
        store,
        project_id=project.id,
        kind="subtitle",
        media_type="application/x-subrip",
        content=synthetic_srt(),
        name="captions.srt",
    )
    thumbnail = (
        _store_asset(
            session,
            store,
            project_id=project.id,
            kind="thumbnail",
            media_type="image/jpeg",
            content=synthetic_thumbnail(),
            name="thumbnail.jpg",
        )
        if with_thumbnail
        else None
    )
    job.final_video_asset_id = final.id
    job.output_asset_id = final.id
    job.srt_asset_id = caption.id
    session.flush()
    from vidgen.db.render_models import CaptionTrackRecord

    track = session.query(CaptionTrackRecord).filter_by(render_job_id=job.id).one()
    track.srt_asset_id = caption.id
    session.flush()

    approval_id: UUID | None = None
    if approved:
        approval = RenderApproval(
            project_id=project.id,
            render_job_id=job.id,
            approved_by=owner_subject,
            lineage_hash=digest(f"{project.id}:approval-lineage"),
            approved_at=datetime.now(UTC),
        )
        session.add(approval)
        session.flush()
        approval_id = approval.id

    run = FinalEditorialRun(
        project_id=project.id,
        render_job_id=job.id,
        final_render_asset_id=final.id,
        render_manifest_asset_id=job.manifest_asset_id,
        render_identity=job.render_identity or digest("render"),
        final_qa_identity=digest(f"{project.id}:final-qa"),
        input_hash=digest("final-qa-input"),
        configuration_hash=digest("final-qa-config"),
        idempotency_key=f"final-qa:{project.id}",
        status="FINAL_QA_PASSED" if gate_decision == "PASS" else "FINAL_QA_FAILED",
        current_phase="COMPLETION_GATE",
        completed_phases=[],
        selected=True,
        final_decision=gate_decision,
        report_asset_id=job.verification_report_asset_id,
        pipeline_version="t22/1",
        gate_version=GATE_VERSION,
        blocking_finding_count=0 if gate_decision == "PASS" else 1,
        review_finding_count=0,
        warning_finding_count=0,
        deterministic_failure_count=0,
        remediation_targets=[],
        cost_microusd=0,
        completed_at=datetime.now(UTC),
    )
    session.add(run)
    session.flush()

    gate_id: UUID | None = None
    if with_gate:
        gate = FinalCompletionGate(
            project_id=project.id,
            final_editorial_run_id=run.id,
            final_render_asset_id=final.id,
            render_identity=run.render_identity,
            decision=gate_decision,
            blocking_finding_count=0 if gate_decision == "PASS" else 1,
            review_finding_count=0,
            deterministic_failure_count=0,
            gate_version=GATE_VERSION,
            reasons=["final_qa_pass"] if gate_decision == "PASS" else ["final_qa_failed"],
            created_at=datetime.now(UTC),
        )
        session.add(gate)
        session.flush()
        gate_id = gate.id

    session.commit()
    return PublishableProject(
        graph=graph,
        project_id=project.id,
        owner_subject=owner_subject,
        render_job_id=job.id,
        final_asset_id=final.id,
        caption_asset_id=caption.id,
        thumbnail_asset_id=thumbnail.id if thumbnail else final.id,
        approval_id=approval_id or UUID(int=0),
        final_editorial_run_id=run.id,
        completion_gate_id=gate_id or UUID(int=0),
        total_bytes=len(synthetic_mp4(video_bytes)),
    )


def connect_fake_channel(
    session: Session,
    *,
    owner_subject: str = "local-user",
    keyring: Keyring | None = None,
    state: FakeYouTubeState | None = None,
) -> tuple[YouTubeConnection, FakeYouTubeState, Keyring]:
    """Seal a fake connection the way the real OAuth callback would.

    Deliberately goes through :class:`YouTubeOAuthService` rather than inserting
    rows: that is what makes the tests exercise state consumption, PKCE, channel
    verification and the credential envelope on every setup.
    """
    resolved_keyring = keyring or development_keyring()
    # A fresh process-wide world by default, so an API test and the pipeline it
    # drives out of band agree about which fake videos exist.
    fake_state = state or reset_shared_state()
    provider = FakeYouTubeProvider(fake_state)
    repository = PublicationRepository(session, resolved_keyring)
    service = YouTubeOAuthService(repository, provider, OAUTH_SETTINGS)
    import asyncio

    authorization, raw_state = service.start(owner_subject=owner_subject)
    assert authorization.state_id is not None
    connection, _ = (
        asyncio.get_event_loop().run_until_complete(
            service.complete(state=raw_state, code="fake-code", owner_subject=owner_subject)
        )
        if False
        else asyncio.run(
            service.complete(state=raw_state, code="fake-code", owner_subject=owner_subject)
        )
    )
    session.commit()
    return connection, fake_state, resolved_keyring


def expired_state_moment() -> datetime:
    """An instant guaranteed to be past any freshly minted OAuth state."""
    return datetime.now(UTC) + timedelta(hours=1)


def secret(value: str) -> SecretValue:
    return SecretValue(value)
