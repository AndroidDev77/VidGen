"""Shot-pacing presets: creative target ranges the Storyboard Director plans against.

A preset says how long an edited shot should *typically* run. It is guidance for
semantic shot planning, never a fixed number of shots per minute: the Director
still cuts only at approved sentence, clause and comedy-beat boundaries, may
hold an establishing, emotional or hero shot longer, and may cut a punchline or
reaction shot shorter. The deterministic retimer remains the timing authority;
the only thing a preset changes there is the hard bound above which an over-long
shot is split, which is always capped by what the visual provider can generate.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from services.storyboard.retimer import RetimerConfig
from vidgen.contracts.generation import ShotPacing
from vidgen.contracts.storyboard import MICROSECONDS_PER_SECOND, ShotPacingGuidance

PACING_PROFILE_VERSION = "shot-pacing/1"


def _seconds(value: str) -> int:
    from decimal import Decimal

    return int(Decimal(value) * MICROSECONDS_PER_SECOND)


@dataclass(frozen=True, slots=True)
class PacingProfile:
    preset: ShotPacing
    #: The preferred edited-shot range. A preference, not a hard limit.
    target_min_us: int
    target_max_us: int
    #: Above this the retimer splits at an approved boundary; the provider's own
    #: maximum still applies on top.
    hard_max_us: int
    #: Below this the retimer merges a shot into a neighbour.
    hard_min_us: int = _seconds("1")
    #: A punchline or reaction shot may be this short without being merged away.
    min_punchline_us: int = _seconds("1.5")
    #: An establishing, emotional or hero shot may run this long.
    hero_max_us: int = _seconds("10")
    version: str = PACING_PROFILE_VERSION

    @property
    def target_midpoint_us(self) -> int:
        return (self.target_min_us + self.target_max_us) // 2

    def material(self) -> dict[str, int | str]:
        return {
            "version": self.version,
            "preset": self.preset.value,
            "target_min_us": self.target_min_us,
            "target_max_us": self.target_max_us,
            "hard_min_us": self.hard_min_us,
            "hard_max_us": self.hard_max_us,
            "min_punchline_us": self.min_punchline_us,
            "hero_max_us": self.hero_max_us,
        }

    def guidance(self) -> ShotPacingGuidance:
        return ShotPacingGuidance(
            preset=self.preset.value,
            profile_version=self.version,
            target_min_duration_us=self.target_min_us,
            target_max_duration_us=self.target_max_us,
            hard_max_duration_us=self.hard_max_us,
            min_punchline_duration_us=self.min_punchline_us,
            hero_max_duration_us=self.hero_max_us,
        )


PACING_PROFILES: dict[ShotPacing, PacingProfile] = {
    ShotPacing.RELAXED: PacingProfile(
        preset=ShotPacing.RELAXED,
        target_min_us=_seconds("6"),
        target_max_us=_seconds("10"),
        hard_max_us=_seconds("10"),
    ),
    # The normal bounds are the retimer's original defaults, so a project that
    # never chose a preset keeps exactly the timing it had.
    ShotPacing.NORMAL: PacingProfile(
        preset=ShotPacing.NORMAL,
        target_min_us=_seconds("4"),
        target_max_us=_seconds("7"),
        hard_max_us=_seconds("7.5"),
    ),
    ShotPacing.FAST: PacingProfile(
        preset=ShotPacing.FAST,
        target_min_us=_seconds("2.5"),
        target_max_us=_seconds("4"),
        hard_max_us=_seconds("5"),
    ),
}
DEFAULT_PACING = ShotPacing.NORMAL


def pacing_profile(preset: ShotPacing | str | None) -> PacingProfile:
    resolved = ShotPacing(preset) if preset is not None else DEFAULT_PACING
    return PACING_PROFILES[resolved]


def retimer_config_for(profile: PacingProfile, base: RetimerConfig | None = None) -> RetimerConfig:
    """The retimer bounds a preset implies, on top of any explicit configuration."""
    config = base or RetimerConfig()
    return replace(
        config,
        min_shot_duration_us=profile.hard_min_us,
        max_shot_duration_us=profile.hard_max_us,
        pacing_preset=profile.preset.value,
        pacing_version=profile.version,
    )
