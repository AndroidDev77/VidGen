from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path
from uuid import UUID

from services.media_worker.commands import CommandRunner
from services.transcription.commands import detect_silence_ranges, encode_flac, probe_duration
from vidgen.contracts.transcription import AudioChunk, TimeInterval
from vidgen.storage.asset_service import AssetService

CHUNKER_VERSION = "1.0"


@dataclass(frozen=True, slots=True)
class ChunkerConfig:
    max_bytes: int = 24 * 1024 * 1024
    overlap_seconds: float = 1.5
    hard_duration_seconds: float = 600
    minimum_chunk_seconds: float = 5
    sample_rate: int = 16_000
    silence_noise_db: float = -38
    minimum_silence_seconds: float = 0.35

    def __post_init__(self) -> None:
        if self.max_bytes <= 0 or self.hard_duration_seconds <= 0:
            raise ValueError("chunk limits must be positive")
        if self.overlap_seconds < 0 or self.minimum_chunk_seconds <= 0:
            raise ValueError("chunk timing must be nonnegative")


def voiced_intervals(
    duration_seconds: float, silence: list[tuple[float, float]]
) -> list[TimeInterval]:
    voiced: list[TimeInterval] = []
    cursor = 0.0
    for start, end in sorted(silence):
        if start > cursor:
            voiced.append(TimeInterval(start_seconds=cursor, end_seconds=start))
        cursor = max(cursor, end)
    if cursor < duration_seconds:
        voiced.append(TimeInterval(start_seconds=cursor, end_seconds=duration_seconds))
    return voiced


def create_audio_chunks(
    *,
    source: Path,
    workspace: Path,
    project_id: UUID,
    parent_audio_asset_id: UUID,
    parent_sha256: str,
    asset_service: AssetService,
    config: ChunkerConfig | None = None,
    runner: CommandRunner | None = None,
) -> tuple[list[AudioChunk], list[TimeInterval], float]:
    config = config or ChunkerConfig()
    duration = probe_duration(source, runner)
    silence = detect_silence_ranges(
        source,
        duration_seconds=duration,
        noise_db=config.silence_noise_db,
        minimum_silence_seconds=config.minimum_silence_seconds,
        runner=runner,
    )
    boundaries = _initial_boundaries(duration, silence, config)
    intervals = list(pairwise(boundaries))

    while True:
        oversized: int | None = None
        encoded: list[Path] = []
        for sequence, (base_start, base_end) in enumerate(intervals):
            start = max(0.0, base_start - (config.overlap_seconds if sequence else 0.0))
            end = min(
                duration,
                base_end + (config.overlap_seconds if sequence < len(intervals) - 1 else 0.0),
            )
            path = workspace / f"chunk-{sequence:05d}.flac"
            encode_flac(
                source,
                path,
                start_seconds=start,
                end_seconds=end,
                sample_rate=config.sample_rate,
                runner=runner,
            )
            encoded.append(path)
            if path.stat().st_size > config.max_bytes:
                oversized = sequence
                break
        if oversized is None:
            break
        for path in encoded:
            path.unlink(missing_ok=True)
        start, end = intervals[oversized]
        if end - start <= 0.25:
            raise ValueError("encoded transcription chunk cannot fit provider byte limit")
        midpoint = round((start + end) / 2, 6)
        intervals[oversized : oversized + 1] = [(start, midpoint), (midpoint, end)]

    chunks: list[AudioChunk] = []
    parameters = {
        "chunker_version": CHUNKER_VERSION,
        "max_bytes": config.max_bytes,
        "overlap_seconds": config.overlap_seconds,
        "hard_duration_seconds": config.hard_duration_seconds,
        "sample_rate": config.sample_rate,
        "codec": "flac",
        "silence_noise_db": config.silence_noise_db,
        "minimum_silence_seconds": config.minimum_silence_seconds,
    }
    for sequence, ((base_start, base_end), path) in enumerate(zip(intervals, encoded, strict=True)):
        start = max(0.0, base_start - (config.overlap_seconds if sequence else 0.0))
        end = min(
            duration,
            base_end + (config.overlap_seconds if sequence < len(intervals) - 1 else 0.0),
        )
        key_material = json.dumps(
            {
                "parent_sha256": parent_sha256,
                "sequence": sequence,
                "start": start,
                "end": end,
                **parameters,
            },
            sort_keys=True,
        )
        stable_key = hashlib.sha256(key_material.encode()).hexdigest()
        stored = asset_service.store_file(
            path=path,
            kind="audio",
            media_type="audio/flac",
            project_id=project_id,
            parent_asset_ids=(parent_audio_asset_id,),
            provider="ffmpeg",
            idempotency_key=f"transcription-chunk:{stable_key}",
            generation_parameters={
                **parameters,
                "sequence": sequence,
                "start_seconds": start,
                "end_seconds": end,
            },
        )
        chunks.append(
            AudioChunk(
                asset_id=stored.id,
                parent_audio_asset_id=parent_audio_asset_id,
                sequence=sequence,
                start_seconds=start,
                end_seconds=end,
                overlap_before_seconds=max(0.0, base_start - start),
                overlap_after_seconds=max(0.0, end - base_end),
                byte_size=stored.byte_size,
                sha256=stored.sha256,
                codec="flac",
                sample_rate=config.sample_rate,
                idempotency_key=f"transcription-chunk:{stable_key}",
            )
        )
    return chunks, voiced_intervals(duration, silence), duration


def _initial_boundaries(
    duration: float, silence: list[tuple[float, float]], config: ChunkerConfig
) -> list[float]:
    silence_midpoints = [(start + end) / 2 for start, end in silence]
    boundaries = [0.0]
    while duration - boundaries[-1] > config.hard_duration_seconds:
        lower = boundaries[-1] + config.minimum_chunk_seconds
        upper = boundaries[-1] + config.hard_duration_seconds
        candidates = [value for value in silence_midpoints if lower <= value <= upper]
        boundary = max(candidates) if candidates else upper
        boundaries.append(round(boundary, 6))
    boundaries.append(duration)
    return boundaries
