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

from apps.api.settings import APISettings

LIST_SETTINGS = (
    ("VIDGEN_ALLOWED_VIDEO_TYPES", "allowed_video_types"),
    ("VIDGEN_CORS_ALLOWED_ORIGINS", "cors_allowed_origins"),
    ("VIDGEN_SUBTITLE_LANGUAGES", "subtitle_languages"),
    ("VIDGEN_YOUTUBE_OAUTH_REDIRECT_TARGETS", "youtube_oauth_redirect_targets"),
)


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # A developer's own environment must not decide the outcome of these tests;
    # every case below also constructs the settings with ``_env_file=None``.
    for variable, _ in LIST_SETTINGS:
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
