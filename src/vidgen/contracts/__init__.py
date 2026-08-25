"""Versioned contracts shared by all VidGen pipeline stages."""

from vidgen.contracts.episode import (
    CharacterDefinition,
    CharacterState,
    EpisodeAnalysis,
    LocationDefinition,
    PlotBeat,
    SceneDefinition,
)
from vidgen.contracts.qa import QAResult
from vidgen.contracts.script import RecapScript, ScriptSegment
from vidgen.contracts.storyboard import ShotDefinition, Storyboard

__all__ = [
    "CharacterDefinition",
    "CharacterState",
    "EpisodeAnalysis",
    "LocationDefinition",
    "PlotBeat",
    "QAResult",
    "RecapScript",
    "SceneDefinition",
    "ScriptSegment",
    "ShotDefinition",
    "Storyboard",
]
