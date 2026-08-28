"""Execute the deterministic T21 2.5D parallax fallback render.

The renderer takes a plan produced by :mod:`services.renderer.parallax_manifest`
and turns approved still images into a repository-standard H.264 ``yuv420p`` MP4
of exactly the canonical shot duration. It costs nothing: no provider, no
credential, no network call.

Two safety properties are structural rather than conventional:

* FFmpeg and FFprobe are always invoked through argument arrays, never a shell
  command string, so nothing derived from a prompt or an asset path can become a
  shell token.
* Media is streamed. Images are read by FFmpeg itself and the rendered clip is
  hashed in bounded chunks, so a whole video file is never loaded into memory.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from services.animation.probe import probe_video
from services.animation.trim import trim_video
from services.renderer.parallax_manifest import (
    RENDERER_VERSION,
    filter_graph,
    frame_count,
    input_arguments,
)
from vidgen.contracts.repair import ParallaxRenderManifest, ParallaxRenderPlan

ENCODING_PROFILE = "h264-crf18-yuv420p-threads1-parallax-v1"
FRAME_TOLERANCE_DIVISOR = 1


class ParallaxRenderError(RuntimeError):
    """The deterministic fallback render could not be produced."""


@dataclass(frozen=True, slots=True)
class ParallaxInputs:
    """The materialized still images, already written to local temporary files."""

    layer_paths: tuple[Path, ...]
    mask_path: Path | None = None
    asset_ids: tuple[UUID, ...] = ()
    asset_hashes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RenderedParallax:
    #: The canonical, exactly-trimmed clip T17 consumes.
    path: Path
    #: The untrimmed render the trim was derived from, kept so the attempt can
    #: persist both the original and the canonical asset the way T15 does.
    untrimmed_path: Path
    manifest: ParallaxRenderManifest
    output_sha256: str
    measured_duration_us: int
    frame_rate: str
    pixel_format: str
    video_codec: str
    ffprobe_json: dict[str, object]


def render_parallax(
    plan: ParallaxRenderPlan, inputs: ParallaxInputs, *, workspace: Path
) -> RenderedParallax:
    """Render one plan to a canonical clip of exactly the planned duration.

    The render itself produces whole frames at the planned frame rate, which can
    exceed the canonical duration by less than one frame. The same deterministic
    trimmer T15 applies to provider output then pins the result to the exact
    canonical duration, so T17 receives a clip it can concatenate without drift.
    """
    if len(inputs.layer_paths) != len(plan.layers):
        raise ParallaxRenderError("every planned layer needs exactly one materialized still")
    frames = frame_count(plan.exact_duration_us, plan.frame_rate)
    graph = filter_graph(plan, frames=frames)
    sources = [str(path) for path in inputs.layer_paths]
    if inputs.mask_path is not None:
        sources.append(str(inputs.mask_path))
    workspace.mkdir(parents=True, exist_ok=True)
    intermediate = workspace / f"parallax-untrimmed-{plan.render_identity[:16]}.mp4"
    arguments = _render_arguments(plan, sources, graph, frames, intermediate)
    _run(arguments, "parallax render")
    trimmed = trim_video(
        intermediate,
        trim_in_seconds=0.0,
        trim_out_seconds=0.0,
        usable_duration_seconds=plan.exact_duration_us / 1_000_000,
        frame_tolerance_seconds=FRAME_TOLERANCE_DIVISOR / plan.frame_rate,
    )
    probe = trimmed.probe
    measured_us = round(probe.duration_seconds * 1_000_000)
    tolerance_us = round(1_000_000 / plan.frame_rate)
    if abs(measured_us - plan.exact_duration_us) > tolerance_us:
        trimmed.path.unlink(missing_ok=True)
        raise ParallaxRenderError(
            "the fallback render did not reach the exact canonical shot duration"
        )
    if (probe.width, probe.height) != (plan.width, plan.height):
        trimmed.path.unlink(missing_ok=True)
        raise ParallaxRenderError("the fallback render did not preserve the shot geometry")
    if probe.pixel_format != plan.pixel_format:
        trimmed.path.unlink(missing_ok=True)
        raise ParallaxRenderError(
            f"the fallback render must be {plan.pixel_format}, got {probe.pixel_format}"
        )
    manifest = ParallaxRenderManifest(
        plan=plan,
        input_asset_ids=list(inputs.asset_ids),
        input_asset_hashes=list(inputs.asset_hashes),
        ffmpeg_arguments=_redact(arguments, sources, intermediate),
        trim_arguments=list(trimmed.manifest.ffmpeg_arguments),
        filter_graph=graph,
        ffmpeg_version=trimmed.ffmpeg_version,
        ffprobe_version=probe.ffprobe_version,
        encoding_profile=ENCODING_PROFILE,
        output_sha256=trimmed.output_sha256,
        measured_duration_us=measured_us,
        measured_width=probe.width,
        measured_height=probe.height,
    )
    return RenderedParallax(
        path=trimmed.path,
        untrimmed_path=intermediate,
        manifest=manifest,
        output_sha256=trimmed.output_sha256,
        measured_duration_us=measured_us,
        frame_rate=probe.frame_rate,
        pixel_format=probe.pixel_format,
        video_codec=probe.video_codec.value,
        ffprobe_json=_bounded_probe(probe.ffprobe_json),
    )


def _render_arguments(
    plan: ParallaxRenderPlan,
    sources: Sequence[str],
    graph: str,
    frames: int,
    output: Path,
) -> list[str]:
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-v",
        "error",
        "-y",
        *input_arguments(plan, sources, frames=frames),
        "-filter_complex",
        graph,
        "-map",
        "[out]",
        "-frames:v",
        str(frames),
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "18",
        "-pix_fmt",
        plan.pixel_format,
        "-r",
        str(plan.frame_rate),
        "-threads",
        "1",
        "-map_metadata",
        "-1",
        "-fflags",
        "+bitexact",
        "-movflags",
        "+faststart",
        str(output),
    ]


def _redact(arguments: Sequence[str], sources: Sequence[str], output: Path) -> list[str]:
    """Replace local paths so a persisted manifest is portable and leaks nothing."""
    replacements = {source: f"<input{index}>" for index, source in enumerate(sources)}
    replacements[str(output)] = "<output>"
    return [replacements.get(argument, argument) for argument in arguments]


def _bounded_probe(payload: dict[str, object]) -> dict[str, object]:
    """Keep only the technical facts a reviewer needs, never the whole blob."""
    streams = payload.get("streams")
    video = next(
        (
            item
            for item in (streams if isinstance(streams, list) else [])
            if isinstance(item, dict) and item.get("codec_type") == "video"
        ),
        {},
    )
    container = payload.get("format")
    return {
        "codec_name": video.get("codec_name"),
        "pix_fmt": video.get("pix_fmt"),
        "width": video.get("width"),
        "height": video.get("height"),
        "avg_frame_rate": video.get("avg_frame_rate"),
        "time_base": video.get("time_base"),
        "nb_read_frames": video.get("nb_read_frames"),
        "duration": video.get("duration"),
        "format_name": container.get("format_name") if isinstance(container, dict) else None,
    }


def _run(arguments: Sequence[str], label: str) -> None:
    completed = subprocess.run(list(arguments), capture_output=True, check=False)
    if completed.returncode:
        detail = completed.stderr.decode(errors="replace")[-500:]
        raise ParallaxRenderError(f"{label} failed: {detail}")


def manifest_bytes(manifest: ParallaxRenderManifest) -> bytes:
    """The canonical, content-addressable serialization of a render manifest."""
    return (
        json.dumps(manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def probe_output(path: Path) -> dict[str, object]:
    return _bounded_probe(probe_video(path).ffprobe_json)


__all__ = [
    "ENCODING_PROFILE",
    "RENDERER_VERSION",
    "ParallaxInputs",
    "ParallaxRenderError",
    "RenderedParallax",
    "file_sha256",
    "manifest_bytes",
    "probe_output",
    "render_parallax",
]
