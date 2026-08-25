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

__all__ = [
    "AudioExtractionResult",
    "CharacterDefinition",
    "CharacterState",
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
    "Storyboard",
]
