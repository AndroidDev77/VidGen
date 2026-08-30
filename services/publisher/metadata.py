"""Deterministic publication drafts, metadata validation and stable identity.

Three jobs live here, and they are together because they all answer "what
exactly is being published?":

* **The initial draft.** Built deterministically from project metadata the user
  already wrote - the project name, its visual style, the render's caption
  language. No paid model is called: T25 does not generate titles, descriptions
  or tags. The draft is a starting point the user is then required to review.
* **Validation.** Every YouTube limit comes from the capability registry and is
  checked here, so a metadata problem is a structured refusal before an upload
  starts rather than a 400 after several gigabytes.
* **Identity.** The publication identity binds the render, its hash, the T22
  report, the approval, the connection, the channel, the metadata version and
  hash, the caption and thumbnail hashes, the initial privacy state, the
  publisher version and the capability profile. Retrying the same completed
  identity therefore returns the existing publication; changing the selected
  render produces a different identity and needs explicit confirmation.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from services.publisher import youtube as capabilities
from services.publisher.contracts import VideoMetadata
from services.publisher.eligibility import EligibleRender
from vidgen.contracts.publication import (
    PrivacyState,
    PublicationFailure,
    PublicationFailureCode,
    PublicationMetadata,
)

#: Bumped when the deterministic draft's *shape* changes, so an existing draft
#: is never silently regenerated in a different form.
DRAFT_TEMPLATE_VERSION = "t25-draft/1"


class PublicationMetadataError(ValueError):
    """A metadata problem, refused before any provider request."""

    def __init__(self, failure: PublicationFailure) -> None:
        super().__init__(failure.summary)
        self.failure = failure


def _refuse(
    code: PublicationFailureCode, summary: str, remediation: str = ""
) -> PublicationMetadataError:
    return PublicationMetadataError(
        PublicationFailure(code=code, summary=summary, retryable=False, remediation=remediation)
    )


def canonical_hash(payload: object) -> str:
    """The stable hash of any JSON-serialisable structure."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _truncate(text: str, limit: int) -> str:
    """Trim to ``limit`` characters on a word boundary where one is close."""
    cleaned = " ".join(text.replace("<", "").replace(">", "").split())
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    spaced = cut.rsplit(" ", 1)[0]
    return (spaced if len(spaced) >= limit - 20 else cut).rstrip()


def _tags_from(project_name: str, visual_style: str) -> list[str]:
    """A small, deterministic tag set derived from words the user already wrote."""
    words: list[str] = []
    for source in (project_name, visual_style):
        for token in source.replace("-", " ").replace("/", " ").split():
            cleaned = "".join(character for character in token if character.isalnum())
            if len(cleaned) < 3:
                continue
            lowered = cleaned.lower()
            if lowered not in words:
                words.append(lowered)
    tags = ["recap", "animated recap", *words]
    selected: list[str] = []
    total = 0
    for tag in tags:
        trimmed = tag[: capabilities.MAX_TAG_LENGTH]
        if total + len(trimmed) > capabilities.MAX_TAGS_TOTAL_CHARACTERS:
            break
        if len(selected) >= capabilities.MAX_TAG_COUNT:
            break
        selected.append(trimmed)
        total += len(trimmed)
    return selected


def initial_draft(
    render: EligibleRender, *, caption_language: str | None = None
) -> PublicationMetadata:
    """The deterministic first draft for one eligible render.

    Called once, when the publication row is created. A resume, a page reload or
    a retry reads the persisted draft instead: this function is never allowed to
    overwrite a user's edits.
    """
    project = render.project
    language = (
        caption_language
        or str(render.render_job.caption_profile.get("language", ""))
        or capabilities.DEFAULT_LANGUAGE
    )
    title = _truncate(f"{project.name} - animated recap", capabilities.MAX_TITLE_LENGTH)
    description = _truncate(
        (
            f"An animated recap of {project.name}, generated with VidGen.\n\n"
            f"Visual style: {project.visual_style}\n"
            "This video contains animated and AI-generated imagery."
        ),
        capabilities.MAX_DESCRIPTION_LENGTH,
    )
    return PublicationMetadata(
        metadata_version=1,
        title=title or "Animated recap",
        description=description,
        tags=_tags_from(project.name, project.visual_style),
        category_id=capabilities.DEFAULT_CATEGORY_ID,
        default_language=language[:16],
        caption_language=language[:16],
        caption_track_name="VidGen recap",
        made_for_kids=False,
        # VidGen output is animated and AI generated. The disclosure defaults on
        # and is always sent; it is not a field the draft can quietly omit.
        contains_synthetic_media=True,
        embeddable=True,
        notify_subscribers=False,
        requested_privacy=PrivacyState.PRIVATE,
    )


def validate(metadata: PublicationMetadata, *, now: datetime | None = None) -> None:
    """Enforce every capability-registry limit. Raises on the first problem."""
    moment = now or datetime.now(UTC)
    if not metadata.title.strip():
        raise _refuse(PublicationFailureCode.INVALID_METADATA, "A title is required.")
    if len(metadata.title) > capabilities.MAX_TITLE_LENGTH:
        raise _refuse(
            PublicationFailureCode.INVALID_METADATA,
            f"The title exceeds YouTube's {capabilities.MAX_TITLE_LENGTH}-character limit.",
        )
    if len(metadata.description) > capabilities.MAX_DESCRIPTION_LENGTH:
        raise _refuse(
            PublicationFailureCode.INVALID_METADATA,
            f"The description exceeds YouTube's {capabilities.MAX_DESCRIPTION_LENGTH}-character "
            "limit.",
        )
    for character in capabilities.FORBIDDEN_METADATA_CHARACTERS:
        if character in metadata.title or character in metadata.description:
            raise _refuse(
                PublicationFailureCode.INVALID_METADATA,
                f"YouTube rejects the {character!r} character in a title or description.",
            )
    total = sum(len(tag) for tag in metadata.tags)
    if total > capabilities.MAX_TAGS_TOTAL_CHARACTERS:
        raise _refuse(
            PublicationFailureCode.INVALID_METADATA,
            f"The tag list is {total} characters; YouTube allows "
            f"{capabilities.MAX_TAGS_TOTAL_CHARACTERS}.",
        )
    if len(metadata.caption_track_name) > capabilities.MAX_CAPTION_NAME_LENGTH:
        raise _refuse(
            PublicationFailureCode.INVALID_METADATA,
            "The caption track name is longer than YouTube accepts.",
        )
    if metadata.initial_privacy is not PrivacyState.PRIVATE:
        raise _refuse(
            PublicationFailureCode.INVALID_METADATA,
            "Every VidGen upload starts private; a public initial upload is not offered.",
        )
    validate_schedule(metadata, now=moment)


def validate_schedule(metadata: PublicationMetadata, *, now: datetime | None = None) -> None:
    """Check a requested scheduled publication against the registry's window."""
    if metadata.scheduled_publish_at is None:
        return
    if not capabilities.DEFAULT_CAPABILITY_PROFILE.supports_scheduled_publication:
        raise _refuse(
            PublicationFailureCode.INVALID_SCHEDULE,
            "The selected YouTube capability profile does not support scheduled publication.",
        )
    moment = now or datetime.now(UTC)
    target = metadata.scheduled_publish_at
    if target.tzinfo is None:
        raise _refuse(
            PublicationFailureCode.INVALID_SCHEDULE,
            "A scheduled publication time must be given in UTC.",
        )
    lead = (target.astimezone(UTC) - moment).total_seconds()
    if lead < capabilities.MIN_SCHEDULE_LEAD_SECONDS:
        raise _refuse(
            PublicationFailureCode.INVALID_SCHEDULE,
            "A scheduled publication must be at least "
            f"{capabilities.MIN_SCHEDULE_LEAD_SECONDS // 60} minutes in the future.",
        )
    if lead > capabilities.MAX_SCHEDULE_LEAD_SECONDS:
        raise _refuse(
            PublicationFailureCode.INVALID_SCHEDULE,
            "A scheduled publication may not be more than a year in the future.",
        )


def to_provider_metadata(
    metadata: PublicationMetadata, *, privacy: PrivacyState | None = None
) -> VideoMetadata:
    """Project the draft into the provider-neutral write payload.

    ``publish_at`` is only carried when the video is going out private and
    scheduled: YouTube ignores - and can reject - a publish time on a video that
    is already public.
    """
    effective = privacy or metadata.initial_privacy
    publish_at = (
        metadata.scheduled_publish_at
        if metadata.scheduled_publish_at is not None and effective is PrivacyState.PRIVATE
        else None
    )
    return VideoMetadata(
        title=metadata.title,
        description=metadata.description,
        tags=tuple(metadata.tags),
        category_id=metadata.category_id,
        default_language=metadata.default_language,
        privacy_status=effective.value,
        made_for_kids=metadata.made_for_kids,
        contains_synthetic_media=metadata.contains_synthetic_media,
        embeddable=metadata.embeddable,
        notify_subscribers=metadata.notify_subscribers,
        publish_at=publish_at,
    )


def metadata_hash(metadata: PublicationMetadata) -> str:
    """The hash of everything a YouTube write would carry.

    Deliberately excludes ``metadata_version``: two drafts with identical
    content hash identically, which is what lets a no-op "edit" avoid creating a
    new version.
    """
    payload = metadata.model_dump(mode="json")
    payload.pop("metadata_version", None)
    return canonical_hash(payload)


def publication_identity(
    *,
    project_id: UUID,
    final_render_asset_id: UUID,
    final_render_sha256: str,
    final_editorial_run_id: UUID,
    final_report_hash: str,
    approval_id: UUID,
    connection_id: UUID,
    channel_id: str,
    metadata_version: int,
    metadata_digest: str,
    caption_asset_id: UUID,
    caption_sha256: str,
    thumbnail_asset_id: UUID | None,
    thumbnail_sha256: str | None,
    initial_privacy: PrivacyState = PrivacyState.PRIVATE,
    publisher_version: str = capabilities.PUBLISHER_VERSION,
    capability_profile_version: str = capabilities.CAPABILITY_PROFILE_VERSION,
) -> str:
    """The stable identity of one publication.

    Every component is something whose change means "this is a different
    publication". The render asset and its hash are both present on purpose: an
    asset ID could in principle be reused, a hash could in principle collide
    across projects, and together they cannot.
    """
    payload: dict[str, Any] = {
        "project_id": str(project_id),
        "final_render_asset_id": str(final_render_asset_id),
        "final_render_sha256": final_render_sha256,
        "final_editorial_run_id": str(final_editorial_run_id),
        "final_report_hash": final_report_hash,
        "approval_id": str(approval_id),
        "connection_id": str(connection_id),
        "channel_id": channel_id,
        "metadata_version": metadata_version,
        "metadata_hash": metadata_digest,
        "caption_asset_id": str(caption_asset_id),
        "caption_sha256": caption_sha256,
        "thumbnail_asset_id": str(thumbnail_asset_id) if thumbnail_asset_id else None,
        "thumbnail_sha256": thumbnail_sha256,
        "initial_privacy": initial_privacy.value,
        "publisher_version": publisher_version,
        "capability_profile_version": capability_profile_version,
    }
    return canonical_hash(payload)


def next_metadata_version(
    stored: PublicationMetadata, edited: PublicationMetadata
) -> PublicationMetadata:
    """Return the edited draft at the correct version.

    An edit that changes nothing keeps the current version; a real change
    increments it, which is what makes "the metadata changed after upload" a
    detectable, updatable event rather than a silent overwrite.
    """
    if metadata_hash(stored) == metadata_hash(edited):
        return stored.model_copy(update={"metadata_version": stored.metadata_version})
    return edited.model_copy(update={"metadata_version": stored.metadata_version + 1})
