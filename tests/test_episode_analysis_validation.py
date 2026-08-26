from uuid import uuid4

from services.analysis.openai_adapter import _response_text, _strict_schema
from services.analysis.validator import validate_episode_analysis
from vidgen.contracts.episode_analysis import (
    BeatDependency,
    CharacterCandidate,
    EpisodeAnalysis,
    PlotBeat,
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


def test_reference_scope_must_match_selected_evidence() -> None:
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
    assert "SOURCE_REFERENCE_SCOPE_MISMATCH" in {item.code for item in report.errors}


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


def test_anonymous_speaker_must_remain_unresolved() -> None:
    analysis = _golden()
    scene = analysis.scenes[0]
    report = validate_episode_analysis(
        analysis,
        valid_scene_ids={scene.scene_id},
        valid_reference_ids={scene.scene_id},
        required_anonymous_labels={"speaker_001"},
    )
    assert "AMBIGUOUS_IDENTITY_RESOLVED_WITHOUT_EVIDENCE" in {item.code for item in report.errors}


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
