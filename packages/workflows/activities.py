from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import copy_context
from threading import Event, Thread

from temporalio import activity

from vidgen.contracts.workflow import StageActivityInput, StageActivityResult

StageHandler = Callable[[StageActivityInput], StageActivityResult]
_handlers: dict[str, StageHandler] = {}
HEARTBEAT_INTERVAL_SECONDS = 30.0


def configure_activity_handlers(handlers: dict[str, StageHandler]) -> None:
    """Install process-local adapters; production adapters own sessions and provider clients."""
    _handlers.clear()
    _handlers.update(handlers)


def _execute(request: StageActivityInput) -> StageActivityResult:
    handler = _handlers.get(request.stage)
    if handler is None:
        raise RuntimeError(f"no activity handler configured for {request.stage}")
    with _activity_heartbeats(request.stage):
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
