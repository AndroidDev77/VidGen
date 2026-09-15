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
* A name that extends a known model with ``-`` is a dated snapshot of it
  (``gpt-image-2-2026-04-21``) and is callable.
* A name that extends a :data:`MODEL_FAMILIES` key with ``-`` is a tier of a
  family this repository knows, and is accepted so adopting a newly published
  tier does not require a code change first.
* A :data:`MODEL_FAMILIES` key on its own is a family, not a model. It is
  rejected, and the error names the tiers it spans.
* Anything else names no family this deployment knows. It is rejected: it has no
  published price either, so it would silently report no spend even if it did
  answer. Adding a model means one entry here and one price row in
  :mod:`vidgen.costs.openai_rates`.

Only real provider calls read these settings. A fake-provider deployment never
does - each fake carries its own recorded model name - so nothing here
constrains local development or the test suites.
"""

from __future__ import annotations

from collections.abc import Mapping
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


def normalize(model: str) -> str:
    """The comparable form of a configured model name."""
    return model.strip().lower()


def _longest_prefix(name: str) -> str | None:
    """The longest known model or family ``name`` is a ``-`` extension of."""
    candidates = sorted(KNOWN_MODELS | MODEL_FAMILIES.keys(), key=len, reverse=True)
    for candidate in candidates:
        if name.startswith(f"{candidate}-"):
            return candidate
    return None


def is_callable_model(model: str) -> bool:
    """Whether ``model`` names something the API can be asked for."""
    return check_model_name(model) is None


def check_model_name(model: str) -> str | None:
    """Explain why ``model`` cannot be called, or return None if it can.

    The string is the reason alone, with no setting name in it, so a caller can
    prefix whichever setting it is validating.
    """
    name = normalize(model)
    if not name:
        return "is empty; every configured model must name a model"
    if name in KNOWN_MODELS:
        return None
    if name in MODEL_FAMILIES:
        tiers = ", ".join(MODEL_FAMILIES[name])
        return (
            f"names the model family {name!r}, which is not a model the API can be "
            f"called with. Name one of its tiers instead: {tiers}"
        )
    if _longest_prefix(name) is not None:
        return None
    families = ", ".join(sorted(MODEL_FAMILIES))
    return (
        f"names {name!r}, which is not an OpenAI model this deployment knows. Known "
        f"models: {', '.join(sorted(KNOWN_MODELS))} (and dated snapshots or newer "
        f"tiers of the {families} families). A model that belongs here needs an "
        "entry in vidgen.providers.openai_models.KNOWN_MODELS and a price row in "
        "vidgen.costs.openai_rates"
    )


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
    Each distinct model is looked up once however many settings name it.
    """
    failures: dict[str, str] = {}
    seen: dict[str, str | None] = {}
    for setting, model in configured.items():
        name = normalize(model)
        if name not in seen:
            reason = check_model_name(name)
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
