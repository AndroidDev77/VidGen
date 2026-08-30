"""Authoritative render-input resolution and the canonical render input hash.

There is exactly one definition of "the inputs this project renders from", and
it is :func:`resolve_render_inputs`. The queue command uses it to stamp a render
job's input identity; the executor uses it again at claim time and refuses to
proceed when the identity it resolves differs from the identity the job was
queued with. That refusal is the whole point: a render whose inputs moved is a
different render, and it needs a new render job rather than a quiet substitution.

The T11-T16 lineage rules are not re-derived here. They live in
:mod:`services.renderer.selection`, which T17 already tests, and this module
layers the T17b concerns on top: the narration bed, the caption configuration,
the render settings, the audio beds, the T21 repair state, the reference
provenance, and the deterministic hash over all of it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.renderer.captions import CaptionConfig
from services.renderer.manifest import render_identity
from services.renderer.selection import (
    AuthoritativeRenderSelection,
    RenderLineageError,
    project_narration_words,
    select_authoritative_inputs,
    visual_qa_provenance,
)
from vidgen.contracts.render import CaptionWord, RenderInputReference
from vidgen.contracts.render_execution import RenderInputSelection
from vidgen.db.continuity_models import shot_reference_bindings
from vidgen.db.models import Asset, AudioAsset, RenderJob
from vidgen.db.repair_models import RepairRun

#: Supported render settings. A render job that asks for anything else is
#: rejected before any FFmpeg work, because T17's manifest contract only
#: validates these profiles and silently downgrading one would produce a
#: deliverable nobody asked for.
SUPPORTED_PROFILES: dict[str, tuple[int, int, int, str]] = {
    "1080p24": (1920, 1080, 24, "16:9"),
    "1080p30": (1920, 1080, 30, "16:9"),
}
SUPPORTED_SUBTITLE_MODES = frozenset({"selectable", "burn_in", "both"})

#: The T17b input-selection policy version. It is part of the input hash, so a
#: change to what counts as an authoritative input produces a new identity
#: instead of silently reusing a render built under the old rules.
INPUT_POLICY_VERSION = "render-input-selection/1.0"
PIPELINE_VERSION = "t17/1"

#: T21 states that mean a shot is not yet eligible for final assembly. The codes
#: mirror :mod:`services.qa.final_inputs`; T17b refuses to render what T22 would
#: refuse to accept.
BLOCKING_REPAIR_STATES = {
    "REPAIR_PLANNING": "active_repair_run",
    "REPAIRING": "active_repair_run",
    "ALTERNATE_PROVIDER": "active_repair_run",
    "FALLBACK_RENDERING": "active_repair_run",
    "REVALIDATING": "active_repair_run",
    "HUMAN_REVIEW_REQUIRED": "unresolved_repair_review",
    "REPAIR_FAILED": "failed_repair_run",
}

#: Audio roles a bed asset may take in the mix. No upstream stage produces music
#: or sound effects yet; the resolution path exists so that when one does, the
#: bed lands in the manifest instead of being dropped on the floor.
AUDIO_BED_ROLES: dict[str, str] = {
    "music": "music",
    "score": "music",
    "sfx": "sfx",
    "sound_effect": "sfx",
}


@dataclass(frozen=True, slots=True)
class RenderSettings:
    """The validated output configuration for one render job."""

    profile: str
    subtitle_mode: str
    language: str
    width: int
    height: int
    frame_rate: int
    aspect_ratio: str


@dataclass(frozen=True, slots=True)
class AudioBed:
    """A selected non-narration audio asset placed on the global timeline."""

    audio_asset_id: UUID
    asset: Asset
    role: str
    start_us: int
    duration_us: int
    gain_millidb: int
    duck_under_narration: bool


@dataclass(frozen=True, slots=True)
class ResolvedRenderInputs:
    """Everything the manifest builder needs, already proven consistent."""

    selection: AuthoritativeRenderSelection
    settings: RenderSettings
    caption_config: CaptionConfig
    narration_asset: Asset
    narration_segment_assets: tuple[Asset, ...]
    words: tuple[CaptionWord, ...]
    audio_beds: tuple[AudioBed, ...]
    repair_run_ids: tuple[UUID, ...]
    character_reference_ids: tuple[UUID, ...]
    location_reference_ids: tuple[UUID, ...]
    reference_bundle_hashes: tuple[str, ...]
    total_duration_us: int
    contract: RenderInputSelection

    @property
    def input_hash(self) -> str:
        return self.contract.input_hash


def render_settings_for(job: RenderJob) -> RenderSettings:
    """Read and validate the render settings persisted on the job row."""
    profile = str((job.video_profile or {}).get("name") or "1080p24")
    subtitle_mode = str((job.caption_profile or {}).get("subtitle_mode") or "selectable")
    language = str((job.caption_profile or {}).get("language") or "en")
    if profile not in SUPPORTED_PROFILES:
        raise RenderLineageError(
            "unsupported_render_settings",
            f"render profile {profile!r} is not supported by the T17 pipeline",
            reference_id=job.id,
        )
    if subtitle_mode not in SUPPORTED_SUBTITLE_MODES:
        raise RenderLineageError(
            "unsupported_render_settings",
            f"subtitle mode {subtitle_mode!r} is not supported by the T17 pipeline",
            reference_id=job.id,
        )
    width, height, frame_rate, aspect_ratio = SUPPORTED_PROFILES[profile]
    return RenderSettings(
        profile=profile,
        subtitle_mode=subtitle_mode,
        language=language,
        width=width,
        height=height,
        frame_rate=frame_rate,
        aspect_ratio=aspect_ratio,
    )


def caption_config_for(settings: RenderSettings) -> CaptionConfig:
    return CaptionConfig(language=settings.language)


def caption_configuration_hash(config: CaptionConfig) -> str:
    return render_identity(
        {
            "max_chars_per_line": config.max_chars_per_line,
            "max_lines": config.max_lines,
            "max_words_per_cue": config.max_words_per_cue,
            "min_duration_us": config.min_duration_us,
            "max_duration_us": config.max_duration_us,
            "max_chars_per_second": config.max_chars_per_second,
            "safe_zone_percent": config.safe_zone_percent,
            "language": config.language,
            "pipeline_version": "captions/1",
        }
    )


def resolve_render_inputs(
    session: Session, *, job: RenderJob, settings: RenderSettings | None = None
) -> ResolvedRenderInputs:
    """Resolve, validate and hash every authoritative input for one render job.

    Raises :class:`~services.renderer.selection.RenderLineageError` - always
    non-retryable - when the project's current state cannot produce a render.
    """
    resolved_settings = settings or render_settings_for(job)
    selection = select_authoritative_inputs(session, job.project_id)
    _reject_blocking_repairs(session, selection)
    repair_run_ids = _locked_repair_runs(session, selection)
    narration_asset = _narration_bed(session, selection)
    segment_assets = _narration_segment_assets(session, selection)
    _intervals, words = project_narration_words(
        session,
        storyboard_run_id=selection.storyboard.id,
        narration_run_id=selection.narration.id,
    )
    total_duration_us = selection.storyboard.total_duration_us
    if not words:
        raise RenderLineageError(
            "narration_alignment_missing",
            "the selected narration has no usable approved word timings",
            reference_id=selection.narration.id,
        )
    if words[-1].end_us > total_duration_us:
        raise RenderLineageError(
            "caption_timing_out_of_bounds",
            "approved narration word timings extend past the canonical timeline",
            reference_id=selection.narration.id,
        )
    beds = _audio_beds(session, selection, total_duration_us)
    characters, locations, bundle_hashes = _reference_provenance(session, selection)
    caption_config = caption_config_for(resolved_settings)
    references = _references(selection, narration_asset, segment_assets, beds)
    contract = RenderInputSelection(
        project_id=selection.project.id,
        render_job_id=job.id,
        approved_script_id=selection.script.id,
        approved_script_version=selection.script.version,
        approved_script_hash=_script_hash(session, selection),
        narration_run_id=selection.narration.id,
        narration_asset_id=narration_asset.id,
        narration_duration_us=total_duration_us,
        narration_word_timing_hash=render_identity(
            [word.model_dump(mode="json") for word in words]
        ),
        storyboard_run_id=selection.storyboard.id,
        storyboard_hash=selection.timing_manifest_asset.sha256,
        timing_manifest_id=selection.timing_manifest_asset.id,
        timing_manifest_hash=selection.timing_manifest_asset.sha256,
        shot_count=len(selection.shots),
        references=references,
        visual_qa_result_ids=list(selection.visual_qa_result_ids),
        repair_result_ids=list(repair_run_ids),
        character_reference_ids=list(characters),
        location_reference_ids=list(locations),
        audio_asset_ids=[bed.audio_asset_id for bed in beds],
        subtitle_mode=resolved_settings.subtitle_mode,  # type: ignore[arg-type]
        render_profile=resolved_settings.profile,
        target_duration_us=total_duration_us,
        aspect_ratio=resolved_settings.aspect_ratio,
        output_width=resolved_settings.width,
        output_height=resolved_settings.height,
        frame_rate=resolved_settings.frame_rate,
        caption_configuration_hash=caption_configuration_hash(caption_config),
        visual_qa_policy_version=selection.visual_qa_policy_version,
        pipeline_version=PIPELINE_VERSION,
        input_hash="0" * 64,
        resolved_at=datetime.now(UTC),
    )
    contract = contract.model_copy(
        update={"input_hash": input_hash(contract, bundle_hashes=bundle_hashes)}
    )
    return ResolvedRenderInputs(
        selection=selection,
        settings=resolved_settings,
        caption_config=caption_config,
        narration_asset=narration_asset,
        narration_segment_assets=segment_assets,
        words=words,
        audio_beds=beds,
        repair_run_ids=repair_run_ids,
        character_reference_ids=characters,
        location_reference_ids=locations,
        reference_bundle_hashes=bundle_hashes,
        total_duration_us=total_duration_us,
        contract=contract,
    )


def input_hash(selection: RenderInputSelection, *, bundle_hashes: tuple[str, ...] = ()) -> str:
    """Hash every material input and setting, and nothing else.

    ``render_job_id``, ``resolved_at`` and the placeholder hash are excluded:
    identity describes the render, not the row or the moment it was resolved.
    Two jobs resolving the same project state must agree.
    """
    material = selection.model_dump(
        mode="json", exclude={"render_job_id", "resolved_at", "input_hash"}
    )
    material["reference_bundle_hashes"] = list(bundle_hashes)
    material["policy_version"] = INPUT_POLICY_VERSION
    return render_identity(material)


def provenance_for(resolved: ResolvedRenderInputs) -> dict[str, Any]:
    """The manifest provenance block naming every input this render came from."""
    selection = resolved.selection
    provenance: dict[str, Any] = {
        "input_policy_version": INPUT_POLICY_VERSION,
        "input_hash": resolved.input_hash,
        "narration_asset_id": str(resolved.narration_asset.id),
        "narration_segment_asset_ids": [
            str(asset.id) for asset in resolved.narration_segment_assets
        ],
        "animation_run_ids": sorted({str(item.animation_run.id) for item in selection.shots}),
        "shot_asset_ids": [str(item.asset.id) for item in selection.shots],
        "repair_run_ids": [str(value) for value in resolved.repair_run_ids],
        "character_reference_ids": [str(value) for value in resolved.character_reference_ids],
        "location_reference_ids": [str(value) for value in resolved.location_reference_ids],
        "reference_bundle_hashes": list(resolved.reference_bundle_hashes),
        "audio_asset_ids": [str(bed.audio_asset_id) for bed in resolved.audio_beds],
        "render_profile": resolved.settings.profile,
        "aspect_ratio": resolved.settings.aspect_ratio,
    }
    provenance.update(visual_qa_provenance(selection))
    return provenance


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------


def _script_hash(session: Session, selection: AuthoritativeRenderSelection) -> str:
    asset = session.get(Asset, selection.script.canonical_script_asset_id)
    if asset is None or asset.project_id != selection.project.id:
        raise RenderLineageError(
            "script_asset_missing",
            "the approved script has no readable canonical asset for this project",
            reference_id=selection.script.id,
        )
    return asset.sha256


def _narration_bed(session: Session, selection: AuthoritativeRenderSelection) -> Asset:
    """The single mixable narration asset: T12's canonical ordered preview."""
    asset_id = selection.narration.preview_asset_id
    if asset_id is None:
        raise RenderLineageError(
            "narration_bed_missing",
            "the selected narration run has no canonical concatenated audio asset",
            reference_id=selection.narration.id,
        )
    asset = session.get(Asset, asset_id)
    if asset is None or asset.project_id != selection.project.id:
        raise RenderLineageError(
            "narration_bed_missing",
            "the narration audio asset is missing or belongs to another project",
            reference_id=asset_id,
        )
    return asset


def _narration_segment_assets(
    session: Session, selection: AuthoritativeRenderSelection
) -> tuple[Asset, ...]:
    assets: list[Asset] = []
    for segment in selection.narration_segments:
        asset = session.get(Asset, segment.normalized_asset_id)
        if asset is None or asset.project_id != selection.project.id:
            raise RenderLineageError(
                "narration_segment_asset_missing",
                "a narration segment asset is missing or belongs to another project",
                reference_id=segment.id,
            )
        assets.append(asset)
    return tuple(assets)


def _reject_blocking_repairs(session: Session, selection: AuthoritativeRenderSelection) -> None:
    shot_ids = [item.shot.id for item in selection.shots]
    if not shot_ids:
        return
    for run in session.scalars(select(RepairRun).where(RepairRun.shot_id.in_(shot_ids))):
        code = BLOCKING_REPAIR_STATES.get(run.state)
        if code is not None:
            raise RenderLineageError(
                code,
                f"a required shot has a T21 repair run in state {run.state}",
                reference_id=run.id,
            )


def _locked_repair_runs(
    session: Session, selection: AuthoritativeRenderSelection
) -> tuple[UUID, ...]:
    """Prove each repaired shot renders its locked T21 output, not the original."""
    selected_asset_by_shot = {item.shot.id: item.asset.id for item in selection.shots}
    locked: list[UUID] = []
    for run in session.scalars(
        select(RepairRun).where(
            RepairRun.shot_id.in_(list(selected_asset_by_shot)), RepairRun.state == "LOCKED"
        )
    ):
        expected = selected_asset_by_shot.get(run.shot_id)
        if run.selected_asset_id is not None and run.selected_asset_id != expected:
            raise RenderLineageError(
                "stale_shot_selection",
                "a shot's locked T21 repair output is not the selected canonical clip",
                reference_id=run.id,
            )
        locked.append(run.id)
    return tuple(sorted(locked, key=str))


def _audio_beds(
    session: Session, selection: AuthoritativeRenderSelection, total_duration_us: int
) -> tuple[AudioBed, ...]:
    """Resolve selected music and sound-effect beds, bounded by the timeline.

    A bed that would run past the canonical duration is rejected rather than
    trimmed: the picture length is fixed by T13 timing, and quietly extending
    the audio would fail verification after an expensive encode.
    """
    rows = list(
        session.scalars(
            select(AudioAsset)
            .where(
                AudioAsset.project_id == selection.project.id,
                AudioAsset.kind.in_(sorted(AUDIO_BED_ROLES)),
            )
            .order_by(AudioAsset.kind, AudioAsset.created_at, AudioAsset.id)
        )
    )
    starts = _segment_start_offsets(selection)
    beds: list[AudioBed] = []
    for row in rows:
        asset = session.get(Asset, row.asset_id)
        if asset is None or asset.project_id != selection.project.id:
            raise RenderLineageError(
                "audio_asset_missing",
                "a selected audio bed asset is missing or belongs to another project",
                reference_id=row.id,
            )
        role = AUDIO_BED_ROLES[row.kind]
        start_us = (
            0
            if role == "music" or row.script_segment_id is None
            else starts.get(row.script_segment_id, 0)
        )
        duration_us = round(row.duration_seconds * 1_000_000)
        if duration_us <= 0 or start_us + duration_us > total_duration_us:
            raise RenderLineageError(
                "audio_bed_out_of_bounds",
                "a selected audio bed does not fit inside the canonical timeline",
                reference_id=row.id,
            )
        beds.append(
            AudioBed(
                audio_asset_id=row.id,
                asset=asset,
                role=role,
                start_us=start_us,
                duration_us=duration_us,
                gain_millidb=-9000 if role == "music" else 0,
                duck_under_narration=role == "music",
            )
        )
    return tuple(beds)


def _segment_start_offsets(selection: AuthoritativeRenderSelection) -> dict[UUID, int]:
    starts: dict[UUID, int] = {}
    for item in selection.shots:
        segment_id = item.shot.script_segment_id
        starts[segment_id] = min(
            starts.get(segment_id, item.shot.global_start_us), item.shot.global_start_us
        )
    return starts


def _reference_provenance(
    session: Session, selection: AuthoritativeRenderSelection
) -> tuple[tuple[UUID, ...], tuple[UUID, ...], tuple[str, ...]]:
    """Collect the T19 character and location references bound to these shots."""
    shot_ids = [item.shot.id for item in selection.shots]
    if not shot_ids:
        return (), (), ()
    rows = session.execute(
        select(
            shot_reference_bindings.c.storyboard_shot_id,
            shot_reference_bindings.c.bundle,
            shot_reference_bindings.c.bundle_hash,
        )
        .where(
            shot_reference_bindings.c.project_id == selection.project.id,
            shot_reference_bindings.c.storyboard_id == selection.storyboard.id,
            shot_reference_bindings.c.storyboard_shot_id.in_(shot_ids),
        )
        .order_by(shot_reference_bindings.c.storyboard_shot_id)
    ).all()
    characters: set[str] = set()
    locations: set[str] = set()
    hashes: list[str] = []
    for _shot_id, bundle, bundle_hash in rows:
        payload = bundle if isinstance(bundle, dict) else {}
        characters.update(str(value) for value in payload.get("character_identity_version_ids", []))
        location = payload.get("location_identity_version_id")
        if location:
            locations.add(str(location))
        hashes.append(str(bundle_hash))
    return (
        tuple(UUID(value) for value in sorted(characters)),
        tuple(UUID(value) for value in sorted(locations)),
        tuple(sorted(hashes)),
    )


def _references(
    selection: AuthoritativeRenderSelection,
    narration_asset: Asset,
    segment_assets: tuple[Asset, ...],
    beds: tuple[AudioBed, ...],
) -> list[RenderInputReference]:
    references = [
        RenderInputReference(
            asset_id=item.asset.id,
            sha256=item.asset.sha256,
            media_type=item.asset.media_type,
            role="locked_t15_clip",
        )
        for item in selection.shots
    ]
    references.append(
        RenderInputReference(
            asset_id=narration_asset.id,
            sha256=narration_asset.sha256,
            media_type=narration_asset.media_type,
            role="narration",
        )
    )
    references.extend(
        RenderInputReference(
            asset_id=asset.id,
            sha256=asset.sha256,
            media_type=asset.media_type,
            role="narration_segment",
        )
        for asset in segment_assets
    )
    references.append(
        RenderInputReference(
            asset_id=selection.timing_manifest_asset.id,
            sha256=selection.timing_manifest_asset.sha256,
            media_type=selection.timing_manifest_asset.media_type,
            role="timing_manifest",
        )
    )
    references.extend(
        RenderInputReference(
            asset_id=bed.asset.id,
            sha256=bed.asset.sha256,
            media_type=bed.asset.media_type,
            role=bed.role,
        )
        for bed in beds
    )
    return references
