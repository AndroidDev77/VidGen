"""The comma-separated list settings must load from the environment.

``.env.example`` documents these as comma-separated lists, and
``docker-compose.local.yml`` feeds ``.env`` into every container as real
environment variables. pydantic-settings treats a sequence field as complex and
JSON-decodes it inside the settings source - before any validator runs - so
without ``NoDecode`` a documented value aborted worker start-up with
``JSONDecodeError: Expecting value: line 1 column 1 (char 0)``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from apps.api.settings import APISettings
from services.qa.rubric import THRESHOLDS
from vidgen.contracts.narration import NarrationQualityThresholds
from vidgen.contracts.visual_qa import DEFAULT_VISUAL_QA_WARN_ONLY_CODES

LIST_SETTINGS = (
    ("VIDGEN_ALLOWED_VIDEO_TYPES", "allowed_video_types"),
    ("VIDGEN_CORS_ALLOWED_ORIGINS", "cors_allowed_origins"),
    ("VIDGEN_SUBTITLE_LANGUAGES", "subtitle_languages"),
    ("VIDGEN_YOUTUBE_OAUTH_REDIRECT_TARGETS", "youtube_oauth_redirect_targets"),
)
#: Parsed the same way, but its values are a bounded vocabulary rather than
#: free text, so it is exercised separately below.
CODE_LIST_SETTINGS = (
    "VIDGEN_WARN_ONLY_VALIDATION_CODES",
    "VIDGEN_SCRIPT_WARN_ONLY_VALIDATION_CODES",
)


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # A developer's own environment must not decide the outcome of these tests;
    # every case below also constructs the settings with ``_env_file=None``.
    for variable, _ in LIST_SETTINGS:
        monkeypatch.delenv(variable, raising=False)
    for variable in CODE_LIST_SETTINGS:
        monkeypatch.delenv(variable, raising=False)


@pytest.mark.parametrize(("variable", "field"), LIST_SETTINGS)
def test_a_comma_separated_environment_value_is_split(
    monkeypatch: pytest.MonkeyPatch, variable: str, field: str
) -> None:
    monkeypatch.setenv(variable, "one, two ,three")
    assert getattr(APISettings(_env_file=None), field) == ("one", "two", "three")


@pytest.mark.parametrize(("variable", "field"), LIST_SETTINGS)
def test_a_single_environment_value_is_a_one_element_tuple(
    monkeypatch: pytest.MonkeyPatch, variable: str, field: str
) -> None:
    monkeypatch.setenv(variable, "one")
    assert getattr(APISettings(_env_file=None), field) == ("one",)


@pytest.mark.parametrize(("variable", "field"), LIST_SETTINGS)
def test_an_empty_environment_value_is_an_empty_tuple(
    monkeypatch: pytest.MonkeyPatch, variable: str, field: str
) -> None:
    monkeypatch.setenv(variable, "")
    assert getattr(APISettings(_env_file=None), field) == ()


def test_the_documented_media_types_load_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The exact value shipped in .env.example.
    monkeypatch.setenv("VIDGEN_ALLOWED_VIDEO_TYPES", "video/mp4,video/quicktime")
    assert APISettings(_env_file=None).allowed_video_types == ("video/mp4", "video/quicktime")


def test_subtitle_languages_are_normalized_to_lower_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDGEN_SUBTITLE_LANGUAGES", "EN, Fr")
    assert APISettings(_env_file=None).subtitle_languages == ("en", "fr")


def test_the_defaults_survive_an_unset_environment() -> None:
    settings = APISettings(_env_file=None)
    assert settings.allowed_video_types == ("video/mp4", "video/quicktime")
    assert settings.cors_allowed_origins == ()
    assert settings.subtitle_languages == ("en",)
    assert settings.youtube_oauth_redirect_targets == ("/",)


def test_warn_only_validation_codes_default_to_the_scene_set_mismatch_tolerance() -> None:
    """The deployment default, which every project without an override uses."""
    assert APISettings(_env_file=None).warn_only_validation_codes == ["SCENE_SET_MISMATCH"]


def test_warn_only_validation_codes_load_as_a_comma_separated_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDGEN_WARN_ONLY_VALIDATION_CODES", "scene_set_mismatch, duplicate_id")
    assert APISettings(_env_file=None).warn_only_validation_codes == [
        "DUPLICATE_ID",
        "SCENE_SET_MISMATCH",
    ]


def test_an_unknown_warn_only_validation_code_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A code the validator never emits would be stored and silently match
    # nothing, so start-up fails instead of tolerating a typo.
    monkeypatch.setenv("VIDGEN_WARN_ONLY_VALIDATION_CODES", "SCENE_SET_MISMATCH,NOT_A_CODE")
    with pytest.raises(ValidationError):
        APISettings(_env_file=None)


def test_script_warn_only_validation_codes_default_to_the_reference_tolerance() -> None:
    """The T11 compression default, which every project without an override uses."""
    assert APISettings(_env_file=None).script_warn_only_validation_codes == [
        "UNKNOWN_SOURCE_REFERENCE"
    ]


def test_script_warn_only_validation_codes_load_as_a_comma_separated_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "VIDGEN_SCRIPT_WARN_ONLY_VALIDATION_CODES", "unknown_beat, unknown_source_reference"
    )
    assert APISettings(_env_file=None).script_warn_only_validation_codes == [
        "UNKNOWN_BEAT",
        "UNKNOWN_SOURCE_REFERENCE",
    ]


def test_an_unknown_script_warn_only_validation_code_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # SCENE_SET_MISMATCH is a real code, but of the analysis validator: the
    # compression validator never emits it, so it would silently match nothing.
    monkeypatch.setenv("VIDGEN_SCRIPT_WARN_ONLY_VALIDATION_CODES", "SCENE_SET_MISMATCH")
    with pytest.raises(ValidationError):
        APISettings(_env_file=None)


def test_narration_warn_only_quality_codes_default_to_alignment_coverage() -> None:
    """Whisper coverage warns by default; every other quality code still fails a take."""
    settings = APISettings(_env_file=None)
    assert settings.narration_warn_only_quality_codes == ["alignment_coverage"]
    assert settings.narration_quality_thresholds() == NarrationQualityThresholds()


def test_narration_warn_only_quality_codes_load_as_a_comma_separated_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "VIDGEN_NARRATION_WARN_ONLY_QUALITY_CODES", "Speaking_Rate, alignment_coverage"
    )
    assert APISettings(_env_file=None).narration_warn_only_quality_codes == [
        "alignment_coverage",
        "speaking_rate",
    ]


def test_an_unknown_narration_quality_code_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    # SCENE_SET_MISMATCH is an analysis validation code; the quality gate never emits it.
    monkeypatch.setenv("VIDGEN_NARRATION_WARN_ONLY_QUALITY_CODES", "SCENE_SET_MISMATCH")
    with pytest.raises(ValidationError):
        APISettings(_env_file=None)


def test_visual_qa_warn_only_codes_default_to_the_contracts_set() -> None:
    settings = APISettings(_env_file=None)
    assert settings.visual_qa_warn_only_codes == sorted(DEFAULT_VISUAL_QA_WARN_ONLY_CODES)
    assert settings.visual_qa_thresholds() == THRESHOLDS


def test_visual_qa_warn_only_codes_load_as_a_comma_separated_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDGEN_VISUAL_QA_WARN_ONLY_CODES", "anatomy_breakage, Prompt_Too_Complex")
    settings = APISettings(_env_file=None)
    assert settings.visual_qa_warn_only_codes == ["ANATOMY_BREAKAGE", "PROMPT_TOO_COMPLEX"]
    assert settings.visual_qa_thresholds().warn_only_codes == [
        "ANATOMY_BREAKAGE",
        "PROMPT_TOO_COMPLEX",
    ]
    monkeypatch.setenv("VIDGEN_VISUAL_QA_WARN_ONLY_CODES", "")
    assert APISettings(_env_file=None).visual_qa_warn_only_codes == []


def test_an_unknown_visual_qa_code_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDGEN_VISUAL_QA_WARN_ONLY_CODES", "alignment_coverage")
    with pytest.raises(ValidationError):
        APISettings(_env_file=None)


def test_narration_quality_limits_load_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDGEN_NARRATION_MIN_ALIGNMENT_COVERAGE", "0.8")
    monkeypatch.setenv("VIDGEN_NARRATION_MAX_TRAILING_SILENCE", "1.25")
    thresholds = APISettings(_env_file=None).narration_quality_thresholds()
    assert thresholds.min_alignment_coverage == 0.8
    assert thresholds.max_trailing_silence == 1.25
    assert thresholds.max_leading_silence == 0.5


def test_a_narration_speaking_rate_window_out_of_order_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VIDGEN_NARRATION_MIN_WPM", "300")
    with pytest.raises(ValidationError, match="narration_min_wpm must be below narration_max_wpm"):
        APISettings(_env_file=None)


def test_storyboard_warn_only_validation_codes_default_to_continuity_tolerance() -> None:
    assert APISettings(_env_file=None).storyboard_warn_only_validation_codes == [
        "continuity_contradiction"
    ]


def test_storyboard_warn_only_validation_codes_load_as_a_comma_separated_list(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "VIDGEN_STORYBOARD_WARN_ONLY_VALIDATION_CODES",
        "Missing_Evidence_Reference, continuity_contradiction",
    )
    assert APISettings(_env_file=None).storyboard_warn_only_validation_codes == [
        "continuity_contradiction",
        "missing_evidence_reference",
    ]


def test_an_unknown_storyboard_warn_only_validation_code_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # too_many_references is a real storyboard code, but demoting it would hand
    # the animator a shot it cannot generate, so it is not eligible.
    monkeypatch.setenv("VIDGEN_STORYBOARD_WARN_ONLY_VALIDATION_CODES", "too_many_references")
    with pytest.raises(ValidationError):
        APISettings(_env_file=None)
