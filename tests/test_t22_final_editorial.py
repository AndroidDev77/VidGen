"""T22 unit tests: deterministic checks, caption QA, gate policy and identity.

These tests exercise the pure logic without building a whole project. The
media-dependent checks still run against real FFmpeg output, because a
deterministic check that never saw a real file proves nothing.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from services.qa import final_audio, final_captions, final_deterministic
from services.qa.final_editorial_provider import (
    FinalEditorialProviderError,
    adjudicator_decided,
    build_registry,
    role_for,
    validate_result,
)
from services.qa.final_evidence import (
    build_contact_sheet,
    extract_frames,
    plan_sample_timestamps,
)
from services.qa.final_fake_provider import (
    FakeEditorialDefect,
    FakeEditorialFinding,
    FakeFinalEditorialProvider,
)
from services.qa.final_gate import (
    adjudication_triggers,
    apply_adjudication,
    bound_findings,
    decide,
    findings_from_checks,
    findings_from_provider,
    remediation_routes,
)
from services.qa.final_rubric import (
    DEFAULT_CONFIGURATION,
    EDITORIAL_DIMENSIONS,
    canonical_hash,
    configuration_hash,
)
from services.renderer.captions import build_caption_track, caption_identity, serialize_srt
from tests.final_qa_fixtures import DELIVERY_FPS, DELIVERY_HEIGHT, DELIVERY_WIDTH, ffmpeg
from vidgen.contracts.final_editorial import (
    ADJUDICATION_CONFIDENCE_FLOOR,
    FinalAudioCheck,
    FinalCaptionCheck,
    FinalCheckType,
    FinalDeterministicCheck,
    FinalEditorialAdjudication,
    FinalEditorialCategory,
    FinalEditorialDimension,
    FinalEditorialProviderFinding,
    FinalEditorialProviderRequest,
    FinalEditorialProviderResult,
    FinalFindingSeverity,
    FinalIssueCode,
    FinalMediaMeasurements,
    FinalQADecision,
    FinalQAInput,
    FinalRemediationTarget,
    FinalSelectedShot,
)
from vidgen.contracts.render import CaptionWord

FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
SHOT_US = 1_000_000
SHOT_COUNT = 4
TIMELINE_US = SHOT_US * SHOT_COUNT


def sha(seed: int) -> str:
    return f"{seed:064x}"


def make_input(**overrides: object) -> FinalQAInput:
    shots = [
        FinalSelectedShot(
            shot_id=UUID(int=100 + index),
            sequence=index,
            video_asset_id=UUID(int=200 + index),
            video_sha256=sha(300 + index),
            global_start_us=index * SHOT_US,
            global_end_us=(index + 1) * SHOT_US,
            shot_workflow_identity=sha(400 + index),
            video_qa_run_id=UUID(int=500 + index),
            video_qa_result_id=UUID(int=600 + index),
        )
        for index in range(SHOT_COUNT)
    ]
    payload: dict[str, object] = {
        "project_id": UUID(int=1),
        "render_job_id": UUID(int=2),
        "render_identity": sha(3),
        "final_video_asset_id": UUID(int=4),
        "final_video_sha256": sha(5),
        "render_manifest_asset_id": UUID(int=6),
        "render_manifest_hash": sha(7),
        "approved_script_id": UUID(int=8),
        "approved_script_version": 1,
        "approved_script_hash": sha(9),
        "narration_run_id": UUID(int=10),
        "narration_asset_ids": [UUID(int=11)],
        "narration_word_timing_hash": sha(12),
        "narration_duration_us": TIMELINE_US,
        "storyboard_run_id": UUID(int=13),
        "storyboard_hash": sha(14),
        "timing_manifest_hash": sha(15),
        "caption_track_id": UUID(int=16),
        "caption_identity": sha(17),
        "caption_asset_ids": [UUID(int=18)],
        "caption_asset_hashes": [sha(19)],
        "shots": shots,
        "timeline_duration_us": TIMELINE_US,
    }
    payload.update(overrides)
    return FinalQAInput.model_validate(payload)


# --- contracts ---------------------------------------------------------------
def test_a_final_qa_input_rejects_shot_coverage_with_a_gap() -> None:
    good = make_input()
    broken = [shot.model_dump() for shot in good.shots]
    broken[2]["global_start_us"] = broken[2]["global_start_us"] + 10
    with pytest.raises(ValueError, match="gap or overlap"):
        make_input(shots=broken)


def test_a_blocking_finding_without_evidence_is_rejected_by_the_contract() -> None:
    from vidgen.contracts.final_editorial import FinalEditorialFinding

    with pytest.raises(ValueError, match="must carry evidence"):
        FinalEditorialFinding(
            finding_id=uuid4(),
            category=FinalEditorialCategory.SCENE_COMPLETENESS,
            severity=FinalFindingSeverity.BLOCKING,
            blocking=True,
            confidence=1.0,
            issue_code=FinalIssueCode.MISSING_SCENE,
            summary="a scene is missing",
            start_us=0,
            end_us=1,
        )


def test_a_gate_cannot_record_a_pass_alongside_a_blocking_finding() -> None:
    from vidgen.contracts.final_editorial import FinalGateDecision

    with pytest.raises(ValueError, match="PASS requires"):
        FinalGateDecision(
            gate_version="final-gate/1.0",
            decision=FinalQADecision.PASS,
            final_video_asset_id=uuid4(),
            render_identity=sha(1),
            blocking_finding_count=1,
            review_finding_count=0,
            warning_finding_count=0,
            deterministic_failure_count=0,
            unresolved_review_count=0,
            decided_at=datetime.now(UTC),
        )


def test_an_adjudicator_below_the_confidence_floor_cannot_decide() -> None:
    with pytest.raises(ValueError, match="confidence floor"):
        FinalEditorialAdjudication(
            adjudication_id=uuid4(),
            policy_version="final-adjudication/1.0",
            confidence=0.79,
            decided=True,
            resulting_decision_hint=FinalQADecision.FAIL,
        )
    assert not adjudicator_decided(0.79)
    assert adjudicator_decided(ADJUDICATION_CONFIDENCE_FLOOR)


# --- identity ----------------------------------------------------------------
def test_the_final_qa_identity_changes_when_any_material_input_changes() -> None:
    base = make_input()
    baseline = canonical_hash(base.model_dump(mode="json"))
    assert canonical_hash(make_input().model_dump(mode="json")) == baseline

    changed_shot = [shot.model_dump() for shot in base.shots]
    changed_shot[0]["video_sha256"] = sha(999)
    assert canonical_hash(make_input(shots=changed_shot).model_dump(mode="json")) != baseline
    assert (
        canonical_hash(make_input(caption_asset_hashes=[sha(998)]).model_dump(mode="json"))
        != baseline
    )
    assert (
        canonical_hash(make_input(final_video_sha256=sha(997)).model_dump(mode="json"))
        != baseline
    )
    assert (
        canonical_hash(make_input(narration_asset_ids=[UUID(int=996)]).model_dump(mode="json"))
        != baseline
    )


def test_changing_a_configured_threshold_changes_the_configuration_hash() -> None:
    baseline = configuration_hash(DEFAULT_CONFIGURATION)
    louder = DEFAULT_CONFIGURATION.model_copy(update={"loudness_tolerance_lu": 2.5})
    rubric = DEFAULT_CONFIGURATION.model_copy(
        update={"editorial_rubric_version": "final-rubric/2.0"}
    )
    assert configuration_hash(louder) != baseline
    assert configuration_hash(rubric) != baseline


# --- deterministic media -----------------------------------------------------
@pytest.fixture
def delivery(tmp_path: Path) -> Path:
    """A small, valid delivery matching the fixture profile."""
    output = tmp_path / "delivery.mp4"
    ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"smptebars=s={DELIVERY_WIDTH}x{DELIVERY_HEIGHT}:r={DELIVERY_FPS}"
            f":d={TIMELINE_US / 1_000_000}",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=330:sample_rate=48000:duration={TIMELINE_US / 1_000_000}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            str(output),
        ]
    )
    return output


def configuration() -> object:
    return DEFAULT_CONFIGURATION.model_copy(
        update={
            "expected_width": DELIVERY_WIDTH,
            "expected_height": DELIVERY_HEIGHT,
            "expected_frame_rate": DELIVERY_FPS,
            "min_bytes_per_second": 1_000,
        }
    )


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="FFmpeg and ffprobe are required")
def test_a_valid_delivery_passes_every_deterministic_media_check(delivery: Path) -> None:
    config = configuration()
    measurements = final_deterministic.measure(delivery, config)  # type: ignore[arg-type]
    assert measurements.video_decoded and measurements.audio_decoded
    assert measurements.monotonic_video_timestamps
    assert measurements.first_frame_valid and measurements.last_frame_valid
    assert (measurements.width, measurements.height) == (DELIVERY_WIDTH, DELIVERY_HEIGHT)

    checks = final_deterministic.evaluate(
        measurements, make_input(subtitle_mode="burn_in"), config  # type: ignore[arg-type]
    )
    failures = [check for check in checks if check.status == "fail"]
    assert not failures, [(check.code.value, check.message) for check in failures]
    # Every check carries the tool and version that produced it.
    assert all(check.tool for check in checks)
    assert all(check.tool_version for check in checks)


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="FFmpeg and ffprobe are required")
def test_a_black_render_is_reported_with_its_exact_interval(tmp_path: Path) -> None:
    black = tmp_path / "black.mp4"
    ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"color=c=black:s={DELIVERY_WIDTH}x{DELIVERY_HEIGHT}:r={DELIVERY_FPS}:d=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:sample_rate=48000:duration=4",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            str(black),
        ]
    )
    config = configuration()
    measurements = final_deterministic.measure(black, config)  # type: ignore[arg-type]
    assert measurements.black_intervals
    checks = final_deterministic.evaluate(
        measurements, make_input(subtitle_mode="burn_in"), config  # type: ignore[arg-type]
    )
    failed = {check.code for check in checks if check.status == "fail"}
    assert FinalIssueCode.UNEXPECTED_BLACK_INTERVAL in failed
    interval = next(
        check
        for check in checks
        if check.code is FinalIssueCode.UNEXPECTED_BLACK_INTERVAL and check.status == "fail"
    )
    assert interval.start_us is not None and interval.end_us is not None
    assert interval.end_us > interval.start_us


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="FFmpeg and ffprobe are required")
def test_a_frozen_render_is_reported_as_an_excessive_freeze(tmp_path: Path) -> None:
    frozen = tmp_path / "frozen.mp4"
    # Five still seconds, then real motion. A freeze that runs to end of file
    # never reports its end, so the fixture makes the interval closed.
    ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            f"color=c=0x2878A0:s={DELIVERY_WIDTH}x{DELIVERY_HEIGHT}:r={DELIVERY_FPS}:d=5",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=s={DELIVERY_WIDTH}x{DELIVERY_HEIGHT}:r={DELIVERY_FPS}:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:sample_rate=48000:duration=7",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-map",
            "2:a",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-ar",
            "48000",
            "-ac",
            "2",
            str(frozen),
        ]
    )
    config = configuration()
    measurements = final_deterministic.measure(frozen, config)  # type: ignore[arg-type]
    checks = final_deterministic.evaluate(
        measurements, make_input(subtitle_mode="burn_in"), config  # type: ignore[arg-type]
    )
    failed = {check.code for check in checks if check.status == "fail"}
    assert FinalIssueCode.EXCESSIVE_FREEZE_INTERVAL in failed


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="FFmpeg and ffprobe are required")
def test_a_duration_that_disagrees_with_the_manifest_is_a_blocking_failure(
    delivery: Path,
) -> None:
    config = configuration()
    measurements = final_deterministic.measure(delivery, config)  # type: ignore[arg-type]
    # The manifest says the timeline is a second longer than the file.
    longer = make_input(
        narration_duration_us=TIMELINE_US,
        timeline_duration_us=TIMELINE_US,
    )
    stretched = longer.model_copy(
        update={
            "shots": [
                *longer.shots[:-1],
                longer.shots[-1].model_copy(update={"global_end_us": TIMELINE_US + 1_000_000}),
            ],
            "timeline_duration_us": TIMELINE_US + 1_000_000,
        }
    )
    checks = final_deterministic.evaluate(measurements, stretched, config)  # type: ignore[arg-type]
    failed = {check.code for check in checks if check.status == "fail"}
    assert FinalIssueCode.TIMELINE_DURATION_MISMATCH in failed
    assert FinalIssueCode.VIDEO_DURATION_MISMATCH in failed


@pytest.mark.skipif(not FFMPEG_AVAILABLE, reason="FFmpeg and ffprobe are required")
def test_deterministic_sampling_and_the_contact_sheet_are_reproducible(
    delivery: Path,
) -> None:
    config = configuration()
    inputs = make_input()
    first = plan_sample_timestamps(inputs, config)  # type: ignore[arg-type]
    assert first == plan_sample_timestamps(inputs, config)  # type: ignore[arg-type]
    assert first == sorted(set(first))

    frames = extract_frames(delivery, inputs, config)  # type: ignore[arg-type]
    again = extract_frames(delivery, inputs, config)  # type: ignore[arg-type]
    assert [frame.sample_id for frame in frames] == [frame.sample_id for frame in again]
    assert [frame.sha256 for frame in frames] == [frame.sha256 for frame in again]

    sheet = build_contact_sheet(frames, columns=4)
    repeat = build_contact_sheet(again, columns=4)
    assert sheet is not None and repeat is not None
    assert sheet.sha256 == repeat.sha256
    assert set(sheet.positions) == {frame.sample_id for frame in frames}


# --- audio -------------------------------------------------------------------
def measurements_with(**overrides: object) -> FinalMediaMeasurements:
    payload: dict[str, object] = {
        "measured_at": datetime.now(UTC),
        "byte_size": 1_000_000,
        "video_duration_us": TIMELINE_US,
        "audio_duration_us": TIMELINE_US,
        "channels": 2,
        "sample_rate_hz": 48_000,
        "video_decoded": True,
        "audio_decoded": True,
        "silence_intervals": [],
    }
    payload.update(overrides)
    return FinalMediaMeasurements.model_validate(payload)


def narration_intervals() -> list[tuple[UUID, int, int]]:
    return [
        (UUID(int=700 + index), index * SHOT_US, (index + 1) * SHOT_US)
        for index in range(SHOT_COUNT)
    ]


def test_audio_checks_catch_an_omitted_narration_interval(tmp_path: Path) -> None:
    config = configuration()
    silent_second_shot = measurements_with(
        silence_intervals=[{"start_us": SHOT_US, "end_us": 2 * SHOT_US}]
    )
    checks, _ = final_audio.evaluate(
        tmp_path / "unused.mp4",
        make_input(),
        config,  # type: ignore[arg-type]
        silent_second_shot,
        narration_intervals=narration_intervals(),
        loudness={"integrated_lufs": -14.0, "true_peak_dbtp": -1.5},
        statistics={"Number_of_samples": 1000.0, "Number_of_clipped_samples": 0.0},
    )
    missing = next(
        check for check in checks if check.code is FinalIssueCode.NARRATION_INTERVAL_MISSING
    )
    assert missing.status == "fail" and missing.blocking
    assert missing.narration_segment_id == UUID(int=701)
    assert missing.start_us == SHOT_US and missing.end_us == 2 * SHOT_US


def test_audio_checks_catch_loudness_true_peak_and_clipping(tmp_path: Path) -> None:
    config = configuration()
    checks, measured = final_audio.evaluate(
        tmp_path / "unused.mp4",
        make_input(),
        config,  # type: ignore[arg-type]
        measurements_with(),
        narration_intervals=narration_intervals(),
        loudness={"integrated_lufs": -8.0, "true_peak_dbtp": 0.4},
        statistics={"Number_of_samples": 1000.0, "Number_of_clipped_samples": 40.0},
    )
    failed = {check.code for check in checks if check.status == "fail"}
    assert FinalIssueCode.LOUDNESS_OUT_OF_RANGE in failed
    assert FinalIssueCode.TRUE_PEAK_EXCEEDED in failed
    assert FinalIssueCode.AUDIO_CLIPPING in failed
    assert measured["clipping_ratio"] == pytest.approx(0.04)


def test_audio_checks_catch_drift_between_the_audio_and_visual_timelines(
    tmp_path: Path,
) -> None:
    config = configuration()
    checks, _ = final_audio.evaluate(
        tmp_path / "unused.mp4",
        make_input(),
        config,  # type: ignore[arg-type]
        measurements_with(audio_duration_us=TIMELINE_US - 900_000),
        narration_intervals=narration_intervals(),
        loudness={"integrated_lufs": -14.0, "true_peak_dbtp": -1.5},
        statistics={"Number_of_samples": 1000.0, "Number_of_clipped_samples": 0.0},
    )
    drift = next(check for check in checks if check.code is FinalIssueCode.AUDIO_VIDEO_DRIFT)
    assert drift.status == "fail"
    assert drift.measurement == pytest.approx(900_000)


def test_a_duplicated_narration_segment_is_reported(tmp_path: Path) -> None:
    config = configuration()
    duplicated = narration_intervals()
    duplicated[2] = (duplicated[1][0], duplicated[2][1], duplicated[2][2])
    checks, _ = final_audio.evaluate(
        tmp_path / "unused.mp4",
        make_input(),
        config,  # type: ignore[arg-type]
        measurements_with(),
        narration_intervals=duplicated,
        loudness={"integrated_lufs": -14.0, "true_peak_dbtp": -1.5},
        statistics={"Number_of_samples": 1000.0, "Number_of_clipped_samples": 0.0},
    )
    duplicate = next(
        check for check in checks if check.code is FinalIssueCode.NARRATION_SEGMENT_DUPLICATED
    )
    assert duplicate.status == "fail"


def test_music_that_neither_ducks_nor_sits_below_narration_is_reported(
    tmp_path: Path,
) -> None:
    config = configuration()
    manifest = {
        "audio_entries": [
            {
                "role": "narration",
                "asset": {"asset_id": str(UUID(int=11))},
                "start_us": 0,
                "duration_us": TIMELINE_US,
            },
            {
                "role": "music",
                "asset": {"asset_id": str(UUID(int=42))},
                "start_us": 0,
                "duration_us": TIMELINE_US,
                "gain_millidb": -1000,
                "duck_under_narration": False,
            },
        ]
    }
    checks, _ = final_audio.evaluate(
        tmp_path / "unused.mp4",
        make_input(),
        config,  # type: ignore[arg-type]
        measurements_with(),
        narration_intervals=narration_intervals(),
        manifest=manifest,
        loudness={"integrated_lufs": -14.0, "true_peak_dbtp": -1.5},
        statistics={"Number_of_samples": 1000.0, "Number_of_clipped_samples": 0.0},
    )
    masked = next(
        check for check in checks if check.code is FinalIssueCode.NARRATION_MASKED_BY_BED
    )
    assert masked.status == "fail"


# --- captions ----------------------------------------------------------------
def caption_words() -> list[CaptionWord]:
    return [
        CaptionWord(
            sequence=index,
            text=("w" + str(index) + ("." if index % 5 == 4 else ",")),
            start_us=index * 250_000,
            end_us=(index + 1) * 250_000,
        )
        for index in range(16)
    ]


def caption_setup() -> tuple[object, list[CaptionWord], FinalQAInput]:
    words = caption_words()
    track, validation = build_caption_track(
        track_id=UUID(int=16), words=words, duration_us=TIMELINE_US
    )
    assert validation.valid
    inputs = make_input(caption_identity=caption_identity(track))
    return track, words, inputs


def test_a_faithful_caption_delivery_passes_every_caption_check() -> None:
    track, words, inputs = caption_setup()
    delivered = serialize_srt(track).encode()  # type: ignore[arg-type]
    checks = final_captions.evaluate(
        inputs,
        configuration(),  # type: ignore[arg-type]
        canonical=track,  # type: ignore[arg-type]
        delivered={inputs.caption_asset_ids[0]: delivered},
        approved_words=words,
        narration_segments=narration_intervals(),
        delivered_hashes={inputs.caption_asset_ids[0]: inputs.caption_asset_hashes[0]},
        declared_caption_identity=inputs.caption_identity,
    )
    failures = [check for check in checks if check.status == "fail"]
    assert not failures, [(check.code.value, check.message) for check in failures]


def test_caption_qa_reports_missing_coverage_with_the_narration_segment_it_serves() -> None:
    track, words, inputs = caption_setup()
    truncated = track.model_copy(  # type: ignore[attr-defined]
        update={"cues": [cue for cue in track.cues if cue.end_us <= SHOT_US]}  # type: ignore[attr-defined]
    )
    checks = final_captions.evaluate(
        inputs,
        configuration(),  # type: ignore[arg-type]
        canonical=truncated,
        delivered={},
        approved_words=words,
        narration_segments=narration_intervals(),
        delivered_hashes={inputs.caption_asset_ids[0]: inputs.caption_asset_hashes[0]},
        declared_caption_identity=inputs.caption_identity,
    )
    missing = next(
        check for check in checks if check.code is FinalIssueCode.CAPTION_COVERAGE_MISSING
    )
    assert missing.status == "fail" and missing.blocking
    assert missing.narration_segment_id is not None
    assert missing.remediation_target is FinalRemediationTarget.REBUILD_CAPTIONS_T17


def test_caption_qa_rejects_overlapping_out_of_bounds_and_unreadable_cues() -> None:
    track, words, inputs = caption_setup()
    cues = list(track.cues)  # type: ignore[attr-defined]
    cues[1] = cues[1].model_copy(update={"start_us": cues[0].end_us - 100_000})
    cues[-1] = cues[-1].model_copy(update={"end_us": TIMELINE_US + 500_000})
    broken = track.model_copy(update={"cues": cues})  # type: ignore[attr-defined]
    checks = final_captions.evaluate(
        inputs,
        configuration(),  # type: ignore[arg-type]
        canonical=broken,
        delivered={},
        approved_words=words,
        narration_segments=narration_intervals(),
        delivered_hashes={inputs.caption_asset_ids[0]: inputs.caption_asset_hashes[0]},
        declared_caption_identity=inputs.caption_identity,
    )
    failed = {check.code for check in checks if check.status == "fail"}
    assert FinalIssueCode.CAPTION_OVERLAP in failed
    assert FinalIssueCode.CAPTION_OUT_OF_BOUNDS in failed


def test_caption_qa_rejects_a_delivered_file_that_will_not_parse() -> None:
    track, words, inputs = caption_setup()
    checks = final_captions.evaluate(
        inputs,
        configuration(),  # type: ignore[arg-type]
        canonical=track,  # type: ignore[arg-type]
        delivered={inputs.caption_asset_ids[0]: b"\xff\xfe not a caption file"},
        approved_words=words,
        narration_segments=narration_intervals(),
        delivered_hashes={inputs.caption_asset_ids[0]: inputs.caption_asset_hashes[0]},
        declared_caption_identity=inputs.caption_identity,
    )
    parse = next(
        check for check in checks if check.code is FinalIssueCode.CAPTION_PARSE_FAILURE
    )
    assert parse.status == "fail"
    assert parse.caption_asset_id == inputs.caption_asset_ids[0]


def test_caption_reflow_is_verified_against_the_declared_identity() -> None:
    track, words, inputs = caption_setup()
    checks = final_captions.evaluate(
        inputs.model_copy(update={"caption_identity": sha(555)}),
        configuration(),  # type: ignore[arg-type]
        canonical=track,  # type: ignore[arg-type]
        delivered={},
        approved_words=words,
        narration_segments=narration_intervals(),
        delivered_hashes={inputs.caption_asset_ids[0]: inputs.caption_asset_hashes[0]},
        declared_caption_identity=sha(555),
    )
    reflow = next(
        check
        for check in checks
        if check.code is FinalIssueCode.CAPTION_REFLOW_NONDETERMINISTIC
    )
    assert reflow.status == "fail"


def test_caption_reading_speed_and_line_length_limits_are_enforced() -> None:
    track, words, inputs = caption_setup()
    cues = list(track.cues)  # type: ignore[attr-defined]
    cues[0] = cues[0].model_copy(
        update={"lines": ["x" * 120], "end_us": cues[0].start_us + 100_000}
    )
    broken = track.model_copy(update={"cues": cues})  # type: ignore[attr-defined]
    checks = final_captions.evaluate(
        inputs,
        configuration(),  # type: ignore[arg-type]
        canonical=broken,
        delivered={},
        approved_words=words,
        narration_segments=narration_intervals(),
        delivered_hashes={inputs.caption_asset_ids[0]: inputs.caption_asset_hashes[0]},
        declared_caption_identity=inputs.caption_identity,
    )
    failed = {check.code for check in checks if check.status == "fail"}
    assert FinalIssueCode.CAPTION_LINE_LENGTH_EXCEEDED in failed
    assert FinalIssueCode.CAPTION_READING_SPEED_EXCEEDED in failed


# --- gate and routing --------------------------------------------------------
def check(code: FinalIssueCode, status: str, **overrides: object) -> FinalDeterministicCheck:
    payload: dict[str, object] = {
        "check_id": uuid4(),
        "check_type": FinalCheckType.MEDIA,
        "check_version": "final-deterministic/1.0",
        "code": code,
        "status": status,
        "blocking": status == "fail",
        "tool": "ffprobe",
    }
    payload.update(overrides)
    return FinalDeterministicCheck.model_validate(payload)


def provider_result(
    findings: list[FinalEditorialProviderFinding], *, confidence: float = 0.95
) -> FinalEditorialProviderResult:
    return FinalEditorialProviderResult(
        attempt_identity=sha(21),
        attempt_type="first_pass",
        provider="fake",
        model="fake-final-editorial-1",
        dimension_scores=[
            FinalEditorialDimension(category=category, score=97.0, confidence=0.95)
            for category in EDITORIAL_DIMENSIONS
        ],
        findings=findings,
        overall_confidence=confidence,
    )


def proposal(
    *,
    category: FinalEditorialCategory,
    severity: FinalFindingSeverity,
    confidence: float,
    code: FinalIssueCode = FinalIssueCode.MISSING_STORY_BEAT,
) -> FinalEditorialProviderFinding:
    return FinalEditorialProviderFinding(
        category=category,
        issue_code=code,
        proposed_severity=severity,
        confidence=confidence,
        summary="a proposed finding",
        start_us=0,
        end_us=SHOT_US,
        shot_ids=[UUID(int=100)],
        caption_cue_sequences=[1],
    )


def test_a_failed_deterministic_check_becomes_an_evidenced_blocking_finding() -> None:
    failing = check(
        FinalIssueCode.VIDEO_DECODE_FAILURE,
        "fail",
        message="the video stream must decode completely",
        measurement=3.0,
    )
    findings = findings_from_checks([failing], timeline_duration_us=TIMELINE_US)
    assert len(findings) == 1
    finding = findings[0]
    assert finding.blocking and finding.severity is FinalFindingSeverity.BLOCKING
    assert finding.confidence == 1.0
    assert finding.provenance == "deterministic"
    assert finding.evidence, "a blocking finding must carry evidence"
    assert finding.remediation_target is FinalRemediationTarget.RERENDER_T17


def test_a_high_average_score_cannot_conceal_a_blocking_finding() -> None:
    result = provider_result(
        [
            proposal(
                category=FinalEditorialCategory.STORY_BEAT_COVERAGE,
                severity=FinalFindingSeverity.BLOCKING,
                confidence=0.95,
            )
        ]
    )
    assert all(dimension.score >= 97.0 for dimension in result.dimension_scores)
    findings = findings_from_provider(result, timeline_duration_us=TIMELINE_US)
    gate = decide(
        findings=findings,
        checks=[],
        final_video_asset_id=UUID(int=4),
        render_identity=sha(3),
    )
    assert gate.decision is FinalQADecision.FAIL
    assert gate.blocking_finding_count == 1


def test_a_low_confidence_blocking_proposal_becomes_review_rather_than_a_failure() -> None:
    result = provider_result(
        [
            proposal(
                category=FinalEditorialCategory.STORY_BEAT_COVERAGE,
                severity=FinalFindingSeverity.BLOCKING,
                confidence=0.5,
            )
        ]
    )
    findings = findings_from_provider(result, timeline_duration_us=TIMELINE_US)
    assert findings[0].severity is FinalFindingSeverity.REVIEW_REQUIRED
    assert not findings[0].blocking
    gate = decide(
        findings=findings,
        checks=[],
        final_video_asset_id=UUID(int=4),
        render_identity=sha(3),
    )
    assert gate.decision is FinalQADecision.REVIEW


def test_a_confident_proposal_in_a_non_blocking_category_does_not_block() -> None:
    result = provider_result(
        [
            proposal(
                category=FinalEditorialCategory.PACING,
                severity=FinalFindingSeverity.BLOCKING,
                confidence=0.99,
                code=FinalIssueCode.PACING_PROBLEM,
            )
        ]
    )
    findings = findings_from_provider(result, timeline_duration_us=TIMELINE_US)
    assert findings[0].severity is FinalFindingSeverity.REVIEW_REQUIRED


def test_an_undecided_adjudication_leaves_the_gate_at_review() -> None:
    result = provider_result(
        [
            proposal(
                category=FinalEditorialCategory.SETUP_AND_PAYOFF,
                severity=FinalFindingSeverity.REVIEW_REQUIRED,
                confidence=0.4,
                code=FinalIssueCode.UNRESOLVED_SETUP,
            )
        ]
    )
    findings = findings_from_provider(result, timeline_duration_us=TIMELINE_US)
    disputed = adjudication_triggers(findings, DEFAULT_CONFIGURATION)
    assert len(disputed) == 1
    undecided = FinalEditorialAdjudication(
        adjudication_id=uuid4(),
        policy_version="final-adjudication/1.0",
        disputed_finding_ids=[disputed[0].finding_id],
        confidence=0.6,
        decided=False,
        resulting_decision_hint=FinalQADecision.REVIEW,
    )
    resolved = apply_adjudication(findings, undecided)
    assert resolved[0].severity is FinalFindingSeverity.REVIEW_REQUIRED
    gate = decide(
        findings=resolved,
        checks=[],
        final_video_asset_id=UUID(int=4),
        render_identity=sha(3),
    )
    assert gate.decision is FinalQADecision.REVIEW


def test_a_confident_adjudication_confirms_a_blocking_category_finding() -> None:
    result = provider_result(
        [
            proposal(
                category=FinalEditorialCategory.LOCATION_CONTINUITY,
                severity=FinalFindingSeverity.REVIEW_REQUIRED,
                confidence=0.6,
                code=FinalIssueCode.LOCATION_CONTRADICTION,
            )
        ]
    )
    findings = findings_from_provider(result, timeline_duration_us=TIMELINE_US)
    decided = FinalEditorialAdjudication(
        adjudication_id=uuid4(),
        policy_version="final-adjudication/1.0",
        disputed_finding_ids=[findings[0].finding_id],
        confirmed_finding_ids=[findings[0].finding_id],
        confidence=0.9,
        decided=True,
        resulting_decision_hint=FinalQADecision.FAIL,
    )
    resolved = apply_adjudication(findings, decided)
    assert resolved[0].blocking
    assert resolved[0].provenance == "adjudication"


def test_a_resolved_review_finding_lets_the_gate_pass() -> None:
    result = provider_result(
        [
            proposal(
                category=FinalEditorialCategory.COMPREHENSIBILITY,
                severity=FinalFindingSeverity.REVIEW_REQUIRED,
                confidence=0.5,
                code=FinalIssueCode.INCOMPREHENSIBLE_SEQUENCE,
            )
        ]
    )
    findings = findings_from_provider(result, timeline_duration_us=TIMELINE_US)
    blocked = decide(
        findings=findings,
        checks=[],
        final_video_asset_id=UUID(int=4),
        render_identity=sha(3),
    )
    assert blocked.decision is FinalQADecision.REVIEW
    passed = decide(
        findings=findings,
        checks=[],
        final_video_asset_id=UUID(int=4),
        render_identity=sha(3),
        resolved_review_ids=frozenset({findings[0].finding_id}),
    )
    assert passed.decision is FinalQADecision.PASS


def test_a_resolved_review_cannot_clear_a_deterministic_failure() -> None:
    failing = check(FinalIssueCode.AUDIO_DECODE_FAILURE, "fail", message="audio will not decode")
    findings = findings_from_checks([failing], timeline_duration_us=TIMELINE_US)
    gate = decide(
        findings=findings,
        checks=[failing],
        final_video_asset_id=UUID(int=4),
        render_identity=sha(3),
        # Even if a reviewer somehow named the deterministic finding, it stands.
        resolved_review_ids=frozenset({findings[0].finding_id}),
    )
    assert gate.decision is FinalQADecision.FAIL
    assert gate.deterministic_failure_count == 1


def test_findings_are_bounded_most_severe_first() -> None:
    result = provider_result(
        [
            proposal(
                category=FinalEditorialCategory.REPETITION,
                severity=FinalFindingSeverity.WARNING,
                confidence=0.9,
                code=FinalIssueCode.EXCESSIVE_REPETITION,
            ),
            proposal(
                category=FinalEditorialCategory.SCENE_COMPLETENESS,
                severity=FinalFindingSeverity.BLOCKING,
                confidence=0.95,
                code=FinalIssueCode.MISSING_SCENE,
            ),
        ]
    )
    findings = findings_from_provider(result, timeline_duration_us=TIMELINE_US)
    bounded = bound_findings(findings, DEFAULT_CONFIGURATION.model_copy(update={"max_findings": 1}))
    assert len(bounded) == 1
    assert bounded[0].severity is FinalFindingSeverity.BLOCKING


def test_remediation_routes_group_findings_by_the_stage_that_owns_them() -> None:
    caption_failure = FinalCaptionCheck(
        check_id=uuid4(),
        check_version="final-caption/1.0",
        code=FinalIssueCode.CAPTION_COVERAGE_MISSING,
        status="fail",
        blocking=True,
        cue_sequence=3,
        message="a narration segment has no caption coverage",
    )
    audio_failure = FinalAudioCheck(
        check_id=uuid4(),
        check_version="final-audio/1.0",
        code=FinalIssueCode.LOUDNESS_OUT_OF_RANGE,
        status="fail",
        blocking=True,
        message="integrated loudness is outside the delivery target",
    )
    findings = findings_from_checks(
        [caption_failure, audio_failure], timeline_duration_us=TIMELINE_US
    )
    routes = {route.target: route for route in remediation_routes(findings)}
    assert FinalRemediationTarget.REBUILD_CAPTIONS_T17 in routes
    assert FinalRemediationTarget.REMIX_AUDIO_T17 in routes
    # Every route that changes a selected input requires a new render.
    assert all(route.requires_new_render for route in routes.values())


# --- provider contract -------------------------------------------------------
def request_for(samples: list[UUID]) -> FinalEditorialProviderRequest:
    from services.qa.final_evidence import deterministic_id
    from vidgen.contracts.final_editorial import FinalEditorialEvidence

    return FinalEditorialProviderRequest(
        final_qa_identity=sha(20),
        attempt_identity=sha(21),
        attempt_type="first_pass",
        attempt_number=1,
        project_id=UUID(int=1),
        final_video_asset_id=UUID(int=4),
        render_identity=sha(3),
        timeline_duration_us=TIMELINE_US,
        samples=[
            FinalEditorialEvidence(
                evidence_id=deterministic_id("sample-evidence", sample),
                evidence_type="sampled_frame",
                start_us=0,
                end_us=0,
                sample_id=sample,
            )
            for sample in samples
        ],
        rubric_version="final-rubric/1.0",
        prompt_version="final-editorial-prompt/1.0",
    )


def test_a_provider_result_missing_a_dimension_is_rejected() -> None:
    request = request_for([UUID(int=900)])
    result = provider_result([]).model_copy(
        update={"dimension_scores": provider_result([]).dimension_scores[:-1]}
    )
    with pytest.raises(FinalEditorialProviderError, match="missing dimensions"):
        validate_result(result, request, known_sample_ids=[UUID(int=900)], known_shot_ids=[])


def test_a_provider_finding_citing_an_unsampled_frame_is_rejected() -> None:
    request = request_for([UUID(int=900)])
    result = provider_result(
        [
            proposal(
                category=FinalEditorialCategory.SCENE_COMPLETENESS,
                severity=FinalFindingSeverity.BLOCKING,
                confidence=0.9,
            ).model_copy(update={"sample_ids": [UUID(int=901)], "shot_ids": []})
        ]
    )
    with pytest.raises(FinalEditorialProviderError, match="unsampled frames"):
        validate_result(result, request, known_sample_ids=[UUID(int=900)], known_shot_ids=[])


def test_a_provider_finding_beyond_the_render_duration_is_rejected() -> None:
    request = request_for([UUID(int=900)])
    result = provider_result(
        [
            proposal(
                category=FinalEditorialCategory.PACING,
                severity=FinalFindingSeverity.WARNING,
                confidence=0.9,
                code=FinalIssueCode.PACING_PROBLEM,
            ).model_copy(
                update={
                    "sample_ids": [UUID(int=900)],
                    "shot_ids": [],
                    "start_us": TIMELINE_US,
                    "end_us": TIMELINE_US + 1,
                }
            )
        ]
    )
    with pytest.raises(FinalEditorialProviderError, match="beyond the final render duration"):
        validate_result(result, request, known_sample_ids=[UUID(int=900)], known_shot_ids=[])


def test_the_provider_registry_binds_a_role_to_a_configured_model() -> None:
    registry = build_registry(
        provider="openai", first_pass_model="model-a", adjudicator_model="model-b"
    )
    assert registry[role_for("first_pass")].model == "model-a"
    assert registry[role_for("adjudication")].model == "model-b"


def test_the_fake_provider_is_deterministic_for_the_same_request() -> None:
    import asyncio

    from services.qa.final_editorial_provider import FinalEditorialCall

    defect = FakeEditorialDefect(
        findings=(
            FakeEditorialFinding(
                category=FinalEditorialCategory.ENDING_COMPLETENESS,
                issue_code=FinalIssueCode.INCOMPLETE_ENDING,
                severity=FinalFindingSeverity.BLOCKING,
                summary="the recap stops mid-scene",
                start_us=0,
                end_us=SHOT_US,
                sample_index=0,
            ),
        )
    )
    provider = FakeFinalEditorialProvider({sha(3): defect})
    call = FinalEditorialCall(request=request_for([UUID(int=900)]))
    first = asyncio.run(provider.evaluate(call))
    second = asyncio.run(provider.evaluate(call))
    assert first.model_dump(mode="json") == second.model_dump(mode="json")
    assert len(first.dimension_scores) == len(EDITORIAL_DIMENSIONS)
    assert first.findings[0].issue_code is FinalIssueCode.INCOMPLETE_ENDING
