"""Production construction of the immutable T17 :class:`RenderManifest`.

Before T17b the manifest was only ever built by tests. This module is the
production construction, and it deliberately adds nothing to T17's contract: it
translates resolved authoritative inputs into the existing shape and lets that
contract's validators reject anything inconsistent.

Two properties matter more than anything else here:

* **Exact timing.** Every boundary is an integer number of microseconds taken
  straight from T13's canonical timing manifest. Nothing is derived by summing
  floats, so a fifty-shot timeline cannot accumulate drift.
* **Stable identity.** The same authoritative inputs and the same render
  configuration produce byte-identical canonical JSON, therefore the same
  ``render_identity``. Temporary paths, signed URLs, wall-clock timestamps and
  row IDs that are not material to the render never enter that hash.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from services.render_execution.inputs import ResolvedRenderInputs, provenance_for
from services.renderer.captions import (
    build_caption_track,
    caption_identity,
    serialize_ass,
    serialize_srt,
    serialize_webvtt,
)
from services.renderer.manifest import bound_manifest_identity
from services.renderer.selection import RenderLineageError, SelectedShotInput
from vidgen.contracts.render import (
    CaptionTrack,
    CaptionValidationReport,
    RenderAudioEntry,
    RenderInputReference,
    RenderManifest,
    RenderShotEntry,
    RenderVideoProfile,
)

#: Namespace for content-derived manifest and caption-track identifiers. A rerun
#: over unchanged inputs regenerates the same UUIDs, so an interrupted execution
#: resumes onto the same rows instead of forking a parallel identity.
_NAMESPACE = uuid5(NAMESPACE_URL, "vidgen:t17b:render-execution")


@dataclass(frozen=True, slots=True)
class CaptionArtifacts:
    """The canonical caption track and its serialized deliverables."""

    track: CaptionTrack
    validation: CaptionValidationReport
    srt: str
    webvtt: str
    ass: str

    def payloads(self) -> dict[str, bytes]:
        return {
            "caption_srt": self.srt.encode("utf-8"),
            "caption_webvtt": self.webvtt.encode("utf-8"),
            "caption_ass": self.ass.encode("utf-8"),
        }


@dataclass(frozen=True, slots=True)
class BuiltManifest:
    manifest: RenderManifest
    captions: CaptionArtifacts


def caption_track_id_for(resolved: ResolvedRenderInputs) -> UUID:
    """A content-derived caption-track ID, stable across retries."""
    return uuid5(_NAMESPACE, f"caption-track:{resolved.input_hash}")


def manifest_id_for(resolved: ResolvedRenderInputs) -> UUID:
    return uuid5(_NAMESPACE, f"manifest:{resolved.input_hash}")


def build_captions(resolved: ResolvedRenderInputs) -> CaptionArtifacts:
    """Build the deliverable caption track from approved text and T12 timings.

    The approved words come from the shared narration projection, so the text is
    the approved script's text and the timing is the measured narration timing -
    never a re-estimate. ``build_caption_track`` rejects reversed, overlapping or
    out-of-range timings, and the resulting cues can never extend past the
    canonical duration.
    """
    track, validation = build_caption_track(
        track_id=caption_track_id_for(resolved),
        words=list(resolved.words),
        duration_us=resolved.total_duration_us,
        config=resolved.caption_config,
    )
    if not validation.valid:
        raise RenderLineageError(
            "caption_validation_failed",
            "the canonical caption track failed validation",
            reference_id=track.caption_track_id,
        )
    return CaptionArtifacts(
        track=track,
        validation=validation,
        srt=serialize_srt(track),
        webvtt=serialize_webvtt(track),
        ass=serialize_ass(track),
    )


def build_manifest(
    resolved: ResolvedRenderInputs,
    captions: CaptionArtifacts,
    caption_assets: dict[str, RenderInputReference],
) -> RenderManifest:
    """Assemble the immutable manifest from resolved inputs and stored captions.

    ``caption_assets`` maps a caption role (``caption_srt``, ``caption_webvtt``
    and, for burn-in modes, ``caption_ass``) to the asset already persisted
    through :class:`~vidgen.storage.asset_service.AssetService`. The manifest
    references those assets by ID and content hash; a signed URL never appears
    in canonical identity.
    """
    selection = resolved.selection
    settings = resolved.settings
    required = {"caption_srt", "caption_webvtt"}
    if settings.subtitle_mode in {"burn_in", "both"}:
        required.add("caption_ass")
    missing = sorted(required - set(caption_assets))
    if missing:
        raise RenderLineageError(
            "caption_asset_missing",
            f"the manifest requires persisted caption assets: {', '.join(missing)}",
        )
    payloads = captions.payloads()
    for role in sorted(required):
        if hashlib.sha256(payloads[role]).hexdigest() != caption_assets[role].sha256:
            raise RenderLineageError(
                "caption_asset_mismatch",
                f"the persisted {role} asset does not match the generated caption track",
            )

    shots: list[RenderShotEntry] = []
    for sequence, item in enumerate(selection.shots):
        record = item.shot
        usable = record.global_end_us - record.global_start_us
        if usable != record.usable_duration_us:
            raise RenderLineageError(
                "shot_timing_gap",
                "canonical shot timing disagrees with its usable duration",
                reference_id=record.id,
            )
        # The T15 canonical clip is already trimmed to its usable duration -
        # ``select_authoritative_inputs`` proves that - so the manifest trims
        # the whole clip rather than reapplying T13's generation-time offsets.
        shots.append(
            RenderShotEntry(
                # The storyboard shot row's own id: T22 and the review UI both
                # resolve a manifest entry back to that row, and a content-derived
                # id would silently break that join. Content stability lives in
                # ``shot_workflow_identity`` instead.
                shot_id=record.id,
                sequence=sequence,
                shot_workflow_identity=_shot_workflow_identity(item),
                animation_run_id=item.animation_run.id,
                video=RenderInputReference(
                    asset_id=item.asset.id,
                    sha256=item.asset.sha256,
                    media_type=item.asset.media_type,
                    role="locked_t15_clip",
                ),
                source_width=item.video.width,
                source_height=item.video.height,
                source_frame_rate=_normalized_frame_rate(item.video.frame_rate),
                source_codec=item.video.codec,
                measured_source_duration_us=round(item.video.canonical_duration * 1_000_000),
                global_start_us=record.global_start_us,
                global_end_us=record.global_end_us,
                exact_usable_duration_us=usable,
                trim_start_us=0,
                trim_end_us=usable,
                parent_asset_ids=[item.video.original_asset_id],
            )
        )

    audio_entries = [
        RenderAudioEntry(
            role="narration",
            asset=RenderInputReference(
                asset_id=resolved.narration_asset.id,
                sha256=resolved.narration_asset.sha256,
                media_type=resolved.narration_asset.media_type,
                role="narration",
            ),
            start_us=0,
            duration_us=resolved.total_duration_us,
        )
    ]
    audio_entries.extend(
        RenderAudioEntry(
            role=bed.role,  # type: ignore[arg-type]
            asset=RenderInputReference(
                asset_id=bed.asset.id,
                sha256=bed.asset.sha256,
                media_type=bed.asset.media_type,
                role=bed.role,
            ),
            start_us=bed.start_us,
            duration_us=bed.duration_us,
            gain_millidb=bed.gain_millidb,
            duck_under_narration=bed.duck_under_narration,
        )
        for bed in resolved.audio_beds
    )

    ordered_captions = [caption_assets["caption_srt"], caption_assets["caption_webvtt"]]
    if "caption_ass" in required:
        ordered_captions.append(caption_assets["caption_ass"])

    manifest = RenderManifest(
        manifest_id=manifest_id_for(resolved),
        render_identity="0" * 64,
        project_id=selection.project.id,
        approved_script_id=selection.script.id,
        approved_script_version=selection.script.version,
        approved_script_hash=resolved.contract.approved_script_hash,
        narration_run_id=selection.narration.id,
        narration_assets=[
            RenderInputReference(
                asset_id=resolved.narration_asset.id,
                sha256=resolved.narration_asset.sha256,
                media_type=resolved.narration_asset.media_type,
                role="narration_preview",
            ),
            *(
                RenderInputReference(
                    asset_id=asset.id,
                    sha256=asset.sha256,
                    media_type=asset.media_type,
                    role="narration_segment",
                )
                for asset in resolved.narration_segment_assets
            ),
        ],
        narration_word_timing_hash=resolved.contract.narration_word_timing_hash,
        narration_duration_us=resolved.total_duration_us,
        storyboard_run_id=selection.storyboard.id,
        storyboard_hash=resolved.contract.storyboard_hash,
        timing_manifest_id=selection.timing_manifest_asset.id,
        timing_manifest_hash=selection.timing_manifest_asset.sha256,
        t16_result_id=f"t16:{selection.storyboard.id}",
        shots=shots,
        caption_track_id=captions.track.caption_track_id,
        caption_identity=caption_identity(captions.track),
        caption_assets=ordered_captions,
        audio_entries=audio_entries,
        video_profile=RenderVideoProfile(frame_rate=settings.frame_rate),  # type: ignore[arg-type]
        subtitle_mode=settings.subtitle_mode,  # type: ignore[arg-type]
        input_hash=resolved.input_hash,
        idempotency_key=f"t17b:{resolved.input_hash}",
        created_at=datetime.now(UTC),
        provenance=provenance_for(resolved),
    )
    return manifest.model_copy(update={"render_identity": bound_manifest_identity(manifest)})


def _shot_workflow_identity(item: SelectedShotInput) -> str:
    """A stable per-shot identity derived from the locked T15/T16 outputs."""
    video = item.video
    shot = item.shot
    return hashlib.sha256(
        "|".join(
            (
                str(shot.stable_shot_id),
                str(video.animation_item_id),
                video.sha256,
                str(shot.global_start_us),
                str(shot.global_end_us),
            )
        ).encode("utf-8")
    ).hexdigest()


def _normalized_frame_rate(value: str) -> str:
    """Normalize a probed frame rate into the manifest's ``num/den`` form."""
    text = (value or "").strip()
    if "/" in text:
        numerator, _, denominator = text.partition("/")
        if numerator.isdigit() and denominator.isdigit() and int(numerator) > 0:
            if int(denominator) > 0:
                return f"{int(numerator)}/{int(denominator)}"
        raise RenderLineageError(
            "unsupported_source_frame_rate", f"unsupported source frame rate {value!r}"
        )
    try:
        rate = float(text)
    except ValueError as error:
        raise RenderLineageError(
            "unsupported_source_frame_rate", f"unsupported source frame rate {value!r}"
        ) from error
    if rate <= 0:
        raise RenderLineageError(
            "unsupported_source_frame_rate", f"unsupported source frame rate {value!r}"
        )
    return f"{round(rate * 1000)}/1000"
