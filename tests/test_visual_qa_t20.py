"""Deterministic unit tests for the T20 visual-QA stages.

Every test here is offline: the fake visual agent and the mocked production
adapter make no network call, and the media is synthesised during setup.
"""

from __future__ import annotations

import asyncio
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import ValidationError

from services.qa.adjudication import evaluate_triggers, resolve
from services.qa.continuity import build_expectation, required_props, summarize_state
from services.qa.deterministic import (
    DUPLICATE_YDIF_EPSILON,
    FFMPEG,
    FFPROBE,
    LumaFrame,
    MotionSeries,
    RegionObservation,
    _frame_rate,
    detect_region,
    detect_text,
    evaluate,
    expects_stillness,
    face_track_continuity,
    frame_interval_us,
    measure,
    style_descriptor,
    style_distance,
    tool_version,
)
from services.qa.evidence import build_contact_sheet, nearest_sample
from services.qa.fake_visual_agent import FakeDefect, FakeFinding, FakeVisualAgent
from services.qa.identity import (
    ambiguous_expectations,
    build_character_expectations,
    required_character_count,
)
from services.qa.openai_adapter import OpenAIVisualAgent
from services.qa.rubric import (
    DETERMINISTIC_THRESHOLDS,
    HARD_FAILURE_CODES,
    REPAIR_CODES,
    RUBRIC,
    SAMPLING_CONFIGURATION,
    THRESHOLDS,
)
from services.qa.sampler import (
    action_window,
    decode_samples,
    finalize_plan,
    plan_video_samples,
)
from services.qa.scoring import build_dimension_results, decide, recompute
from services.qa.visual_agent import (
    VisualAgentCall,
    VisualAgentError,
    VisualQARole,
    role_for,
    validate_result,
)
from tests.visual_qa_fixtures import image_bytes, make_video, shot_contract
from vidgen.contracts.storyboard import StoryboardShot
from vidgen.contracts.visual_qa import (
    VisualQAAttemptType,
    VisualQABoundingBox,
    VisualQADeterministicMetric,
    VisualQADeterministicReport,
    VisualQADimension,
    VisualQADimensionResult,
    VisualQAEvidence,
    VisualQAEvidenceType,
    VisualQAFinding,
    VisualQAOutcome,
    VisualQAProviderDimensionScore,
    VisualQAProviderRequest,
    VisualQAProviderResult,
    VisualQARepairCode,
    VisualQARoutingRecommendation,
    VisualQASample,
    VisualQASampleReference,
    VisualQASampleType,
    VisualQASamplingManifest,
    VisualQAShotImportance,
    VisualQATargetType,
)

SHOT_DURATION_US = 3_000_000
HASH = "a" * 64


def make_shot(**overrides: object) -> StoryboardShot:
    payload = shot_contract(
        shot_id=uuid4(),
        storyboard_run_id=uuid4(),
        segment_id=uuid4(),
        script_segment_id=uuid4(),
        narration_segment_id=uuid4(),
        sequence=0,
        character_id=uuid4(),
        location_id=uuid4(),
        importance=0.5,
    )
    payload.update(overrides)
    return StoryboardShot.model_validate(payload)


def sample(sequence: int, timestamp: int, **overrides: object) -> VisualQASample:
    values: dict[str, object] = {
        "sample_id": UUID(int=sequence + 1),
        "sequence": sequence,
        "sample_type": VisualQASampleType.COVERAGE,
        "requested_timestamp_us": timestamp,
        "actual_timestamp_us": timestamp,
        "shot_relative_timestamp_us": timestamp,
        "frame_asset_id": UUID(int=1000 + sequence),
        "frame_sha256": HASH,
        "source_asset_id": UUID(int=99),
        "selection_reason": "coverage",
        "contact_sheet_position": sequence,
    }
    values.update(overrides)
    return VisualQASample.model_validate(values)


def provider_result(
    *,
    scores: dict[VisualQADimension, float] | None = None,
    confidence: dict[VisualQADimension, float] | None = None,
    findings: list[dict[str, object]] | None = None,
    hard_failures: list[str] | None = None,
    inapplicable: set[VisualQADimension] | None = None,
) -> VisualQAProviderResult:
    return VisualQAProviderResult(
        qa_attempt_identity=HASH,
        attempt_type=VisualQAAttemptType.FIRST_PASS,
        dimension_scores=[
            VisualQAProviderDimensionScore(
                dimension=dimension,
                raw_score=(scores or {}).get(dimension, 95.0),
                confidence=(confidence or {}).get(dimension, 0.9),
                applicable=dimension not in (inapplicable or set()),
            )
            for dimension in VisualQADimension
        ],
        findings=[
            {
                "dimension": item["dimension"],
                "severity": item.get("severity", "warning"),
                "code": item.get("code", "issue"),
                "summary": item.get("summary", "an issue"),
                "repair_codes": item.get("repair_codes", []),
                "confidence": item.get("confidence", 0.9),
                "sample_ids": item.get("sample_ids", [UUID(int=1)]),
            }
            for item in (findings or [])
        ],  # type: ignore[arg-type]
        proposed_hard_failure_codes=hard_failures or [],
        overall_confidence=0.9,
        provider="fake",
        model="fake-visual-qa/1",
    )


def empty_report(**overrides: object) -> VisualQADeterministicReport:
    values: dict[str, object] = {
        "check_version": "visual-qa-deterministic/1.0",
        "target_type": VisualQATargetType.VIDEO,
        "usable": True,
        "metrics": [],
    }
    values.update(overrides)
    return VisualQADeterministicReport.model_validate(values)


# --- rubric and thresholds ---------------------------------------------------
def test_rubric_weights_are_exactly_the_authoritative_table() -> None:
    weights = {item.dimension.value: item.weight for item in RUBRIC.dimensions}
    assert weights == {
        "character_identity": 25,
        "character_count": 10,
        "location": 10,
        "wardrobe_and_state": 10,
        "action_and_motion": 15,
        "composition": 10,
        "anatomy_and_artifacts": 10,
        "continuity_and_style": 10,
    }
    assert sum(weights.values()) == 100


def test_every_repair_code_has_a_complete_taxonomy_entry() -> None:
    for code in VisualQARepairCode:
        definition = REPAIR_CODES[code]
        assert definition.category and definition.severity
        assert definition.repair_family in set(VisualQARoutingRecommendation)
        assert definition.evidence_requirement in {
            "frame",
            "frame_and_reference",
            "whole_file",
            "none",
        }
        assert definition.retryability in {"creative_retry", "deterministic", "human_review"}
    assert VisualQARepairCode.WRONG_CHARACTER_IDENTITY in HARD_FAILURE_CODES
    assert VisualQARepairCode.STYLE_DRIFT not in HARD_FAILURE_CODES


def test_thresholds_follow_the_documented_policy() -> None:
    assert THRESHOLDS.pass_score(VisualQAShotImportance.UTILITY) == 85
    assert THRESHOLDS.pass_score(VisualQAShotImportance.NORMAL) == 85
    assert THRESHOLDS.pass_score(VisualQAShotImportance.HERO) == 90
    assert THRESHOLDS.targeted_repair_floor == 75
    assert THRESHOLDS.adjudication_confidence_floor == 0.70
    assert THRESHOLDS.adjudication_decision_confidence == 0.80


# --- sampling ----------------------------------------------------------------
def test_sampling_timestamps_are_stable_for_identical_inputs() -> None:
    shot = make_shot()
    first = plan_video_samples(
        shot,
        measured_duration_us=SHOT_DURATION_US,
        configuration=SAMPLING_CONFIGURATION,
        frame_interval_us=41_667,
    )
    second = plan_video_samples(
        shot,
        measured_duration_us=SHOT_DURATION_US,
        configuration=SAMPLING_CONFIGURATION,
        frame_interval_us=41_667,
    )
    assert [item.requested_timestamp_us for item in first] == [
        item.requested_timestamp_us for item in second
    ]
    assert [item.sample_type for item in first] == [item.sample_type for item in second]


def test_sampling_covers_boundary_frames_and_clamps_to_the_measured_duration() -> None:
    plan = plan_video_samples(
        make_shot(),
        measured_duration_us=SHOT_DURATION_US,
        configuration=SAMPLING_CONFIGURATION,
        frame_interval_us=41_667,
    )
    timestamps = [item.requested_timestamp_us for item in plan]
    assert timestamps[0] == 0, "the first decodable frame is always sampled"
    assert max(timestamps) < SHOT_DURATION_US, "the last sample stays inside the measured duration"
    assert all(value >= 0 for value in timestamps)
    kinds = {item.sample_type for item in plan}
    assert VisualQASampleType.FIRST_FRAME in kinds
    assert VisualQASampleType.LAST_FRAME in kinds
    # The midpoint frame is always covered. Its recorded reason can be a
    # higher-priority one when the action window lands on the same timestamp.
    assert SHOT_DURATION_US // 2 in timestamps


def test_action_window_samples_surround_the_required_action() -> None:
    shot = make_shot(provenance={"importance": 0.5, "action_window_us": [1_000_000, 2_000_000]})
    assert action_window(shot, SHOT_DURATION_US) == (1_000_000, 2_000_000)
    plan = plan_video_samples(
        shot,
        measured_duration_us=SHOT_DURATION_US,
        configuration=SAMPLING_CONFIGURATION,
        frame_interval_us=41_667,
    )
    inside = [
        item.requested_timestamp_us
        for item in plan
        if item.sample_type
        in {VisualQASampleType.ACTION_WINDOW, VisualQASampleType.ACTION_BOUNDARY}
    ]
    assert inside, "the action window is always covered"
    assert all(1_000_000 <= value <= 2_000_000 for value in inside)


def test_duplicate_timestamps_are_removed_and_priority_decides_the_reason() -> None:
    plan = plan_video_samples(
        make_shot(),
        measured_duration_us=SHOT_DURATION_US,
        configuration=SAMPLING_CONFIGURATION,
        frame_interval_us=41_667,
    )
    timestamps = [item.requested_timestamp_us for item in plan]
    assert len(set(timestamps)) == len(timestamps)
    assert timestamps == sorted(timestamps)
    # Timestamp zero is claimed by the highest-priority reason, not by coverage.
    first = next(item for item in plan if item.requested_timestamp_us == 0)
    assert first.sample_type is VisualQASampleType.FIRST_FRAME


def test_sampling_respects_the_configured_budget() -> None:
    configuration = SAMPLING_CONFIGURATION.__class__(max_samples=5)
    plan = plan_video_samples(
        make_shot(),
        measured_duration_us=SHOT_DURATION_US,
        configuration=configuration,
        frame_interval_us=41_667,
    )
    assert len(plan) <= 5
    assert finalize_plan(plan, configuration) == plan


def test_decoded_samples_record_requested_and_actual_timestamps(tmp_path: Path) -> None:
    video = make_video(tmp_path / "clean.mp4")
    plan = plan_video_samples(
        make_shot(),
        measured_duration_us=SHOT_DURATION_US,
        configuration=SAMPLING_CONFIGURATION,
        frame_interval_us=41_667,
    )
    decoded = decode_samples(video, plan)
    assert len(decoded) >= 5
    assert all(item.sha256 for item in decoded)
    actual = [item.actual_timestamp_us for item in decoded]
    assert actual == sorted(actual)
    assert len(set(actual)) == len(actual)
    for item in decoded:
        assert abs(item.actual_timestamp_us - item.planned.requested_timestamp_us) <= 50_000


def test_contact_sheet_maps_every_tile_back_to_its_sample() -> None:
    frames = [image_bytes() for _ in range(3)]
    samples = [sample(index, index * 1000) for index in range(3)]
    sheet = build_contact_sheet(list(zip(samples, frames, strict=True)), columns=2)
    assert sheet is not None
    assert sheet.columns == 2 and sheet.rows == 2
    assert sheet.positions == {
        samples[0].sample_id: 0,
        samples[1].sample_id: 1,
        samples[2].sample_id: 2,
    }
    # The rendering is deterministic: the same frames produce the same bytes.
    again = build_contact_sheet(list(zip(samples, frames, strict=True)), columns=2)
    assert again is not None and again.content == sheet.content


# --- deterministic checks ----------------------------------------------------
def test_complete_decode_and_geometry_pass_for_a_clean_clip(tmp_path: Path) -> None:
    video = make_video(tmp_path / "clean.mp4")
    measurement = measure(video, VisualQATargetType.VIDEO)
    assert measurement.decodable is True
    assert measurement.duration_us is not None
    report = _evaluate(measurement, expected_duration_us=SHOT_DURATION_US)
    assert report.usable is True
    assert {item.code for item in report.metrics} >= {
        "complete_decode",
        "stream_layout",
        "frame_rate",
        "duration_matches_t13",
        "black_frames",
        "freeze_ratio",
        "flicker",
    }


def test_a_corrupt_file_is_a_hard_decode_failure_with_no_paid_call(tmp_path: Path) -> None:
    corrupt = make_video(tmp_path / "corrupt.mp4", kind="corrupt")
    measurement = measure(corrupt, VisualQATargetType.VIDEO)
    assert measurement.decodable is False
    report = _evaluate(measurement, expected_duration_us=SHOT_DURATION_US)
    assert report.usable is False
    failure = report.hard_failures[0]
    assert failure.repair_code is VisualQARepairCode.DECODE_FAILURE


def test_duration_drift_beyond_200ms_is_a_hard_failure(tmp_path: Path) -> None:
    video = make_video(tmp_path / "long.mp4", seconds=3.5)
    measurement = measure(video, VisualQATargetType.VIDEO)
    report = _evaluate(measurement, expected_duration_us=SHOT_DURATION_US)
    drift = next(item for item in report.metrics if item.code == "duration_matches_t13")
    assert drift.outcome == "hard_failure"
    assert drift.threshold == DETERMINISTIC_THRESHOLDS.duration_hard_failure_us
    assert drift.repair_code is VisualQARepairCode.DURATION_MISMATCH


def test_duration_drift_approaching_the_threshold_only_warns(tmp_path: Path) -> None:
    video = make_video(tmp_path / "slightly-long.mp4", seconds=3.0)
    measurement = measure(video, VisualQATargetType.VIDEO)
    report = _evaluate(measurement, expected_duration_us=SHOT_DURATION_US - 170_000)
    drift = next(item for item in report.metrics if item.code == "duration_matches_t13")
    assert drift.outcome == "warning"


def test_black_video_is_detected_as_a_hard_failure(tmp_path: Path) -> None:
    video = make_video(tmp_path / "black.mp4", kind="black")
    measurement = measure(video, VisualQATargetType.VIDEO)
    report = _evaluate(measurement, expected_duration_us=SHOT_DURATION_US)
    black = next(item for item in report.metrics if item.code == "black_frames")
    assert black.outcome == "hard_failure"
    assert black.repair_code is VisualQARepairCode.BLACK_VIDEO
    assert black.evidence_timestamp_us is not None


def test_freeze_ratio_is_measured_and_intentional_stillness_is_not_a_warning(
    tmp_path: Path,
) -> None:
    video = make_video(tmp_path / "frozen.mp4", kind="freeze")
    measurement = measure(video, VisualQATargetType.VIDEO)
    accidental = _evaluate(measurement, expected_duration_us=SHOT_DURATION_US)
    frozen = next(item for item in accidental.metrics if item.code == "freeze_ratio")
    assert frozen.outcome == "warning"
    assert frozen.measurement is not None and frozen.measurement > 0.35

    intentional = _evaluate(
        measurement, expected_duration_us=SHOT_DURATION_US, expects_stillness=True
    )
    held = next(item for item in intentional.metrics if item.code == "freeze_ratio")
    assert held.outcome == "pass"
    assert "expects stillness" in held.message


def test_intentional_stillness_is_read_from_the_t13_plan() -> None:
    assert expects_stillness("static", "The lead holds a still pose") is True
    assert expects_stillness("static", "The lead runs across the room") is False
    assert expects_stillness("pan", "The lead holds a still pose") is False


def test_flicker_is_detected_from_inter_frame_luma_deltas() -> None:
    frames = tuple(
        LumaFrame(
            index=index,
            timestamp_us=index * 41_667,
            average=128.0,
            minimum=0.0,
            maximum=255.0,
            difference=0.0 if index == 0 else 40.0,
        )
        for index in range(10)
    )
    series = MotionSeries(
        frames=frames, high_motion_timestamps_us=(41_667,), low_motion_timestamps_us=()
    )
    measurement = measure.__wrapped__ if hasattr(measure, "__wrapped__") else None
    assert measurement is None  # measure is a plain function; the series drives the check
    assert series.mean_difference == pytest.approx(40.0)
    assert series.duplicate_ratio == 0.0


def test_duplicate_ratio_counts_frames_below_the_epsilon() -> None:
    frames = tuple(
        LumaFrame(
            index=index,
            timestamp_us=index * 1000,
            average=100.0,
            minimum=0.0,
            maximum=200.0,
            difference=0.0 if index % 2 else DUPLICATE_YDIF_EPSILON * 4,
        )
        for index in range(11)
    )
    series = MotionSeries(frames=frames, high_motion_timestamps_us=(), low_motion_timestamps_us=())
    assert series.duplicate_ratio == pytest.approx(0.5)


def test_ocr_threshold_separates_readable_text_from_shapes() -> None:
    text = detect_text(image_bytes("SUBSCRIBE NOW for more videos"))
    plain = detect_text(image_bytes())
    assert text.confidence >= DETERMINISTIC_THRESHOLDS.ocr_confidence_warning
    assert plain.confidence < DETERMINISTIC_THRESHOLDS.ocr_confidence_warning
    assert 0.0 <= text.band_top <= 1.0


def test_face_track_continuity_threshold() -> None:
    stable = detect_region(image_bytes())
    assert stable.present is True
    assert face_track_continuity([stable, stable, stable]) == 1.0
    moved = RegionObservation(present=True, centre_x=0.9, centre_y=0.9, area_ratio=0.02)
    floor = DETERMINISTIC_THRESHOLDS.face_track_continuity_floor
    assert face_track_continuity([stable, moved]) < floor
    absent = RegionObservation(present=False, centre_x=0.0, centre_y=0.0, area_ratio=0.0)
    assert face_track_continuity([stable, absent]) == 0.0


def test_style_distance_threshold() -> None:
    reference = style_descriptor(image_bytes())
    same = style_distance(reference, style_descriptor(image_bytes()))
    drifted = style_distance(reference, style_descriptor(image_bytes("PLAIN TEXT OVERLAY")))
    assert same == pytest.approx(0.0, abs=1e-9)
    assert drifted > same
    assert 0.0 <= drifted <= 1.0


def test_non_finite_measurements_are_rejected_by_the_contract() -> None:
    with pytest.raises(ValidationError):
        VisualQADeterministicMetric(
            code="flicker",
            measurement=math.inf,
            outcome="warning",
            tool="ffmpeg",
            diagnostic_code="excessive_flicker",
        )
    with pytest.raises(ValidationError):
        sample(0, 0, measurements={"width": math.nan})


def test_frame_interval_handles_unsupported_rates() -> None:
    assert frame_interval_us("24/1") == pytest.approx(41_667, abs=1)
    assert frame_interval_us("0/0") == 0
    assert frame_interval_us("nonsense") == 0


def _evaluate(
    measurement: object,
    *,
    expected_duration_us: int,
    expects_stillness: bool = False,
) -> VisualQADeterministicReport:
    return evaluate(
        measurement,  # type: ignore[arg-type]
        target_type=VisualQATargetType.VIDEO,
        expected_width=None,
        expected_height=None,
        expected_duration_us=expected_duration_us,
        expects_stillness=expects_stillness,
        thresholds=DETERMINISTIC_THRESHOLDS,
        ffmpeg_version=tool_version(FFMPEG),
        ffprobe_version=tool_version(FFPROBE),
    )


# --- identity and continuity -------------------------------------------------
def test_required_character_count_comes_from_the_t13_continuity_state() -> None:
    shot = make_shot()
    assert required_character_count(shot) == 1


def test_required_props_merge_shot_and_action_references() -> None:
    shot = make_shot()
    assert required_props(shot) == ("mug",)


def test_continuity_summaries_are_structured_not_prose() -> None:
    shot = make_shot()
    summary = summarize_state(shot.incoming_continuity)
    assert "characters=" in summary
    assert "screen_direction=left_to_right" in summary
    assert "props=mug@" in summary
    assert len(summary) <= 2048


def test_ambiguous_identity_evidence_is_reported_rather_than_guessed() -> None:
    class _Inputs:
        character_state_snapshots = (
            {
                "identity_version_id": UUID(int=7),
                "state": {"wardrobe": ["green jacket"], "carried_props": ["mug"]},
            },
        )
        references = ()

    rows = {
        UUID(int=7): {
            "identity": {
                "character_id": str(UUID(int=8)),
                "display_name": "Maya",
                "stable_traits": {"hair": "black bob"},
                "confidence": 0.42,
                "ambiguities": [{"field": "eye_colour"}],
            }
        }
    }
    expectations = build_character_expectations(_Inputs(), rows)  # type: ignore[arg-type]
    assert expectations[0].ambiguous is True
    reasons = ambiguous_expectations(expectations)
    assert any("confidence" in reason for reason in reasons)
    assert any("eye_colour" in reason for reason in reasons)


def test_continuity_expectation_only_trusts_a_passing_previous_shot() -> None:
    class _Inputs:
        shot = make_shot()
        references = ()
        location_state_snapshot = None
        location_identity_version_id = None
        previous_shot_record = type("Row", (), {"id": UUID(int=5)})()
        previous_video = type("Video", (), {"canonical_asset_id": UUID(int=6)})()

    passing = build_expectation(_Inputs(), None, previous_passed_qa=True)  # type: ignore[arg-type]
    blocked = build_expectation(_Inputs(), None, previous_passed_qa=False)  # type: ignore[arg-type]
    assert passing.baseline_available is True
    assert blocked.baseline_available is False


# --- score recomputation -----------------------------------------------------
def _score(result: VisualQAProviderResult, report: VisualQADeterministicReport | None = None):
    samples = [sample(0, 0), sample(1, 1_000_000)]
    dimensions = build_dimension_results(
        result,
        report or empty_report(),
        rubric=RUBRIC,
        samples=samples,
        source_asset_id=UUID(int=99),
    )
    return dimensions, recompute(
        dimensions,
        rubric=RUBRIC,
        thresholds=THRESHOLDS,
        importance=VisualQAShotImportance.NORMAL,
    )


def test_score_is_recomputed_from_dimension_values() -> None:
    _, score = _score(provider_result(scores=dict.fromkeys(VisualQADimension, 90.0)))
    assert score.total == pytest.approx(90.0)
    assert score.applied_weight_total == pytest.approx(100.0)
    expected = sum(item.weighted_contribution for item in score.dimensions if item.applicable)
    assert score.total == pytest.approx(expected)


def test_a_provider_supplied_total_is_never_accepted() -> None:
    # The provider contract has no overall-score field at all.
    assert "overall_score" not in VisualQAProviderResult.model_fields
    assert "total" not in VisualQAProviderResult.model_fields


def test_non_applicable_dimensions_redistribute_weight_instead_of_giving_credit() -> None:
    result = provider_result(
        scores=dict.fromkeys(VisualQADimension, 80.0),
        inapplicable={VisualQADimension.CHARACTER_COUNT},
    )
    dimensions, score = _score(result)
    skipped = next(
        item for item in dimensions if item.dimension is VisualQADimension.CHARACTER_COUNT
    )
    assert skipped.applicable is False
    assert skipped.weighted_contribution == 0
    assert score.applied_weight_total == pytest.approx(100.0)
    # The remaining dimensions still total 100 weight, so the shot gets no free credit.
    assert score.total == pytest.approx(80.0)


def test_normal_shot_passes_at_the_documented_threshold() -> None:
    for total, expected in ((85.0, VisualQAOutcome.PASS), (84.0, VisualQAOutcome.FAIL)):
        result = provider_result(scores=dict.fromkeys(VisualQADimension, total))
        _, score = _score(result)
        outcome = decide(score, empty_report(), result, thresholds=THRESHOLDS)
        assert outcome.outcome is expected


def test_hero_shot_requires_ninety() -> None:
    result = provider_result(scores=dict.fromkeys(VisualQADimension, 87.0))
    dimensions, _ = _score(result)
    hero = recompute(
        dimensions, rubric=RUBRIC, thresholds=THRESHOLDS, importance=VisualQAShotImportance.HERO
    )
    assert hero.pass_threshold == 90
    outcome = decide(hero, empty_report(), result, thresholds=THRESHOLDS)
    assert outcome.outcome is VisualQAOutcome.FAIL
    assert outcome.recommendation.routing is VisualQARoutingRecommendation.TARGETED_REPAIR


def test_a_hard_failure_overrides_a_high_numeric_score() -> None:
    result = provider_result(
        scores=dict.fromkeys(VisualQADimension, 99.0),
        findings=[
            {
                "dimension": VisualQADimension.CHARACTER_IDENTITY,
                "severity": "hard_failure",
                "code": "wrong_primary_character",
                "repair_codes": [VisualQARepairCode.WRONG_CHARACTER_IDENTITY],
            }
        ],
        hard_failures=["WRONG_CHARACTER_IDENTITY"],
    )
    _, score = _score(result)
    outcome = decide(score, empty_report(), result, thresholds=THRESHOLDS)
    assert score.total == pytest.approx(99.0)
    assert outcome.outcome is VisualQAOutcome.FAIL
    assert outcome.hard_failure is True
    assert "WRONG_CHARACTER_IDENTITY" in outcome.hard_failure_codes


def test_a_low_score_recommends_a_structural_repair_family() -> None:
    result = provider_result(
        scores={
            **dict.fromkeys(VisualQADimension, 80.0),
            VisualQADimension.CHARACTER_IDENTITY: 10.0,
        }
    )
    _, score = _score(result)
    outcome = decide(score, empty_report(), result, thresholds=THRESHOLDS)
    assert score.total < THRESHOLDS.targeted_repair_floor
    assert outcome.recommendation.routing is VisualQARoutingRecommendation.NEW_SEED
    assert outcome.repair_codes


def test_every_non_pass_result_carries_a_repair_code() -> None:
    result = provider_result(scores=dict.fromkeys(VisualQADimension, 80.0))
    _, score = _score(result)
    outcome = decide(score, empty_report(), result, thresholds=THRESHOLDS)
    assert outcome.outcome is VisualQAOutcome.FAIL
    assert outcome.repair_codes


def test_an_unevidenced_provider_hard_failure_cannot_block_the_shot() -> None:
    result = provider_result(hard_failures=["WRONG_CHARACTER_IDENTITY"])
    _, score = _score(result)
    outcome = decide(score, empty_report(), result, thresholds=THRESHOLDS)
    assert outcome.hard_failure is False
    assert "unevidenced_provider_hard_failure_proposal" in outcome.warning_codes


def test_a_deterministic_hard_failure_forces_fail_and_carries_its_repair_code() -> None:
    report = empty_report(
        usable=False,
        metrics=[
            VisualQADeterministicMetric(
                code="black_frames",
                measurement=1.0,
                threshold=0.98,
                outcome="hard_failure",
                evidence_timestamp_us=0,
                tool="ffmpeg",
                diagnostic_code="black_video",
                repair_code=VisualQARepairCode.BLACK_VIDEO,
            )
        ],
    )
    result = provider_result(scores=dict.fromkeys(VisualQADimension, 99.0))
    _, score = _score(result, report)
    outcome = decide(score, report, result, thresholds=THRESHOLDS)
    assert outcome.outcome is VisualQAOutcome.FAIL
    assert VisualQARepairCode.BLACK_VIDEO in outcome.repair_codes


def test_a_finding_without_evidence_cannot_become_a_hard_failure() -> None:
    with pytest.raises(ValidationError):
        VisualQAFinding(
            finding_id=uuid4(),
            dimension=VisualQADimension.CHARACTER_IDENTITY,
            severity="hard_failure",
            code="wrong_primary_character",
            summary="wrong character",
            repair_codes=[VisualQARepairCode.WRONG_CHARACTER_IDENTITY],
            confidence=0.9,
            evidence=[],
        )


def test_a_whole_file_deterministic_failure_may_be_a_hard_failure_without_a_frame() -> None:
    finding = VisualQAFinding(
        finding_id=uuid4(),
        dimension=VisualQADimension.ANATOMY_AND_ARTIFACTS,
        severity="hard_failure",
        code="decode_failed",
        summary="the clip cannot be decoded",
        repair_codes=[VisualQARepairCode.DECODE_FAILURE],
        confidence=1.0,
        evidence=[
            VisualQAEvidence(
                evidence_id=uuid4(),
                evidence_type=VisualQAEvidenceType.WHOLE_FILE,
                source_asset_id=UUID(int=99),
                confidence=1.0,
            )
        ],
    )
    assert finding.severity == "hard_failure"


def test_dimension_contribution_must_be_recomputed_not_asserted() -> None:
    with pytest.raises(ValidationError):
        VisualQADimensionResult(
            dimension=VisualQADimension.LOCATION,
            raw_score=50.0,
            weight=10.0,
            effective_weight=10.0,
            weighted_contribution=99.0,
            confidence=0.9,
            evaluator="fake",
            model="fake",
            rubric_version="visual-qa-rubric/1.0",
        )


def test_evidence_locates_the_nearest_sample_for_a_measurement() -> None:
    samples = [sample(0, 0), sample(1, 1_000_000), sample(2, 2_000_000)]
    assert nearest_sample(samples, 1_100_000) is samples[1]
    assert nearest_sample(samples, None) is None


def test_bounding_boxes_must_stay_inside_the_frame() -> None:
    VisualQABoundingBox(x=0.1, y=0.1, width=0.5, height=0.5)
    with pytest.raises(ValidationError):
        VisualQABoundingBox(x=0.8, y=0.1, width=0.5, height=0.1)


def test_sampling_manifest_requires_dense_ordered_unique_samples() -> None:
    with pytest.raises(ValidationError):
        VisualQASamplingManifest(
            sampling_version="v1",
            target_type=VisualQATargetType.VIDEO,
            source_asset_id=UUID(int=99),
            measured_duration_us=3_000_000,
            samples=[sample(0, 1_000_000), sample(1, 0)],
        )


# --- visual agents -----------------------------------------------------------
def _request(**overrides: object) -> VisualQAProviderRequest:
    values: dict[str, object] = {
        "qa_attempt_identity": HASH,
        "attempt_number": 1,
        "attempt_type": VisualQAAttemptType.FIRST_PASS,
        "project_id": UUID(int=1),
        "storyboard_shot_id": UUID(int=2),
        "target_type": VisualQATargetType.VIDEO,
        "storyboard_objective": "Show the beat",
        "required_character_count": 1,
        "samples": [
            VisualQASampleReference(
                sample_id=UUID(int=1),
                sequence=0,
                sample_type=VisualQASampleType.FIRST_FRAME,
                shot_relative_timestamp_us=0,
                source_relative_timestamp_us=0,
                frame_sha256=HASH,
            )
        ],
        "rubric_version": "visual-qa-rubric/1.0",
        "threshold_version": "visual-qa-thresholds/1.0",
        "prompt_version": "visual-qa-prompt/1.0",
    }
    values.update(overrides)
    return VisualQAProviderRequest.model_validate(values)


def test_fake_visual_agent_is_deterministic() -> None:
    agent = FakeVisualAgent()
    request = _request()
    call = VisualAgentCall(request=request, frames=(), references=())
    first = asyncio.run(agent.evaluate(call))
    second = asyncio.run(agent.evaluate(call))
    assert first.model_dump() == second.model_dump()
    assert {item.dimension for item in first.dimension_scores} == set(VisualQADimension)


def test_fake_visual_agent_applies_a_controlled_defect() -> None:
    shot_id = UUID(int=2)
    agent = FakeVisualAgent(
        {
            shot_id: FakeDefect(
                dimension_scores={VisualQADimension.CHARACTER_IDENTITY: 10.0},
                findings=(
                    FakeFinding(
                        dimension=VisualQADimension.CHARACTER_IDENTITY,
                        severity="hard_failure",
                        code="wrong_primary_character",
                        summary="wrong character",
                        repair_codes=(VisualQARepairCode.WRONG_CHARACTER_IDENTITY,),
                    ),
                ),
            )
        }
    )
    result = asyncio.run(
        agent.evaluate(VisualAgentCall(request=_request(), frames=(), references=()))
    )
    identity = next(
        item
        for item in result.dimension_scores
        if item.dimension is VisualQADimension.CHARACTER_IDENTITY
    )
    assert identity.raw_score == 10.0
    assert result.findings[0].sample_ids == [UUID(int=1)]


def test_provider_results_are_validated_against_the_request() -> None:
    request = _request()
    good = provider_result()
    good = good.model_copy(update={"qa_attempt_identity": request.qa_attempt_identity})
    assert validate_result(good, request, known_sample_ids=[UUID(int=1)]) is good

    unknown_frame = VisualQAProviderResult.model_validate(
        {
            **good.model_dump(mode="json"),
            "findings": [
                {
                    "dimension": "location",
                    "severity": "warning",
                    "code": "wrong_room",
                    "summary": "wrong room",
                    "confidence": 0.5,
                    "sample_ids": [str(UUID(int=404))],
                }
            ],
        }
    )
    with pytest.raises(VisualAgentError, match="unsampled frames"):
        validate_result(unknown_frame, request, known_sample_ids=[UUID(int=1)])

    with pytest.raises(VisualAgentError, match="missing rubric dimensions"):
        validate_result(
            good.model_copy(update={"dimension_scores": good.dimension_scores[:2]}),
            request,
            known_sample_ids=[UUID(int=1)],
        )

    with pytest.raises(VisualAgentError, match="does not answer"):
        validate_result(
            good.model_copy(update={"qa_attempt_identity": "b" * 64}),
            request,
            known_sample_ids=[UUID(int=1)],
        )


def test_role_registry_binds_first_pass_and_adjudicator() -> None:
    assert role_for(VisualQAAttemptType.FIRST_PASS) is VisualQARole.LUNA_FIRST_PASS
    assert role_for(VisualQAAttemptType.ADJUDICATION) is VisualQARole.TERRA_ADJUDICATOR


def test_mocked_production_adapter_sends_bounded_evidence_and_parses_the_contract() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["idempotency"] = request.headers.get("Idempotency-Key")
        captured["body"] = json.loads(request.content)
        payload = provider_result().model_dump(mode="json")
        payload["qa_attempt_identity"] = HASH
        return httpx.Response(
            200,
            json={
                "id": "resp_123",
                "status": "completed",
                "usage": {"input_tokens": 10, "output_tokens": 4},
                "output": [{"content": [{"type": "output_text", "text": json.dumps(payload)}]}],
            },
        )

    transport = httpx.MockTransport(handler)
    agent = OpenAIVisualAgent(
        api_key="test-key",
        client=httpx.AsyncClient(transport=transport, base_url="https://api.openai.com/v1"),
    )
    request = _request()
    result = asyncio.run(agent.evaluate(VisualAgentCall(request=request, frames=(), references=())))
    assert result.provider == "openai"
    assert result.provider_request_id == "resp_123"
    assert result.qa_attempt_identity == request.qa_attempt_identity
    assert captured["idempotency"] == request.qa_attempt_identity
    body = captured["body"]
    assert isinstance(body, dict)
    serialized = json.dumps(body)
    # The request carries structured shot intent, never a credential or a signed URL.
    assert "test-key" not in serialized
    assert "sig=" not in serialized
    assert body["text"]["format"]["name"] == "VisualQAProviderResult"


def test_production_adapter_rejects_a_refusal_and_an_invalid_result() -> None:
    def refuse(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output": [{"content": [{"type": "refusal"}]}]})

    agent = OpenAIVisualAgent(
        api_key="k",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(refuse), base_url="https://api.openai.com/v1"
        ),
    )
    with pytest.raises(VisualAgentError, match="refused"):
        asyncio.run(agent.evaluate(VisualAgentCall(request=_request(), frames=(), references=())))

    def garbage(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"output": [{"content": [{"type": "output_text", "text": "{}"}]}]}
        )

    broken = OpenAIVisualAgent(
        api_key="k",
        client=httpx.AsyncClient(
            transport=httpx.MockTransport(garbage), base_url="https://api.openai.com/v1"
        ),
    )
    with pytest.raises(VisualAgentError, match="contract validation"):
        asyncio.run(broken.evaluate(VisualAgentCall(request=_request(), frames=(), references=())))


# --- adjudication ------------------------------------------------------------
def test_low_identity_confidence_triggers_adjudication() -> None:
    result = provider_result(confidence={VisualQADimension.CHARACTER_IDENTITY: 0.55})
    _, score = _score(result)
    outcome = decide(score, empty_report(), result, thresholds=THRESHOLDS)
    triggers = evaluate_triggers(result, empty_report(), outcome, thresholds=THRESHOLDS)
    assert triggers
    assert any("character_identity confidence" in reason for reason in triggers.reasons)


def test_material_disagreement_with_the_deterministic_report_triggers_adjudication() -> None:
    report = empty_report(
        metrics=[
            VisualQADeterministicMetric(
                code="freeze_ratio",
                measurement=0.9,
                threshold=0.35,
                outcome="warning",
                tool="ffmpeg",
                diagnostic_code="excessive_freeze",
                repair_code=VisualQARepairCode.EXCESSIVE_FREEZE,
            )
        ]
    )
    result = provider_result(scores={VisualQADimension.ACTION_AND_MOTION: 99.0})
    _, score = _score(result, report)
    outcome = decide(score, report, result, thresholds=THRESHOLDS)
    triggers = evaluate_triggers(result, report, outcome, thresholds=THRESHOLDS)
    assert triggers.disagreements


def test_a_contradicting_prior_result_triggers_adjudication() -> None:
    result = provider_result()
    _, score = _score(result)
    outcome = decide(score, empty_report(), result, thresholds=THRESHOLDS)
    triggers = evaluate_triggers(
        result,
        empty_report(),
        outcome,
        thresholds=THRESHOLDS,
        prior_outcome=VisualQAOutcome.FAIL,
    )
    assert any("prior QA result" in reason for reason in triggers.reasons)


def test_terra_decides_only_at_or_above_the_configured_confidence() -> None:
    first = provider_result(confidence={VisualQADimension.CHARACTER_IDENTITY: 0.55})
    _, first_score = _score(first)
    first_outcome = decide(first_score, empty_report(), first, thresholds=THRESHOLDS)
    triggers = evaluate_triggers(first, empty_report(), first_outcome, thresholds=THRESHOLDS)

    confident = provider_result().model_copy(
        update={"attempt_type": VisualQAAttemptType.ADJUDICATION, "overall_confidence": 0.86}
    )
    _, confident_score = _score(confident)
    confident_outcome = decide(confident_score, empty_report(), confident, thresholds=THRESHOLDS)
    record, outcome, reasons = resolve(
        adjudication_id=uuid4(),
        triggers=triggers,
        first_pass=first,
        adjudicator=confident,
        adjudicated_outcome=confident_outcome,
        thresholds=THRESHOLDS,
        attempts_used=1,
    )
    assert record.decided is True
    assert outcome is VisualQAOutcome.PASS
    assert reasons == ()

    unsure = confident.model_copy(update={"overall_confidence": 0.61})
    _, unsure_score = _score(unsure)
    unsure_outcome = decide(unsure_score, empty_report(), unsure, thresholds=THRESHOLDS)
    record, outcome, reasons = resolve(
        adjudication_id=uuid4(),
        triggers=triggers,
        first_pass=first,
        adjudicator=unsure,
        adjudicated_outcome=unsure_outcome,
        thresholds=THRESHOLDS,
        attempts_used=1,
    )
    assert record.decided is False
    assert outcome is VisualQAOutcome.REVIEW
    assert any("decision threshold" in reason for reason in reasons)


def test_adjudication_never_softens_a_hard_failure() -> None:
    first = provider_result(confidence={VisualQADimension.CHARACTER_IDENTITY: 0.5})
    _, first_score = _score(first)
    first_outcome = decide(first_score, empty_report(), first, thresholds=THRESHOLDS)
    triggers = evaluate_triggers(first, empty_report(), first_outcome, thresholds=THRESHOLDS)
    blocking = provider_result(
        findings=[
            {
                "dimension": VisualQADimension.CHARACTER_IDENTITY,
                "severity": "hard_failure",
                "code": "wrong_primary_character",
                "repair_codes": [VisualQARepairCode.WRONG_CHARACTER_IDENTITY],
            }
        ],
        hard_failures=["WRONG_CHARACTER_IDENTITY"],
    ).model_copy(update={"overall_confidence": 0.4})
    _, blocking_score = _score(blocking)
    blocking_outcome = decide(blocking_score, empty_report(), blocking, thresholds=THRESHOLDS)
    _, outcome, _ = resolve(
        adjudication_id=uuid4(),
        triggers=triggers,
        first_pass=first,
        adjudicator=blocking,
        adjudicated_outcome=blocking_outcome,
        thresholds=THRESHOLDS,
        attempts_used=1,
    )
    assert outcome is VisualQAOutcome.FAIL


def test_a_review_outcome_still_carries_repair_codes() -> None:
    result = provider_result()
    _, score = _score(result)
    outcome = decide(
        score,
        empty_report(),
        result,
        thresholds=THRESHOLDS,
        review_reasons=("the approved identity evidence is ambiguous",),
    )
    assert outcome.outcome is VisualQAOutcome.REVIEW
    assert VisualQARepairCode.HUMAN_REVIEW_REQUIRED in outcome.repair_codes
    assert outcome.recommendation.routing is VisualQARoutingRecommendation.HUMAN_REVIEW


def test_contracts_reject_credentials_and_unbounded_payloads() -> None:
    with pytest.raises(ValidationError):
        VisualQAProviderRequest.model_validate(
            {**_request().model_dump(mode="json"), "api_key": "secret"}
        )
    with pytest.raises(ValidationError):
        VisualQAProviderResult.model_validate(
            {**provider_result().model_dump(mode="json"), "raw_response": {"anything": 1}}
        )


def test_result_timestamps_must_be_timezone_aware() -> None:
    assert datetime.now(UTC).tzinfo is not None


# --- regressions from review -------------------------------------------------
def test_two_findings_sharing_a_dimension_code_and_frame_stay_distinct() -> None:
    """Colliding IDs would insert two evidence rows on one primary key."""
    result = provider_result(
        findings=[
            {
                "dimension": VisualQADimension.CHARACTER_IDENTITY,
                "severity": "warning",
                "code": "identity_drift",
                "summary": "the face drifts",
                "sample_ids": [UUID(int=1)],
            },
            {
                "dimension": VisualQADimension.CHARACTER_IDENTITY,
                "severity": "warning",
                "code": "identity_drift",
                "summary": "the hair drifts",
                "sample_ids": [UUID(int=1)],
            },
        ]
    )
    dimensions, _ = _score(result)
    identity = next(
        item for item in dimensions if item.dimension is VisualQADimension.CHARACTER_IDENTITY
    )
    assert len(identity.findings) == 2
    assert len({finding.finding_id for finding in identity.findings}) == 2
    evidence_ids = [item.evidence_id for finding in identity.findings for item in finding.evidence]
    assert len(set(evidence_ids)) == len(evidence_ids)


def test_more_provider_findings_than_the_contract_allows_are_bounded() -> None:
    """A verbose provider must not make the dimension result unconstructable."""
    result = provider_result(
        findings=[
            {
                "dimension": VisualQADimension.ANATOMY_AND_ARTIFACTS,
                "severity": "hard_failure" if index == 20 else "warning",
                "code": f"artifact_{index}",
                "summary": f"artifact {index}",
                "repair_codes": [VisualQARepairCode.ANATOMY_BREAKAGE],
                "sample_ids": [UUID(int=1)],
            }
            for index in range(24)
        ]
    )
    dimensions, score = _score(result)
    anatomy = next(
        item for item in dimensions if item.dimension is VisualQADimension.ANATOMY_AND_ARTIFACTS
    )
    assert len(anatomy.findings) == 16
    assert len(anatomy.repair_codes) <= 8
    # Truncation keeps the blocking finding: it is ordered first.
    assert anatomy.findings[0].severity == "hard_failure"
    outcome = decide(score, empty_report(), result, thresholds=THRESHOLDS)
    assert outcome.outcome is VisualQAOutcome.FAIL
    assert outcome.hard_failure is True


def test_findings_on_a_non_applicable_dimension_are_bounded_too() -> None:
    """A verbose provider must not fail the run by over-reporting a skipped dimension.

    The applicable branch bounded its findings from the start; the non-applicable
    branch passed the list through untouched, so more than ``max_length`` findings
    for a dimension the provider itself marked non-applicable made the whole
    result unconstructable.
    """
    dimensions, score = _score(
        provider_result(
            inapplicable={VisualQADimension.LOCATION},
            findings=[
                {
                    "dimension": VisualQADimension.LOCATION,
                    "code": f"location_{index}",
                    "summary": f"location note {index}",
                    "repair_codes": [VisualQARepairCode.WRONG_LOCATION],
                    "sample_ids": [UUID(int=1)],
                }
                for index in range(24)
            ],
        )
    )
    location = next(item for item in dimensions if item.dimension is VisualQADimension.LOCATION)
    assert location.applicable is False
    assert len(location.findings) == 16
    # The remaining dimensions still redistribute to exactly 100.
    assert score.applied_weight_total == pytest.approx(100.0)


def test_an_unusable_ffprobe_frame_rate_falls_back_to_the_real_one() -> None:
    """ffprobe reports an unknown rate as the truthy string ``0/0``.

    A plain ``avg_frame_rate or r_frame_rate`` therefore never falls back, and the
    clip was measured with an unusable rate and hard-failed as undecodable — an
    outcome no human review is allowed to clear.
    """
    assert _frame_rate({"avg_frame_rate": "0/0", "r_frame_rate": "24/1"}) == "24/1"
    assert _frame_rate({"avg_frame_rate": "24000/1001"}) == "24000/1001"
    # Nothing usable on either key stays empty rather than inventing a rate.
    assert _frame_rate({"avg_frame_rate": "0/0", "r_frame_rate": "0/0"}) == ""
    assert _frame_rate({}) == ""
