"""Export stable JSON Schema files for all public contracts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vidgen.contracts import (
    AudioChunk,
    AudioExtractionResult,
    CanonicalTranscriptArtifact,
    CharacterDefinition,
    ChunkTranscriptionResult,
    DiarizationResult,
    EpisodeAnalysis,
    ExtractedFrame,
    MediaProbeResult,
    MediaProcessingResult,
    QAResult,
    RecapScript,
    SceneDefinition,
    SceneDetectionResult,
    ShotDefinition,
    SpeakerTurn,
    Storyboard,
    TranscriptCoverage,
    TranscriptionResult,
    TranscriptSegment,
    TranscriptWord,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "packages" / "contracts" / "schema"
CONTRACTS = (
    EpisodeAnalysis,
    CharacterDefinition,
    SceneDefinition,
    RecapScript,
    Storyboard,
    ShotDefinition,
    QAResult,
    MediaProbeResult,
    AudioExtractionResult,
    SceneDetectionResult,
    ExtractedFrame,
    MediaProcessingResult,
    AudioChunk,
    CanonicalTranscriptArtifact,
    TranscriptWord,
    TranscriptSegment,
    SpeakerTurn,
    ChunkTranscriptionResult,
    DiarizationResult,
    TranscriptCoverage,
    TranscriptionResult,
)


def rendered_schemas() -> dict[Path, str]:
    """Return canonical file paths and deterministic schema JSON."""
    return {
        OUTPUT / f"{contract.__name__}.v1.json": json.dumps(
            contract.model_json_schema(), indent=2, sort_keys=True
        )
        + "\n"
        for contract in CONTRACTS
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    failures: list[str] = []
    for path, content in rendered_schemas().items():
        if args.check:
            if not path.exists() or path.read_text() != content:
                failures.append(str(path.relative_to(ROOT)))
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
    if failures:
        parser.error("stale contract schemas: " + ", ".join(failures))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
