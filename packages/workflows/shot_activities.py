"""T16 activity boundaries; configured handlers own databases, assets and providers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from temporalio import activity

from vidgen.contracts.shot_workflow import (
    ProjectShotFanoutInput,
    ProjectShotFanoutResult,
    ResolveShotFanoutResult,
    ShotWorkflowInput,
    ShotWorkflowProgress,
    ShotWorkflowResult,
)

ShotActivityHandler = Callable[[Any], Any]
_handlers: dict[str, ShotActivityHandler] = {}


def configure_shot_activity_handlers(handlers: dict[str, ShotActivityHandler]) -> None:
    _handlers.clear()
    _handlers.update(handlers)


def _run(name: str, request: Any) -> Any:
    handler = _handlers.get(name)
    if handler is None:
        raise RuntimeError(f"no T16 activity handler configured for {name}")
    return handler(request)


@activity.defn(name="resolve_shot_fanout")
def resolve_shot_fanout(request: ProjectShotFanoutInput) -> ResolveShotFanoutResult:
    """Validate selected T13 lineage and return ordered, compact shot inputs."""
    return cast(ResolveShotFanoutResult, _run("resolve_shot_fanout", request))


@activity.defn(name="resolve_shot_input")
def resolve_shot_input(request: ShotWorkflowInput) -> ShotWorkflowProgress:
    return cast(ShotWorkflowProgress, _run("resolve_shot_input", request))


@activity.defn(name="run_shot_keyframe")
def run_shot_keyframe(request: ShotWorkflowInput) -> ShotWorkflowProgress:
    """Invoke/resume T14 by stable identity; provider bytes remain in the service."""
    return cast(ShotWorkflowProgress, _run("run_shot_keyframe", request))


@activity.defn(name="run_shot_animation")
def run_shot_animation(request: ShotWorkflowInput) -> ShotWorkflowResult:
    """Invoke/resume T15, including polling an already persisted remote task."""
    return cast(ShotWorkflowResult, _run("run_shot_animation", request))


@activity.defn(name="persist_shot_checkpoint")
def persist_shot_checkpoint(request: ShotWorkflowProgress) -> ShotWorkflowProgress:
    return cast(ShotWorkflowProgress, _run("persist_shot_checkpoint", request))


@activity.defn(name="persist_shot_fanout_checkpoint")
def persist_shot_fanout_checkpoint(request: ProjectShotFanoutResult) -> ProjectShotFanoutResult:
    return cast(ProjectShotFanoutResult, _run("persist_shot_fanout_checkpoint", request))


SHOT_ACTIVITIES = [
    resolve_shot_fanout,
    resolve_shot_input,
    run_shot_keyframe,
    run_shot_animation,
    persist_shot_checkpoint,
    persist_shot_fanout_checkpoint,
]
