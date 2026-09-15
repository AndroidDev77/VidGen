"""Verify every configured OpenAI model against the account, before any run.

    uv run python -m scripts.verify_models

Loading the settings already refuses a name this repository knows is not a model
(a family name such as ``gpt-5.6``, for instance). This script adds the half only
the provider can answer: whether the configured key may actually call each one.
Both checks are free - ``models.retrieve`` is a metadata lookup that spends no
tokens - and both are worth a few seconds at deployment time, because the stage
that would otherwise find out is T22 final editorial QA, which runs after the
render has been paid for.

Exit codes: ``0`` when every configured model is usable, ``1`` when one is not,
``2`` when there is no API key to check with.
"""

from __future__ import annotations

import sys

from apps.api.settings import OPENAI_MODEL_SETTINGS, get_settings
from vidgen.providers.openai_models import preflight_models

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_NO_KEY = 2


def main() -> int:
    settings = get_settings()
    configured = {name: getattr(settings, name) for name in OPENAI_MODEL_SETTINGS}
    if not settings.openai_api_key:
        print("no VIDGEN_OPENAI_API_KEY configured; nothing to verify against", file=sys.stderr)
        for setting, model in configured.items():
            print(f"  {setting} = {model}")
        return EXIT_NO_KEY

    from openai import OpenAI

    client = OpenAI(api_key=settings.openai_api_key, max_retries=0)
    failures = preflight_models(configured, client.models.retrieve)
    for setting, model in configured.items():
        status = "FAIL" if setting in failures else "ok"
        print(f"  [{status}] {setting} = {model}")
    for reason in failures.values():
        print(reason, file=sys.stderr)
    if failures:
        print(f"{len(failures)} configured model(s) unusable", file=sys.stderr)
        return EXIT_FAILED
    print(f"all {len(configured)} configured models are callable with this key")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
