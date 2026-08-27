"""FFmpeg executor with containment, time limits, and cancellation-safe processes."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from vidgen.contracts.render import RenderCommandPlan


def contained(root: Path, candidate: Path) -> Path:
    root = root.resolve()
    candidate = candidate.resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError("render path escapes attempt directory")
    return candidate


class CommandExecutor:
    def __init__(
        self,
        *,
        timeout_seconds: int = 900,
        output_limit: int = 1_000_000,
        heartbeat: Callable[[str], None] | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.output_limit = output_limit
        self.heartbeat = heartbeat or (lambda _: None)
        self.executions = 0

    def run(self, arguments: list[str], phase: str) -> None:
        self.heartbeat(phase)
        self.executions += 1
        result = subprocess.run(
            arguments, check=False, capture_output=True, timeout=self.timeout_seconds
        )
        if result.returncode:
            error = result.stderr[-self.output_limit :].decode("utf-8", "replace")
            raise RuntimeError(f"FFmpeg phase {phase} failed: {error}")

    def execute(self, plan: RenderCommandPlan) -> None:
        for index, args in enumerate(plan.normalization_arguments):
            self.run(args, f"normalize:{index}")
        self.run(plan.picture_arguments, "picture")
        self.run(plan.premaster_arguments, "premaster")
        self.run(plan.loudness_pass1_arguments, "loudness-measure")
        self.run(plan.loudness_pass2_arguments, "loudness-normalize")
        self.run(plan.final_arguments, "encode")
