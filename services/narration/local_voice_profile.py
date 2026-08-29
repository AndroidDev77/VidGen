"""Local bootstrap for the project-scoped fake voice profile.

The T12 narration stage resolves its voice from ``project.settings`` while
project creation stores empty settings, so a freshly created project cannot
reach narration until a voice profile exists and is selected. Production
deployments select a real profile deliberately; local fake-provider runs need a
deterministic, credential-free one. This module owns that bootstrap so the CLI,
the API and the tests all agree on the same identity.

The bootstrap never overwrites a voice profile the project already selected. It
only repairs a selection that does not resolve to a usable profile.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from vidgen.db.models import Project
from vidgen.db.narration_models import VoiceProfileRecord

#: The settings key the T12 narration activity reads.
VOICE_PROFILE_SETTING = "voice_profile_id"

#: Fixed namespace so a project's local fake profile keeps one stable ID across
#: repeated bootstraps and across a recreated local database.
LOCAL_VOICE_PROFILE_NAMESPACE = UUID("2f4d6f1c-4a1e-5b7d-9c3a-0d5e8f1b2c34")

FAKE_PROVIDER = "fake"
FAKE_PROVIDER_VOICE_ID = "vidgen-local-fake-voice"
FAKE_MODEL = "fake-tts"
FAKE_LANGUAGE = "en"
FAKE_VERSION = 1
#: The keys ``NarrationPipeline`` reads out of ``VoiceProfileRecord.configuration``.
FAKE_CONFIGURATION: dict[str, Any] = {
    "default_pace": 1.0,
    "output_format": "wav",
    "default_speaking_instructions": "neutral local development narration",
}

#: Every outcome the bootstrap can reach, so a caller can report exactly what
#: happened without re-deriving it from the database.
BootstrapAction = Literal["created", "reused", "assigned", "unchanged", "repaired"]


class LocalVoiceProfileError(RuntimeError):
    """The local bootstrap cannot produce a usable voice profile."""


@dataclass(frozen=True, slots=True)
class LocalVoiceProfileResult:
    """What the bootstrap did, for CLI output and for assertions in tests."""

    project_id: UUID
    voice_profile_id: UUID
    provider: str
    action: BootstrapAction


def configuration_hash(configuration: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def local_voice_profile_id(project_id: UUID) -> UUID:
    """The deterministic ID of a project's local fake voice profile."""
    return uuid5(LOCAL_VOICE_PROFILE_NAMESPACE, f"{project_id}:{FAKE_PROVIDER_VOICE_ID}")


def _usable_selection(session: Session, project: Project) -> VoiceProfileRecord | None:
    """The profile the project already selected, when it is usable.

    A selection that is absent, malformed, missing from the database, or owned
    by another project is not usable and is what ``repaired`` replaces.
    """
    raw = project.settings.get(VOICE_PROFILE_SETTING)
    if raw in (None, ""):
        return None
    try:
        profile_id = UUID(str(raw))
    except ValueError:
        return None
    profile = session.get(VoiceProfileRecord, profile_id)
    if profile is None or profile.project_id not in (None, project.id):
        return None
    return profile


def _existing_local_profile(session: Session, project_id: UUID) -> VoiceProfileRecord | None:
    deterministic = session.get(VoiceProfileRecord, local_voice_profile_id(project_id))
    if deterministic is not None:
        return deterministic
    return session.scalar(
        select(VoiceProfileRecord)
        .where(
            VoiceProfileRecord.project_id == project_id,
            VoiceProfileRecord.provider == FAKE_PROVIDER,
            VoiceProfileRecord.provider_voice_id == FAKE_PROVIDER_VOICE_ID,
        )
        .order_by(VoiceProfileRecord.created_at, VoiceProfileRecord.id)
    )


def ensure_local_voice_profile(
    session: Session, *, project_id: UUID, provider: str = FAKE_PROVIDER
) -> LocalVoiceProfileResult:
    """Give ``project_id`` a usable narration voice profile, idempotently.

    Returns without touching anything when the project already selects a
    resolvable profile, so a production selection survives a repeated run.
    """
    if provider != FAKE_PROVIDER:
        raise LocalVoiceProfileError(
            "the local bootstrap only creates fake voice profiles; "
            "a production profile must be selected deliberately"
        )
    project = session.get(Project, project_id)
    if project is None:
        raise LocalVoiceProfileError(f"project {project_id} does not exist")

    selected = _usable_selection(session, project)
    if selected is not None:
        return LocalVoiceProfileResult(
            project_id=project.id,
            voice_profile_id=selected.id,
            provider=selected.provider,
            action="unchanged",
        )
    repairing = bool(project.settings.get(VOICE_PROFILE_SETTING))

    profile = _existing_local_profile(session, project.id)
    created = profile is None
    if profile is None:
        profile = VoiceProfileRecord(
            id=local_voice_profile_id(project.id),
            project_id=project.id,
            provider=FAKE_PROVIDER,
            provider_voice_id=FAKE_PROVIDER_VOICE_ID,
            model=FAKE_MODEL,
            language=FAKE_LANGUAGE,
            version=FAKE_VERSION,
            configuration=dict(FAKE_CONFIGURATION),
            configuration_hash=configuration_hash(FAKE_CONFIGURATION),
        )
        session.add(profile)
        session.flush()

    project.settings = {**project.settings, VOICE_PROFILE_SETTING: str(profile.id)}
    session.commit()
    action: BootstrapAction
    if repairing:
        action = "repaired"
    elif created:
        action = "created"
    else:
        action = "assigned"
    return LocalVoiceProfileResult(
        project_id=project.id,
        voice_profile_id=profile.id,
        provider=profile.provider,
        action=action,
    )
