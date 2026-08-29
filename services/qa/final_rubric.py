"""Versioned T22 configuration, rubric and identity material.

Every threshold a final-QA run applies lives here, behind a version string. The
whole configuration is hashed into the final-QA identity, so raising a loudness
tolerance or adding an editorial dimension produces a new identity and a new run
instead of silently re-grading a render against different rules.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from vidgen.contracts.final_editorial import (
    FinalEditorialCategory,
    FinalQAConfiguration,
    FinalRemediationTarget,
)

#: Bumped whenever the pipeline's stage behaviour changes.
FINAL_QA_PIPELINE_VERSION = "final-editorial/1.0.0"
GATE_VERSION = "final-gate/1.0"

DETERMINISTIC_CHECK_VERSION = "final-deterministic/1.0"
AUDIO_CHECK_VERSION = "final-audio/1.0"
CAPTION_CHECK_VERSION = "final-caption/1.0"
EDITORIAL_RUBRIC_VERSION = "final-rubric/1.0"
PROMPT_VERSION = "final-editorial-prompt/1.0"
ADJUDICATION_POLICY_VERSION = "final-adjudication/1.0"
SAMPLING_VERSION = "final-sampling/1.0"
CONFIGURATION_VERSION = "final-qa-config/1.0"

#: The default delivery and tolerance profile. A deployment overrides fields on a
#: copy; it never mutates this object.
DEFAULT_CONFIGURATION = FinalQAConfiguration(
    configuration_version=CONFIGURATION_VERSION,
    deterministic_check_version=DETERMINISTIC_CHECK_VERSION,
    audio_check_version=AUDIO_CHECK_VERSION,
    caption_check_version=CAPTION_CHECK_VERSION,
    editorial_rubric_version=EDITORIAL_RUBRIC_VERSION,
    prompt_version=PROMPT_VERSION,
    adjudication_policy_version=ADJUDICATION_POLICY_VERSION,
)

#: Every dimension the editorial pass must return. A provider result missing one
#: is a provider-contract failure, never a silent pass.
EDITORIAL_DIMENSIONS: tuple[FinalEditorialCategory, ...] = tuple(FinalEditorialCategory)

#: Categories whose confirmed findings are structurally blocking regardless of
#: any score. A high average must never conceal one of these.
BLOCKING_CATEGORIES: frozenset[FinalEditorialCategory] = frozenset(
    {
        FinalEditorialCategory.STORY_BEAT_COVERAGE,
        FinalEditorialCategory.SCENE_COMPLETENESS,
        FinalEditorialCategory.CHARACTER_IDENTITY_CONTINUITY,
        FinalEditorialCategory.LOCATION_CONTINUITY,
        FinalEditorialCategory.VISUAL_CONTRADICTION,
        FinalEditorialCategory.NARRATION_VISUAL_AGREEMENT,
        FinalEditorialCategory.CAPTION_NARRATION_AGREEMENT,
        FinalEditorialCategory.ENDING_COMPLETENESS,
        FinalEditorialCategory.SCRIPT_CONTRADICTION,
        FinalEditorialCategory.SOURCE_CONTRADICTION,
    }
)

#: Where a confirmed finding in each category is routed. T22 never executes the
#: route; the parent workflow or an authorized user action does.
REMEDIATION_ROUTING: dict[FinalEditorialCategory, FinalRemediationTarget] = {
    FinalEditorialCategory.STORY_BEAT_COVERAGE: FinalRemediationTarget.CORRECT_SCRIPT_UPSTREAM,
    FinalEditorialCategory.NARRATIVE_STRUCTURE: FinalRemediationTarget.CORRECT_SCRIPT_UPSTREAM,
    FinalEditorialCategory.SCENE_COMPLETENESS: FinalRemediationTarget.RERENDER_T17,
    FinalEditorialCategory.CHARACTER_IDENTITY_CONTINUITY: (
        FinalRemediationTarget.CORRECT_REFERENCE_T19
    ),
    FinalEditorialCategory.CHARACTER_STATE_CONTINUITY: FinalRemediationTarget.REPAIR_SHOT_T21,
    FinalEditorialCategory.LOCATION_CONTINUITY: FinalRemediationTarget.CORRECT_REFERENCE_T19,
    FinalEditorialCategory.PROP_AND_WARDROBE_CONTINUITY: FinalRemediationTarget.REPAIR_SHOT_T21,
    FinalEditorialCategory.VISUAL_CONTRADICTION: FinalRemediationTarget.REPAIR_SHOT_T21,
    FinalEditorialCategory.SHOT_TO_SHOT_CONTINUITY: FinalRemediationTarget.REGENERATE_SHOT_T16,
    FinalEditorialCategory.TRANSITION_COHERENCE: FinalRemediationTarget.RERENDER_T17,
    FinalEditorialCategory.NARRATION_VISUAL_AGREEMENT: FinalRemediationTarget.REGENERATE_SHOT_T16,
    FinalEditorialCategory.CAPTION_NARRATION_AGREEMENT: (
        FinalRemediationTarget.REBUILD_CAPTIONS_T17
    ),
    FinalEditorialCategory.COMPREHENSIBILITY: FinalRemediationTarget.HUMAN_EDITORIAL_REVIEW,
    FinalEditorialCategory.SETUP_AND_PAYOFF: FinalRemediationTarget.CORRECT_SCRIPT_UPSTREAM,
    FinalEditorialCategory.NARRATIVE_JUMP: FinalRemediationTarget.CORRECT_SCRIPT_UPSTREAM,
    FinalEditorialCategory.REPETITION: FinalRemediationTarget.CORRECT_SCRIPT_UPSTREAM,
    FinalEditorialCategory.PACING: FinalRemediationTarget.RERENDER_T17,
    FinalEditorialCategory.DEAD_AIR: FinalRemediationTarget.REMIX_AUDIO_T17,
    FinalEditorialCategory.ENDING_COMPLETENESS: FinalRemediationTarget.RERENDER_T17,
    FinalEditorialCategory.SCRIPT_CONTRADICTION: FinalRemediationTarget.CORRECT_SCRIPT_UPSTREAM,
    FinalEditorialCategory.SOURCE_CONTRADICTION: FinalRemediationTarget.CORRECT_SCRIPT_UPSTREAM,
}


def canonical_hash(value: Any) -> str:
    """Stable SHA-256 over canonically serialized identity material."""
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def configuration_hash(configuration: FinalQAConfiguration) -> str:
    """Hash every configured threshold, including the version strings."""
    return canonical_hash(configuration.model_dump(mode="json"))


def rubric_material() -> dict[str, Any]:
    """The rubric fragment of the final-QA identity."""
    return {
        "pipeline_version": FINAL_QA_PIPELINE_VERSION,
        "gate_version": GATE_VERSION,
        "sampling_version": SAMPLING_VERSION,
        "dimensions": [dimension.value for dimension in EDITORIAL_DIMENSIONS],
        "blocking_categories": sorted(item.value for item in BLOCKING_CATEGORIES),
        "routing": {
            category.value: target.value
            for category, target in sorted(
                REMEDIATION_ROUTING.items(), key=lambda item: item[0].value
            )
        },
    }
