from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import copy_context
from threading import Event, Thread
from typing import Any

from temporalio import activity

from vidgen.contracts.workflow import (
    AnimationActivityInput,
    FinalQAActivityInput,
    FinalQAActivityResult,
    RenderActivityInput,
    RenderActivityResult,
    StageActivityInput,
    StageActivityResult,
)

StageHandler = Callable[[Any], StageActivityResult]
FinalQAHandler = Callable[[FinalQAActivityInput], FinalQAActivityResult]
RenderHandler = Callable[[RenderActivityInput], RenderActivityResult]
_handlers: dict[str, StageHandler] = {}
_final_qa_handler: dict[str, FinalQAHandler] = {}
_render_handler: dict[str, RenderHandler] = {}
HEARTBEAT_INTERVAL_SECONDS = 30.0


def configure_activity_handlers(handlers: dict[str, StageHandler]) -> None:
    """Install process-local adapters; production adapters own sessions and provider clients."""
    _handlers.clear()
    _handlers.update(handlers)


def configure_final_qa_handler(handler: FinalQAHandler | None) -> None:
    """Install the T22 adapter, which returns a bounded ID-only result."""
    _final_qa_handler.clear()
    if handler is not None:
        _final_qa_handler["final_editorial_qa"] = handler


def configure_render_handler(handler: RenderHandler | None) -> None:
    """Install the T17b adapter, which returns a bounded ID-only result."""
    _render_handler.clear()
    if handler is not None:
        _render_handler["render"] = handler


def _execute(request: StageActivityInput | AnimationActivityInput) -> StageActivityResult:
    stage = request.stage if isinstance(request, StageActivityInput) else "animation"
    handler = _handlers.get(stage)
    if handler is None:
        raise RuntimeError(f"no activity handler configured for {stage}")
    with _activity_heartbeats(stage):
        return handler(request)


@contextmanager
def _activity_heartbeats(stage: str) -> Iterator[None]:
    """Heartbeat blocking activity handlers until completion or cancellation."""
    stopped = Event()

    def heartbeat() -> None:
        while not stopped.wait(HEARTBEAT_INTERVAL_SECONDS):
            activity.heartbeat({"stage": stage})

    activity.heartbeat({"stage": stage})
    context = copy_context()
    thread = Thread(
        target=context.run,
        args=(heartbeat,),
        name=f"heartbeat-{stage}",
        daemon=True,
    )
    thread.start()
    try:
        yield
    finally:
        stopped.set()
        thread.join(timeout=1)


@activity.defn(name="run_upload_activity")
def run_upload_activity(request: StageActivityInput) -> StageActivityResult:
    return _execute(request)


@activity.defn(name="run_media_processing_activity")
def run_media_processing_activity(request: StageActivityInput) -> StageActivityResult:
    return _execute(request)


@activity.defn(name="run_transcript_acquisition_activity")
def run_transcript_acquisition_activity(request: StageActivityInput) -> StageActivityResult:
    # The configured T07B acquisition adapter owns subtitle-first routing. Provider
    # failures escape this activity and are retried; only its explicit unavailable
    # outcome may invoke the T07 audio adapter.
    return _execute(request)


@activity.defn(name="run_evidence_activity")
def run_evidence_activity(request: StageActivityInput) -> StageActivityResult:
    return _execute(request)


@activity.defn(name="run_episode_analysis_activity")
def run_episode_analysis_activity(request: StageActivityInput) -> StageActivityResult:
    return _execute(request)


@activity.defn(name="run_script_generation_activity")
def run_script_generation_activity(request: StageActivityInput) -> StageActivityResult:
    return _execute(request)


@activity.defn(name="run_narration_activity")
def run_narration_activity(request: StageActivityInput) -> StageActivityResult:
    return _execute(request)


@activity.defn(name="run_storyboard_activity")
def run_storyboard_activity(request: StageActivityInput) -> StageActivityResult:
    # T13 receives IDs only. The configured adapter loads the selected episode
    # model, approved script, and completed narration run from the database, and
    # returns the storyboard run and canonical asset IDs.
    return _execute(request)


@activity.defn(name="run_image_generation_activity")
def run_image_generation_activity(request: StageActivityInput) -> StageActivityResult:
    """T14 ID-only boundary; prompts and images never enter workflow history."""
    return _execute(request)


@activity.defn(name="run_animation_activity")
def run_animation_activity(request: AnimationActivityInput) -> StageActivityResult:
    """T15 ID-only boundary; prompts, URLs, video and probe JSON stay out of history."""
    return _execute(request)


@activity.defn(name="run_final_editorial_qa_activity")
def run_final_editorial_qa_activity(request: FinalQAActivityInput) -> FinalQAActivityResult:
    """T22 ID-only boundary.

    The activity receives references and returns counts, IDs and a gate
    decision. Reports, findings, sampled frames, caption text, media bytes and
    provider payloads stay in durable storage and never reach workflow history.
    """
    handler = _final_qa_handler.get("final_editorial_qa")
    if handler is None:
        raise RuntimeError("no activity handler configured for final_editorial_qa")
    with _activity_heartbeats("final_editorial_qa"):
        return handler(request)


@activity.defn(name="run_render_activity")
def run_render_activity(request: RenderActivityInput) -> RenderActivityResult:
    """Execute the project's render job through the canonical T17b executor.

    The activity is safe to retry: the executor claims the job under a lease,
    resumes from its durable checkpoint, and returns the existing result for a
    job that is already complete. A retry therefore never produces a second
    render job, a second render, or a duplicate asset row.
    """
    handler = _render_handler.get("render")
    if handler is None:
        raise RuntimeError("no render activity handler configured")
    with _activity_heartbeats("render"):
        return handler(request)
