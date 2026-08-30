"""Product-level narration voice selection.

T12 resolves a project's narration voice from ``project.settings``, and project
creation stored empty settings, so every browser-created project reached
narration and failed there. ``scripts/create_local_voice_profile.py`` repaired
that by hand for local runs; this module is the supported product path the API
and the UI use instead, and the CLI stays as an idempotent diagnostic.

Three rules shape everything below:

* **A profile never carries a credential.** It names a provider and an
  externally provisioned voice; the credential for that provider is resolved by
  the worker from configuration. Nothing here reads or returns a secret.
* **Scope is enforced, not advertised.** A project-scoped profile belongs to
  exactly one project and is refused for any other; a shared profile has no
  project and is offered to every project the owner has.
* **Changing the voice changes the generation identity.** A profile's
  configuration hash and version are part of what T12 hashes, so selecting a
  different voice produces a new narration identity and invalidates exactly the
  lineage that depended on the old one - and nothing above it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.narration.local_voice_profile import (
    FAKE_CONFIGURATION,
    FAKE_LANGUAGE,
    FAKE_MODEL,
    FAKE_PROVIDER,
    FAKE_PROVIDER_VOICE_ID,
    VOICE_PROFILE_SETTING,
    configuration_hash,
)
from vidgen.contracts.control_commands import VoiceProfileSelection
from vidgen.db.models import Project
from vidgen.db.narration_models import VoiceProfileRecord

#: Namespace for the deterministic IDs of catalog options, so an option has a
#: stable identity in the list before anyone has selected it.
CATALOG_NAMESPACE = UUID("bf2a1c07-4d5e-5f36-9a2b-71c0d3e4f5a6")


@dataclass(frozen=True, slots=True)
class NarrationDeployment:
    """Which narration providers this deployment can actually use.

    Derived here rather than in a route so no API module has to name a
    provider, and so the catalog cannot offer a voice the T12 activity would
    refuse for a missing credential.
    """

    paid_provider_configured: bool
    fake_allowed: bool

    @classmethod
    def from_settings(cls, settings: Any) -> NarrationDeployment:
        return cls(
            paid_provider_configured=bool(getattr(settings, "openai_api_key", None)),
            fake_allowed=bool(getattr(settings, "temporal_allow_fake_providers", False)),
        )


class VoiceProfileError(RuntimeError):
    """A structured, owner-renderable voice-selection failure."""

    def __init__(self, code: str, summary: str) -> None:
        super().__init__(summary)
        self.code = code
        self.summary = summary


@dataclass(frozen=True, slots=True)
class VoiceProfileOption:
    """One voice a project may be configured with, before it is persisted."""

    provider: str
    provider_voice_id: str
    model: str
    language: str
    output_format: str
    configuration: dict[str, Any]
    label: str


def _fake_option() -> VoiceProfileOption:
    return VoiceProfileOption(
        provider=FAKE_PROVIDER,
        provider_voice_id=FAKE_PROVIDER_VOICE_ID,
        model=FAKE_MODEL,
        language=FAKE_LANGUAGE,
        output_format=str(FAKE_CONFIGURATION["output_format"]),
        configuration=dict(FAKE_CONFIGURATION),
        label="Deterministic local narrator (no provider cost)",
    )


#: The configured OpenAI narrators. Voice IDs are provider-published names, not
#: credentials, and the model is the one this repository has verified.
_OPENAI_VOICES = (
    ("alloy", "Alloy - neutral narrator"),
    ("ash", "Ash - dry, deadpan narrator"),
    ("sage", "Sage - warm, conversational narrator"),
)
OPENAI_NARRATION_MODEL = "gpt-4o-mini-tts"


def configured_catalog(deployment: NarrationDeployment) -> tuple[VoiceProfileOption, ...]:
    """The voices this deployment can actually narrate with.

    Deliberately derived from configuration rather than hard-coded: offering a
    voice whose provider has no credential would only move the failure from
    project setup into the middle of a paid workflow.
    """
    options: list[VoiceProfileOption] = []
    if deployment.paid_provider_configured:
        options.extend(
            VoiceProfileOption(
                provider="openai",
                provider_voice_id=voice_id,
                model=OPENAI_NARRATION_MODEL,
                language="en",
                output_format="wav",
                configuration={
                    "default_pace": 1.0,
                    "output_format": "wav",
                    "default_speaking_instructions": "clear editorial recap narration",
                },
                label=label,
            )
            for voice_id, label in _OPENAI_VOICES
        )
    if deployment.fake_allowed or not deployment.paid_provider_configured:
        options.append(_fake_option())
    return tuple(options)


def catalog_option_id(option: VoiceProfileOption) -> UUID:
    """The stable, project-independent ID of one catalog voice.

    Deliberately not project-scoped: the catalog is offered before a project
    exists, so ``POST /projects`` can name a voice from it. Selecting it
    materializes a project-scoped row with its own ID.
    """
    return uuid5(CATALOG_NAMESPACE, f"{option.provider}:{option.provider_voice_id}")


def project_profile_id(project_id: UUID, option: VoiceProfileOption) -> UUID:
    """The stable ID of the project-scoped row a catalog option materializes to."""
    return uuid5(CATALOG_NAMESPACE, f"{project_id}:{option.provider}:{option.provider_voice_id}")


def _selection(record: VoiceProfileRecord, *, selected: bool) -> VoiceProfileSelection:
    configuration = dict(record.configuration or {})
    return VoiceProfileSelection(
        voice_profile_id=record.id,
        project_id=record.project_id,
        provider=record.provider,
        provider_voice_id=record.provider_voice_id,
        model=record.model,
        language=record.language,
        profile_version=record.version,
        configuration_hash=record.configuration_hash,
        output_format=str(configuration.get("output_format", "wav")),
        scope="project" if record.project_id is not None else "shared",
        selected=selected,
    )


def _option_selection(option: VoiceProfileOption, *, selected: bool) -> VoiceProfileSelection:
    return VoiceProfileSelection(
        voice_profile_id=catalog_option_id(option),
        project_id=None,
        provider=option.provider,
        provider_voice_id=option.provider_voice_id,
        model=option.model,
        language=option.language,
        profile_version=1,
        configuration_hash=configuration_hash(option.configuration),
        output_format=option.output_format,
        scope="shared",
        selected=selected,
    )


def current_selection(session: Session, project: Project) -> VoiceProfileSelection | None:
    """The project's selected voice, when it resolves to a usable profile.

    A selection that is absent, malformed, missing, or owned by another project
    is not a selection: returning ``None`` is what makes the workflow-start
    precondition refuse before Temporal rather than fail inside narration.
    """
    raw = (project.settings or {}).get(VOICE_PROFILE_SETTING)
    if raw in (None, ""):
        return None
    try:
        profile_id = UUID(str(raw))
    except ValueError:
        return None
    record = session.get(VoiceProfileRecord, profile_id)
    if record is None or record.project_id not in (None, project.id):
        return None
    return _selection(record, selected=True)


def available_profiles(
    session: Session, project: Project, deployment: NarrationDeployment
) -> list[VoiceProfileSelection]:
    """Every voice this project may select, persisted rows first.

    A persisted profile shadows the catalog option it came from, so a project
    that already selected a voice sees one entry for it rather than two.
    """
    selected = current_selection(session, project)
    selected_id = selected.voice_profile_id if selected else None
    profiles: list[VoiceProfileSelection] = []
    seen: set[tuple[str, str]] = set()
    for record in session.scalars(
        select(VoiceProfileRecord)
        .where(VoiceProfileRecord.project_id.in_([project.id, None]))
        .order_by(VoiceProfileRecord.provider, VoiceProfileRecord.provider_voice_id)
    ):
        seen.add((record.provider, record.provider_voice_id))
        profiles.append(_selection(record, selected=record.id == selected_id))
    for option in configured_catalog(deployment):
        if (option.provider, option.provider_voice_id) in seen:
            continue
        profiles.append(_option_selection(option, selected=False))
    return profiles


def select_profile(
    session: Session,
    project: Project,
    deployment: NarrationDeployment,
    *,
    voice_profile_id: UUID | None = None,
    provider: str | None = None,
    provider_voice_id: str | None = None,
    model: str | None = None,
    language: str = "en",
) -> VoiceProfileSelection:
    """Select a voice for this project, materializing a catalog option if needed.

    Accepts either an existing profile ID or an externally provisioned voice
    described by provider and voice ID. Both paths validate scope before they
    write: a profile belonging to another project is refused with the same
    not-found answer a nonexistent one gets, so selection cannot be used to
    probe another owner's data.
    """
    catalog = configured_catalog(deployment)
    record: VoiceProfileRecord | None = None
    if voice_profile_id is not None:
        record = session.get(VoiceProfileRecord, voice_profile_id)
        if record is not None and record.project_id not in (None, project.id):
            raise VoiceProfileError(
                "voice_profile_not_found",
                "That voice profile does not exist.",
            )
        if record is None:
            # A catalog voice that has not been materialized for this project
            # yet. Catalog IDs are project-independent so a project can be
            # created with one; an ID that is neither a catalog voice nor a
            # profile of this project is simply not found.
            option = next(
                (item for item in catalog if catalog_option_id(item) == voice_profile_id),
                None,
            )
            if option is None:
                raise VoiceProfileError(
                    "voice_profile_not_found", "That voice profile does not exist."
                )
            record = _materialize(session, project, option)
    else:
        if not provider or not provider_voice_id:
            raise VoiceProfileError(
                "voice_profile_invalid",
                "Select an existing voice profile, or name a provider and voice ID.",
            )
        configured = {item.provider for item in catalog}
        if provider not in configured:
            raise VoiceProfileError(
                "voice_provider_unavailable",
                f"The {provider} narration provider is not configured in this deployment.",
            )
        template = next(item for item in catalog if item.provider == provider)
        option = VoiceProfileOption(
            provider=provider,
            provider_voice_id=provider_voice_id,
            model=model or template.model,
            language=language,
            output_format=template.output_format,
            configuration=dict(template.configuration),
            label=f"{provider}:{provider_voice_id}",
        )
        record = _materialize(session, project, option)
    project.settings = {**(project.settings or {}), VOICE_PROFILE_SETTING: str(record.id)}
    session.flush()
    return _selection(record, selected=True)


def _materialize(
    session: Session, project: Project, option: VoiceProfileOption
) -> VoiceProfileRecord:
    """Persist a catalog option as this project's own profile, idempotently."""
    profile_id = project_profile_id(project.id, option)
    existing = session.get(VoiceProfileRecord, profile_id)
    if existing is not None:
        return existing
    record = VoiceProfileRecord(
        id=profile_id,
        project_id=project.id,
        provider=option.provider,
        provider_voice_id=option.provider_voice_id,
        model=option.model,
        language=option.language,
        version=1,
        configuration=dict(option.configuration),
        configuration_hash=configuration_hash(option.configuration),
    )
    session.add(record)
    session.flush()
    return record
