from uuid import uuid4

import pytest
from pydantic import ValidationError

from services.script.compressor import compress_plot, structural_roles
from services.script.validator import validate_compressed_plot_plan
from tests.test_script_pipeline import _make_analysis
from vidgen.contracts.script import (
    ChannelVoiceConfig,
    CompressedPlotPlan,
    JokeAnnotation,
    OmittedPlotBeat,
    PlotCompressionRequest,
    RecapScript,
    ScriptSegment,
    TextSpan,
)


def _request(analysis, **overrides):
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
    return PlotCompressionRequest(**defaults)


def test_humor_intensity_out_of_range_is_rejected() -> None:
    analysis = _make_analysis(uuid4())
    with pytest.raises(ValidationError):
        from vidgen.contracts.script import ComedyWritingRequest

        ComedyWritingRequest(
            project_id=analysis.project_id,
            episode_analysis_id=analysis.episode_id,
            compressed_plot_plan_id=uuid4(),
            input_hash="a" * 64,
            idempotency_key="k",
            contract_version="1.0",
            prompt_version="comedy-script-v1",
            provider_configuration_version="fake-script-v1",
            compressed_plot=_valid_plan(analysis),
            channel_voice=ChannelVoiceConfig(narrator_persona="x"),
            humor_intensity=1.5,
            target_words=600,
        )


def _valid_plan(analysis) -> CompressedPlotPlan:
    request = _request(analysis)
    return compress_plot(analysis=analysis, request=request, plan_id=uuid4())


def test_recap_script_rejects_duplicate_or_nonmonotonic_sequences() -> None:
    analysis = _make_analysis(uuid4())
    plan = _valid_plan(analysis)
    beat = plan.selected_beats[0]
    segment_kwargs = dict(
        type="NARRATION",
        speaker_kind="narrator",
        text="Something happens.",
        plot_beat_ids=[beat.plot_beat_id],
        estimated_duration_ms=1000,
        content_hash="a" * 64,
    )
    with pytest.raises(ValidationError):
        RecapScript(
            script_id=uuid4(),
            version=1,
            project_id=analysis.project_id,
            episode_analysis_id=analysis.episode_id,
            compressed_plot_plan_id=plan.plan_id,
            target_duration_ms=1000,
            target_word_count=10,
            actual_word_count=10,
            voice_profile_ref="narrator",
            humor_intensity=0.5,
            segments=[
                ScriptSegment(segment_id=uuid4(), sequence=0, **segment_kwargs),
                ScriptSegment(segment_id=uuid4(), sequence=0, **segment_kwargs),
            ],
        )


def test_compressed_plot_plan_rejects_beat_in_both_selected_and_omitted() -> None:
    analysis = _make_analysis(uuid4())
    plan = _valid_plan(analysis)
    beat = plan.selected_beats[0]
    with pytest.raises(ValidationError):
        CompressedPlotPlan.model_validate(
            {
                **plan.model_dump(mode="json"),
                "omitted_beats": [
                    OmittedPlotBeat(plot_beat_id=beat.plot_beat_id, reason="dup").model_dump(
                        mode="json"
                    )
                ],
            }
        )


def test_joke_annotation_rejects_callback_id_on_non_callback_type() -> None:
    with pytest.raises(ValidationError):
        JokeAnnotation(
            joke_id=uuid4(),
            joke_type="commentary",
            callback_id=uuid4(),
            source_beat_ids=[uuid4()],
        )


def test_text_span_requires_end_after_start() -> None:
    with pytest.raises(ValidationError):
        TextSpan(start=5, end=5)


def test_omitted_beat_confusion_flag_requires_explanation() -> None:
    with pytest.raises(ValidationError):
        OmittedPlotBeat(plot_beat_id=uuid4(), reason="low value", may_cause_confusion=True)


def test_structural_roles_are_stable_and_cover_setup_and_resolution() -> None:
    analysis = _make_analysis(uuid4())
    roles = structural_roles(analysis.plot_beats)
    values = set(roles.values())
    assert "setup" in values
    assert "resolution" in values
    # Re-running must produce an identical mapping (used by both compressor and validator).
    assert roles == structural_roles(analysis.plot_beats)


def test_compression_retains_mandatory_and_required_beats() -> None:
    analysis = _make_analysis(uuid4())
    extra_required = analysis.plot_beats[7].plot_beat_id
    request = _request(analysis, required_beat_ids=[extra_required])
    plan = compress_plot(analysis=analysis, request=request, plan_id=uuid4())
    selected_ids = {beat.plot_beat_id for beat in plan.selected_beats}
    mandatory_ids = {beat.plot_beat_id for beat in analysis.plot_beats if beat.mandatory}
    assert mandatory_ids <= selected_ids
    assert extra_required in selected_ids
    report = validate_compressed_plot_plan(plan, analysis=analysis, request=request)
    assert report.valid, report.errors


def test_compression_preserves_dependency_order_and_causal_bridges() -> None:
    analysis = _make_analysis(uuid4())
    request = _request(analysis)
    plan = compress_plot(analysis=analysis, request=request, plan_id=uuid4())
    sequence_by_id = {beat.plot_beat_id: beat.sequence for beat in plan.selected_beats}
    selected_ids = set(sequence_by_id)
    for dependency in analysis.beat_dependencies:
        if dependency.cause_beat_id in selected_ids and dependency.effect_beat_id in selected_ids:
            assert (
                sequence_by_id[dependency.cause_beat_id] < sequence_by_id[dependency.effect_beat_id]
            )
    report = validate_compressed_plot_plan(plan, analysis=analysis, request=request)
    assert report.valid, report.errors


def test_omitted_beats_all_have_reasons() -> None:
    from vidgen.contracts.episode_analysis import PlotBeat, SourceReference

    project_id = uuid4()
    analysis = _make_analysis(project_id, beat_count=15)
    # Add independent low-value beats with no dependency edges, so the compressor
    # has genuine freedom to drop them without breaking any causal chain.
    ref = SourceReference(reference_type="project", reference_id=project_id)
    extra_beats = [
        PlotBeat(
            plot_beat_id=uuid4(),
            sequence=100 + i,
            scene_ids=[analysis.scenes[0].scene_id],
            summary=f"Minor aside {i}",
            importance=0.1,
            payoff_score=0.05,
            mandatory=False,
            source_references=[ref],
        )
        for i in range(5)
    ]
    analysis = analysis.model_copy(update={"plot_beats": [*analysis.plot_beats, *extra_beats]})
    request = _request(analysis, target_words=300)
    plan = compress_plot(analysis=analysis, request=request, plan_id=uuid4())
    assert plan.omitted_beats
    assert all(beat.reason.strip() for beat in plan.omitted_beats)


def test_word_budget_sums_within_two_percent_of_target() -> None:
    analysis = _make_analysis(uuid4())
    request = _request(analysis, target_words=777)
    plan = compress_plot(analysis=analysis, request=request, plan_id=uuid4())
    total = sum(item.words for item in plan.word_budget.allocations)
    assert abs(total - 777) / 777 <= 0.02


def test_unsupported_beat_id_is_rejected_by_validator() -> None:
    analysis = _make_analysis(uuid4())
    request = _request(analysis)
    plan = compress_plot(analysis=analysis, request=request, plan_id=uuid4())
    bogus = plan.selected_beats[0].model_copy(update={"plot_beat_id": uuid4()})
    tampered = plan.model_copy(update={"selected_beats": [bogus, *plan.selected_beats[1:]]})
    report = validate_compressed_plot_plan(tampered, analysis=analysis, request=request)
    assert not report.valid
    assert any(error.code == "UNKNOWN_BEAT" for error in report.errors)


def test_stable_plan_hash_is_deterministic_across_reruns() -> None:
    from services.script.canonicalize import canonical_plan_hash

    analysis = _make_analysis(uuid4())
    request = _request(analysis)
    plan_id = uuid4()
    plan_a = compress_plot(analysis=analysis, request=request, plan_id=plan_id)
    plan_b = compress_plot(analysis=analysis, request=request, plan_id=plan_id)
    assert canonical_plan_hash(plan_a) == canonical_plan_hash(plan_b)
