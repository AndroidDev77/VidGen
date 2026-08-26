from uuid import uuid4

from services.script.compressor import compress_plot
from services.script.diff import build_script_diff
from services.script.editor import propose_revision
from services.script.rubric import approval_recommendation, default_rubric, score_script
from services.script.validator import (
    build_beat_coverage,
    canonical_word_count,
    ngram_overlap_ratio,
    validate_recap_script,
)
from services.script.writer import write_script
from tests.test_script_pipeline import _make_analysis
from vidgen.contracts.script import ChannelVoiceConfig, ComedyWritingRequest, PlotCompressionRequest


def _plan(analysis, **overrides):
    defaults = dict(
        project_id=analysis.project_id,
        episode_analysis_id=analysis.episode_id,
        episode_analysis=analysis,
        input_hash="a" * 64,
        idempotency_key="k",
        contract_version="1.0",
        prompt_version="comedy-script-v1",
        provider_configuration_version="fake-script-v1",
        target_duration_ms=240_000,
        target_words=600,
        target_words_per_minute=150,
        required_beat_ids=[],
        excluded_topics=[],
        recap_mode="full_recap",
    )
    defaults.update(overrides)
    request = PlotCompressionRequest(**defaults)
    return request, compress_plot(analysis=analysis, request=request, plan_id=uuid4())


def _script(analysis, plan, **overrides):
    defaults = dict(
        project_id=analysis.project_id,
        episode_analysis_id=analysis.episode_id,
        compressed_plot_plan_id=plan.plan_id,
        input_hash="a" * 64,
        idempotency_key="k",
        contract_version="1.0",
        prompt_version="comedy-script-v1",
        provider_configuration_version="fake-script-v1",
        compressed_plot=plan,
        channel_voice=ChannelVoiceConfig(narrator_persona="Wry narrator"),
        humor_intensity=0.8,
        target_words=plan.word_budget.total_target_words,
        recap_mode="full_recap",
    )
    defaults.update(overrides)
    request = ComedyWritingRequest(**defaults)
    return write_script(plan=plan, request=request, script_id=uuid4(), version=1)


def test_valid_script_covers_every_selected_and_mandatory_beat() -> None:
    analysis = _make_analysis(uuid4())
    _, plan = _plan(analysis)
    script = _script(analysis, plan)
    report = validate_recap_script(script, analysis=analysis, plan=plan)
    assert report.valid, report.errors
    mandatory_ids = {beat.plot_beat_id for beat in plan.selected_beats if beat.mandatory}
    covered = {item.plot_beat_id for item in script.beat_coverage if item.coverage == "covered"}
    assert mandatory_ids <= covered
    selected_ids = {beat.plot_beat_id for beat in plan.selected_beats}
    assert selected_ids <= covered


def test_missing_beat_coverage_is_detected() -> None:
    analysis = _make_analysis(uuid4())
    _, plan = _plan(analysis)
    script = _script(analysis, plan)
    dropped_beat_id = script.segments[0].plot_beat_ids[0]
    tampered_segments = [
        segment.model_copy(
            update={"plot_beat_ids": [b for b in segment.plot_beat_ids if b != dropped_beat_id]}
        )
        if segment.segment_id == script.segments[0].segment_id
        else segment
        for segment in script.segments
    ]
    tampered = script.model_copy(update={"segments": tampered_segments})
    coverage = build_beat_coverage(tampered, plan)
    tampered = tampered.model_copy(update={"beat_coverage": coverage})
    report = validate_recap_script(tampered, analysis=analysis, plan=plan)
    assert not report.valid
    codes = {error.code for error in report.errors}
    assert "BEAT_NOT_COVERED" in codes or "MANDATORY_BEAT_NOT_COVERED" in codes


def test_word_count_outside_tolerance_is_rejected() -> None:
    analysis = _make_analysis(uuid4())
    _, plan = _plan(analysis)
    script = _script(analysis, plan)
    tampered = script.model_copy(update={"target_word_count": script.actual_word_count * 3})
    report = validate_recap_script(tampered, analysis=analysis, plan=plan)
    assert not report.valid
    assert any(error.code == "WORD_COUNT_OUT_OF_RANGE" for error in report.errors)


def test_canonical_word_count_matches_actual_word_count_field() -> None:
    analysis = _make_analysis(uuid4())
    _, plan = _plan(analysis)
    script = _script(analysis, plan)
    assert script.actual_word_count == sum(
        canonical_word_count(segment.text) for segment in script.segments
    )


def test_unknown_plot_beat_reference_is_rejected() -> None:
    analysis = _make_analysis(uuid4())
    _, plan = _plan(analysis)
    script = _script(analysis, plan)
    tampered_first = script.segments[0].model_copy(update={"plot_beat_ids": [uuid4()]})
    tampered = script.model_copy(update={"segments": [tampered_first, *script.segments[1:]]})
    report = validate_recap_script(tampered, analysis=analysis, plan=plan)
    assert not report.valid
    assert any(error.code == "UNKNOWN_PLOT_BEAT_REFERENCE" for error in report.errors)


def test_callback_payoff_must_occur_after_setup() -> None:
    analysis = _make_analysis(uuid4())
    _, plan = _plan(analysis)
    script = _script(analysis, plan)
    assert script.callbacks, "fixture expected to produce a callback"
    callback = script.callbacks[0]
    reversed_callback = callback.model_copy(
        update={
            "setup_segment_id": callback.payoff_segment_id,
            "payoff_segment_id": callback.setup_segment_id,
        }
    )
    tampered = script.model_copy(update={"callbacks": [reversed_callback]})
    report = validate_recap_script(tampered, analysis=analysis, plan=plan)
    assert not report.valid
    assert any(error.code == "CALLBACK_PAYOFF_BEFORE_SETUP" for error in report.errors)


def test_joke_span_beyond_text_length_is_rejected() -> None:
    analysis = _make_analysis(uuid4())
    _, plan = _plan(analysis)
    script = _script(analysis, plan)
    segment = script.segments[0]
    joke = segment.joke_annotations[0]
    from vidgen.contracts.script import TextSpan

    bad_joke = joke.model_copy(
        update={"punchline_span": TextSpan(start=0, end=len(segment.text) + 500)}
    )
    tampered_segment = segment.model_copy(update={"joke_annotations": [bad_joke]})
    tampered = script.model_copy(update={"segments": [tampered_segment, *script.segments[1:]]})
    report = validate_recap_script(tampered, analysis=analysis, plan=plan)
    assert not report.valid
    assert any(error.code == "INVALID_JOKE_SPAN" for error in report.errors)


def test_prohibited_pattern_is_rejected() -> None:
    analysis = _make_analysis(uuid4())
    _, plan = _plan(analysis)
    script = _script(analysis, plan)
    report = validate_recap_script(
        script, analysis=analysis, plan=plan, prohibited_patterns=["Real subtle"]
    )
    assert not report.valid
    assert any(error.code == "PROHIBITED_PATTERN" for error in report.errors)


def test_near_verbatim_transcript_copying_is_detected() -> None:
    transcript = (
        "the quick brown fox jumps directly over the extremely lazy dog while everyone "
        "watches in complete silence and total disbelief at the whole thing"
    )
    assert ngram_overlap_ratio(transcript, transcript) == 1.0
    assert ngram_overlap_ratio("totally unrelated text about spaceships", transcript) < 0.5


def test_locked_segment_is_rejected_if_changed_between_versions() -> None:
    analysis = _make_analysis(uuid4())
    _, plan = _plan(analysis)
    script = _script(analysis, plan)
    locked_first = script.segments[0].model_copy(update={"locked": True})
    previous = script.model_copy(update={"segments": [locked_first, *script.segments[1:]]})
    changed_first = locked_first.model_copy(
        update={"text": locked_first.text + " extra", "content_hash": "b" * 64}
    )
    current = script.model_copy(update={"segments": [changed_first, *script.segments[1:]]})
    report = validate_recap_script(current, analysis=analysis, plan=plan, previous_script=previous)
    assert not report.valid
    assert any(error.code == "LOCKED_SEGMENT_CHANGED" for error in report.errors)


def test_editor_scores_and_approval_thresholds() -> None:
    analysis = _make_analysis(uuid4())
    _, plan = _plan(analysis)
    script = _script(analysis, plan)
    report = validate_recap_script(script, analysis=analysis, plan=plan)
    scores = score_script(script, validation_error_count=len(report.errors))
    rubric = default_rubric()
    approved = approval_recommendation(
        scores,
        rubric,
        mandatory_coverage_ratio=1.0,
        word_count_within_target=True,
        validation_valid=True,
    )
    assert approved == "approve"
    # An overall score below threshold must not approve even if everything else passes.
    low_scores = scores.model_copy(update={"overall": 50})
    assert (
        approval_recommendation(
            low_scores,
            rubric,
            mandatory_coverage_ratio=1.0,
            word_count_within_target=True,
            validation_valid=True,
        )
        != "approve"
    )
    # Plot fidelity below threshold must not approve even with a high overall score.
    low_fidelity = scores.model_copy(update={"overall": 99, "plot_fidelity": 50})
    assert (
        approval_recommendation(
            low_fidelity,
            rubric,
            mandatory_coverage_ratio=1.0,
            word_count_within_target=True,
            validation_valid=True,
        )
        != "approve"
    )


def test_revision_reducing_mandatory_coverage_is_rejected() -> None:
    analysis = _make_analysis(uuid4())
    _, plan = _plan(analysis)
    script = _script(analysis, plan)
    previous_coverage = {item.plot_beat_id: item.coverage for item in script.beat_coverage}
    mandatory_beat_id = next(item.plot_beat_id for item in script.beat_coverage if item.mandatory)
    regressed_segments = [
        segment.model_copy(
            update={"plot_beat_ids": [b for b in segment.plot_beat_ids if b != mandatory_beat_id]}
        )
        for segment in script.segments
    ]
    regressed = script.model_copy(update={"segments": regressed_segments})
    coverage = build_beat_coverage(regressed, plan)
    regressed = regressed.model_copy(update={"beat_coverage": coverage})
    report = validate_recap_script(
        regressed, analysis=analysis, plan=plan, previous_coverage=previous_coverage
    )
    assert not report.valid
    assert any(error.code == "COVERAGE_REGRESSED" for error in report.errors)


def test_propose_revision_produces_structured_diff_and_converges() -> None:
    analysis = _make_analysis(uuid4())
    _, plan = _plan(analysis)
    script = _script(analysis, plan)
    edits, revised = propose_revision(script)
    diff = build_script_diff(script, revised, edits)
    assert diff.from_version == script.version
    assert diff.to_version == revised.version
    if edits:
        assert diff.changed_segments
        assert all(item.segment_id for item in diff.changed_segments)
    else:
        assert not diff.changed_segments


def test_diff_against_no_previous_version_marks_everything_added() -> None:
    analysis = _make_analysis(uuid4())
    _, plan = _plan(analysis)
    script = _script(analysis, plan)
    diff = build_script_diff(None, script, [])
    assert diff.from_version is None
    assert set(diff.added_segment_ids) == {segment.segment_id for segment in script.segments}


def test_locked_segment_spanning_multiple_beats_is_not_duplicated() -> None:
    # Regression for a Copilot review finding: a locked segment tagged with more
    # than one beat ID must be appended once, not once per beat it covers, and
    # must be re-sequenced to its new position rather than keeping a stale one.
    analysis = _make_analysis(uuid4())
    _request, plan = _plan(analysis)
    first_two = plan.selected_beats[:2]
    shared_segment = (
        _script(analysis, plan)
        .segments[0]
        .model_copy(
            update={
                "plot_beat_ids": [first_two[0].plot_beat_id, first_two[1].plot_beat_id],
                "locked": True,
                "sequence": 99,
            }
        )
    )
    writing_request = ComedyWritingRequest(
        project_id=analysis.project_id,
        episode_analysis_id=analysis.episode_id,
        compressed_plot_plan_id=plan.plan_id,
        input_hash="a" * 64,
        idempotency_key="k",
        contract_version="1.0",
        prompt_version="comedy-script-v1",
        provider_configuration_version="fake-script-v1",
        compressed_plot=plan,
        channel_voice=ChannelVoiceConfig(narrator_persona="Wry narrator"),
        humor_intensity=0.8,
        target_words=plan.word_budget.total_target_words,
        recap_mode="full_recap",
        locked_segments=[shared_segment],
    )
    script = write_script(plan=plan, request=writing_request, script_id=uuid4(), version=2)

    segment_ids = [segment.segment_id for segment in script.segments]
    assert len(segment_ids) == len(set(segment_ids))
    matches = [
        segment for segment in script.segments if segment.segment_id == shared_segment.segment_id
    ]
    assert len(matches) == 1
    assert matches[0].sequence == 0
    report = validate_recap_script(script, analysis=analysis, plan=plan)
    assert report.valid, report.errors
