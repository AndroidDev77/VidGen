"""Create or repair a project's local fake narration voice profile.

The T12 narration stage reads ``project.settings["voice_profile_id"]``, and
project creation stores empty settings. This command closes that gap for local
fake-provider runs: it creates one deterministic project-scoped fake voice
profile, selects it, and prints its ID. It is idempotent, needs no paid
credential, and never replaces a voice profile the project already resolves to.

    uv run python scripts/create_local_voice_profile.py PROJECT_UUID --provider fake
"""

from __future__ import annotations

import argparse
from uuid import UUID

from services.narration.local_voice_profile import (
    FAKE_PROVIDER,
    LocalVoiceProfileError,
    ensure_local_voice_profile,
)
from vidgen.db.session import build_engine, session_factory


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("project_id", type=UUID)
    parser.add_argument(
        "--provider",
        choices=(FAKE_PROVIDER,),
        default=FAKE_PROVIDER,
        help="only the credential-free fake provider is bootstrapped locally",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    with session_factory(build_engine())() as session:
        try:
            result = ensure_local_voice_profile(
                session, project_id=args.project_id, provider=args.provider
            )
        except LocalVoiceProfileError as error:
            print(f"error: {error}")
            return 2
    print(f"project_id={result.project_id}")
    print(f"voice_profile_id={result.voice_profile_id}")
    print(f"provider={result.provider}")
    print(f"action={result.action}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
