"""Every configured OpenAI model must name something the API can be called with.

A deployment configured from ``.env.example`` used to leave both T22 final
editorial QA settings on a default of ``gpt-5.6`` - a family name the pricing
fallback resolves happily and the Responses API rejects with ``400``. Nothing
noticed until the last stage of the pipeline, after transcription, analysis,
script, narration, storyboard, every keyframe, every animation and the render had
already been paid for, and the activity then burned all three of its attempts.

These tests hold the three parts of that fix together: the defaults name real
tiers, ``.env.example`` documents every stage rather than the handful someone
happened to hit, and a family name is refused when settings load instead of when
a stage reaches the provider.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from apps.api.settings import OPENAI_MODEL_SETTINGS, APISettings
from services.qa.final_editorial_provider import DEFAULT_REGISTRY as FINAL_QA_REGISTRY
from services.qa.final_editorial_provider import FinalEditorialRole
from services.qa.visual_agent import DEFAULT_REGISTRY as VISUAL_QA_REGISTRY
from services.qa.visual_agent import VisualQARole
from services.script.commands import ScriptCommandOptions
from services.storyboard.providers import DEFAULT_STORYBOARD_MODEL
from vidgen.costs.openai_rates import ALIASES, RATES, resolve
from vidgen.providers.openai_models import (
    KNOWN_MODELS,
    MODEL_FAMILIES,
    ModelNameProblem,
    UnusableModelError,
    check_model_name,
    is_callable_model,
    model_from_env,
    preflight_models,
)

ENV_EXAMPLE = Path(__file__).resolve().parents[1] / ".env.example"


def _environment_variable(setting: str) -> str:
    return f"VIDGEN_{setting.upper()}"


def _documented_values() -> dict[str, str]:
    """Every ``VIDGEN_*`` assignment ``.env.example`` actually sets."""
    values: dict[str, str] = {}
    for line in ENV_EXAMPLE.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return values


@pytest.fixture(autouse=True)
def _isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    # A developer's own exported model names must not decide these outcomes.
    for setting in OPENAI_MODEL_SETTINGS:
        monkeypatch.delenv(_environment_variable(setting), raising=False)
    monkeypatch.delenv("VIDGEN_ALLOW_UNKNOWN_MODELS", raising=False)


@pytest.mark.parametrize("setting", OPENAI_MODEL_SETTINGS)
def test_every_model_default_names_a_callable_model(setting: str) -> None:
    value = getattr(APISettings(_env_file=None), setting)
    assert check_model_name(value) is None, f"{setting} defaults to {value!r}"


def test_the_final_qa_defaults_are_the_two_roles_the_design_names() -> None:
    """Luna sees the whole recap; Terra only adjudicates what is borderline."""
    settings = APISettings(_env_file=None)
    assert settings.final_qa_first_pass_model == "gpt-5.6-luna"
    assert settings.final_qa_adjudicator_model == "gpt-5.6-terra"
    assert settings.visual_qa_first_pass_model == "gpt-5.6-luna"
    assert settings.visual_qa_adjudicator_model == "gpt-5.6-terra"


@pytest.mark.parametrize("setting", OPENAI_MODEL_SETTINGS)
def test_a_family_name_is_refused_when_settings_load(
    monkeypatch: pytest.MonkeyPatch, setting: str
) -> None:
    """The regression: ``gpt-5.6`` prices, reads like a model, and cannot be called.

    Refusing it here means the API, the worker and the dispatcher all fail to
    start, which is as early as this mistake can possibly be found.
    """
    monkeypatch.setenv(_environment_variable(setting), "gpt-5.6")
    with pytest.raises(ValidationError, match="model family"):
        APISettings(_env_file=None)


def test_the_refusal_names_the_tiers_to_use_instead(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDGEN_FINAL_QA_FIRST_PASS_MODEL", "gpt-5.6")
    with pytest.raises(ValidationError) as error:
        APISettings(_env_file=None)
    message = str(error.value)
    assert "final_qa_first_pass_model" in message
    for tier in MODEL_FAMILIES["gpt-5.6"]:
        assert tier in message


def test_an_unknown_model_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDGEN_ANALYSIS_MODEL", "gtp-5.6-terra")
    with pytest.raises(ValidationError, match="not an OpenAI model this deployment knows"):
        APISettings(_env_file=None)


def test_an_empty_model_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDGEN_STORYBOARD_MODEL", "  ")
    with pytest.raises(ValidationError, match="is empty"):
        APISettings(_env_file=None)


def test_a_configured_model_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VIDGEN_ANALYSIS_MODEL", "  gpt-5.6-sol  ")
    assert APISettings(_env_file=None).analysis_model == "gpt-5.6-sol"


@pytest.mark.parametrize(
    "model",
    [
        # A dated snapshot of a known model, which is what the image setting is.
        "gpt-image-2-2026-04-21",
        "gpt-5.6-terra-2026-07-01",
    ],
)
def test_a_dated_snapshot_is_accepted(model: str) -> None:
    """A snapshot prices as the model it pins, so accepting one costs nothing."""
    assert is_callable_model(model)


@pytest.mark.parametrize("model", ["gpt-5.6-nova", "gpt-5.6-", "gpt-4o-"])
def test_a_name_that_only_looks_like_a_snapshot_is_refused(model: str) -> None:
    """An unrecognized tier would price through the family alias.

    The tiers of ``gpt-5.6`` are a factor of twenty apart, so a project's T23
    hard cap would be enforced against a number that could be that far out.
    """
    problem = check_model_name(model)
    assert problem is not None
    assert problem.problem is ModelNameProblem.UNKNOWN


def test_a_configured_model_is_lower_cased(monkeypatch: pytest.MonkeyPatch) -> None:
    """The API is case-sensitive, so what was checked is what must be sent."""
    monkeypatch.setenv("VIDGEN_ANALYSIS_MODEL", "GPT-5.6-Terra")
    assert APISettings(_env_file=None).analysis_model == "gpt-5.6-terra"


def test_an_unknown_model_can_be_allowed_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    """The registry must never be the thing that keeps a deployment from starting."""
    monkeypatch.setenv("VIDGEN_ANALYSIS_MODEL", "gpt-7-nimbus")
    monkeypatch.setenv("VIDGEN_ALLOW_UNKNOWN_MODELS", "true")
    assert APISettings(_env_file=None).analysis_model == "gpt-7-nimbus"


def test_a_family_name_is_refused_even_when_unknown_models_are_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """It is not a model under any configuration; nothing may opt into it."""
    monkeypatch.setenv("VIDGEN_FINAL_QA_FIRST_PASS_MODEL", "gpt-5.6")
    monkeypatch.setenv("VIDGEN_ALLOW_UNKNOWN_MODELS", "true")
    with pytest.raises(ValidationError, match="model family"):
        APISettings(_env_file=None)


def test_a_cli_model_from_the_environment_is_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator CLIs resolve their model without APISettings."""
    monkeypatch.delenv("VIDGEN_FINAL_QA_FIRST_PASS_MODEL", raising=False)
    assert model_from_env("VIDGEN_FINAL_QA_FIRST_PASS_MODEL") is None
    monkeypatch.setenv("VIDGEN_FINAL_QA_FIRST_PASS_MODEL", " GPT-5.6-Luna ")
    assert model_from_env("VIDGEN_FINAL_QA_FIRST_PASS_MODEL") == "gpt-5.6-luna"
    monkeypatch.setenv("VIDGEN_FINAL_QA_FIRST_PASS_MODEL", "gpt-5.6")
    with pytest.raises(UnusableModelError, match="VIDGEN_FINAL_QA_FIRST_PASS_MODEL"):
        model_from_env("VIDGEN_FINAL_QA_FIRST_PASS_MODEL")


def test_every_setting_that_names_a_model_is_validated() -> None:
    """A new stage must not be able to add an unchecked model setting.

    ``veo_model`` is the one exception: it names a Google model, not an OpenAI
    one, and is bound by the Veo capability profile instead.
    """
    fields = {name for name in APISettings.model_fields if name.endswith("_model")}
    assert fields - set(OPENAI_MODEL_SETTINGS) == {"veo_model"}


@pytest.mark.parametrize("setting", OPENAI_MODEL_SETTINGS)
def test_env_example_documents_every_model_setting(setting: str) -> None:
    """The example config is what a new deployment is configured from.

    Documenting only some stages is how this failed: everything the local
    ``.env`` happened to override worked, and the one stage it did not - final
    editorial QA - was left on a default that could never answer.
    """
    documented = _documented_values()
    variable = _environment_variable(setting)
    assert variable in documented, f"{variable} is missing from .env.example"
    assert check_model_name(documented[variable]) is None


def test_env_example_documents_the_deployment_defaults() -> None:
    """A stage left unset must behave exactly as the example says it will."""
    documented = _documented_values()
    settings = APISettings(_env_file=None)
    for setting in OPENAI_MODEL_SETTINGS:
        assert documented[_environment_variable(setting)] == getattr(settings, setting)


def test_the_compiled_in_defaults_match_the_settings_defaults() -> None:
    """An adapter built without settings must not reach for a different model."""
    settings = APISettings(_env_file=None)
    first_pass = VISUAL_QA_REGISTRY[VisualQARole.LUNA_FIRST_PASS]
    assert first_pass.model == settings.visual_qa_first_pass_model
    assert (
        VISUAL_QA_REGISTRY[VisualQARole.TERRA_ADJUDICATOR].model
        == settings.visual_qa_adjudicator_model
    )
    assert (
        FINAL_QA_REGISTRY[FinalEditorialRole.LUNA_FIRST_PASS].model
        == settings.final_qa_first_pass_model
    )
    assert (
        FINAL_QA_REGISTRY[FinalEditorialRole.TERRA_ADJUDICATOR].model
        == settings.final_qa_adjudicator_model
    )
    assert DEFAULT_STORYBOARD_MODEL == settings.storyboard_model
    options = ScriptCommandOptions()
    assert options.compressor_model == settings.script_compressor_model
    assert options.writer_model == settings.script_writer_model
    assert options.editor_model == settings.script_editor_model


def test_the_known_models_and_the_price_table_agree() -> None:
    """A model that answers but prices as nothing reports no spend at all."""
    assert set(RATES) <= KNOWN_MODELS
    assert set(ALIASES) == set(MODEL_FAMILIES)
    for family, tiers in MODEL_FAMILIES.items():
        assert ALIASES[family] in tiers
        for tier in tiers:
            assert tier in KNOWN_MODELS
    for model in KNOWN_MODELS:
        assert resolve(model) is not None or model in {
            # Audio and image models are not token-priced, so the token rate
            # table has nothing to say about them.
            "whisper-1",
            "gpt-image-2",
            "gpt-4o-mini-tts",
        }


def test_a_family_name_is_never_a_callable_model() -> None:
    for family in MODEL_FAMILIES:
        assert not is_callable_model(family)
        assert resolve(family) is not None


class _Retriever:
    """A stand-in for ``OpenAI().models.retrieve``. No network, no paid call."""

    def __init__(self, available: set[str]) -> None:
        self.available = available
        self.calls: list[str] = []

    def __call__(self, model: str, /) -> object:
        self.calls.append(model)
        if model not in self.available:
            raise RuntimeError(f"404 model_not_found: The model '{model}' does not exist")
        return {"id": model}


def test_preflight_passes_when_every_configured_model_is_available() -> None:
    settings = APISettings(_env_file=None)
    configured = {name: getattr(settings, name) for name in OPENAI_MODEL_SETTINGS}
    retriever = _Retriever(set(configured.values()))
    assert preflight_models(configured, retriever) == {}
    # One lookup per distinct model, however many settings name it.
    assert sorted(retriever.calls) == sorted(set(configured.values()))


def test_preflight_reports_a_model_this_account_cannot_use() -> None:
    settings = APISettings(_env_file=None)
    configured = {name: getattr(settings, name) for name in OPENAI_MODEL_SETTINGS}
    available = set(configured.values()) - {settings.final_qa_first_pass_model}
    failures = preflight_models(configured, _Retriever(available))
    assert set(failures) == {"final_qa_first_pass_model", "visual_qa_first_pass_model"}
    assert "model_not_found" in failures["final_qa_first_pass_model"]


def test_preflight_refuses_a_family_name_without_asking_the_provider() -> None:
    retriever = _Retriever(set())
    failures = preflight_models({"final_qa_first_pass_model": "gpt-5.6"}, retriever)
    assert "model family" in failures["final_qa_first_pass_model"]
    assert retriever.calls == []
