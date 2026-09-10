"""Narration retries across runs: one attempt sequence per generation identity.

``workflow:continue`` creates a new ``NarrationRun`` for the same project. The
segment identities are content hashes, so the new run's rows carry the same
identities as the failed run's rows, and the provider idempotency key
``{identity}:{attempt}`` must continue the earlier sequence rather than collide
with it on ``uq_narration_attempts_provider_idempotency_key``.
"""

from __future__ import annotations

import asyncio
import wave
from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from services.narration.fake_provider import FakeNarrationProvider
from services.narration.pipeline import NarrationPipeline
from services.narration.quality import QualityThresholds
from tests.storyboard_fixtures import StoryboardFixture, build_fixture
from vidgen.contracts.narration import (
    NarrationPreviewManifest,
    NarrationProviderRequest,
    NarrationProviderResult,
)
from vidgen.db.models import Asset
from vidgen.db.narration_models import NarrationAttemptRecord, NarrationRun, NarrationSegment
from vidgen.db.narration_repository import NarrationRepository
from vidgen.db.script_models import ScriptSegment

ONE_SEGMENT = ("Our hero wakes up late, again, and the toaster is already on fire.",)


class ScriptedFakeProvider(FakeNarrationProvider):
    """The fake provider, told what to do on each call.

    ``"ok"`` produces the deterministic fake take, ``"silence"`` the same take
    behind a second of leading silence (a real quality failure), ``"raise"``
    a provider outage after the pipeline's durable pre-provider checkpoint.
    """

    def __init__(self, steps: list[str]) -> None:
        self.steps = steps
        self.calls = 0

    async def generate(
        self, request: NarrationProviderRequest, destination: Path
    ) -> NarrationProviderResult:
        step = self.steps[self.calls]
        self.calls += 1
        if step == "raise":
            raise RuntimeError("provider outage")
        result = await super().generate(request, destination)
        if step == "silence":
            _prepend_silence(destination, seconds=1.0)
            result = result.model_copy(update={"byte_size": destination.stat().st_size})  # noqa: ASYNC240
        return result


def _prepend_silence(path: Path, *, seconds: float) -> None:
    with wave.open(str(path), "rb") as source:
        params = source.getparams()
        frames = source.readframes(source.getnframes())
    silence = b"\x00" * int(seconds * params.framerate) * params.sampwidth * params.nchannels
    with wave.open(str(path), "wb") as target:
        target.setparams(params)
        target.writeframes(silence + frames)


def _run(
    fixture: StoryboardFixture,
    provider: ScriptedFakeProvider,
    key: str,
    *,
    thresholds: QualityThresholds | None = None,
    cancellation_check: Callable[[], bool] | None = None,
) -> None:
    pipeline = NarrationPipeline(
        fixture.session,
        fixture.blobs,
        provider,
        cancellation_check=cancellation_check,
        **({"thresholds": thresholds} if thresholds is not None else {}),
    )
    asyncio.run(
        pipeline.process(
            project_id=fixture.project.id,
            voice_profile_id=fixture.narration_run.voice_profile_id,
            idempotency_key=key,
        )
    )


def _run_row(fixture: StoryboardFixture, key: str) -> tuple[NarrationRun, NarrationSegment]:
    run = NarrationRepository(fixture.session).run_by_key(fixture.project.id, key)
    assert run is not None
    row = fixture.session.scalars(
        select(NarrationSegment).where(NarrationSegment.narration_run_id == run.id)
    ).one()
    return run, row


def _attempts(fixture: StoryboardFixture, identity: str) -> list[NarrationAttemptRecord]:
    fixture.session.expire_all()
    return NarrationRepository(fixture.session).attempts_for_identity(identity)


def _fixture(tmp_path: Path) -> StoryboardFixture:
    return build_fixture(tmp_path, texts=ONE_SEGMENT, database_name="retries.db")


def test_a_retried_run_resumes_the_attempt_the_first_run_interrupted(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    with pytest.raises(RuntimeError, match="provider outage"):
        _run(fixture, ScriptedFakeProvider(["raise"]), "run-1")
    first_run, first_row = _run_row(fixture, "run-1")
    assert (first_run.status, first_run.error_code) == ("narration_failed", "RuntimeError")
    assert first_row.status == "pending"
    interrupted = _attempts(fixture, first_row.generation_identity)
    assert [(a.attempt_number, a.completed_at) for a in interrupted] == [(1, None)]

    _run(fixture, ScriptedFakeProvider(["ok"]), "run-2")

    second_run, second_row = _run_row(fixture, "run-2")
    assert second_run.status == "narration_complete"
    assert second_row.generation_identity == first_row.generation_identity
    attempts = _attempts(fixture, second_row.generation_identity)
    # The same attempt row, now completed; no second key ``{identity}:1`` exists.
    assert [a.id for a in attempts] == [interrupted[0].id]
    assert attempts[0].completed_at is not None
    assert attempts[0].provider_idempotency_key == f"{second_row.generation_identity}:1"
    assert attempts[0].narration_segment_id == first_row.id
    assert second_row.status == "complete"
    assert second_row.selected_attempt_id == attempts[0].id
    assert second_row.normalized_asset_id is not None


def test_a_retried_run_continues_after_a_completed_quality_failure(tmp_path: Path) -> None:
    """Attempt 1 failed quality in run 1; run 2 makes attempt 2, not a colliding attempt 1."""
    fixture = _fixture(tmp_path)
    first_provider = ScriptedFakeProvider(["silence"])
    with pytest.raises(RuntimeError, match="cancelled"):
        # The activity is cancelled between the quality verdict and the next attempt.
        _run(fixture, first_provider, "run-1", cancellation_check=lambda: first_provider.calls >= 1)
    _first_run, first_row = _run_row(fixture, "run-1")
    failed = _attempts(fixture, first_row.generation_identity)
    assert [(a.attempt_number, a.failure_classification) for a in failed] == [(1, "QUALITY")]
    assert failed[0].completed_at is not None
    assert failed[0].quality_result is not None
    assert "leading_silence" in {d["code"] for d in failed[0].quality_result["diagnostics"]}

    _run(fixture, ScriptedFakeProvider(["ok"]), "run-2")

    second_run, second_row = _run_row(fixture, "run-2")
    assert second_run.status == "narration_complete"
    attempts = _attempts(fixture, second_row.generation_identity)
    assert [a.attempt_number for a in attempts] == [1, 2]
    assert [a.provider_idempotency_key for a in attempts] == [
        f"{second_row.generation_identity}:1",
        f"{second_row.generation_identity}:2",
    ]
    assert attempts[1].narration_segment_id == second_row.id
    assert attempts[1].failure_classification is None
    # The retry guidance carries over from the earlier run's verdict.
    assert "begin and end promptly" in attempts[1].instructions
    assert second_row.selected_attempt_id == attempts[1].id


def test_a_retried_run_reports_exhaustion_instead_of_reusing_a_key(tmp_path: Path) -> None:
    """Three quality failures under identical inputs stay exhausted in the next run."""
    fixture = _fixture(tmp_path)
    impossible = QualityThresholds(min_wpm=1000, max_wpm=2000)
    with pytest.raises(RuntimeError, match="exhausted three attempts"):
        _run(fixture, ScriptedFakeProvider(["ok", "ok", "ok"]), "run-1", thresholds=impossible)
    _first_run, first_row = _run_row(fixture, "run-1")
    assert [a.attempt_number for a in _attempts(fixture, first_row.generation_identity)] == [
        1,
        2,
        3,
    ]

    provider = ScriptedFakeProvider(["ok"])
    with pytest.raises(RuntimeError, match="exhausted three attempts"):
        _run(fixture, provider, "run-2", thresholds=impossible)

    second_run, second_row = _run_row(fixture, "run-2")
    assert (second_run.status, second_run.error_code) == ("narration_failed", "RuntimeError")
    assert second_row.generation_identity == first_row.generation_identity
    # Nothing was spent: no fourth attempt row, no provider call, no key reuse.
    assert provider.calls == 0
    attempts = _attempts(fixture, second_row.generation_identity)
    assert [a.attempt_number for a in attempts] == [1, 2, 3]
    assert {a.narration_segment_id for a in attempts} == {first_row.id}
    assert fixture.project.status == "narration_failed"


def test_changed_quality_settings_start_a_fresh_attempt_sequence(tmp_path: Path) -> None:
    """A different gate is a different identity, so the exhausted history does not apply."""
    fixture = _fixture(tmp_path)
    impossible = QualityThresholds(min_wpm=1000, max_wpm=2000)
    with pytest.raises(RuntimeError, match="exhausted three attempts"):
        _run(fixture, ScriptedFakeProvider(["ok", "ok", "ok"]), "run-1", thresholds=impossible)

    _run(fixture, ScriptedFakeProvider(["ok"]), "run-2")

    second_run, second_row = _run_row(fixture, "run-2")
    _first_run, first_row = _run_row(fixture, "run-1")
    assert second_run.status == "narration_complete"
    assert second_row.generation_identity != first_row.generation_identity
    assert [a.attempt_number for a in _attempts(fixture, second_row.generation_identity)] == [1]


def test_attempts_for_identity_spans_runs_and_ignores_other_identities(tmp_path: Path) -> None:
    fixture = _fixture(tmp_path)
    repo = NarrationRepository(fixture.session)
    assert repo.attempts_for_identity("0" * 64) == []
    first_provider = ScriptedFakeProvider(["silence"])
    with pytest.raises(RuntimeError, match="cancelled"):
        _run(fixture, first_provider, "run-1", cancellation_check=lambda: first_provider.calls >= 1)
    _run(fixture, ScriptedFakeProvider(["ok"]), "run-2")
    _first_run, first_row = _run_row(fixture, "run-1")
    _second_run, second_row = _run_row(fixture, "run-2")
    across = repo.attempts_for_identity(first_row.generation_identity)
    assert [(a.attempt_number, a.narration_segment_id) for a in across] == [
        (1, first_row.id),
        (2, second_row.id),
    ]
    # The per-row query still answers for one row alone.
    assert [a.attempt_number for a in repo.attempts(first_row.id)] == [1]
    assert [a.attempt_number for a in repo.attempts(second_row.id)] == [2]
    unrelated: list[UUID] = [
        a.id for a in repo.attempts_for_identity(fixture.narration_segments[0].generation_identity)
    ]
    assert unrelated == []


def test_pause_segments_are_skipped_by_the_generation_loop(tmp_path: Path) -> None:
    """Defensive: a pause that reached narration is neither voiced nor previewed."""
    fixture = _fixture(tmp_path)
    fixture.session.add(
        ScriptSegment(
            script_id=fixture.script.id,
            sequence=len(fixture.script_segments),
            stable_segment_id=uuid4(),
            segment_type="PAUSE",
            speaker_kind="narrator",
            text="",
            content_hash="b" * 64,
            plot_beat_ids=[],
            source_scene_ids=[],
            estimated_duration_ms=750,
        )
    )
    fixture.session.commit()
    provider = ScriptedFakeProvider(["ok"])
    _run(fixture, provider, "run-1")
    run, row = _run_row(fixture, "run-1")
    assert run.status == "narration_complete"
    assert provider.calls == 1
    assert row.script_segment_id == fixture.script_segments[0].id
    manifest_asset = fixture.session.get(Asset, UUID(run.parameters["manifest_asset_id"]))
    assert manifest_asset is not None
    manifest = NarrationPreviewManifest.model_validate_json(
        fixture.blobs.read(manifest_asset.storage_key)
    )
    assert manifest.segment_ids == [fixture.script_segments[0].id]
