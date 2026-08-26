"""Export stable JSON Schema files for all public contracts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from vidgen.contracts import (
    AliasEvidence,
    AnalysisInference,
    AnalysisObservation,
    AnalysisValidationError,
    AnalysisValidationReport,
    AudioChunk,
    AudioExtractionResult,
    BeatDependency,
    CanonicalScene,
    CanonicalSubtitleTranscriptArtifact,
    CanonicalTranscriptArtifact,
    CharacterCandidate,
    CharacterDefinition,
    ChunkTranscriptionResult,
    DiarizationResult,
    EpisodeAnalysis,
    EpisodeAnalysisResult,
    EpisodeSynthesisRequest,
    EvidencePackage,
    ExtractedFrame,
    LocationCandidate,
    MediaProbeResult,
    MediaProcessingResult,
    ProjectWorkflowInput,
    ProjectWorkflowState,
    ProviderEpisodeAnalysisResult,
    ProviderMetadata,
    ProviderSceneAnalysisResult,
    ProviderSubtitleDownload,
    QAResult,
    RecapScript,
    Relationship,
    SceneAnalysisRequest,
    SceneAnalysisResult,
    SceneDefinition,
    SceneDetectionResult,
    SceneEvidenceExcerpt,
    ShotDefinition,
    SourceReference,
    SpeakerTurn,
    StageActivityInput,
    StageActivityResult,
    StateEvent,
    Storyboard,
    StructuredNote,
    SubtitleCandidate,
    SubtitleCue,
    SubtitleImportResult,
    SubtitleQuality,
    SubtitleSearchRequest,
    TranscriptCoverage,
    TranscriptionResult,
    TranscriptSegment,
    TranscriptWord,
    UnresolvedAmbiguity,
    WorkflowFailure,
)

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "packages" / "contracts" / "schema"
CONTRACTS = (
    EpisodeAnalysis,
    EpisodeAnalysisResult,
    EpisodeSynthesisRequest,
    SceneAnalysisRequest,
    SceneAnalysisResult,
    SceneEvidenceExcerpt,
    SourceReference,
    AnalysisObservation,
    AnalysisInference,
    CharacterCandidate,
    LocationCandidate,
    StateEvent,
    Relationship,
    BeatDependency,
    UnresolvedAmbiguity,
    AnalysisValidationError,
    AnalysisValidationReport,
    AliasEvidence,
    ProviderMetadata,
    ProviderSceneAnalysisResult,
    ProviderEpisodeAnalysisResult,
    StructuredNote,
    CanonicalScene,
    EvidencePackage,
    ProjectWorkflowInput,
    ProjectWorkflowState,
    StageActivityInput,
    StageActivityResult,
    WorkflowFailure,
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
    SubtitleCue,
    SubtitleCandidate,
    SubtitleQuality,
    SubtitleSearchRequest,
    ProviderSubtitleDownload,
    CanonicalSubtitleTranscriptArtifact,
    SubtitleImportResult,
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
