from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from runwayml import APIConnectionError, APITimeoutError

from services.animation.pipeline_errors import (
    AmbiguousVideoSubmission,
    VideoSubmissionNotSent,
)
from services.animation.runway import RunwayVideoProvider
from vidgen.contracts.animation import RunwayModel, VideoProvider, VideoProviderRequest


class Resource:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response

    async def retrieve(self, value):
        self.calls.append(value)
        return self.response

    async def delete(self, value):
        self.calls.append(value)


def request() -> VideoProviderRequest:
    return VideoProviderRequest(
        application_idempotency_key="stable",
        project_id=uuid4(),
        animation_run_id=uuid4(),
        animation_item_id=uuid4(),
        storyboard_id=uuid4(),
        storyboard_version=1,
        shot_id=uuid4(),
        shot_sequence=0,
        first_keyframe_asset_id=uuid4(),
        first_keyframe_sha256="a" * 64,
        compiled_motion_prompt="subject turns once",
        provider=VideoProvider.RUNWAY,
        model=RunwayModel.GEN4_TURBO,
        requested_duration_seconds=4,
        width=1280,
        height=720,
        attempt_number=1,
        provider_configuration_version="runway/2024-11-06",
    )


def test_submission_maps_exact_request_and_retrieval_redacts_outputs() -> None:
    created = Resource(SimpleNamespace(id="task-1"))
    retrieved = Resource(
        SimpleNamespace(
            status="SUCCEEDED",
            model="gen4_turbo",
            duration=4,
            created_at=datetime.now(UTC),
            output=["https://signed.invalid/output.mp4"],
            progress=1,
        )
    )
    client = SimpleNamespace(image_to_video=created, tasks=retrieved)
    provider = RunwayVideoProvider(client)
    submitted = asyncio.run(provider.submit(request(), "data:image/png;base64,abc"))
    assert submitted.remote_task_id == "task-1"
    assert created.calls[0] == {
        "model": "gen4_turbo",
        "prompt_image": "data:image/png;base64,abc",
        "prompt_text": "subject turns once",
        "duration": 4,
        "ratio": "1280:720",
    }
    result = asyncio.run(provider.retrieve("task-1"))
    assert result.status.value == "succeeded"
    assert result.output_handles == ("https://signed.invalid/output.mp4",)
    assert "signed.invalid" not in result.model_dump_json()


class LoopBoundResource:
    """A resource whose connections belong to the loop that opened them.

    Faithful to the failure the real client has: the first call binds it to the
    running loop, and any later call from a different loop fails the way httpx
    does when it is handed a pooled connection from a loop that has closed.
    """

    def __init__(self, response, calls: list[object]) -> None:
        self.response = response
        self.calls = calls
        self._loop: asyncio.AbstractEventLoop | None = None

    async def create(self, **kwargs):
        self._bind()
        self.calls.append(kwargs)
        return self.response

    def _bind(self) -> None:
        running = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = running
        elif self._loop is not running:
            raise APIConnectionError(
                request=httpx.Request("POST", "https://api.invalid/v1/image_to_video")
            ) from RuntimeError("Event loop is closed")


class LoopBoundClient:
    def __init__(self, calls: list[object], closed: list[object]) -> None:
        self.image_to_video = LoopBoundResource(SimpleNamespace(id="task-1"), calls)
        self.tasks = LoopBoundResource(SimpleNamespace(id="task-1"), calls)
        self._closed = closed

    async def close(self) -> None:
        self._closed.append(self)


def failing_client(cause: BaseException, *, timeout: bool = False):
    """A client whose submit fails the way the SDK reports a transport failure."""
    request = httpx.Request("POST", "https://api.invalid/v1/image_to_video")
    error = APITimeoutError(request=request) if timeout else APIConnectionError(request=request)
    error.__cause__ = cause

    class Failing:
        async def create(self, **kwargs):
            raise error

    return SimpleNamespace(image_to_video=Failing(), tasks=Failing())


def test_a_client_shared_across_two_event_loops_breaks_the_second_submit() -> None:
    """The regression itself, so the fake below is known to be faithful.

    Every activity runs its coroutine in its own ``asyncio.run`` loop. One
    client shared across them hands the second activity a connection belonging
    to a loop that has already closed.
    """
    calls: list[object] = []
    client = LoopBoundClient(calls, [])
    provider = RunwayVideoProvider(client)
    assert asyncio.run(provider.submit(request(), "data:image/png;base64,abc"))
    with pytest.raises(VideoSubmissionNotSent):
        asyncio.run(provider.submit(request(), "data:image/png;base64,abc"))


def test_one_provider_submits_across_separate_event_loops() -> None:
    """The fix: a shared provider opens - and releases - one client per loop."""
    calls: list[object] = []
    closed: list[object] = []
    provider = RunwayVideoProvider(
        client_factory=lambda: LoopBoundClient(calls, closed),
    )

    async def submit_and_release():
        try:
            return await provider.submit(request(), "data:image/png;base64,abc")
        finally:
            await provider.release_loop_client()

    first = asyncio.run(submit_and_release())
    second = asyncio.run(submit_and_release())
    assert first.remote_task_id == second.remote_task_id == "task-1"
    assert len(calls) == 2
    # One client per loop, and each one closed on the loop that opened it.
    assert len(closed) == 2


def test_a_closed_event_loop_is_never_an_ambiguous_outcome() -> None:
    """The failure that stranded ten shots: local, so no remote task exists."""
    provider = RunwayVideoProvider(failing_client(RuntimeError("Event loop is closed")))
    with pytest.raises(VideoSubmissionNotSent):
        asyncio.run(provider.submit(request(), "data:image/png;base64,abc"))


@pytest.mark.parametrize(
    "cause",
    [
        httpx.ConnectError("connection refused"),
        httpx.ConnectTimeout("handshake timed out"),
        httpx.PoolTimeout("no connection available"),
    ],
)
def test_a_connection_that_was_never_established_is_not_ambiguous(cause: Exception) -> None:
    provider = RunwayVideoProvider(failing_client(cause))
    with pytest.raises(VideoSubmissionNotSent):
        asyncio.run(provider.submit(request(), "data:image/png;base64,abc"))


@pytest.mark.parametrize(
    ("cause", "timeout"),
    [
        (httpx.ReadTimeout("no response"), True),
        (httpx.WriteTimeout("half-written"), True),
        (httpx.RemoteProtocolError("server disconnected"), False),
        (OSError("connection reset by peer"), False),
    ],
)
def test_a_failure_after_the_request_could_have_been_sent_stays_ambiguous(
    cause: Exception, timeout: bool
) -> None:
    """Runway may hold a task for any of these, so none of them may be retried."""
    provider = RunwayVideoProvider(failing_client(cause, timeout=timeout))
    with pytest.raises(AmbiguousVideoSubmission):
        asyncio.run(provider.submit(request(), "data:image/png;base64,abc"))


def test_a_provider_takes_a_client_or_a_factory_but_never_both() -> None:
    with pytest.raises(ValueError, match="exactly one"):
        RunwayVideoProvider()
    with pytest.raises(ValueError, match="exactly one"):
        RunwayVideoProvider(SimpleNamespace(), client_factory=SimpleNamespace)


def test_releasing_a_caller_owned_client_leaves_it_alone() -> None:
    """A caller that injected its own client keeps owning its lifetime."""
    closed: list[object] = []
    client = LoopBoundClient([], closed)
    provider = RunwayVideoProvider(client)

    async def release() -> None:
        await provider.release_loop_client()

    asyncio.run(release())
    assert closed == []
