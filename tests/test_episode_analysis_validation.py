from uuid import uuid4

from services.analysis.openai_adapter import _response_text, _strict_schema
from services.analysis.validator import validate_episode_analysis
from vidgen.contracts.episode_analysis import EpisodeAnalysis


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


def test_unknown_character_and_overlapping_chronology_are_rejected() -> None:
    analysis = _golden().model_copy(deep=True)
    analysis.scenes[0].character_ids = [uuid4()]
    assert "UNKNOWN_CHARACTER" in {item.code for item in _validate(analysis).errors}


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
