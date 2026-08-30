"""A cancellable, heartbeating FFmpeg executor with bounded diagnostics.

T17's :class:`~services.renderer.render.CommandExecutor` runs a command to
completion and raises on a nonzero exit. That is the right shape for a library.
An executor running a fifty-shot encode inside a leased job needs three more
things, and this subclass adds exactly those: it polls instead of blocking so a
cancellation request terminates FFmpeg promptly, it heartbeats the lease while a
phase runs, and it keeps only the tail of stderr so a pathological encode cannot
turn a log line into a memory or storage problem.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass

from services.renderer.render import CommandExecutor


class RenderCancelled(RuntimeError):
    """Raised when a cancellation request stops an in-flight render."""


class RenderTimeout(RuntimeError):
    """Raised when a phase exceeds the configured execution timeout."""


@dataclass
class PhaseRecord:
    phase: str
    duration_seconds: float
    exit_code: int


class CancellableCommandExecutor(CommandExecutor):
    """Run FFmpeg phases under a lease, a timeout and a cancellation check."""

    def __init__(
        self,
        *,
        timeout_seconds: int = 3600,
        output_limit: int = 64_000,
        heartbeat: Callable[[str], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
        poll_interval_seconds: float = 0.5,
    ) -> None:
        super().__init__(
            timeout_seconds=timeout_seconds, output_limit=output_limit, heartbeat=heartbeat
        )
        self.cancelled = cancelled or (lambda: False)
        self.poll_interval_seconds = poll_interval_seconds
        self.phases: list[PhaseRecord] = []
        self.last_stderr_tail = ""

    def run(self, arguments: list[str], phase: str) -> subprocess.CompletedProcess[bytes]:
        if not arguments or not isinstance(arguments, list):
            raise TypeError("subprocess arguments must be a non-empty argument array")
        if self.cancelled():
            raise RenderCancelled(f"render cancelled before phase {phase}")
        self.heartbeat(phase)
        self.executions += 1
        started = time.monotonic()
        # An argument array, never a shell string: no manifest value, asset
        # hash or filesystem path is ever interpreted by a shell.
        process = subprocess.Popen(arguments, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        last_heartbeat = started
        try:
            while process.poll() is None:
                if self.cancelled():
                    self._terminate(process)
                    raise RenderCancelled(f"render cancelled during phase {phase}")
                elapsed = time.monotonic() - started
                if elapsed > self.timeout_seconds:
                    self._terminate(process)
                    raise RenderTimeout(f"FFmpeg phase {phase} exceeded {self.timeout_seconds}s")
                if time.monotonic() - last_heartbeat >= 15:
                    self.heartbeat(phase)
                    last_heartbeat = time.monotonic()
                time.sleep(self.poll_interval_seconds)
            stdout, stderr = process.communicate()
        finally:
            if process.poll() is None:  # pragma: no cover - defensive
                self._terminate(process)
        duration = time.monotonic() - started
        returncode = process.returncode or 0
        self.phases.append(
            PhaseRecord(phase=phase, duration_seconds=round(duration, 3), exit_code=returncode)
        )
        tail = stderr[-self.output_limit :].decode("utf-8", "replace")
        self.last_stderr_tail = tail
        if returncode:
            raise RuntimeError(f"FFmpeg phase {phase} failed with exit {returncode}: {tail}")
        return subprocess.CompletedProcess(arguments, returncode, stdout, stderr)

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes]) -> None:
        """Stop FFmpeg gracefully, then forcibly, and never leave it running."""
        process.terminate()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover - only a wedged encode
            process.kill()
            process.communicate()
