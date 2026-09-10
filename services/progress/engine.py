"""The stage-agnostic half of progress reporting.

Every long-running stage persists a run row whose status moves through a
small, ordered set of phases, and most persist one row per unit of work
(scenes, chunks, segments, shots). ``StageSpec`` describes how those statuses
map onto the bar the dashboard shows: which phase each status means, how much
of the bar the counted phases span, and the sentence shown beside the bar.
``calculate_progress`` turns a spec plus the current status and counts into a
``StageProgress``; the per-stage loaders only gather the inputs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any


class ProgressState(StrEnum):
    """The generic lifecycle the dashboard reacts to, whatever the stage."""

    QUEUED = "queued"
    RUNNING = "running"
    #: Stopped at a human gate: nothing is running until the owner acts.
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"


#: States in which no further change will arrive without an owner's action.
SETTLED_STATES: frozenset[ProgressState] = frozenset(
    {ProgressState.WAITING, ProgressState.COMPLETED, ProgressState.FAILED}
)


@dataclass(frozen=True)
class PhaseSpec:
    """One phase of a stage as the bar shows it.

    ``start`` and ``end`` bound the phase's share of the bar. A counted phase
    fills that range as its completed count grows; every other phase sits at
    ``start``.

    ``message`` is a format template with ``{done}`` (completed count),
    ``{next}`` (the item being worked on, never past the total), ``{total}``
    and ``{units}`` (the plural unit) available. ``count_label`` names what the
    counts count, e.g. ``"scenes analyzed"``; it falls back to the stage's.
    """

    state: ProgressState
    start: float
    message: str
    end: float | None = None
    counted: bool = False
    count_label: str | None = None


@dataclass(frozen=True)
class StageSpec:
    """How one stage's statuses read as progress."""

    #: A stable stage id; the ``PipelineStage`` value where the timeline has one.
    stage: str
    #: The heading the dashboard shows for the stage.
    label: str
    #: Singular noun for one unit of counted work, e.g. ``"scene"``.
    unit: str
    #: What the counts count, e.g. ``"scenes analyzed"``.
    count_label: str
    #: Run or project status literal -> phase key.
    status_phases: dict[str, str]
    #: Phase key -> how it is shown. Must include the phase named by
    #: ``unknown_phase``.
    phases: dict[str, PhaseSpec]
    #: The phase reported for a status the spec does not know, so an unexpected
    #: or future status never takes the status endpoint down.
    unknown_phase: str = "queued"


@dataclass(frozen=True)
class StageProgress:
    stage: str
    label: str
    state: ProgressState
    #: The stage-specific phase key, e.g. ``"scene_analysis"``.
    phase: str
    completed_count: int
    total_count: int
    unit: str
    count_label: str
    #: 0 to 100, at most one decimal place.
    percentage: float
    #: One sentence the dashboard shows next to the bar.
    message: str
    error_code: str | None
    #: When the run or one of its checkpoints last changed.
    updated_at: datetime | None


def plural(unit: str, count: int) -> str:
    return unit if count == 1 else f"{unit}s"


def calculate_progress(
    spec: StageSpec,
    *,
    status: str,
    completed_count: int,
    total_count: int,
    error_code: str | None = None,
    updated_at: datetime | None = None,
    share: float | None = None,
) -> StageProgress:
    """Turn a stage's status and work counts into what the dashboard shows.

    Counts are clamped so a stray row can never push the bar past its phase,
    and a failed stage keeps the share its finished items earned rather than
    reading as a restart from zero. ``share`` overrides the counted fraction
    for stages whose bar is fed by something other than the two counts, such
    as a render job's own durable percentage.
    """
    phase_key = spec.status_phases.get(status, spec.unknown_phase)
    phase = spec.phases[phase_key]
    total = max(total_count, 0)
    done = min(max(completed_count, 0), total)
    fraction = share if share is not None else (done / total if total else 0.0)
    fraction = min(max(fraction, 0.0), 1.0)
    counted = next((item for item in spec.phases.values() if item.counted), None)
    if phase.counted:
        percentage = phase.start + ((phase.end or phase.start) - phase.start) * fraction
    elif phase.state is ProgressState.FAILED and counted is not None:
        percentage = counted.start + ((counted.end or counted.start) - counted.start) * fraction
    else:
        percentage = phase.start
    values: dict[str, Any] = {
        "done": done,
        "next": min(done + 1, total),
        "total": total,
        "units": plural(spec.unit, total),
    }
    if "{" in phase.message and total == 0:
        # Nothing to count yet: the template's numbers would read "1 of 0".
        message = phase.message.split(" {")[0].rstrip(" (,")
    else:
        message = phase.message.format(**values)
    failed = phase.state is ProgressState.FAILED
    if failed and error_code:
        message = f"{message} ({error_code})"
    return StageProgress(
        stage=spec.stage,
        label=spec.label,
        state=phase.state,
        phase=phase_key,
        completed_count=done,
        total_count=total,
        unit=spec.unit,
        count_label=phase.count_label or spec.count_label,
        percentage=round(min(max(percentage, 0.0), 100.0), 1),
        message=message,
        error_code=error_code if failed else None,
        updated_at=updated_at,
    )


def latest(*timestamps: datetime | None) -> datetime | None:
    """The most recent of the given timestamps, ignoring missing ones."""
    present = [item for item in timestamps if item is not None]
    return max(present) if present else None
