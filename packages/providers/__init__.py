"""Deterministic provider test doubles."""

from packages.providers.image_generation import DeterministicFakeImageProvider
from packages.providers.subtitles import FakeSubtitleProvider

__all__ = ["DeterministicFakeImageProvider", "FakeSubtitleProvider"]
