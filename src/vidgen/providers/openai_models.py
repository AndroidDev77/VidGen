"""The OpenAI model names a deployment of this repository may be configured with.

Every model a stage calls is deployment configuration (``APISettings``), never a
per-project setting, so this module is the one place that decides whether a
configured name can actually be sent to the API.

It exists because of a failure that is expensive in exactly the wrong way. The
bare family name ``gpt-5.6`` prices correctly (see
:mod:`vidgen.costs.openai_rates`, where it resolves to the middle tier) and
reads like a model, but the API has no such model: a request naming it comes
back ``400`` every time. Configured on a stage that runs early, that is a cheap
mistake. Configured on T22 final editorial QA - the last stage of the pipeline -
it is discovered only after transcription, analysis, script, narration,
storyboard, every keyframe, every animation and the render have all been paid
for. :func:`check_model_name` is what turns that into a start-up error instead.

What the rules are
------------------

* An exact :data:`KNOWN_MODELS` entry is callable.
* A name that extends a known model with ``-`` and something else is a dated
  snapshot of it (``gpt-image-2-2026-04-21``) and is callable. A snapshot prices
  as the model it pins, so accepting one costs nothing.
* A :data:`MODEL_FAMILIES` key on its own is a family, not a model. It is
  refused, and the error names the tiers it spans.
* Anything else - a typo, a tier of a known family this repository has not heard
  of, a model from a family it does not know - is refused as unknown. Not
  because it cannot possibly work, but because nothing here can price it: an
  unrecognized ``gpt-5.6-*`` tier prices through the family alias at the middle
  tier, and the tiers of that one family are a factor of twenty apart, so a
  project's T23 hard cap would be enforced against a number that could be off by
  that much. Naming a new model therefore means one entry here and one price row
  in :mod:`vidgen.costs.openai_rates`, which is the same pair of facts a
  deployment needs anyway.

A deployment that has to run a model this repository does not know yet can set
``VIDGEN_ALLOW_UNKNOWN_MODELS=true``, which downgrades the unknown case to a
logged warning. A family name is refused either way: it is not a model under any
configuration, and the spend it would waste is the whole point of this module.

Only real provider calls read these settings. A fake-provider deployment never
does - each fake carries its own recorded model name - so nothing here
constrains local development or the test suites.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

#: Model IDs that may be sent to the API as-is. Keep in step with the priced
#: tiers in :mod:`vidgen.costs.openai_rates`; ``tests/test_model_configuration``
#: asserts the two stay consistent.
KNOWN_MODELS: frozenset[str] = frozenset(
    {
        # Responses API: creative, editorial and vision agents.
        "gpt-6-astra",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5-nano",
        "gpt-4.1-mini",
        "gpt-4o",
        "gpt-4o-mini",
        "o3",
        # Images API.
        "gpt-image-2",
        # Speech-to-text and text-to-speech.
        "whisper-1",
        "gpt-4o-transcribe",
        "gpt-4o-transcribe-diarize",
        "gpt-4o-mini-tts",
    }
)

#: Family names that identify a group of tiers rather than one model. They are
#: accepted by the pricing fallback, which only has to land on the right order of
#: magnitude, and refused here, because a request has to name a real model.
MODEL_FAMILIES: dict[str, tuple[str, ...]] = {
    "gpt-5.6": ("gpt-5.6-luna", "gpt-5.6-terra", "gpt-5.6-sol"),
    "gpt-6": ("gpt-6-astra",),
}


class ModelNameProblem(StrEnum):
    """Why a configured model name cannot be used. The kinds differ in gravity."""

    #: Nothing was configured at all.
    EMPTY = "empty"
    #: A family rather than a model. Never callable, under any configuration.
    FAMILY = "family"
    #: Not a model this repository knows how to call or price.
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ModelNameError:
    """One rejected model name: what kind of problem, and how to say so."""

    problem: ModelNameProblem
    #: The reason alone, with no setting name in it, so a caller can prefix
    #: whichever setting it is validating.
    reason: str


def normalize(model: str) -> str:
    """The comparable form of a configured model name.

    Lower-cased, because the API is case-sensitive and every published model ID
    is lower-case: ``GPT-5.6-Terra`` is a name a person writes and the provider
    rejects. Callers store what this returns, so a name is checked and sent in
    exactly the same form.
    """
    return model.strip().lower()


def _snapshot_of(name: str) -> str | None:
    """The known model ``name`` is a dated snapshot of, if it is one."""
    for candidate in sorted(KNOWN_MODELS, key=len, reverse=True):
        prefix = f"{candidate}-"
        if name.startswith(prefix) and len(name) > len(prefix):
            return candidate
    return None


def is_callable_model(model: str) -> bool:
    """Whether ``model`` names something the API can be asked for."""
    return check_model_name(model) is None


def check_model_name(model: str) -> ModelNameError | None:
    """Explain why ``model`` cannot be called, or return None if it can."""
    name = normalize(model)
    if not name:
        return ModelNameError(
            ModelNameProblem.EMPTY, "is empty; every configured model must name a model"
        )
    if name in KNOWN_MODELS or _snapshot_of(name) is not None:
        return None
    if name in MODEL_FAMILIES:
        tiers = ", ".join(MODEL_FAMILIES[name])
        return ModelNameError(
            ModelNameProblem.FAMILY,
            f"names the model family {name!r}, which is not a model the API can be "
            f"called with. Name one of its tiers instead: {tiers}",
        )
    return ModelNameError(
        ModelNameProblem.UNKNOWN,
        f"names {name!r}, which is not an OpenAI model this deployment knows. Known "
        f"models: {', '.join(sorted(KNOWN_MODELS))}, or a dated snapshot of one. A "
        "model that belongs here needs an entry in "
        "vidgen.providers.openai_models.KNOWN_MODELS and a price row in "
        "vidgen.costs.openai_rates, without which its spend cannot be reconciled",
    )


class UnusableModelError(ValueError):
    """A configured model name that cannot be called. Deterministic."""


def require_callable_model(model: str, *, setting: str) -> str:
    """Return the normalized ``model``, or raise naming ``setting`` and the reason.

    For the entry points that resolve a model themselves rather than through
    ``APISettings`` - the operator CLIs in ``scripts/`` - so a mistyped
    ``VIDGEN_*_MODEL`` fails before the run starts there too.
    """
    problem = check_model_name(model)
    if problem is not None:
        raise UnusableModelError(f"{setting} {problem.reason}")
    return normalize(model)


def model_from_env(variable: str) -> str | None:
    """The checked model ``variable`` names, or None when it is not set.

    The operator CLIs in ``scripts/`` read their model straight from the
    environment rather than through ``APISettings``, so without this a mistyped
    ``VIDGEN_*_MODEL`` would reach the provider there exactly as it used to.
    Unset stays unset: the caller then falls back to its own registry default.
    """
    value = os.getenv(variable)
    if value is None:
        return None
    return require_callable_model(value, setting=variable)


class ModelLookup(Protocol):
    """Retrieve one model by ID, raising if the account cannot use it.

    ``openai.OpenAI().models.retrieve`` satisfies this. It is a free metadata
    lookup: it spends no tokens, so verifying every configured model costs
    nothing at all.
    """

    def __call__(self, model: str, /) -> object: ...


def preflight_models(configured: Mapping[str, str], retrieve: ModelLookup) -> dict[str, str]:
    """Check every configured model against the provider, keyed by setting name.

    :func:`check_model_name` catches a name this repository knows is wrong.
    Only the provider can say whether a plausible name is one *this account*
    may call, which is the other half of the same failure: a model that exists
    for someone else still answers ``404``. Both halves are far cheaper to learn
    at deployment time than after a render.

    Returns the settings that failed and why - empty when everything is usable.
    Each distinct model is looked up once however many settings name it. A
    failure that is really about the credential or the network fails every
    lookup identically, which is what :mod:`scripts.verify_models` reads to say
    so rather than blaming twelve model names.
    """
    failures: dict[str, str] = {}
    seen: dict[str, str | None] = {}
    for setting, model in configured.items():
        name = normalize(model)
        if name not in seen:
            problem = check_model_name(name)
            reason = None if problem is None else problem.reason
            if reason is None:
                try:
                    retrieve(name)
                except Exception as error:  # any provider error means unusable
                    reason = f"names {name!r}, which this account cannot use: {error}"
            seen[name] = reason
        recorded = seen[name]
        if recorded is not None:
            failures[setting] = f"{setting} {recorded}"
    return failures
