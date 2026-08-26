"""Deterministic T13 timing solver.

The Storyboard Director proposes semantic shots. This module owns final timing
and never asks a provider anything. All arithmetic is exact integer microseconds,
so identical inputs always produce byte-identical output and no binary
floating-point drift can accumulate across a run.

Hard rules enforced here:

* Measured T12 narration duration is the timing authority.
* Narration is never stretched and the approved script is never changed.
* Shot intervals are monotonic, gapless, non-overlapping, and strictly positive.
* The final shot of a segment ends at the exact measured narration duration.
* An unsupported duration is solved by splitting at an approved boundary, or by
  requesting the next supported generation duration and recording trimming.
* If no valid supported solution exists, the plan is rejected.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from math import ceil

from vidgen.contracts.storyboard import (
    MICROSECONDS_PER_SECOND,
    BoundaryKind,
    NarrationBoundary,
    StoryboardShotProposal,
    StoryboardValidationDiagnostic,
    TimingAdjustment,
    VisualProviderCapability,
)

RETIMER_VERSION = "storyboard-retimer/1.0.0"

#: Higher is preferred when snapping a split to an approved boundary.
_BOUNDARY_RANK: dict[BoundaryKind, int] = {"sentence": 3, "clause": 2, "beat": 1, "word": 0}
_MAX_SOLVER_PASSES = 32


class RetimerError(RuntimeError):
    """A timing failure carrying the diagnostic that describes it."""

    def __init__(self, diagnostic: StoryboardValidationDiagnostic) -> None:
        super().__init__(diagnostic.message)
        self.diagnostic = diagnostic


@dataclass(frozen=True, slots=True)
class RetimerConfig:
    """Deterministic solver configuration. Part of the storyboard input identity."""

    version: str = RETIMER_VERSION
    min_shot_duration_us: int = 1_000_000
    max_shot_duration_us: int = 7_500_000
    boundary_snap_window_us: int = 500_000

    def material(self) -> dict[str, int | str]:
        return {
            "version": self.version,
            "min_shot_duration_us": self.min_shot_duration_us,
            "max_shot_duration_us": self.max_shot_duration_us,
            "boundary_snap_window_us": self.boundary_snap_window_us,
        }


@dataclass(slots=True)
class ShotTiming:
    """One canonical, fully solved shot interval inside a narration segment."""

    segment_sequence: int
    shot_sequence: int
    proposal_sequence: int
    start_us: int
    end_us: int
    usable_duration_us: int
    requested_generation_duration_us: int
    trim_start_us: int
    trim_end_us: int
    transition_handle_us: int
    word_start_index: int
    word_end_index: int
    clause_label: str


@dataclass(slots=True)
class SegmentTiming:
    shots: list[ShotTiming]
    adjustments: list[TimingAdjustment] = field(default_factory=list)
    residual_allocation_us: int = 0
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _Interval:
    start_us: int
    end_us: int
    word_start_index: int
    word_end_index: int
    proposal_sequence: int
    clause_label: str

    @property
    def duration_us(self) -> int:
        return self.end_us - self.start_us


def _diagnostic(
    code: str,
    message: str,
    *,
    segment_sequence: int,
    shot_sequence: int = -1,
    repairable: bool = True,
    measured_us: int | None = None,
    expected_us: int | None = None,
    entity_path: str = "segment",
) -> StoryboardValidationDiagnostic:
    return StoryboardValidationDiagnostic(
        code=code,  # type: ignore[arg-type]
        severity="error",
        repairable=repairable,
        message=message,
        entity_path=entity_path,
        segment_sequence=segment_sequence,
        shot_sequence=shot_sequence,
        measured_us=measured_us,
        expected_us=expected_us,
    )


def select_generation_duration(needed_us: int, capability: VisualProviderCapability) -> int | None:
    """The smallest supported generated duration that covers ``needed_us``."""
    if needed_us <= capability.min_generation_duration_us:
        return capability.min_generation_duration_us
    if needed_us > capability.max_generation_duration_us:
        return None
    if capability.supported_generation_durations_us:
        for duration in capability.supported_generation_durations_us:
            if duration >= needed_us:
                return duration
        return None
    offset = needed_us - capability.min_generation_duration_us
    steps = ceil(offset / capability.duration_increment_us)
    candidate = capability.min_generation_duration_us + steps * capability.duration_increment_us
    return candidate if candidate <= capability.max_generation_duration_us else None


def allocate_residual(total_us: int, parts: int) -> list[int]:
    """Split ``total_us`` into ``parts`` exact microsecond shares.

    The remainder is allocated deterministically: one microsecond each to the
    earliest shares, so the same inputs always yield the same distribution.
    """
    if parts <= 0:
        raise ValueError("parts must be positive")
    base, remainder = divmod(total_us, parts)
    return [base + (1 if index < remainder else 0) for index in range(parts)]


def _word_end_offsets(word_timings: list[NarrationBoundary], duration_us: int) -> list[int]:
    offsets: list[int] = []
    previous = 0
    for timing in sorted(word_timings, key=lambda item: item.word_index):
        offset = min(max(timing.offset_us, previous), duration_us)
        offsets.append(offset)
        previous = offset
    return offsets


def _boundary_kinds(approved: list[NarrationBoundary], word_count: int) -> dict[int, BoundaryKind]:
    kinds: dict[int, BoundaryKind] = {}
    for boundary in approved:
        if 0 <= boundary.word_index < word_count:
            current = kinds.get(boundary.word_index, "word")
            if _BOUNDARY_RANK[boundary.kind] >= _BOUNDARY_RANK[current]:
                kinds[boundary.word_index] = boundary.kind
    return kinds


def retime_segment(
    *,
    segment_sequence: int,
    narration_duration_us: int,
    word_timings: list[NarrationBoundary],
    approved_boundaries: list[NarrationBoundary],
    proposals: list[StoryboardShotProposal],
    capability: VisualProviderCapability,
    config: RetimerConfig,
) -> SegmentTiming:
    """Solve one narration segment's shot durations exactly against its audio."""
    if narration_duration_us <= 0:
        raise RetimerError(
            _diagnostic(
                "nonpositive_duration",
                "measured narration duration must be positive",
                segment_sequence=segment_sequence,
                repairable=False,
                measured_us=narration_duration_us,
            )
        )
    if not proposals:
        raise RetimerError(
            _diagnostic(
                "narration_coverage_gap",
                "the Storyboard Director proposed no shots for this narration segment",
                segment_sequence=segment_sequence,
            )
        )
    word_ends = _word_end_offsets(word_timings, narration_duration_us)
    word_count = len(word_ends)
    kinds = _boundary_kinds(approved_boundaries, word_count)
    ordered = sorted(proposals, key=lambda item: item.proposal_sequence)
    _require_contiguous_word_ranges(ordered, word_count, segment_sequence)

    adjustments: list[TimingAdjustment] = []
    warnings: list[str] = []
    intervals = _initial_intervals(
        ordered, word_ends, narration_duration_us, segment_sequence, adjustments
    )
    intervals, residual = _solve_durations(
        intervals,
        word_ends=word_ends,
        kinds=kinds,
        capability=capability,
        config=config,
        segment_sequence=segment_sequence,
        adjustments=adjustments,
        warnings=warnings,
    )
    _assert_exact_coverage(intervals, narration_duration_us, segment_sequence)
    shots = _finalize(
        intervals,
        ordered_proposals={item.proposal_sequence: item for item in ordered},
        capability=capability,
        segment_sequence=segment_sequence,
        adjustments=adjustments,
    )
    return SegmentTiming(
        shots=shots,
        adjustments=adjustments,
        residual_allocation_us=residual,
        warnings=warnings,
    )


def _require_contiguous_word_ranges(
    proposals: list[StoryboardShotProposal], word_count: int, segment_sequence: int
) -> None:
    cursor = 0
    for proposal in proposals:
        if proposal.word_start_index != cursor:
            raise RetimerError(
                _diagnostic(
                    "word_range_gap",
                    f"proposal {proposal.proposal_sequence} starts at word "
                    f"{proposal.word_start_index} but word {cursor} is uncovered",
                    segment_sequence=segment_sequence,
                    shot_sequence=proposal.proposal_sequence,
                    entity_path=f"proposals[{proposal.proposal_sequence}].word_start_index",
                )
            )
        cursor = proposal.word_end_index
    if cursor != word_count:
        raise RetimerError(
            _diagnostic(
                "word_range_gap",
                f"proposed shots cover {cursor} of {word_count} narration words",
                segment_sequence=segment_sequence,
                entity_path="proposals",
                measured_us=cursor,
                expected_us=word_count,
            )
        )


def _initial_intervals(
    proposals: list[StoryboardShotProposal],
    word_ends: list[int],
    duration_us: int,
    segment_sequence: int,
    adjustments: list[TimingAdjustment],
) -> list[_Interval]:
    intervals: list[_Interval] = []
    start = 0
    for index, proposal in enumerate(proposals):
        last = index == len(proposals) - 1
        end = duration_us if last else word_ends[proposal.word_end_index - 1]
        end = min(max(end, start), duration_us)
        if end - start != proposal.desired_duration_us:
            adjustments.append(
                TimingAdjustment(
                    segment_sequence=segment_sequence,
                    proposal_sequence=proposal.proposal_sequence,
                    shot_sequence=index,
                    kind="final_end_snap" if last else "boundary_snap",
                    proposed_duration_us=proposal.desired_duration_us,
                    canonical_duration_us=end - start,
                    delta_us=(end - start) - proposal.desired_duration_us,
                    reason=(
                        "final shot snapped to the measured narration duration"
                        if last
                        else "shot boundary snapped to the approved narration word boundary"
                    ),
                )
            )
        intervals.append(
            _Interval(
                start_us=start,
                end_us=end,
                word_start_index=proposal.word_start_index,
                word_end_index=proposal.word_end_index,
                proposal_sequence=proposal.proposal_sequence,
                clause_label=proposal.clause_label,
            )
        )
        start = end
    return intervals


def _effective_max_usable(capability: VisualProviderCapability, config: RetimerConfig) -> int:
    return min(config.max_shot_duration_us, capability.max_generation_duration_us)


def _solve_durations(
    intervals: list[_Interval],
    *,
    word_ends: list[int],
    kinds: dict[int, BoundaryKind],
    capability: VisualProviderCapability,
    config: RetimerConfig,
    segment_sequence: int,
    adjustments: list[TimingAdjustment],
    warnings: list[str],
) -> tuple[list[_Interval], int]:
    """Split over-long intervals and merge under-long ones until both hold."""
    maximum = _effective_max_usable(capability, config)
    minimum = min(config.min_shot_duration_us, maximum)
    residual_total = 0
    for _ in range(_MAX_SOLVER_PASSES):
        split, residual = _split_over_long(
            intervals,
            word_ends=word_ends,
            kinds=kinds,
            maximum=maximum,
            config=config,
            segment_sequence=segment_sequence,
            adjustments=adjustments,
            warnings=warnings,
        )
        residual_total += residual
        merged = _merge_under_long(
            split,
            minimum=minimum,
            maximum=maximum,
            segment_sequence=segment_sequence,
            adjustments=adjustments,
        )
        if merged == intervals:
            return merged, residual_total
        intervals = merged
    raise RetimerError(
        _diagnostic(
            "impossible_duration_allocation",
            "the timing solver could not satisfy the configured minimum and maximum shot "
            "durations against the measured narration",
            segment_sequence=segment_sequence,
        )
    )


def _split_over_long(
    intervals: list[_Interval],
    *,
    word_ends: list[int],
    kinds: dict[int, BoundaryKind],
    maximum: int,
    config: RetimerConfig,
    segment_sequence: int,
    adjustments: list[TimingAdjustment],
    warnings: list[str],
) -> tuple[list[_Interval], int]:
    result: list[_Interval] = []
    residual_total = 0
    for interval in intervals:
        if interval.duration_us <= maximum:
            result.append(interval)
            continue
        parts = ceil(interval.duration_us / maximum)
        pieces, residual = _split_interval(
            interval,
            parts=parts,
            word_ends=word_ends,
            kinds=kinds,
            config=config,
            segment_sequence=segment_sequence,
            warnings=warnings,
        )
        residual_total += residual
        for piece in pieces:
            adjustments.append(
                TimingAdjustment(
                    segment_sequence=segment_sequence,
                    proposal_sequence=interval.proposal_sequence,
                    shot_sequence=-1,
                    kind="split",
                    proposed_duration_us=interval.duration_us,
                    canonical_duration_us=piece.duration_us,
                    delta_us=piece.duration_us - interval.duration_us,
                    reason=(
                        "proposed shot exceeded the supported maximum generated duration and was "
                        "split at an approved narration boundary"
                    ),
                )
            )
        result.extend(pieces)
    return result, residual_total


def _split_interval(
    interval: _Interval,
    *,
    parts: int,
    word_ends: list[int],
    kinds: dict[int, BoundaryKind],
    config: RetimerConfig,
    segment_sequence: int,
    warnings: list[str],
) -> tuple[list[_Interval], int]:
    candidates = [
        (index, word_ends[index])
        for index in range(interval.word_start_index, interval.word_end_index - 1)
        if interval.start_us < word_ends[index] < interval.end_us
    ]
    if not candidates:
        return _split_without_boundaries(
            interval, parts=parts, segment_sequence=segment_sequence, warnings=warnings
        )
    shares = allocate_residual(interval.duration_us, parts)
    ideal: list[int] = []
    cursor = interval.start_us
    for share in shares[:-1]:
        cursor += share
        ideal.append(cursor)
    chosen: list[tuple[int, int]] = []
    used: set[int] = set()
    lower_bound = interval.start_us
    for target in ideal:
        pick = _snap_to_boundary(
            target,
            candidates=[item for item in candidates if item[0] not in used],
            lower_bound=lower_bound,
            upper_bound=interval.end_us,
            kinds=kinds,
            window_us=config.boundary_snap_window_us,
        )
        if pick is None:
            continue
        used.add(pick[0])
        chosen.append(pick)
        lower_bound = pick[1]
    if not chosen:
        return _split_without_boundaries(
            interval, parts=parts, segment_sequence=segment_sequence, warnings=warnings
        )
    pieces: list[_Interval] = []
    start_time = interval.start_us
    start_word = interval.word_start_index
    for word_index, time in chosen:
        pieces.append(
            _Interval(
                start_us=start_time,
                end_us=time,
                word_start_index=start_word,
                word_end_index=word_index + 1,
                proposal_sequence=interval.proposal_sequence,
                clause_label=interval.clause_label,
            )
        )
        start_time = time
        start_word = word_index + 1
    pieces.append(
        _Interval(
            start_us=start_time,
            end_us=interval.end_us,
            word_start_index=start_word,
            word_end_index=interval.word_end_index,
            proposal_sequence=interval.proposal_sequence,
            clause_label=interval.clause_label,
        )
    )
    return pieces, 0


def _snap_to_boundary(
    target_us: int,
    *,
    candidates: list[tuple[int, int]],
    lower_bound: int,
    upper_bound: int,
    kinds: dict[int, BoundaryKind],
    window_us: int,
) -> tuple[int, int] | None:
    usable = [item for item in candidates if lower_bound < item[1] < upper_bound]
    if not usable:
        return None
    windowed = [item for item in usable if abs(item[1] - target_us) <= window_us]
    pool = windowed or usable
    # Deterministic preference: strongest approved boundary, then nearest, then earliest.
    return min(
        pool,
        key=lambda item: (
            -_BOUNDARY_RANK[kinds.get(item[0], "word")] if windowed else 0,
            abs(item[1] - target_us),
            item[1],
            item[0],
        ),
    )


def _split_without_boundaries(
    interval: _Interval,
    *,
    parts: int,
    segment_sequence: int,
    warnings: list[str],
) -> tuple[list[_Interval], int]:
    """Last resort when a single word outlives the provider maximum."""
    if interval.word_end_index - interval.word_start_index > 1:
        raise RetimerError(
            _diagnostic(
                "impossible_duration_allocation",
                "no approved clause, beat, or word boundary is available inside a shot that "
                "exceeds the supported maximum generated duration",
                segment_sequence=segment_sequence,
                shot_sequence=interval.proposal_sequence,
            )
        )
    shares = allocate_residual(interval.duration_us, parts)
    residual = interval.duration_us - (interval.duration_us // parts) * parts
    warnings.append(
        "a single narration word exceeded the supported maximum generated duration and was "
        "divided evenly; the residual microseconds were allocated to the earliest shots"
    )
    pieces: list[_Interval] = []
    cursor = interval.start_us
    for share in shares:
        pieces.append(
            _Interval(
                start_us=cursor,
                end_us=cursor + share,
                word_start_index=interval.word_start_index,
                word_end_index=interval.word_end_index,
                proposal_sequence=interval.proposal_sequence,
                clause_label=interval.clause_label,
            )
        )
        cursor += share
    return pieces, residual


def _merge_under_long(
    intervals: list[_Interval],
    *,
    minimum: int,
    maximum: int,
    segment_sequence: int,
    adjustments: list[TimingAdjustment],
) -> list[_Interval]:
    """Merge shots shorter than the configured minimum into a neighbour.

    A whole segment shorter than the minimum is legitimate: its single shot keeps
    its exact usable duration and the generation duration is rounded up instead.
    """
    result = list(intervals)
    changed = True
    while changed and len(result) > 1:
        changed = False
        for index, interval in enumerate(result):
            if interval.duration_us >= minimum:
                continue
            previous = result[index - 1] if index > 0 else None
            following = result[index + 1] if index + 1 < len(result) else None
            target = _merge_target(previous, following, maximum)
            if target is None:
                continue
            if target is previous and previous is not None:
                merged = _merge(previous, interval)
                result[index - 1 : index + 1] = [merged]
            elif following is not None:
                merged = _merge(interval, following)
                result[index : index + 2] = [merged]
            else:  # pragma: no cover - guarded by _merge_target
                continue
            adjustments.append(
                TimingAdjustment(
                    segment_sequence=segment_sequence,
                    proposal_sequence=interval.proposal_sequence,
                    shot_sequence=-1,
                    kind="merge",
                    proposed_duration_us=interval.duration_us,
                    canonical_duration_us=merged.duration_us,
                    delta_us=merged.duration_us - interval.duration_us,
                    reason=(
                        "shot fell below the configured minimum edit duration and was merged "
                        "into its neighbouring shot"
                    ),
                )
            )
            changed = True
            break
    return result


def _merge_target(
    previous: _Interval | None, following: _Interval | None, maximum: int
) -> _Interval | None:
    options = [item for item in (previous, following) if item is not None]
    fitting = [item for item in options if item.duration_us <= maximum]
    if not fitting:
        return None
    # Deterministic: prefer the previous shot, then the shorter neighbour.
    if previous is not None and any(item is previous for item in fitting):
        return previous
    return fitting[0]


def _merge(left: _Interval, right: _Interval) -> _Interval:
    return _Interval(
        start_us=left.start_us,
        end_us=right.end_us,
        word_start_index=left.word_start_index,
        word_end_index=right.word_end_index,
        proposal_sequence=left.proposal_sequence,
        clause_label=left.clause_label or right.clause_label,
    )


def _assert_exact_coverage(
    intervals: list[_Interval], duration_us: int, segment_sequence: int
) -> None:
    cursor = 0
    for index, interval in enumerate(intervals):
        if interval.duration_us <= 0:
            raise RetimerError(
                _diagnostic(
                    "nonpositive_duration",
                    "a solved shot had zero or negative duration",
                    segment_sequence=segment_sequence,
                    shot_sequence=index,
                    measured_us=interval.duration_us,
                )
            )
        if interval.start_us < cursor:
            raise RetimerError(
                _diagnostic(
                    "invalid_overlap",
                    "solved shots overlap",
                    segment_sequence=segment_sequence,
                    shot_sequence=index,
                )
            )
        if interval.start_us > cursor:
            raise RetimerError(
                _diagnostic(
                    "narration_coverage_gap",
                    "solved shots left an unexplained narration gap",
                    segment_sequence=segment_sequence,
                    shot_sequence=index,
                    measured_us=interval.start_us,
                    expected_us=cursor,
                )
            )
        cursor = interval.end_us
    if cursor != duration_us:
        raise RetimerError(
            _diagnostic(
                "narration_coverage_gap",
                "solved shots do not end at the measured narration duration",
                segment_sequence=segment_sequence,
                measured_us=cursor,
                expected_us=duration_us,
            )
        )


def _finalize(
    intervals: list[_Interval],
    *,
    ordered_proposals: dict[int, StoryboardShotProposal],
    capability: VisualProviderCapability,
    segment_sequence: int,
    adjustments: list[TimingAdjustment],
) -> list[ShotTiming]:
    shots: list[ShotTiming] = []
    for index, interval in enumerate(intervals):
        proposal = ordered_proposals[interval.proposal_sequence]
        # A handle belongs to the piece that still carries the proposal's own edge; a
        # split or merge removes the edge and therefore the handle.
        first_piece = interval.word_start_index == proposal.word_start_index
        last_piece = interval.word_end_index == proposal.word_end_index
        handle_in = proposal.transition_in.handle_us if first_piece else 0
        handle_out = proposal.transition_out.handle_us if last_piece else 0
        usable = interval.duration_us
        handle_in, handle_out, generation = _fit_generation(
            usable, handle_in, handle_out, capability, segment_sequence, index
        )
        slack = generation - usable - handle_in - handle_out
        trim_start, trim_end = _distribute_trim(
            handle_in, handle_out, slack, capability, segment_sequence, index
        )
        if generation != usable:
            adjustments.append(
                TimingAdjustment(
                    segment_sequence=segment_sequence,
                    proposal_sequence=interval.proposal_sequence,
                    shot_sequence=index,
                    kind="generation_round_up",
                    proposed_duration_us=usable,
                    canonical_duration_us=generation,
                    delta_us=generation - usable,
                    reason=(
                        "the exact usable duration is not a supported generated duration; the "
                        "next supported duration is requested and deterministically trimmed"
                    ),
                )
            )
            adjustments.append(
                TimingAdjustment(
                    segment_sequence=segment_sequence,
                    proposal_sequence=interval.proposal_sequence,
                    shot_sequence=index,
                    kind="trim",
                    proposed_duration_us=generation,
                    canonical_duration_us=usable,
                    delta_us=usable - generation,
                    reason=(
                        f"trim {trim_start} us from the head and {trim_end} us from the tail "
                        f"under the {capability.trimming_policy} policy"
                    ),
                )
            )
        shots.append(
            ShotTiming(
                segment_sequence=segment_sequence,
                shot_sequence=index,
                proposal_sequence=interval.proposal_sequence,
                start_us=interval.start_us,
                end_us=interval.end_us,
                usable_duration_us=usable,
                requested_generation_duration_us=generation,
                trim_start_us=trim_start,
                trim_end_us=trim_end,
                transition_handle_us=handle_in + handle_out,
                word_start_index=interval.word_start_index,
                word_end_index=interval.word_end_index,
                clause_label=interval.clause_label,
            )
        )
    return shots


def _fit_generation(
    usable: int,
    handle_in: int,
    handle_out: int,
    capability: VisualProviderCapability,
    segment_sequence: int,
    shot_sequence: int,
) -> tuple[int, int, int]:
    """Shrink transition handles, never narration, until a duration is supported."""
    while True:
        generation = select_generation_duration(usable + handle_in + handle_out, capability)
        if generation is not None:
            return handle_in, handle_out, generation
        if handle_out > 0:
            handle_out = 0
            continue
        if handle_in > 0:
            handle_in = 0
            continue
        raise RetimerError(
            _diagnostic(
                "unsupported_provider_duration",
                f"usable duration {usable} us exceeds the maximum generated duration "
                f"{capability.max_generation_duration_us} us supported by capability profile "
                f"{capability.capability_profile_id!r}",
                segment_sequence=segment_sequence,
                shot_sequence=shot_sequence,
                measured_us=usable,
                expected_us=capability.max_generation_duration_us,
            )
        )


def _distribute_trim(
    handle_in: int,
    handle_out: int,
    slack: int,
    capability: VisualProviderCapability,
    segment_sequence: int,
    shot_sequence: int,
) -> tuple[int, int]:
    if capability.trimming_policy == "none":
        if slack:
            raise RetimerError(
                _diagnostic(
                    "unsupported_provider_duration",
                    "capability profile forbids trimming but the usable duration is not an "
                    "exactly supported generated duration",
                    segment_sequence=segment_sequence,
                    shot_sequence=shot_sequence,
                    measured_us=slack,
                    repairable=False,
                )
            )
        return handle_in, handle_out
    if capability.trimming_policy == "trim_start":
        return handle_in + slack, handle_out
    if capability.trimming_policy == "trim_center":
        head = slack // 2
        return handle_in + head, handle_out + (slack - head)
    return handle_in, handle_out + slack


def format_us(microseconds: int) -> str:
    """Render exact microseconds for human-facing output without float drift."""
    whole, fraction = divmod(microseconds, MICROSECONDS_PER_SECOND)
    return f"{whole}.{fraction:06d}"
