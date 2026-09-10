from uuid import uuid4

from services.analysis.openai_adapter import _response_text, _strict_schema
from services.analysis.validator import validate_episode_analysis
from vidgen.contracts.episode_analysis import (
    BeatDependency,
    CharacterCandidate,
    EpisodeAnalysis,
    PlotBeat,
    UnresolvedAmbiguity,
)


def _golden() -> EpisodeAnalysis:
    return EpisodeAnalysis.model_validate_json(
        open("tests/fixtures/contracts/episode_analysis.valid.json").read()
    )


def _validate(analysis: EpisodeAnalysis):
    scene = analysis.scenes[0]
    return validate_episode_analysis(
        analysis, valid_scene_ids={scene.scene_id}, valid_reference_ids={scene.scene_id}
    )


def test_golden_analysis_passes_all_deterministic_gates() -> None:
    assert _validate(_golden()).valid


def test_missing_and_cross_package_reference_is_rejected() -> None:
    analysis = _golden().model_copy(deep=True)
    analysis.source_references[0].reference_id = uuid4()
    assert "UNKNOWN_SOURCE_REFERENCE" in {item.code for item in _validate(analysis).errors}


def test_reference_timestamps_need_not_match_the_selected_evidence_exactly() -> None:
    """Membership in the selected evidence is the gate, not exact metadata.

    A model cannot reliably reproduce the scene_id and timestamps of the
    evidence it was handed, so a reference is judged by whether its
    reference_id is one of the selected ones. Only that is enforced; a
    disagreeing time boundary is tolerated.
    """
    analysis = _golden().model_copy(deep=True)
    expected = analysis.source_references[0].model_copy(deep=True)
    analysis.source_references[0].end_ms = 999
    scene = analysis.scenes[0]
    report = validate_episode_analysis(
        analysis,
        valid_scene_ids={scene.scene_id},
        valid_reference_ids={scene.scene_id},
        valid_references=[expected],
    )
    assert report.valid, report.errors


def test_scene_and_reference_ids_copied_from_the_input_are_accepted() -> None:
    """The reduce step must reuse upstream IDs rather than mint new ones.

    Every scene_id and reference_id in the analysis belongs to the selected
    evidence, so an output that copies them through is valid.
    """
    analysis = _golden().model_copy(deep=True)
    scene = analysis.scenes[0]
    reference = analysis.source_references[0]
    report = validate_episode_analysis(
        analysis,
        valid_scene_ids={scene.scene_id},
        valid_reference_ids={reference.reference_id},
    )
    assert report.valid, report.errors


def test_an_invented_scene_id_is_reported_as_scene_set_mismatch() -> None:
    """A freshly generated scene_id is the SCENE_SET_MISMATCH finding.

    Whether that finding fails the run is the deployment's choice — the code
    is tolerated by default — so the gate is asserted with nothing tolerated.
    """
    analysis = _golden().model_copy(deep=True)
    analysis.scenes[0].scene_id = uuid4()
    report = validate_episode_analysis(
        analysis,
        valid_scene_ids={_golden().scenes[0].scene_id},
        valid_reference_ids={analysis.source_references[0].reference_id},
        warn_only_codes=set(),
    )
    assert not report.valid
    assert "SCENE_SET_MISMATCH" in {item.code for item in report.errors}


def test_an_invented_scene_id_on_a_state_event_or_plot_beat_is_rejected() -> None:
    analysis = _golden().model_copy(deep=True)
    scene = analysis.scenes[0]
    reference = analysis.source_references[0]
    analysis.plot_beats = [
        PlotBeat(
            plot_beat_id=uuid4(),
            sequence=1,
            scene_ids=[uuid4()],
            summary="Beat citing a scene that does not exist",
            importance=1,
            payoff_score=1,
            mandatory=False,
            source_references=[reference],
        )
    ]
    report = validate_episode_analysis(
        analysis,
        valid_scene_ids={scene.scene_id},
        valid_reference_ids={reference.reference_id},
    )
    assert "UNKNOWN_SCENE" in {item.code for item in report.errors}


def test_an_invented_reference_id_is_rejected_everywhere_it_appears() -> None:
    """A generated reference_id is the UNKNOWN_SOURCE_REFERENCE failure."""
    analysis = _golden().model_copy(deep=True)
    scene = analysis.scenes[0]
    valid_reference_ids = {analysis.source_references[0].reference_id}
    analysis.scenes[0].source_references[0].reference_id = uuid4()
    report = validate_episode_analysis(
        analysis,
        valid_scene_ids={scene.scene_id},
        valid_reference_ids=valid_reference_ids,
    )
    codes = {item.code for item in report.errors}
    paths = {item.entity_path for item in report.errors}
    assert "UNKNOWN_SOURCE_REFERENCE" in codes
    assert "scenes.0.source_references.0.reference_id" in paths


def test_a_warn_only_code_is_demoted_to_a_warning_and_the_report_stays_valid() -> None:
    """A tolerated code is reported, not failed.

    A reduce model that renames a scene ID it was told to copy produces a
    SCENE_SET_MISMATCH. With the code tolerated the finding stays visible in
    the report as a warning instead of failing the run and paying to generate
    the analysis again.
    """
    analysis = _golden().model_copy(deep=True)
    scene = analysis.scenes[0]
    report = validate_episode_analysis(
        analysis,
        valid_scene_ids={uuid4()},
        valid_reference_ids={scene.scene_id},
        warn_only_codes={"SCENE_SET_MISMATCH"},
    )
    assert report.valid, report.errors
    assert "SCENE_SET_MISMATCH" not in {item.code for item in report.errors}
    assert "SCENE_SET_MISMATCH" in {item.code for item in report.warnings}


def test_a_code_outside_warn_only_codes_still_fails_validation() -> None:
    analysis = _golden().model_copy(deep=True)
    scene = analysis.scenes[0]
    report = validate_episode_analysis(
        analysis,
        valid_scene_ids={uuid4()},
        valid_reference_ids={scene.scene_id},
        warn_only_codes={"UNSUPPORTED_ALIAS_MERGE"},
    )
    assert not report.valid
    assert "SCENE_SET_MISMATCH" in {item.code for item in report.errors}


def test_an_empty_warn_only_set_tolerates_nothing() -> None:
    """``None`` means the default; an empty set explicitly means "tolerate nothing"."""
    analysis = _golden().model_copy(deep=True)
    scene = analysis.scenes[0]
    report = validate_episode_analysis(
        analysis,
        valid_scene_ids={uuid4()},
        valid_reference_ids={scene.scene_id},
        warn_only_codes=set(),
    )
    assert not report.valid
    assert "SCENE_SET_MISMATCH" in {item.code for item in report.errors}


def test_unknown_character_and_overlapping_chronology_are_rejected() -> None:
    analysis = _golden().model_copy(deep=True)
    analysis.scenes[0].character_ids = [uuid4()]
    assert "UNKNOWN_CHARACTER" in {item.code for item in _validate(analysis).errors}


def test_alias_merge_without_specific_evidence_is_rejected() -> None:
    analysis = _golden().model_copy(deep=True)
    reference = analysis.source_references[0]
    analysis.characters = [
        CharacterCandidate(
            character_id=uuid4(),
            canonical_name="Speaker 1",
            aliases=["Alex"],
            anonymous=True,
            confidence=0.5,
            source_references=[reference],
        )
    ]
    assert "UNSUPPORTED_ALIAS_MERGE" in {item.code for item in _validate(analysis).errors}


def test_mandatory_beat_and_dependency_failures_are_structured() -> None:
    analysis = _golden().model_copy(deep=True)
    scene_id = analysis.scenes[0].scene_id
    first, second = uuid4(), uuid4()
    analysis.plot_beats = [
        PlotBeat(
            plot_beat_id=first,
            sequence=1,
            scene_ids=[scene_id],
            summary="Cause",
            importance=1,
            payoff_score=0,
            mandatory=True,
        ),
        PlotBeat(
            plot_beat_id=second,
            sequence=2,
            scene_ids=[scene_id],
            summary="Effect",
            importance=1,
            payoff_score=1,
            mandatory=False,
            source_references=analysis.source_references,
        ),
    ]
    analysis.beat_dependencies = [
        BeatDependency(
            cause_beat_id=second, effect_beat_id=first, source_references=analysis.source_references
        )
    ]
    codes = {item.code for item in _validate(analysis).errors}
    assert {"MANDATORY_BEAT_WITHOUT_EVIDENCE", "CAUSE_AFTER_EFFECT"} <= codes


def test_missing_dependency_endpoint_is_rejected() -> None:
    analysis = _golden().model_copy(deep=True)
    analysis.beat_dependencies = [
        BeatDependency(
            cause_beat_id=uuid4(),
            effect_beat_id=uuid4(),
            source_references=analysis.source_references,
        )
    ]
    assert "UNKNOWN_BEAT_DEPENDENCY" in {item.code for item in _validate(analysis).errors}


def test_an_anonymous_speaker_left_unresolved_is_reported_but_does_not_block() -> None:
    """Reported as a warning, not an error.

    Quietly resolving an anonymous speaker into a character is worth
    surfacing, but not worth discarding an otherwise sound analysis and paying
    to generate it again.
    """
    analysis = _golden()
    scene = analysis.scenes[0]
    report = validate_episode_analysis(
        analysis,
        valid_scene_ids={scene.scene_id},
        valid_reference_ids={scene.scene_id},
        required_anonymous_labels={"speaker_001"},
    )
    assert "AMBIGUOUS_IDENTITY_RESOLVED_WITHOUT_EVIDENCE" in {item.code for item in report.warnings}
    assert "speaker_001" in " ".join(item.message for item in report.warnings)
    assert report.valid, report.errors


def test_an_anonymous_speaker_declared_unresolved_produces_no_warning() -> None:
    analysis = _golden().model_copy(deep=True)
    scene = analysis.scenes[0]
    analysis.unresolved_ambiguities = [
        UnresolvedAmbiguity(
            ambiguity_id=uuid4(),
            description="speaker_001 could not be attributed to any character",
            source_references=analysis.source_references[:1],
        )
    ]
    report = validate_episode_analysis(
        analysis,
        valid_scene_ids={scene.scene_id},
        valid_reference_ids={scene.scene_id},
        required_anonymous_labels={"speaker_001"},
    )
    assert not report.warnings
    assert report.valid, report.errors


def test_strict_openai_schema_requires_every_property_and_closes_objects() -> None:
    schema = _strict_schema(EpisodeAnalysis.model_json_schema())
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_raw_responses_output_array_is_parsed() -> None:
    assert (
        _response_text(
            {
                "status": "completed",
                "output": [{"content": [{"type": "output_text", "text": "{}"}]}],
            }
        )
        == "{}"
    )
