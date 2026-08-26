from __future__ import annotations

from collections.abc import Callable

from temporalio import activity

from vidgen.contracts.workflow import StageActivityInput, StageActivityResult

StageHandler = Callable[[StageActivityInput], StageActivityResult]
_handlers: dict[str, StageHandler] = {}


def configure_activity_handlers(handlers: dict[str, StageHandler]) -> None:
    """Install process-local adapters; production adapters own sessions and provider clients."""
    _handlers.clear()
    _handlers.update(handlers)


def _execute(request: StageActivityInput) -> StageActivityResult:
    handler = _handlers.get(request.stage)
    if handler is None:
        raise RuntimeError(f"no activity handler configured for {request.stage}")
    return handler(request)


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
