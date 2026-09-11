from uuid import uuid4

import pytest
from pydantic import ValidationError

from services.script.compressor import compress_plot, structural_roles
from services.script.settings import resolve_script_settings
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


def test_resolve_script_settings_honors_prohibited_patterns_key() -> None:
    # Regression for a Copilot review finding: settings must read the same
    # "prohibited_patterns" key the pipeline/contracts/docs use, not a
    # differently-named key that would silently disable enforcement.
    from vidgen.db.models import Project

    project = Project(
        name="test",
        visual_style="flat",
        target_duration_seconds=240,
        humor_intensity=5,
        settings={"script": {"prohibited_patterns": ["banned phrase"]}},
    )
    settings = resolve_script_settings(project)
    assert settings.prohibited_patterns == ["banned phrase"]


def test_resolve_script_settings_rejects_non_mapping_script_settings() -> None:
    # Regression for a Copilot review finding: a malformed
    # project.settings["script"] (not an object) must raise the intended
    # ScriptSettingsError, not an unhandled TypeError/ValueError from dict().
    from services.script.settings import ScriptSettingsError
    from vidgen.db.models import Project

    project = Project(
        name="test",
        visual_style="flat",
        target_duration_seconds=240,
        humor_intensity=5,
        settings={"script": "not-an-object"},
    )
    with pytest.raises(ScriptSettingsError):
        resolve_script_settings(project)


def test_resolve_script_settings_rejects_non_numeric_words_per_minute() -> None:
    # Regression for a Copilot review finding: a malformed numeric override
    # (e.g. a non-numeric string) must raise ScriptSettingsError, not an
    # unhandled TypeError/ValueError from int()/float().
    from services.script.settings import ScriptSettingsError
    from vidgen.db.models import Project

    project = Project(
        name="test",
        visual_style="flat",
        target_duration_seconds=240,
        humor_intensity=5,
        settings={"script": {"target_words_per_minute": "fast"}},
    )
    with pytest.raises(ScriptSettingsError):
        resolve_script_settings(project)


def test_excluded_topic_causal_ancestor_is_still_selected_for_completeness() -> None:
    # A beat matching an excluded topic must still be pulled in via causal closure
    # when a required/mandatory beat causally depends on it; causal completeness
    # takes precedence over topic exclusion (regression for a Copilot review finding).
    analysis = _make_analysis(uuid4(), beat_count=15)
    excluded_beat = analysis.plot_beats[5]
    tampered_beat = excluded_beat.model_copy(update={"summary": excluded_beat.summary + " wombat"})
    beats = [
        tampered_beat if b.plot_beat_id == excluded_beat.plot_beat_id else b
        for b in analysis.plot_beats
    ]
    analysis = analysis.model_copy(update={"plot_beats": beats})

    request = _request(analysis, excluded_topics=["wombat"])
    plan = compress_plot(analysis=analysis, request=request, plan_id=uuid4())

    selected_ids = {beat.plot_beat_id for beat in plan.selected_beats}
    assert excluded_beat.plot_beat_id in selected_ids
    assert not any(beat.plot_beat_id == excluded_beat.plot_beat_id for beat in plan.omitted_beats)
    report = validate_compressed_plot_plan(plan, analysis=analysis, request=request)
    assert report.valid, report.errors
    assert not any(error.code == "MISSING_CAUSAL_BRIDGE" for error in report.errors)


def test_stable_plan_hash_is_deterministic_across_reruns() -> None:
    from services.script.canonicalize import canonical_plan_hash

    analysis = _make_analysis(uuid4())
    request = _request(analysis)
    plan_id = uuid4()
    plan_a = compress_plot(analysis=analysis, request=request, plan_id=plan_id)
    plan_b = compress_plot(analysis=analysis, request=request, plan_id=plan_id)
    assert canonical_plan_hash(plan_a) == canonical_plan_hash(plan_b)


def _plan_with_an_invented_reference(analysis, request):
    """A compressed plan whose first beat cites a reference_id nothing issued."""
    plan = compress_plot(analysis=analysis, request=request, plan_id=uuid4())
    beat = plan.selected_beats[0]
    invented = beat.source_references[0].model_copy(update={"reference_id": uuid4()})
    tampered_beat = beat.model_copy(update={"source_references": [invented]})
    return plan.model_copy(update={"selected_beats": [tampered_beat, *plan.selected_beats[1:]]})


def test_a_warn_only_code_is_demoted_to_a_warning_and_the_plan_stays_valid() -> None:
    """A compressor that invents a reference_id is reported, not failed.

    The citation is wrong, but the plan it describes is otherwise sound, and
    failing it pays to compress the whole episode again.
    """
    analysis = _make_analysis(uuid4())
    request = _request(analysis)
    plan = _plan_with_an_invented_reference(analysis, request)
    report = validate_compressed_plot_plan(
        plan, analysis=analysis, request=request, warn_only_codes={"UNKNOWN_SOURCE_REFERENCE"}
    )
    assert report.valid, report.errors
    assert "UNKNOWN_SOURCE_REFERENCE" not in {error.code for error in report.errors}
    assert "UNKNOWN_SOURCE_REFERENCE" in {note.code for note in report.warnings}


def test_a_code_outside_warn_only_codes_still_fails_compression_validation() -> None:
    analysis = _make_analysis(uuid4())
    request = _request(analysis)
    plan = _plan_with_an_invented_reference(analysis, request)
    report = validate_compressed_plot_plan(
        plan, analysis=analysis, request=request, warn_only_codes={"UNKNOWN_BEAT"}
    )
    assert not report.valid
    assert "UNKNOWN_SOURCE_REFERENCE" in {error.code for error in report.errors}


def test_an_empty_warn_only_set_tolerates_nothing_in_compression() -> None:
    """``None`` means the default; an empty set explicitly means "tolerate nothing"."""
    analysis = _make_analysis(uuid4())
    request = _request(analysis)
    plan = _plan_with_an_invented_reference(analysis, request)
    report = validate_compressed_plot_plan(
        plan, analysis=analysis, request=request, warn_only_codes=set()
    )
    assert not report.valid
    assert "UNKNOWN_SOURCE_REFERENCE" in {error.code for error in report.errors}


def test_the_default_tolerates_an_invented_reference_but_not_an_unknown_beat() -> None:
    analysis = _make_analysis(uuid4())
    request = _request(analysis)
    assert validate_compressed_plot_plan(
        _plan_with_an_invented_reference(analysis, request), analysis=analysis, request=request
    ).valid
    plan = compress_plot(analysis=analysis, request=request, plan_id=uuid4())
    bogus = plan.selected_beats[0].model_copy(update={"plot_beat_id": uuid4()})
    tampered = plan.model_copy(update={"selected_beats": [bogus, *plan.selected_beats[1:]]})
    assert not validate_compressed_plot_plan(tampered, analysis=analysis, request=request).valid


def test_every_code_either_t11_validator_emits_may_be_tolerated() -> None:
    """The eligible set is the union of both validators' vocabularies, deduplicated."""
    from vidgen.contracts.script import (
        PLOT_PLAN_VALIDATION_CODES,
        RECAP_SCRIPT_VALIDATION_CODES,
        SCRIPT_WARN_ONLY_ELIGIBLE_VALIDATION_CODES,
    )

    assert set(SCRIPT_WARN_ONLY_ELIGIBLE_VALIDATION_CODES) == set(PLOT_PLAN_VALIDATION_CODES) | set(
        RECAP_SCRIPT_VALIDATION_CODES
    )
    assert len(SCRIPT_WARN_ONLY_ELIGIBLE_VALIDATION_CODES) == len(
        set(SCRIPT_WARN_ONLY_ELIGIBLE_VALIDATION_CODES)
    )


def test_every_structural_compression_code_may_be_tolerated() -> None:
    """A structural omission is now an owner's choice, not an unconditional failure."""
    from vidgen.contracts.script import SCRIPT_WARN_ONLY_ELIGIBLE_VALIDATION_CODES

    analysis = _make_analysis(uuid4())
    request = _request(analysis)
    plan = compress_plot(analysis=analysis, request=request, plan_id=uuid4())
    stripped = plan.model_copy(update={"selected_beats": plan.selected_beats[:1]})
    report = validate_compressed_plot_plan(
        stripped,
        analysis=analysis,
        request=request,
        warn_only_codes=set(SCRIPT_WARN_ONLY_ELIGIBLE_VALIDATION_CODES),
    )
    assert report.valid, report.errors
    assert report.warnings
