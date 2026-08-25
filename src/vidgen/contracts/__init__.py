"""Versioned contracts shared by all VidGen pipeline stages."""

from vidgen.contracts.episode import (
    CharacterDefinition,
    CharacterState,
    EpisodeAnalysis,
    LocationDefinition,
    PlotBeat,
    SceneDefinition,
)
from vidgen.contracts.media import (
    AudioExtractionResult,
    ExtractedFrame,
    MediaProbeResult,
    MediaProcessingResult,
    SceneDetectionResult,
)
from vidgen.contracts.qa import QAResult
from vidgen.contracts.script import RecapScript, ScriptSegment
from vidgen.contracts.storyboard import ShotDefinition, Storyboard
from vidgen.contracts.transcription import (
    AudioChunk,
    CanonicalTranscriptArtifact,
    ChunkTranscriptionResult,
    DiarizationRequest,
    DiarizationResult,
    SpeakerTurn,
    TimeInterval,
    TranscriptCoverage,
    TranscriptionRequest,
    TranscriptionResult,
    TranscriptionWarning,
    TranscriptSegment,
    TranscriptWord,
)

__all__ = [
    "AudioChunk",
    "AudioExtractionResult",
    "CanonicalTranscriptArtifact",
    "CharacterDefinition",
    "CharacterState",
    "ChunkTranscriptionResult",
    "DiarizationRequest",
    "DiarizationResult",
    "EpisodeAnalysis",
    "ExtractedFrame",
    "LocationDefinition",
    "MediaProbeResult",
    "MediaProcessingResult",
    "PlotBeat",
    "QAResult",
    "RecapScript",
    "SceneDefinition",
    "SceneDetectionResult",
    "ScriptSegment",
    "ShotDefinition",
    "SpeakerTurn",
    "Storyboard",
    "TimeInterval",
    "TranscriptCoverage",
    "TranscriptSegment",
    "TranscriptWord",
    "TranscriptionRequest",
    "TranscriptionResult",
    "TranscriptionWarning",
]
