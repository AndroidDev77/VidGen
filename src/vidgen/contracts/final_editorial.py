"""Strict, versioned T22 final editorial-QA contracts.

T22 inspects the *assembled* recap: the canonical T17 final render, its manifest,
the delivered captions and the final audio mix. It is the last gate before a
project may complete, so these contracts are shaped by four rules:

* Deterministic truth outranks semantic opinion. A decode failure, a stale
  lineage, a missing caption cue or a drifting audio track is recorded as a
  :class:`FinalDeterministicCheck`, :class:`FinalAudioCheck` or
  :class:`FinalCaptionCheck` with ``blocking=True``, and no provider score,
  averaged dimension or human decision can turn it into a ``PASS``.
* A provider never supplies the canonical decision. It proposes findings;
  application code recomputes the gate from validated, bounded results.
* Every actionable finding carries exact evidence: a global timestamp range and
  the shot, narration segment, caption cue or sampled frame it refers to, so a
  remediation route always points somewhere real.
* Nothing here may carry credentials, signed URLs, media bytes, unrestricted
  provider payloads or unbounded model prose. Provider metadata stays in the
  provider models and is never merged into the canonical report.

T22 identifies and gates. It never performs a paid generation call and never
runs another creative repair loop; it emits a bounded
:class:`FinalRemediationTarget` that an existing stage owns.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from vidgen.contracts.common import StrictContract

CONTRACT_VERSION = "final-editorial/1.0"

Sha256 = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Confidence = Annotated[float, Field(ge=0, le=1)]
Microseconds = Annotated[int, Field(ge=0)]

#: Terra only decides when it is at least this sure. Anything less is ``REVIEW``.
ADJUDICATION_CONFIDENCE_FLOOR = 0.80


class FinalQAPhase(StrEnum):
    """The restartable phases. Each one checkpoints and is reused when clean."""

    INPUT_VALIDATION = "INPUT_VALIDATION"
    DETERMINISTIC_MEDIA_QA = "DETERMINISTIC_MEDIA_QA"
    CAPTION_QA = "CAPTION_QA"
    EDITORIAL_ANALYSIS = "EDITORIAL_ANALYSIS"
    ADJUDICATION = "ADJUDICATION"
    COMPLETION_GATE = "COMPLETION_GATE"


#: Canonical phase order. A phase never runs before its predecessor completed.
PHASE_ORDER: tuple[FinalQAPhase, ...] = (
    FinalQAPhase.INPUT_VALIDATION,
    FinalQAPhase.DETERMINISTIC_MEDIA_QA,
    FinalQAPhase.CAPTION_QA,
    FinalQAPhase.EDITORIAL_ANALYSIS,
    FinalQAPhase.ADJUDICATION,
    FinalQAPhase.COMPLETION_GATE,
)


class FinalQAStatus(StrEnum):
    """Workflow-visible run status, mirrored by the Temporal project workflow."""

    FINAL_QA_QUEUED = "FINAL_QA_QUEUED"
    FINAL_QA_VALIDATING_INPUTS = "FINAL_QA_VALIDATING_INPUTS"
    FINAL_QA_CHECKING_MEDIA = "FINAL_QA_CHECKING_MEDIA"
    FINAL_QA_CHECKING_CAPTIONS = "FINAL_QA_CHECKING_CAPTIONS"
    FINAL_QA_ANALYZING = "FINAL_QA_ANALYZING"
    FINAL_QA_ADJUDICATING = "FINAL_QA_ADJUDICATING"
    FINAL_QA_REVIEW_REQUIRED = "FINAL_QA_REVIEW_REQUIRED"
    FINAL_QA_PASSED = "FINAL_QA_PASSED"
    FINAL_QA_FAILED = "FINAL_QA_FAILED"


#: The statuses from which no further work happens without a new run.
TERMINAL_STATUSES: frozenset[FinalQAStatus] = frozenset(
    {
        FinalQAStatus.FINAL_QA_REVIEW_REQUIRED,
        FinalQAStatus.FINAL_QA_PASSED,
        FinalQAStatus.FINAL_QA_FAILED,
    }
)


class FinalQADecision(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    REVIEW = "REVIEW"


class FinalFindingSeverity(StrEnum):
    """Severity is structural, never derived from an average score."""

    BLOCKING = "blocking"
    REVIEW_REQUIRED = "review_required"
    WARNING = "warning"
    INFORMATIONAL = "informational"


#: Highest first. Truncation always preserves the most severe findings.
SEVERITY_ORDER: dict[FinalFindingSeverity, int] = {
    FinalFindingSeverity.BLOCKING: 0,
    FinalFindingSeverity.REVIEW_REQUIRED: 1,
    FinalFindingSeverity.WARNING: 2,
    FinalFindingSeverity.INFORMATIONAL: 3,
}


class FinalCheckType(StrEnum):
    """Which deterministic family produced a persisted check row."""

    LINEAGE = "lineage"
    MEDIA = "media"
    TIMELINE = "timeline"
    AUDIO = "audio"
    CAPTION = "caption"
    MANIFEST = "manifest"


class FinalEditorialCategory(StrEnum):
    """The bounded editorial dimensions T22 evaluates on the assembled recap."""

    STORY_BEAT_COVERAGE = "story_beat_coverage"
    NARRATIVE_STRUCTURE = "narrative_structure"
    SCENE_COMPLETENESS = "scene_completeness"
    CHARACTER_IDENTITY_CONTINUITY = "character_identity_continuity"
    CHARACTER_STATE_CONTINUITY = "character_state_continuity"
    LOCATION_CONTINUITY = "location_continuity"
    PROP_AND_WARDROBE_CONTINUITY = "prop_and_wardrobe_continuity"
    VISUAL_CONTRADICTION = "visual_contradiction"
    SHOT_TO_SHOT_CONTINUITY = "shot_to_shot_continuity"
    TRANSITION_COHERENCE = "transition_coherence"
    NARRATION_VISUAL_AGREEMENT = "narration_visual_agreement"
    CAPTION_NARRATION_AGREEMENT = "caption_narration_agreement"
    COMPREHENSIBILITY = "comprehensibility"
    SETUP_AND_PAYOFF = "setup_and_payoff"
    NARRATIVE_JUMP = "narrative_jump"
    REPETITION = "repetition"
    PACING = "pacing"
    DEAD_AIR = "dead_air"
    ENDING_COMPLETENESS = "ending_completeness"
    SCRIPT_CONTRADICTION = "script_contradiction"
    SOURCE_CONTRADICTION = "source_contradiction"


class FinalIssueCode(StrEnum):
    """The bounded structured issue taxonomy. Adding a member is a version change."""

    # Deterministic media
    RENDER_MISSING = "RENDER_MISSING"
    RENDER_EMPTY = "RENDER_EMPTY"
    RENDER_UNREADABLE = "RENDER_UNREADABLE"
    CONTAINER_MISMATCH = "CONTAINER_MISMATCH"
    VIDEO_CODEC_MISMATCH = "VIDEO_CODEC_MISMATCH"
    AUDIO_CODEC_MISMATCH = "AUDIO_CODEC_MISMATCH"
    VIDEO_DECODE_FAILURE = "VIDEO_DECODE_FAILURE"
    AUDIO_DECODE_FAILURE = "AUDIO_DECODE_FAILURE"
    MISSING_VIDEO_STREAM = "MISSING_VIDEO_STREAM"
    MISSING_AUDIO_STREAM = "MISSING_AUDIO_STREAM"
    UNEXPECTED_STREAM = "UNEXPECTED_STREAM"
    RESOLUTION_MISMATCH = "RESOLUTION_MISMATCH"
    PIXEL_FORMAT_MISMATCH = "PIXEL_FORMAT_MISMATCH"
    FRAME_RATE_INVALID = "FRAME_RATE_INVALID"
    TIME_BASE_INVALID = "TIME_BASE_INVALID"
    DURATION_INVALID = "DURATION_INVALID"
    VIDEO_DURATION_MISMATCH = "VIDEO_DURATION_MISMATCH"
    AUDIO_DURATION_MISMATCH = "AUDIO_DURATION_MISMATCH"
    AV_DURATION_DRIFT = "AV_DURATION_DRIFT"
    TIMELINE_DURATION_MISMATCH = "TIMELINE_DURATION_MISMATCH"
    SHOT_COVERAGE_GAP = "SHOT_COVERAGE_GAP"
    SHOT_COVERAGE_OVERLAP = "SHOT_COVERAGE_OVERLAP"
    TRANSITION_HANDLE_MISMATCH = "TRANSITION_HANDLE_MISMATCH"
    UNEXPECTED_BLACK_INTERVAL = "UNEXPECTED_BLACK_INTERVAL"
    EXCESSIVE_FREEZE_INTERVAL = "EXCESSIVE_FREEZE_INTERVAL"
    CORRUPT_RENDER_SECTION = "CORRUPT_RENDER_SECTION"
    INVALID_BOUNDARY_FRAME = "INVALID_BOUNDARY_FRAME"
    NON_MONOTONIC_TIMESTAMPS = "NON_MONOTONIC_TIMESTAMPS"
    START_OFFSET_OUT_OF_RANGE = "START_OFFSET_OUT_OF_RANGE"
    NON_FINITE_MEASUREMENT = "NON_FINITE_MEASUREMENT"
    FILE_SIZE_OUT_OF_RANGE = "FILE_SIZE_OUT_OF_RANGE"
    BITRATE_OUT_OF_RANGE = "BITRATE_OUT_OF_RANGE"

    # Audio mix
    NARRATION_INTERVAL_MISSING = "NARRATION_INTERVAL_MISSING"
    NARRATION_ORDER_MISMATCH = "NARRATION_ORDER_MISMATCH"
    NARRATION_SEGMENT_DUPLICATED = "NARRATION_SEGMENT_DUPLICATED"
    NARRATION_SEGMENT_OMITTED = "NARRATION_SEGMENT_OMITTED"
    NARRATION_TIMING_DRIFT = "NARRATION_TIMING_DRIFT"
    AUDIO_VIDEO_DRIFT = "AUDIO_VIDEO_DRIFT"
    LOUDNESS_OUT_OF_RANGE = "LOUDNESS_OUT_OF_RANGE"
    TRUE_PEAK_EXCEEDED = "TRUE_PEAK_EXCEEDED"
    AUDIO_CLIPPING = "AUDIO_CLIPPING"
    NON_FINITE_SAMPLE = "NON_FINITE_SAMPLE"
    LEADING_SILENCE_OUT_OF_RANGE = "LEADING_SILENCE_OUT_OF_RANGE"
    TRAILING_SILENCE_OUT_OF_RANGE = "TRAILING_SILENCE_OUT_OF_RANGE"
    ABNORMAL_INTERNAL_SILENCE = "ABNORMAL_INTERNAL_SILENCE"
    NARRATION_MASKED_BY_BED = "NARRATION_MASKED_BY_BED"
    CHANNEL_LAYOUT_MISMATCH = "CHANNEL_LAYOUT_MISMATCH"
    SAMPLE_RATE_MISMATCH = "SAMPLE_RATE_MISMATCH"
    AUDIO_DISCONTINUITY = "AUDIO_DISCONTINUITY"
    BACKGROUND_AUDIO_OVERRUN = "BACKGROUND_AUDIO_OVERRUN"
    UNAPPROVED_PROVIDER_AUDIO = "UNAPPROVED_PROVIDER_AUDIO"

    # Captions
    CAPTION_COVERAGE_MISSING = "CAPTION_COVERAGE_MISSING"
    CAPTION_TEXT_MISMATCH = "CAPTION_TEXT_MISMATCH"
    CAPTION_ORDER_INVALID = "CAPTION_ORDER_INVALID"
    CAPTION_NEGATIVE_START = "CAPTION_NEGATIVE_START"
    CAPTION_NONPOSITIVE_DURATION = "CAPTION_NONPOSITIVE_DURATION"
    CAPTION_OUT_OF_BOUNDS = "CAPTION_OUT_OF_BOUNDS"
    CAPTION_OVERLAP = "CAPTION_OVERLAP"
    CAPTION_TIMING_DRIFT = "CAPTION_TIMING_DRIFT"
    CAPTION_CUE_MISSING = "CAPTION_CUE_MISSING"
    CAPTION_CUE_DUPLICATED = "CAPTION_CUE_DUPLICATED"
    CAPTION_ASSET_HASH_MISMATCH = "CAPTION_ASSET_HASH_MISMATCH"
    CAPTION_LINE_COUNT_EXCEEDED = "CAPTION_LINE_COUNT_EXCEEDED"
    CAPTION_LINE_LENGTH_EXCEEDED = "CAPTION_LINE_LENGTH_EXCEEDED"
    CAPTION_READING_SPEED_EXCEEDED = "CAPTION_READING_SPEED_EXCEEDED"
    CAPTION_REFLOW_NONDETERMINISTIC = "CAPTION_REFLOW_NONDETERMINISTIC"
    CAPTION_PUNCTUATION_ALTERED = "CAPTION_PUNCTUATION_ALTERED"
    CAPTION_ENCODING_INVALID = "CAPTION_ENCODING_INVALID"
    CAPTION_PARSE_FAILURE = "CAPTION_PARSE_FAILURE"
    CAPTION_SAFE_AREA_VIOLATION = "CAPTION_SAFE_AREA_VIOLATION"
    CAPTION_LANGUAGE_MISMATCH = "CAPTION_LANGUAGE_MISMATCH"

    # Editorial
    MISSING_STORY_BEAT = "MISSING_STORY_BEAT"
    DUPLICATED_SCENE = "DUPLICATED_SCENE"
    MISSING_SCENE = "MISSING_SCENE"
    OUT_OF_ORDER_SCENE = "OUT_OF_ORDER_SCENE"
    CONTINUITY_CONTRADICTION = "CONTINUITY_CONTRADICTION"
    IDENTITY_CONTRADICTION = "IDENTITY_CONTRADICTION"
    LOCATION_CONTRADICTION = "LOCATION_CONTRADICTION"
    PROP_CONTRADICTION = "PROP_CONTRADICTION"
    NARRATION_VISUAL_MISMATCH = "NARRATION_VISUAL_MISMATCH"
    CAPTION_NARRATION_MISMATCH = "CAPTION_NARRATION_MISMATCH"
    INCOMPREHENSIBLE_SEQUENCE = "INCOMPREHENSIBLE_SEQUENCE"
    UNRESOLVED_SETUP = "UNRESOLVED_SETUP"
    ABRUPT_NARRATIVE_JUMP = "ABRUPT_NARRATIVE_JUMP"
    EXCESSIVE_REPETITION = "EXCESSIVE_REPETITION"
    PACING_PROBLEM = "PACING_PROBLEM"
    UNINTENDED_DEAD_AIR = "UNINTENDED_DEAD_AIR"
    INCOMPLETE_ENDING = "INCOMPLETE_ENDING"
    SCRIPT_CONTRADICTION = "SCRIPT_CONTRADICTION"
    SOURCE_CONTRADICTION = "SOURCE_CONTRADICTION"
    AMBIGUOUS_EDITORIAL_EVIDENCE = "AMBIGUOUS_EDITORIAL_EVIDENCE"


class FinalRemediationTarget(StrEnum):
    """Where a confirmed issue is routed. T22 never executes the route itself."""

    NONE = "NONE"
    RERENDER_T17 = "RERENDER_T17"
    REBUILD_CAPTIONS_T17 = "REBUILD_CAPTIONS_T17"
    REMIX_AUDIO_T17 = "REMIX_AUDIO_T17"
    REGENERATE_SHOT_T16 = "REGENERATE_SHOT_T16"
    REPAIR_SHOT_T21 = "REPAIR_SHOT_T21"
    CORRECT_REFERENCE_T19 = "CORRECT_REFERENCE_T19"
    CORRECT_SCRIPT_UPSTREAM = "CORRECT_SCRIPT_UPSTREAM"
    HUMAN_EDITORIAL_REVIEW = "HUMAN_EDITORIAL_REVIEW"


class FinalQAFailureCode(StrEnum):
    """Structural failures raised before any paid provider request."""

    PROJECT_NOT_FOUND = "project_not_found"
    RENDER_NOT_SELECTED = "render_not_selected"
    RENDER_INCOMPLETE = "render_incomplete"
    RENDER_ASSET_MISSING = "render_asset_missing"
    RENDER_MANIFEST_MISSING = "render_manifest_missing"
    RENDER_MANIFEST_INVALID = "render_manifest_invalid"
    CROSS_PROJECT_ASSET = "cross_project_asset"
    STALE_RENDER_LINEAGE = "stale_render_lineage"
    STALE_SHOT_SELECTION = "stale_shot_selection"
    MISSING_UPSTREAM_OUTPUT = "missing_upstream_output"
    MISSING_VIDEO_QA_RESULT = "missing_video_qa_result"
    FAILING_VIDEO_QA_RESULT = "failing_video_qa_result"
    UNRESOLVED_REPAIR_REVIEW = "unresolved_repair_review"
    ACTIVE_REPAIR_RUN = "active_repair_run"
    FAILED_REPAIR_RUN = "failed_repair_run"
    NARRATION_HASH_MISMATCH = "narration_hash_mismatch"
    STORYBOARD_HASH_MISMATCH = "storyboard_hash_mismatch"
    CAPTION_HASH_MISMATCH = "caption_hash_mismatch"
    RENDER_HASH_MISMATCH = "render_hash_mismatch"
    SHOT_ORDER_MISMATCH = "shot_order_mismatch"
    ASSET_REFERENCE_MISMATCH = "asset_reference_mismatch"
    INCOMPLETE_FINAL_ASSET = "incomplete_final_asset"
    IDENTITY_CONFLICT = "identity_conflict"
    UNVALIDATED_PROVIDER_OUTPUT = "unvalidated_provider_output"
    BUDGET_DENIED = "budget_denied"


class FinalQAFailure(StrictContract):
    """A structured, non-retryable T22 lineage or configuration failure."""

    schema_version: Literal["1.0"] = "1.0"
    code: FinalQAFailureCode
    message: str = Field(min_length=1, max_length=500)
    retryable: bool = False
    reference_id: UUID | None = None


class FinalQAConfiguration(StrictContract):
    """Versioned thresholds. Every value here is bound into the final-QA identity.

    Changing any threshold is a configuration change, which produces a new
    identity and therefore a new run rather than silently re-grading an old one.
    """

    schema_version: Literal["1.0"] = "1.0"
    configuration_version: str = Field(min_length=1, max_length=64)
    deterministic_check_version: str = Field(min_length=1, max_length=64)
    audio_check_version: str = Field(min_length=1, max_length=64)
    caption_check_version: str = Field(min_length=1, max_length=64)
    editorial_rubric_version: str = Field(min_length=1, max_length=64)
    prompt_version: str = Field(min_length=1, max_length=64)
    adjudication_policy_version: str = Field(min_length=1, max_length=64)

    # Delivery profile
    expected_container: str = Field(default="mp4", min_length=1, max_length=32)
    expected_video_codec: str = Field(default="h264", min_length=1, max_length=32)
    expected_audio_codec: str = Field(default="aac", min_length=1, max_length=32)
    expected_width: int = Field(default=1920, gt=0, le=16384)
    expected_height: int = Field(default=1080, gt=0, le=16384)
    expected_pixel_format: str = Field(default="yuv420p", min_length=1, max_length=32)
    expected_frame_rate: int = Field(default=24, gt=0, le=240)
    expected_sample_rate_hz: int = Field(default=48000, gt=0)
    expected_channels: int = Field(default=2, ge=1, le=8)
    expected_caption_language: str = Field(default="en", pattern=r"^[a-z]{2,3}(-[A-Z]{2})?$")

    # Media tolerances
    # The fps=24 filter rounds each shot's output to a frame boundary, so the
    # cumulative duration drift across N shots is up to N × 41 667 µs.  For a
    # 20-shot project that is ≈833 ms.  The defaults here are widened to 1 s
    # so that frame-aligned renders pass T22 without requiring a re-encode.
    duration_tolerance_us: int = Field(default=1_000_000, ge=0, le=2_000_000)
    av_drift_tolerance_us: int = Field(default=1_000_000, ge=0, le=2_000_000)
    start_offset_tolerance_us: int = Field(default=50_000, ge=0, le=1_000_000)
    max_black_interval_us: int = Field(default=500_000, ge=0, le=10_000_000)
    max_freeze_interval_us: int = Field(default=2_000_000, ge=0, le=30_000_000)
    max_bytes_per_second: int = Field(default=3_500_000, gt=0)
    min_bytes_per_second: int = Field(default=8_000, gt=0)

    # Audio thresholds
    target_integrated_lufs: float = Field(default=-14.0, ge=-30, le=-5)
    loudness_tolerance_lu: float = Field(default=1.5, ge=0, le=6)
    true_peak_ceiling_dbtp: float = Field(default=-1.0, ge=-6, le=0)
    max_clipping_ratio: float = Field(default=0.0005, ge=0, le=1)
    max_leading_silence_us: int = Field(default=1_500_000, ge=0, le=30_000_000)
    max_trailing_silence_us: int = Field(default=2_500_000, ge=0, le=30_000_000)
    max_internal_silence_us: int = Field(default=2_000_000, ge=0, le=30_000_000)
    narration_timing_tolerance_us: int = Field(default=1_000_000, ge=0, le=5_000_000)
    min_narration_headroom_db: float = Field(default=6.0, ge=0, le=40)

    # Caption thresholds
    max_caption_lines: int = Field(default=2, ge=1, le=4)
    max_caption_line_characters: int = Field(default=42, ge=10, le=120)
    max_caption_reading_speed_cps: float = Field(default=26.0, gt=0, le=60)
    caption_timing_tolerance_us: int = Field(default=1_000_000, ge=0, le=5_000_000)
    caption_safe_area_percent: int = Field(default=10, ge=0, le=30)

    # Sampling and bounds
    editorial_sample_count: int = Field(default=24, ge=1, le=64)
    contact_sheet_columns: int = Field(default=6, ge=1, le=8)
    max_findings: int = Field(default=64, ge=1, le=256)
    max_adjudications: int = Field(default=8, ge=0, le=32)

    @model_validator(mode="after")
    def coherent_bounds(self) -> FinalQAConfiguration:
        if self.min_bytes_per_second >= self.max_bytes_per_second:
            raise ValueError("bitrate bounds must be ordered")
        return self


class FinalSelectedShot(StrictContract):
    """One selected shot as the render manifest recorded it, with its QA lineage."""

    schema_version: Literal["1.0"] = "1.0"
    shot_id: UUID
    sequence: int = Field(ge=0)
    video_asset_id: UUID
    video_sha256: Sha256
    global_start_us: Microseconds
    global_end_us: int = Field(gt=0)
    shot_workflow_identity: Sha256
    video_qa_run_id: UUID
    video_qa_result_id: UUID
    repair_run_id: UUID | None = None
    selected_repair_attempt_id: UUID | None = None

    @model_validator(mode="after")
    def ordered_interval(self) -> FinalSelectedShot:
        if self.global_end_us <= self.global_start_us:
            raise ValueError("shot interval end must follow start")
        return self


class FinalQAInput(StrictContract):
    """The exact canonical inputs one final-QA run was bound to.

    Every field is part of the input hash. A newer selected animation, a new
    render, a rebuilt caption asset or a different approved script produces a
    different hash and therefore a different run: a report is never reused for
    material that changed.
    """

    schema_version: Literal["1.0"] = "1.0"
    project_id: UUID
    render_job_id: UUID
    render_identity: Sha256
    final_video_asset_id: UUID
    final_video_sha256: Sha256
    render_manifest_asset_id: UUID
    render_manifest_hash: Sha256
    approved_script_id: UUID
    approved_script_version: int = Field(gt=0)
    approved_script_hash: Sha256
    narration_run_id: UUID
    narration_asset_ids: list[UUID] = Field(min_length=1, max_length=1000)
    narration_word_timing_hash: Sha256
    narration_duration_us: int = Field(gt=0)
    storyboard_run_id: UUID
    storyboard_hash: Sha256
    timing_manifest_hash: Sha256
    caption_track_id: UUID
    caption_identity: Sha256
    caption_asset_ids: list[UUID] = Field(min_length=1, max_length=8)
    caption_asset_hashes: list[Sha256] = Field(min_length=1, max_length=8)
    final_audio_asset_id: UUID | None = None
    shots: list[FinalSelectedShot] = Field(min_length=1, max_length=500)
    character_identity_version_ids: list[UUID] = Field(default_factory=list, max_length=256)
    location_identity_version_ids: list[UUID] = Field(default_factory=list, max_length=256)
    subtitle_mode: Literal["selectable", "burn_in", "both"] = "selectable"
    timeline_duration_us: int = Field(gt=0)

    @model_validator(mode="after")
    def dense_contiguous_shots(self) -> FinalQAInput:
        if len(self.caption_asset_ids) != len(self.caption_asset_hashes):
            raise ValueError("every caption asset must carry its hash")
        for index, shot in enumerate(self.shots):
            if shot.sequence != index:
                raise ValueError("selected shot sequences must be dense")
            expected = 0 if index == 0 else self.shots[index - 1].global_end_us
            if shot.global_start_us != expected:
                raise ValueError("selected shot coverage contains a gap or overlap")
        if self.shots[-1].global_end_us != self.timeline_duration_us:
            raise ValueError("selected shot coverage must equal the canonical timeline")
        return self


class FinalMediaMeasurements(StrictContract):
    """Every measured property of the assembled render, with its tool versions."""

    schema_version: Literal["1.0"] = "1.0"
    measured_at: datetime
    ffmpeg_version: str = Field(default="", max_length=256)
    ffprobe_version: str = Field(default="", max_length=256)
    container_format: str = Field(default="", max_length=128)
    byte_size: int = Field(ge=0)
    bit_rate: float | None = Field(default=None, ge=0)
    video_codec: str = Field(default="", max_length=64)
    audio_codec: str = Field(default="", max_length=64)
    width: int | None = Field(default=None, gt=0)
    height: int | None = Field(default=None, gt=0)
    pixel_format: str = Field(default="", max_length=32)
    frame_rate: str = Field(default="", max_length=32)
    video_time_base: str = Field(default="", max_length=32)
    audio_time_base: str = Field(default="", max_length=32)
    container_duration_us: int | None = Field(default=None, ge=0)
    video_duration_us: int | None = Field(default=None, ge=0)
    audio_duration_us: int | None = Field(default=None, ge=0)
    video_start_us: int = 0
    audio_start_us: int = 0
    sample_rate_hz: int | None = Field(default=None, gt=0)
    channels: int | None = Field(default=None, gt=0)
    subtitle_stream_count: int = Field(default=0, ge=0)
    video_decoded: bool = False
    audio_decoded: bool = False
    monotonic_video_timestamps: bool = True
    first_frame_valid: bool = False
    last_frame_valid: bool = False
    black_intervals: list[dict[str, int]] = Field(default_factory=list, max_length=512)
    freeze_intervals: list[dict[str, int]] = Field(default_factory=list, max_length=512)
    silence_intervals: list[dict[str, int]] = Field(default_factory=list, max_length=512)
    integrated_lufs: float | None = None
    true_peak_dbtp: float | None = None
    loudness_range: float | None = None
    clipping_ratio: float | None = Field(default=None, ge=0, le=1)
    decode_error_count: int = Field(default=0, ge=0)

    @field_validator(
        "bit_rate",
        "integrated_lufs",
        "true_peak_dbtp",
        "loudness_range",
        "clipping_ratio",
    )
    @classmethod
    def finite(cls, value: float | None) -> float | None:
        if value is not None and (value != value or value in {float("inf"), float("-inf")}):
            raise ValueError("media measurements must be finite")
        return value


class FinalEditorialEvidence(StrictContract):
    """Exact, resolvable evidence for one finding. Never bytes, never a URL."""

    schema_version: Literal["1.0"] = "1.0"
    evidence_id: UUID
    evidence_type: Literal[
        "sampled_frame",
        "contact_sheet_tile",
        "deterministic_measurement",
        "caption_cue",
        "audio_interval",
        "manifest_reference",
        "whole_file",
    ]
    start_us: Microseconds
    end_us: Microseconds
    frame_asset_id: UUID | None = None
    sample_id: UUID | None = None
    contact_sheet_asset_id: UUID | None = None
    contact_sheet_position: int | None = Field(default=None, ge=0)
    caption_cue_sequence: int | None = Field(default=None, ge=1)
    shot_id: UUID | None = None
    measurement: float | None = None
    threshold: float | None = None
    tool: str = Field(default="", max_length=64)
    tool_version: str = Field(default="", max_length=128)
    explanation: str = Field(default="", max_length=500)

    @field_validator("measurement", "threshold")
    @classmethod
    def finite(cls, value: float | None) -> float | None:
        if value is not None and (value != value or value in {float("inf"), float("-inf")}):
            raise ValueError("evidence measurements must be finite")
        return value

    @model_validator(mode="after")
    def ordered_range(self) -> FinalEditorialEvidence:
        if self.end_us < self.start_us:
            raise ValueError("evidence end must not precede its start")
        return self


class FinalEditorialFinding(StrictContract):
    """One canonical finding. Provider prose never reaches this model unbounded."""

    schema_version: Literal["1.0"] = "1.0"
    finding_id: UUID
    category: FinalEditorialCategory
    severity: FinalFindingSeverity
    blocking: bool
    confidence: Confidence
    issue_code: FinalIssueCode
    summary: str = Field(min_length=1, max_length=500)
    start_us: Microseconds
    end_us: Microseconds
    shot_ids: list[UUID] = Field(default_factory=list, max_length=64)
    script_segment_ids: list[UUID] = Field(default_factory=list, max_length=64)
    narration_segment_ids: list[UUID] = Field(default_factory=list, max_length=64)
    caption_cue_sequences: list[int] = Field(default_factory=list, max_length=64)
    sample_ids: list[UUID] = Field(default_factory=list, max_length=64)
    evidence: list[FinalEditorialEvidence] = Field(default_factory=list, max_length=16)
    expected_behavior: str = Field(default="", max_length=500)
    observed_behavior: str = Field(default="", max_length=500)
    remediation_target: FinalRemediationTarget = FinalRemediationTarget.NONE
    source_check: str = Field(default="", max_length=64)
    provider_attempt_number: int | None = Field(default=None, ge=1)
    provenance: Literal["deterministic", "provider", "adjudication", "human"] = "deterministic"

    @model_validator(mode="after")
    def blocking_findings_are_evidenced(self) -> FinalEditorialFinding:
        if self.end_us < self.start_us:
            raise ValueError("finding end must not precede its start")
        if self.blocking and self.severity is not FinalFindingSeverity.BLOCKING:
            raise ValueError("a blocking finding must carry blocking severity")
        if self.severity is FinalFindingSeverity.BLOCKING and not self.blocking:
            raise ValueError("blocking severity must set the blocking flag")
        if self.blocking and not self.evidence:
            raise ValueError("a blocking finding must carry evidence")
        return self


class FinalDeterministicCheck(StrictContract):
    """One deterministic media or timeline check with its measurement."""

    schema_version: Literal["1.0"] = "1.0"
    check_id: UUID
    check_type: FinalCheckType
    check_version: str = Field(min_length=1, max_length=64)
    code: FinalIssueCode
    status: Literal["pass", "fail", "warning", "not_applicable"]
    blocking: bool = False
    measurement: float | None = None
    threshold: float | None = None
    unit: str = Field(default="", max_length=32)
    start_us: Microseconds | None = None
    end_us: Microseconds | None = None
    tool: str = Field(default="", max_length=64)
    tool_version: str = Field(default="", max_length=128)
    message: str = Field(default="", max_length=500)

    @field_validator("measurement", "threshold")
    @classmethod
    def finite(cls, value: float | None) -> float | None:
        if value is not None and (value != value or value in {float("inf"), float("-inf")}):
            raise ValueError("check measurements must be finite")
        return value

    @model_validator(mode="after")
    def failures_block(self) -> FinalDeterministicCheck:
        if self.status == "fail" and not self.blocking:
            raise ValueError("a failed deterministic check is always blocking")
        if self.status != "fail" and self.blocking:
            raise ValueError("only a failed deterministic check may be blocking")
        return self


class FinalAudioCheck(FinalDeterministicCheck):
    """A deterministic audio-mix check, located against the global timeline."""

    check_type: Literal[FinalCheckType.AUDIO] = FinalCheckType.AUDIO
    narration_segment_id: UUID | None = None


class FinalCaptionCheck(FinalDeterministicCheck):
    """A deterministic caption check, located against a cue and its narration."""

    check_type: Literal[FinalCheckType.CAPTION] = FinalCheckType.CAPTION
    cue_sequence: int | None = Field(default=None, ge=1)
    narration_segment_id: UUID | None = None
    caption_asset_id: UUID | None = None
    remediation_target: FinalRemediationTarget = FinalRemediationTarget.REBUILD_CAPTIONS_T17


class FinalEditorialDimension(StrictContract):
    """One recomputed editorial dimension result.

    ``score`` is descriptive only. The gate reads findings, never this number:
    a high average must never conceal a blocking issue.
    """

    schema_version: Literal["1.0"] = "1.0"
    category: FinalEditorialCategory
    applicable: bool = True
    score: float = Field(ge=0, le=100)
    confidence: Confidence
    blocking_finding_count: int = Field(default=0, ge=0)
    review_finding_count: int = Field(default=0, ge=0)
    warning_finding_count: int = Field(default=0, ge=0)
    summary: str = Field(default="", max_length=500)


class FinalEditorialProviderRequest(StrictContract):
    """Exactly the bounded material one editorial evaluation may see."""

    schema_version: Literal["1.0"] = "1.0"
    final_qa_identity: Sha256
    attempt_identity: Sha256
    attempt_type: Literal["first_pass", "adjudication"]
    attempt_number: int = Field(ge=1)
    project_id: UUID
    final_video_asset_id: UUID
    render_identity: Sha256
    timeline_duration_us: int = Field(gt=0)
    script_structure: list[str] = Field(default_factory=list, max_length=128)
    plot_beats: list[str] = Field(default_factory=list, max_length=128)
    storyboard_timing_summary: list[str] = Field(default_factory=list, max_length=500)
    shot_map: list[str] = Field(default_factory=list, max_length=500)
    transition_map: list[str] = Field(default_factory=list, max_length=500)
    continuity_summary: list[str] = Field(default_factory=list, max_length=256)
    video_qa_summary: list[str] = Field(default_factory=list, max_length=500)
    repair_summary: list[str] = Field(default_factory=list, max_length=500)
    caption_timing_summary: list[str] = Field(default_factory=list, max_length=500)
    audio_measurements: dict[str, float] = Field(default_factory=dict)
    samples: list[FinalEditorialEvidence] = Field(default_factory=list, max_length=64)
    contact_sheet_asset_id: UUID | None = None
    review_proxy_asset_id: UUID | None = None
    disputed_findings: list[str] = Field(default_factory=list, max_length=32)
    rubric_version: str = Field(min_length=1, max_length=64)
    prompt_version: str = Field(min_length=1, max_length=64)
    trace_context: dict[str, str] = Field(default_factory=dict)

    @field_validator("audio_measurements")
    @classmethod
    def finite_measurements(cls, value: dict[str, float]) -> dict[str, float]:
        for name, measurement in value.items():
            if measurement != measurement or measurement in {float("inf"), float("-inf")}:
                raise ValueError(f"audio measurement {name} is not finite")
        return value


class FinalEditorialProviderFinding(StrictContract):
    """A provider proposal. Never canonical until application code validates it."""

    schema_version: Literal["1.0"] = "1.0"
    category: FinalEditorialCategory
    issue_code: FinalIssueCode
    proposed_severity: FinalFindingSeverity
    confidence: Confidence
    summary: str = Field(min_length=1, max_length=500)
    start_us: Microseconds
    end_us: Microseconds
    shot_ids: list[UUID] = Field(default_factory=list, max_length=32)
    sample_ids: list[UUID] = Field(default_factory=list, max_length=32)
    caption_cue_sequences: list[int] = Field(default_factory=list, max_length=32)
    expected_behavior: str = Field(default="", max_length=500)
    observed_behavior: str = Field(default="", max_length=500)
    proposed_remediation: FinalRemediationTarget = FinalRemediationTarget.NONE

    @model_validator(mode="after")
    def ordered(self) -> FinalEditorialProviderFinding:
        if self.end_us < self.start_us:
            raise ValueError("provider finding end must not precede its start")
        return self


class FinalEditorialProviderResult(StrictContract):
    """One bounded provider reply. Provider metadata never enters the report."""

    schema_version: Literal["1.0"] = "1.0"
    attempt_identity: Sha256
    attempt_type: Literal["first_pass", "adjudication"]
    provider: str = Field(min_length=1, max_length=64)
    model: str = Field(min_length=1, max_length=128)
    provider_request_id: str | None = Field(default=None, max_length=255)
    dimension_scores: list[FinalEditorialDimension] = Field(default_factory=list, max_length=32)
    findings: list[FinalEditorialProviderFinding] = Field(default_factory=list, max_length=64)
    overall_confidence: Confidence = 1.0
    narrative_summary: str = Field(default="", max_length=1000)
    usage: dict[str, float] = Field(default_factory=dict)
    redacted_metadata: dict[str, str] = Field(default_factory=dict)


class FinalEditorialAdjudication(StrictContract):
    """One bounded Terra second opinion over disputed first-pass findings."""

    schema_version: Literal["1.0"] = "1.0"
    adjudication_id: UUID
    policy_version: str = Field(min_length=1, max_length=64)
    triggers: list[str] = Field(default_factory=list, max_length=16)
    disputed_finding_ids: list[UUID] = Field(default_factory=list, max_length=32)
    confirmed_finding_ids: list[UUID] = Field(default_factory=list, max_length=32)
    dismissed_finding_ids: list[UUID] = Field(default_factory=list, max_length=32)
    confidence: Confidence
    decided: bool
    resulting_decision_hint: FinalQADecision
    rationale: str = Field(default="", max_length=1000)
    provider: str = Field(default="", max_length=64)
    model: str = Field(default="", max_length=128)

    @model_validator(mode="after")
    def confidence_floor(self) -> FinalEditorialAdjudication:
        if self.decided and self.confidence < ADJUDICATION_CONFIDENCE_FLOOR:
            raise ValueError("an adjudicator may only decide at or above the confidence floor")
        if not self.decided and self.resulting_decision_hint is not FinalQADecision.REVIEW:
            raise ValueError("an undecided adjudication must resolve to REVIEW")
        return self


class FinalRemediationRoute(StrictContract):
    """One structured remediation route derived from confirmed findings."""

    schema_version: Literal["1.0"] = "1.0"
    target: FinalRemediationTarget
    finding_ids: list[UUID] = Field(min_length=1, max_length=64)
    shot_ids: list[UUID] = Field(default_factory=list, max_length=64)
    caption_cue_sequences: list[int] = Field(default_factory=list, max_length=64)
    reason: str = Field(min_length=1, max_length=500)
    requires_new_render: bool = True


class FinalGateDecision(StrictContract):
    """The completion gate's recomputed decision for one render identity."""

    schema_version: Literal["1.0"] = "1.0"
    gate_version: str = Field(min_length=1, max_length=64)
    decision: FinalQADecision
    final_video_asset_id: UUID
    render_identity: Sha256
    blocking_finding_count: int = Field(ge=0)
    review_finding_count: int = Field(ge=0)
    warning_finding_count: int = Field(ge=0)
    deterministic_failure_count: int = Field(ge=0)
    unresolved_review_count: int = Field(ge=0)
    reasons: list[str] = Field(default_factory=list, max_length=32)
    decided_at: datetime

    @model_validator(mode="after")
    def pass_is_clean(self) -> FinalGateDecision:
        if self.decision is FinalQADecision.PASS and (
            self.blocking_finding_count
            or self.deterministic_failure_count
            or self.unresolved_review_count
        ):
            raise ValueError("PASS requires no blocking failure and no unresolved review")
        if self.decision is FinalQADecision.FAIL and not (
            self.blocking_finding_count or self.deterministic_failure_count
        ):
            raise ValueError("FAIL requires at least one confirmed blocking issue")
        if self.decision is FinalQADecision.REVIEW and not self.unresolved_review_count:
            raise ValueError("REVIEW requires at least one unresolved review finding")
        return self


class FinalEditorialReport(StrictContract):
    """The immutable persisted report. Never overwritten, never reused."""

    schema_version: Literal["1.0"] = "1.0"
    report_version: str = Field(default=CONTRACT_VERSION, min_length=1, max_length=64)
    final_editorial_run_id: UUID
    project_id: UUID
    final_qa_identity: Sha256
    input_hash: Sha256
    configuration_hash: Sha256
    inputs: FinalQAInput
    configuration: FinalQAConfiguration
    measurements: FinalMediaMeasurements
    deterministic_checks: list[FinalDeterministicCheck] = Field(
        default_factory=list, max_length=256
    )
    audio_checks: list[FinalAudioCheck] = Field(default_factory=list, max_length=256)
    caption_checks: list[FinalCaptionCheck] = Field(default_factory=list, max_length=512)
    dimensions: list[FinalEditorialDimension] = Field(default_factory=list, max_length=32)
    findings: list[FinalEditorialFinding] = Field(default_factory=list, max_length=256)
    adjudication: FinalEditorialAdjudication | None = None
    remediation_routes: list[FinalRemediationRoute] = Field(default_factory=list, max_length=32)
    gate: FinalGateDecision
    first_pass_provider: str = Field(default="", max_length=64)
    first_pass_model: str = Field(default="", max_length=128)
    adjudicator_provider: str = Field(default="", max_length=64)
    adjudicator_model: str = Field(default="", max_length=128)
    provider_request_ids: list[str] = Field(default_factory=list, max_length=16)
    cost_microusd: int = Field(default=0, ge=0)
    tool_versions: dict[str, str] = Field(default_factory=dict)
    trace_context: dict[str, str] = Field(default_factory=dict)
    created_at: datetime

    @model_validator(mode="after")
    def gate_agrees_with_findings(self) -> FinalEditorialReport:
        blocking = sum(1 for finding in self.findings if finding.blocking)
        if self.gate.blocking_finding_count != blocking:
            raise ValueError("gate blocking count must match the recorded findings")
        deterministic = sum(
            1
            for check in (*self.deterministic_checks, *self.audio_checks, *self.caption_checks)
            if check.status == "fail"
        )
        if self.gate.deterministic_failure_count != deterministic:
            raise ValueError("gate deterministic failure count must match the recorded checks")
        if self.gate.render_identity != self.inputs.render_identity:
            raise ValueError("the gate must decide the render the report was built from")
        return self


class FinalEditorialResult(StrictContract):
    """The compact projection returned by the pipeline, CLI and activity."""

    schema_version: Literal["1.0"] = "1.0"
    final_editorial_run_id: UUID
    project_id: UUID
    final_video_asset_id: UUID
    render_manifest_asset_id: UUID
    final_qa_identity: Sha256
    input_hash: Sha256
    configuration_hash: Sha256
    status: FinalQAStatus
    phase: FinalQAPhase
    decision: FinalQADecision | None = None
    deterministic_check_count: int = Field(default=0, ge=0)
    deterministic_failure_count: int = Field(default=0, ge=0)
    audio_check_count: int = Field(default=0, ge=0)
    audio_failure_count: int = Field(default=0, ge=0)
    caption_check_count: int = Field(default=0, ge=0)
    caption_failure_count: int = Field(default=0, ge=0)
    blocking_finding_count: int = Field(default=0, ge=0)
    review_finding_count: int = Field(default=0, ge=0)
    warning_finding_count: int = Field(default=0, ge=0)
    remediation_targets: list[FinalRemediationTarget] = Field(default_factory=list, max_length=16)
    first_pass_provider: str = Field(default="", max_length=64)
    first_pass_model: str = Field(default="", max_length=128)
    adjudicated: bool = False
    adjudication_confidence: Confidence | None = None
    cost_microusd: int = Field(default=0, ge=0)
    report_asset_id: UUID | None = None
    error_code: str | None = Field(default=None, max_length=128)
    reused: bool = False
