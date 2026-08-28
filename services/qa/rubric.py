"""Versioned T20 rubric, thresholds, warning limits and repair-code taxonomy.

Everything a QA identity binds that is not an input asset lives here, so a
threshold change is always a version change and never a silent behavioural
drift. The pipeline imports these constants; it never inlines a weight, a
threshold or a model name.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Final

from vidgen.contracts.visual_qa import (
    VisualQADimension,
    VisualQARepairCode,
    VisualQARoutingRecommendation,
    VisualQARubric,
    VisualQARubricDimension,
    VisualQAThresholds,
)

RUBRIC_VERSION: Final = "visual-qa-rubric/1.0"
THRESHOLD_VERSION: Final = "visual-qa-thresholds/1.0"
SAMPLING_VERSION: Final = "visual-qa-sampler/1.0"
DETERMINISTIC_CHECK_VERSION: Final = "visual-qa-deterministic/1.0"
ADJUDICATION_POLICY_VERSION: Final = "visual-qa-adjudication/1.0"
PROMPT_VERSION: Final = "visual-qa-prompt/1.0"

#: The authoritative weights from the technical design. The total is exactly 100.
RUBRIC: Final = VisualQARubric(
    rubric_version=RUBRIC_VERSION,
    dimensions=[
        VisualQARubricDimension(dimension=VisualQADimension.CHARACTER_IDENTITY, weight=25),
        VisualQARubricDimension(dimension=VisualQADimension.CHARACTER_COUNT, weight=10),
        VisualQARubricDimension(dimension=VisualQADimension.LOCATION, weight=10),
        VisualQARubricDimension(dimension=VisualQADimension.WARDROBE_AND_STATE, weight=10),
        VisualQARubricDimension(dimension=VisualQADimension.ACTION_AND_MOTION, weight=15),
        VisualQARubricDimension(dimension=VisualQADimension.COMPOSITION, weight=10),
        VisualQARubricDimension(dimension=VisualQADimension.ANATOMY_AND_ARTIFACTS, weight=10),
        VisualQARubricDimension(dimension=VisualQADimension.CONTINUITY_AND_STYLE, weight=10),
    ],
)

THRESHOLDS: Final = VisualQAThresholds(threshold_version=THRESHOLD_VERSION)


@dataclass(frozen=True, slots=True)
class DeterministicThresholds:
    """Configurable, versioned deterministic warning and hard-failure limits."""

    version: str = DETERMINISTIC_CHECK_VERSION
    #: More than two black frames is a warning; an entirely black clip is a hard failure.
    max_black_frames: int = 2
    black_luma_ceiling: float = 8.0
    black_video_ratio: float = 0.98
    #: Freeze beyond this ratio warns unless the storyboard explicitly expects stillness.
    freeze_ratio_warning: float = 0.35
    duplicate_frame_ratio_warning: float = 0.50
    #: Mean absolute inter-frame luma delta above this reads as flicker.
    flicker_delta_warning: float = 26.0
    #: Unintended readable text at or above this detector confidence is a hard failure.
    ocr_confidence_warning: float = 0.80
    #: Face-track continuity below this warns and can trigger adjudication.
    face_track_continuity_floor: float = 0.75
    #: Normalized perceptual style distance from the approved reference.
    style_distance_warning: float = 0.35
    #: A duration error over 200 ms is a hard failure; drift approaching it warns.
    duration_hard_failure_us: int = 200_000
    duration_warning_us: int = 150_000
    brightness_floor: float = 12.0
    brightness_ceiling: float = 243.0
    #: A video QA run without this much located evidence cannot be adjudicated safely.
    minimum_sample_count: int = 3


DETERMINISTIC_THRESHOLDS: Final = DeterministicThresholds()


@dataclass(frozen=True, slots=True)
class SamplingConfiguration:
    """Deterministic sampling configuration bound into the QA identity."""

    version: str = SAMPLING_VERSION
    coverage_sample_count: int = 5
    action_window_samples: int = 3
    max_samples: int = 32
    contact_sheet_columns: int = 4
    #: Minimum spacing enforced when deduplicating clamped timestamps.
    minimum_spacing_us: int = 1_000
    #: How far inside the measured duration the final decodable frame is taken.
    final_frame_backoff_us: int = 20_000

    def material(self) -> dict[str, int | str]:
        return {
            "version": self.version,
            "coverage_sample_count": self.coverage_sample_count,
            "action_window_samples": self.action_window_samples,
            "max_samples": self.max_samples,
            "contact_sheet_columns": self.contact_sheet_columns,
            "minimum_spacing_us": self.minimum_spacing_us,
            "final_frame_backoff_us": self.final_frame_backoff_us,
        }


SAMPLING_CONFIGURATION: Final = SamplingConfiguration()


@dataclass(frozen=True, slots=True)
class RepairCodeDefinition:
    """One entry of the bounded taxonomy T21 consumes."""

    code: VisualQARepairCode
    category: str
    severity: str
    repair_family: VisualQARoutingRecommendation
    evidence_requirement: str
    retryability: str
    hard_failure: bool


def _entry(
    code: VisualQARepairCode,
    category: str,
    severity: str,
    family: VisualQARoutingRecommendation,
    evidence: str,
    retryability: str,
    hard_failure: bool,
) -> tuple[VisualQARepairCode, RepairCodeDefinition]:
    return code, RepairCodeDefinition(
        code=code,
        category=category,
        severity=severity,
        repair_family=family,
        evidence_requirement=evidence,
        retryability=retryability,
        hard_failure=hard_failure,
    )


_TARGETED = VisualQARoutingRecommendation.TARGETED_REPAIR
_SIMPLIFY = VisualQARoutingRecommendation.PROMPT_SIMPLIFICATION
_SEED = VisualQARoutingRecommendation.NEW_SEED
_SPLIT = VisualQARoutingRecommendation.COMPOSITION_SPLIT
_HUMAN = VisualQARoutingRecommendation.HUMAN_REVIEW

#: Every repair code maps to a category, severity, T21 repair family, evidence
#: requirement, retryability classification, and whether it is a hard failure.
REPAIR_CODES: Final[dict[VisualQARepairCode, RepairCodeDefinition]] = dict(
    (
        _entry(
            VisualQARepairCode.WRONG_CHARACTER_IDENTITY,
            "identity",
            "blocking",
            _SEED,
            "frame_and_reference",
            "creative_retry",
            True,
        ),
        _entry(
            VisualQARepairCode.MISSING_PRIMARY_CHARACTER,
            "identity",
            "blocking",
            _SEED,
            "frame",
            "creative_retry",
            True,
        ),
        _entry(
            VisualQARepairCode.EXTRA_CHARACTER,
            "count",
            "blocking",
            _SIMPLIFY,
            "frame",
            "creative_retry",
            False,
        ),
        _entry(
            VisualQARepairCode.WRONG_CHARACTER_COUNT,
            "count",
            "blocking",
            _SIMPLIFY,
            "frame",
            "creative_retry",
            True,
        ),
        _entry(
            VisualQARepairCode.WRONG_WARDROBE,
            "state",
            "blocking",
            _TARGETED,
            "frame_and_reference",
            "creative_retry",
            False,
        ),
        _entry(
            VisualQARepairCode.WRONG_CHARACTER_STATE,
            "state",
            "blocking",
            _TARGETED,
            "frame_and_reference",
            "creative_retry",
            False,
        ),
        _entry(
            VisualQARepairCode.WRONG_LOCATION,
            "location",
            "blocking",
            _SEED,
            "frame_and_reference",
            "creative_retry",
            True,
        ),
        _entry(
            VisualQARepairCode.WRONG_LOCATION_STATE,
            "location",
            "major",
            _TARGETED,
            "frame_and_reference",
            "creative_retry",
            False,
        ),
        _entry(
            VisualQARepairCode.MISSING_REQUIRED_PROP,
            "props",
            "blocking",
            _TARGETED,
            "frame",
            "creative_retry",
            True,
        ),
        _entry(
            VisualQARepairCode.WRONG_PROP_OWNERSHIP,
            "props",
            "major",
            _TARGETED,
            "frame",
            "creative_retry",
            False,
        ),
        _entry(
            VisualQARepairCode.MISSING_MANDATORY_ACTION,
            "action",
            "blocking",
            _TARGETED,
            "frame",
            "creative_retry",
            True,
        ),
        _entry(
            VisualQARepairCode.WRONG_ACTION,
            "action",
            "major",
            _TARGETED,
            "frame",
            "creative_retry",
            False,
        ),
        _entry(
            VisualQARepairCode.INSUFFICIENT_MOTION,
            "motion",
            "major",
            _TARGETED,
            "frame",
            "creative_retry",
            False,
        ),
        _entry(
            VisualQARepairCode.EXCESSIVE_MOTION,
            "motion",
            "major",
            _TARGETED,
            "frame",
            "creative_retry",
            False,
        ),
        _entry(
            VisualQARepairCode.CAMERA_PLAN_MISMATCH,
            "composition",
            "major",
            _TARGETED,
            "frame",
            "creative_retry",
            False,
        ),
        _entry(
            VisualQARepairCode.COMPOSITION_MISMATCH,
            "composition",
            "major",
            _SPLIT,
            "frame",
            "creative_retry",
            False,
        ),
        _entry(
            VisualQARepairCode.SCREEN_DIRECTION_CONTRADICTION,
            "continuity",
            "blocking",
            _TARGETED,
            "frame",
            "creative_retry",
            True,
        ),
        _entry(
            VisualQARepairCode.FACE_BREAKAGE,
            "anatomy",
            "blocking",
            _SEED,
            "frame",
            "creative_retry",
            True,
        ),
        _entry(
            VisualQARepairCode.ANATOMY_BREAKAGE,
            "anatomy",
            "blocking",
            _SEED,
            "frame",
            "creative_retry",
            True,
        ),
        _entry(
            VisualQARepairCode.UNINTENDED_TEXT,
            "artifacts",
            "blocking",
            _TARGETED,
            "frame",
            "creative_retry",
            True,
        ),
        _entry(
            VisualQARepairCode.STYLE_DRIFT,
            "style",
            "major",
            _TARGETED,
            "frame_and_reference",
            "creative_retry",
            False,
        ),
        _entry(
            VisualQARepairCode.CONTINUITY_BREAK,
            "continuity",
            "blocking",
            _TARGETED,
            "frame",
            "creative_retry",
            True,
        ),
        _entry(
            VisualQARepairCode.BLACK_VIDEO,
            "technical",
            "blocking",
            _SEED,
            "whole_file",
            "creative_retry",
            True,
        ),
        _entry(
            VisualQARepairCode.EXCESSIVE_FREEZE,
            "technical",
            "major",
            _TARGETED,
            "frame",
            "creative_retry",
            False,
        ),
        _entry(
            VisualQARepairCode.EXCESSIVE_FLICKER,
            "technical",
            "major",
            _TARGETED,
            "frame",
            "creative_retry",
            False,
        ),
        _entry(
            VisualQARepairCode.DURATION_MISMATCH,
            "technical",
            "blocking",
            _TARGETED,
            "whole_file",
            "deterministic",
            True,
        ),
        _entry(
            VisualQARepairCode.DECODE_FAILURE,
            "technical",
            "blocking",
            _SEED,
            "whole_file",
            "deterministic",
            True,
        ),
        _entry(
            VisualQARepairCode.PROMPT_TOO_COMPLEX,
            "prompt",
            "major",
            _SIMPLIFY,
            "frame",
            "creative_retry",
            False,
        ),
        _entry(
            VisualQARepairCode.TOO_MANY_CHARACTERS,
            "prompt",
            "major",
            _SPLIT,
            "frame",
            "creative_retry",
            False,
        ),
        _entry(
            VisualQARepairCode.TOO_MANY_REFERENCES,
            "prompt",
            "major",
            _SIMPLIFY,
            "none",
            "deterministic",
            False,
        ),
        _entry(
            VisualQARepairCode.AMBIGUOUS_VISUAL_EVIDENCE,
            "evidence",
            "review",
            _HUMAN,
            "frame",
            "human_review",
            False,
        ),
        _entry(
            VisualQARepairCode.HUMAN_REVIEW_REQUIRED,
            "evidence",
            "review",
            _HUMAN,
            "none",
            "human_review",
            False,
        ),
    )
)

#: Codes that always force ``FAIL`` regardless of the recomputed numeric score.
HARD_FAILURE_CODES: Final[frozenset[VisualQARepairCode]] = frozenset(
    code for code, definition in REPAIR_CODES.items() if definition.hard_failure
)

#: Which rubric dimension owns each repair code, for dimension-level attribution.
REPAIR_CODE_DIMENSIONS: Final[dict[VisualQARepairCode, VisualQADimension]] = {
    VisualQARepairCode.WRONG_CHARACTER_IDENTITY: VisualQADimension.CHARACTER_IDENTITY,
    VisualQARepairCode.MISSING_PRIMARY_CHARACTER: VisualQADimension.CHARACTER_IDENTITY,
    VisualQARepairCode.EXTRA_CHARACTER: VisualQADimension.CHARACTER_COUNT,
    VisualQARepairCode.WRONG_CHARACTER_COUNT: VisualQADimension.CHARACTER_COUNT,
    VisualQARepairCode.TOO_MANY_CHARACTERS: VisualQADimension.CHARACTER_COUNT,
    VisualQARepairCode.WRONG_WARDROBE: VisualQADimension.WARDROBE_AND_STATE,
    VisualQARepairCode.WRONG_CHARACTER_STATE: VisualQADimension.WARDROBE_AND_STATE,
    VisualQARepairCode.MISSING_REQUIRED_PROP: VisualQADimension.WARDROBE_AND_STATE,
    VisualQARepairCode.WRONG_PROP_OWNERSHIP: VisualQADimension.WARDROBE_AND_STATE,
    VisualQARepairCode.WRONG_LOCATION: VisualQADimension.LOCATION,
    VisualQARepairCode.WRONG_LOCATION_STATE: VisualQADimension.LOCATION,
    VisualQARepairCode.MISSING_MANDATORY_ACTION: VisualQADimension.ACTION_AND_MOTION,
    VisualQARepairCode.WRONG_ACTION: VisualQADimension.ACTION_AND_MOTION,
    VisualQARepairCode.INSUFFICIENT_MOTION: VisualQADimension.ACTION_AND_MOTION,
    VisualQARepairCode.EXCESSIVE_MOTION: VisualQADimension.ACTION_AND_MOTION,
    VisualQARepairCode.EXCESSIVE_FREEZE: VisualQADimension.ACTION_AND_MOTION,
    VisualQARepairCode.CAMERA_PLAN_MISMATCH: VisualQADimension.COMPOSITION,
    VisualQARepairCode.COMPOSITION_MISMATCH: VisualQADimension.COMPOSITION,
    VisualQARepairCode.FACE_BREAKAGE: VisualQADimension.ANATOMY_AND_ARTIFACTS,
    VisualQARepairCode.ANATOMY_BREAKAGE: VisualQADimension.ANATOMY_AND_ARTIFACTS,
    VisualQARepairCode.UNINTENDED_TEXT: VisualQADimension.ANATOMY_AND_ARTIFACTS,
    VisualQARepairCode.EXCESSIVE_FLICKER: VisualQADimension.ANATOMY_AND_ARTIFACTS,
    VisualQARepairCode.BLACK_VIDEO: VisualQADimension.ANATOMY_AND_ARTIFACTS,
    VisualQARepairCode.DECODE_FAILURE: VisualQADimension.ANATOMY_AND_ARTIFACTS,
    VisualQARepairCode.DURATION_MISMATCH: VisualQADimension.ACTION_AND_MOTION,
    VisualQARepairCode.SCREEN_DIRECTION_CONTRADICTION: VisualQADimension.CONTINUITY_AND_STYLE,
    VisualQARepairCode.CONTINUITY_BREAK: VisualQADimension.CONTINUITY_AND_STYLE,
    VisualQARepairCode.STYLE_DRIFT: VisualQADimension.CONTINUITY_AND_STYLE,
    VisualQARepairCode.PROMPT_TOO_COMPLEX: VisualQADimension.COMPOSITION,
    VisualQARepairCode.TOO_MANY_REFERENCES: VisualQADimension.COMPOSITION,
    VisualQARepairCode.AMBIGUOUS_VISUAL_EVIDENCE: VisualQADimension.CHARACTER_IDENTITY,
    VisualQARepairCode.HUMAN_REVIEW_REQUIRED: VisualQADimension.CHARACTER_IDENTITY,
}

#: Dimensions that never apply to a still keyframe. Their weight is redistributed.
KEYFRAME_INAPPLICABLE_DIMENSIONS: Final[frozenset[VisualQADimension]] = frozenset()


def rubric_material() -> dict[str, object]:
    """Return exactly the rubric and threshold fields bound into a QA identity."""
    return {
        "rubric_version": RUBRIC.rubric_version,
        "weights": {item.dimension.value: item.weight for item in RUBRIC.dimensions},
        "threshold_version": THRESHOLDS.threshold_version,
        "thresholds": THRESHOLDS.model_dump(mode="json"),
        "deterministic": asdict(DETERMINISTIC_THRESHOLDS),
        "sampling": SAMPLING_CONFIGURATION.material(),
        "adjudication_policy_version": ADJUDICATION_POLICY_VERSION,
        "prompt_version": PROMPT_VERSION,
    }


#: The repair code a dimension contributes when it scores badly without a
#: specific finding of its own. Keeps "every non-pass result carries a repair
#: code" true without inventing a defect the evidence does not show.
DIMENSION_DEFAULT_REPAIR: Final[dict[VisualQADimension, VisualQARepairCode]] = {
    VisualQADimension.CHARACTER_IDENTITY: VisualQARepairCode.WRONG_CHARACTER_IDENTITY,
    VisualQADimension.CHARACTER_COUNT: VisualQARepairCode.WRONG_CHARACTER_COUNT,
    VisualQADimension.LOCATION: VisualQARepairCode.WRONG_LOCATION_STATE,
    VisualQADimension.WARDROBE_AND_STATE: VisualQARepairCode.WRONG_CHARACTER_STATE,
    VisualQADimension.ACTION_AND_MOTION: VisualQARepairCode.WRONG_ACTION,
    VisualQADimension.COMPOSITION: VisualQARepairCode.COMPOSITION_MISMATCH,
    VisualQADimension.ANATOMY_AND_ARTIFACTS: VisualQARepairCode.ANATOMY_BREAKAGE,
    VisualQADimension.CONTINUITY_AND_STYLE: VisualQARepairCode.CONTINUITY_BREAK,
}

#: Which structural repair family a badly scoring dimension implies when the
#: total is below the targeted-repair floor and a cosmetic fix cannot help.
DIMENSION_STRUCTURAL_ROUTING: Final[dict[VisualQADimension, VisualQARoutingRecommendation]] = {
    VisualQADimension.CHARACTER_IDENTITY: VisualQARoutingRecommendation.NEW_SEED,
    VisualQADimension.ANATOMY_AND_ARTIFACTS: VisualQARoutingRecommendation.NEW_SEED,
    VisualQADimension.CHARACTER_COUNT: VisualQARoutingRecommendation.COMPOSITION_SPLIT,
    VisualQADimension.COMPOSITION: VisualQARoutingRecommendation.COMPOSITION_SPLIT,
    VisualQADimension.LOCATION: VisualQARoutingRecommendation.PROMPT_SIMPLIFICATION,
    VisualQADimension.WARDROBE_AND_STATE: VisualQARoutingRecommendation.PROMPT_SIMPLIFICATION,
    VisualQADimension.ACTION_AND_MOTION: VisualQARoutingRecommendation.PROMPT_SIMPLIFICATION,
    VisualQADimension.CONTINUITY_AND_STYLE: VisualQARoutingRecommendation.PROMPT_SIMPLIFICATION,
}
