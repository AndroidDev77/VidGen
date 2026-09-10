"""Resolve, persist and identify a project's generation settings.

The settings live under ``Project.settings["generation"]`` as the versioned
``ProjectGenerationSettings`` contract. Everything that reads them - the
storyboard pipeline, the animation pipeline, the repair pipeline, the API and
the Temporal fan-out - goes through this module so a legacy project resolves to
exactly one deterministic answer everywhere.

Compatibility decision
----------------------

A project created before these settings existed has no ``generation`` block.
Its effective behaviour on ``main`` was Gen-4 Turbo for every shot: the previous
router upgraded to Gen-4.5 only when ``settings["quality_profile"]`` named
``premium`` or ``hero``, which nothing wrote, and only for shots the storyboard
marked as hero, which it never did. Such a project therefore resolves to
``economy`` so that continuing it never starts spending on Gen-4.5 silently. A
legacy ``quality_profile`` of ``premium`` or ``hero`` expressed exactly the
hero-only upgrade that ``balanced`` now means, so it maps to ``balanced``. New
projects default to ``balanced``. Both mappings are deterministic, recorded in
``origin`` and bound into the generation identity.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from services.animation.providers import capability_registry_hash
from services.animation.routing import QUALITY_REPAIR_POLICY_VERSION, ROUTING_POLICY_VERSION
from vidgen.contracts.generation import (
    GenerationQuality,
    GenerationSettingsOrigin,
    ProjectGenerationSettings,
    ShotPacing,
)
from vidgen.contracts.narration import NarrationQualityThresholds
from vidgen.db.models import Project

GENERATION_SETTINGS_KEY = "generation"
LEGACY_QUALITY_PROFILE_KEY = "quality_profile"
LEGACY_BALANCED_PROFILES = frozenset({"premium", "hero"})
#: Bounded so it can travel through Temporal history and identity material.
GENERATION_POLICY_IDENTITY_MAX_LENGTH = 160


class GenerationSettingsError(ValueError):
    """A stored generation block that cannot be resolved. Deterministic."""


def resolve_generation_settings(settings: Mapping[str, Any] | None) -> ProjectGenerationSettings:
    """The fully resolved settings for a project's stored ``settings`` JSON."""
    stored = settings if isinstance(settings, Mapping) else {}
    block = stored.get(GENERATION_SETTINGS_KEY)
    if isinstance(block, Mapping):
        try:
            return ProjectGenerationSettings.model_validate(dict(block))
        except ValueError as error:
            raise GenerationSettingsError(
                f"stored generation settings are invalid: {error}"
            ) from error
    legacy = stored.get(LEGACY_QUALITY_PROFILE_KEY)
    quality = (
        GenerationQuality.BALANCED
        if isinstance(legacy, str) and legacy in LEGACY_BALANCED_PROFILES
        else GenerationQuality.ECONOMY
    )
    return ProjectGenerationSettings(
        generation_quality=quality,
        shot_pacing=ShotPacing.NORMAL,
        premium_fallback_allowed=False,
        origin=GenerationSettingsOrigin.LEGACY_DEFAULT,
    )


def project_generation_settings(project: Project) -> ProjectGenerationSettings:
    return resolve_generation_settings(project.settings)


def with_generation_settings(
    settings: Mapping[str, Any] | None, generation: ProjectGenerationSettings
) -> dict[str, Any]:
    """A new settings mapping carrying ``generation``; the JSON column needs a new object."""
    merged = dict(settings) if isinstance(settings, Mapping) else {}
    merged[GENERATION_SETTINGS_KEY] = generation.model_dump(mode="json")
    return merged


def effective_scene_detection_threshold(
    generation: ProjectGenerationSettings, global_default: float
) -> float:
    """The scene-cut sensitivity to use: the project's override, else the deployment default."""
    return (
        generation.scene_detection_threshold
        if generation.scene_detection_threshold is not None
        else global_default
    )


def effective_warn_only_validation_codes(
    generation: ProjectGenerationSettings, global_default: Sequence[str]
) -> frozenset[str]:
    """The episode-analysis codes to demote to warnings for this project."""
    return frozenset(
        generation.warn_only_validation_codes
        if generation.warn_only_validation_codes is not None
        else global_default
    )


def effective_script_warn_only_validation_codes(
    generation: ProjectGenerationSettings, global_default: Sequence[str]
) -> frozenset[str]:
    """The compression-validation codes to demote to warnings for this project."""
    return frozenset(
        generation.script_warn_only_validation_codes
        if generation.script_warn_only_validation_codes is not None
        else global_default
    )


def effective_storyboard_warn_only_validation_codes(
    generation: ProjectGenerationSettings, global_default: Sequence[str]
) -> frozenset[str]:
    """The storyboard-validation codes to record as warnings for this project."""
    return frozenset(
        generation.storyboard_warn_only_validation_codes
        if generation.storyboard_warn_only_validation_codes is not None
        else global_default
    )


def effective_narration_warn_only_quality_codes(
    generation: ProjectGenerationSettings, global_default: Sequence[str]
) -> frozenset[str]:
    """The T12 quality codes to demote to warnings for this project."""
    return frozenset(
        generation.narration_warn_only_quality_codes
        if generation.narration_warn_only_quality_codes is not None
        else global_default
    )


def effective_narration_quality_thresholds(
    generation: ProjectGenerationSettings, deployment: NarrationQualityThresholds
) -> NarrationQualityThresholds:
    """The T12 quality gate the narration pipeline is handed for this project.

    ``deployment`` carries the deployment-wide limits and warn-only set. The
    project's limit overrides replace only the limits they set; its warn-only
    override, when present, replaces the deployment's set outright (an empty
    list means "every code fails"). Raises ``GenerationSettingsError`` when the
    combination is not a valid gate, e.g. a project floor above the deployment
    ceiling for speaking rate.
    """
    overrides = generation.narration_quality_thresholds
    try:
        resolved = overrides.apply(deployment) if overrides is not None else deployment
        codes = effective_narration_warn_only_quality_codes(generation, deployment.warn_only_codes)
        return NarrationQualityThresholds.model_validate(
            {**resolved.model_dump(), "warn_only_codes": sorted(codes)}
        )
    except ValueError as error:
        raise GenerationSettingsError(
            f"narration quality settings cannot be resolved: {error}"
        ) from error


def generation_policy_identity(
    generation: ProjectGenerationSettings,
    *,
    capability_profile_id: str,
    capability_hash: str,
) -> str:
    """A compact, versioned identifier binding every routing-material setting.

    It is what Temporal carries: the quality mode, the pacing preset, the
    premium fallback flag, the routing and quality-repair policy versions, the
    T13 capability profile the storyboard was planned against, and the hash of
    the whole provider capability registry. Anything that changes the model a
    shot may use, or the durations it may be generated at, changes this string
    and therefore every shot identity derived from it.
    """
    identity = ";".join(
        (
            f"gq={generation.generation_quality.value}",
            f"sp={generation.shot_pacing.value}",
            f"pf={int(generation.premium_fallback_allowed)}",
            f"rp={ROUTING_POLICY_VERSION}",
            f"qr={QUALITY_REPAIR_POLICY_VERSION}",
            f"cp={capability_profile_id}@{capability_hash[:16]}",
            f"rg={capability_registry_hash()[:16]}",
        )
    )
    if len(identity) > GENERATION_POLICY_IDENTITY_MAX_LENGTH:
        raise GenerationSettingsError("generation policy identity exceeds its bounded length")
    return identity
