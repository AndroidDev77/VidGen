"""T13 Temporal integration, project status, and CLI surface."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from temporalio import activity

from packages.workflows import activities
from packages.workflows.project import ProjectWorkflow
from services.storyboard.commands import (
    StoryboardCommandOptions,
    build_director,
    generate_storyboard,
    resolve_capability,
)
from services.storyboard.fake_provider import FakeStoryboardDirector
from services.storyboard.pipeline import StoryboardPipeline
from tests.storyboard_fixtures import build_fixture
from vidgen.contracts.workflow import (
    ProjectWorkflowInput,
    StageActivityInput,
    StageActivityResult,
)
from vidgen.db.storyboard_models import StoryboardRun
from workers.temporal_worker.registry import ACTIVITIES

STORYBOARD_STATUSES = (
    "storyboard_queued",
    "storyboard_directing",
    "storyboard_retiming",
    "storyboard_validating",
    "storyboard_repairing",
    "storyboard_complete",
    "storyboard_failed",
)


def test_storyboard_activity_is_registered_after_narration() -> None:
    names = [item.__name__ for item in ACTIVITIES]
    assert "run_storyboard_activity" in names
    assert names.index("run_storyboard_activity") > names.index("run_narration_activity")


def test_workflow_input_key_leaves_room_for_the_storyboard_stage() -> None:
    request = ProjectWorkflowInput(
        project_id=uuid4(), source_video_id=uuid4(), idempotency_key="x" * 220
    )
    assert len(f"{request.idempotency_key}:storyboard") <= 255


def test_storyboard_activity_carries_only_identifiers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Workflow history must never hold storyboard JSON or provider payloads."""
    monkeypatch.setattr(activity, "heartbeat", lambda *_: None)
    captured: list[StageActivityInput] = []

    def handler(request: StageActivityInput) -> StageActivityResult:
        captured.append(request)
        return StageActivityResult(
            stage=request.stage, entity_id=uuid4(), asset_id=uuid4(), reused=False
        )

    activities.configure_activity_handlers({"storyboard": handler})
    request = StageActivityInput(
        project_id=uuid4(),
        source_video_id=uuid4(),
        stage="storyboard",
        idempotency_key="project-key:storyboard",
    )
    result = activities.run_storyboard_activity(request)
    assert captured == [request]
    # Only UUIDs, the stage name, and the idempotency key cross the boundary.
    assert set(request.model_dump()) == {
        "schema_version",
        "project_id",
        "source_video_id",
        "stage",
        "idempotency_key",
    }
    assert set(result.model_dump()) == {
        "schema_version",
        "stage",
        "entity_id",
        "asset_id",
        "reused",
    }


def test_project_status_transitions_reach_storyboard_complete(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    observed: list[str] = []
    pipeline = StoryboardPipeline(fixture.session, fixture.blobs, FakeStoryboardDirector())
    original = pipeline._process_segment

    async def observing(**kwargs):
        observed.append(fixture.project.status)
        outcome = await original(**kwargs)
        observed.append(fixture.project.status)
        return outcome

    pipeline._process_segment = observing  # type: ignore[method-assign]
    result = asyncio.run(pipeline.process(project_id=fixture.project.id, idempotency_key="k"))
    assert result.status == "storyboard_complete"
    assert "storyboard_directing" in observed
    assert "storyboard_validating" in observed
    assert fixture.project.status == "storyboard_complete"
    assert set(observed) <= set(STORYBOARD_STATUSES)


def test_failed_run_marks_the_project_and_run_failed(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)

    class _Broken(FakeStoryboardDirector):
        async def propose(self, request):
            raise TimeoutError("provider timed out")

    pipeline = StoryboardPipeline(fixture.session, fixture.blobs, _Broken())
    with pytest.raises(TimeoutError):
        asyncio.run(pipeline.process(project_id=fixture.project.id, idempotency_key="k"))
    run = fixture.session.scalar(select(StoryboardRun))
    assert run.status == "storyboard_failed"
    assert run.error_code == "TimeoutError"
    assert fixture.project.status == "storyboard_failed"


def test_cancellation_is_observed_between_segments(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    pipeline = StoryboardPipeline(
        fixture.session, fixture.blobs, FakeStoryboardDirector(), cancellation_check=lambda: True
    )
    with pytest.raises(RuntimeError, match="cancelled"):
        asyncio.run(pipeline.process(project_id=fixture.project.id, idempotency_key="k"))
    assert fixture.project.status == "storyboard_failed"


def test_workflow_runs_storyboard_after_narration() -> None:
    workflow_source = Path(ProjectWorkflow.__module__.replace(".", "/") + ".py").read_text()
    narration = workflow_source.index('"narration"')
    storyboard = workflow_source.index('"storyboard"')
    assert narration < storyboard


# -- CLI surface --------------------------------------------------------------


def test_fake_mode_requires_no_provider_credentials() -> None:
    director = build_director(StoryboardCommandOptions(provider="fake"))
    assert director.name == "fake"
    assert resolve_capability(StoryboardCommandOptions()).capability_profile_id


def test_production_mode_requires_an_api_key() -> None:
    with pytest.raises(ValueError, match="API key"):
        build_director(StoryboardCommandOptions(provider="openai"))
    with pytest.raises(ValueError, match="unsupported storyboard provider"):
        build_director(StoryboardCommandOptions(provider="nope"))


def test_command_entry_point_runs_and_resumes(tmp_path: Path) -> None:
    fixture = build_fixture(tmp_path)
    options = StoryboardCommandOptions(provider="fake", idempotency_key="cli-key")
    first = asyncio.run(
        generate_storyboard(
            fixture.session, fixture.blobs, project_id=fixture.project.id, options=options
        )
    )
    second = asyncio.run(
        generate_storyboard(
            fixture.session, fixture.blobs, project_id=fixture.project.id, options=options
        )
    )
    assert first.storyboard_run_id == second.storyboard_run_id
    assert first.shot_count == second.shot_count > 0
    assert first.provider == "fake"
    assert first.total_duration_us > 0
