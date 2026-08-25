"""Machine and human QA contracts."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import Field

from vidgen.contracts.common import Score, StrictContract


class QACheck(StrictContract):
    name: Literal[
        "character_identity",
        "character_count",
        "location",
        "wardrobe",
        "artifact_free",
        "face_quality",
        "text_free",
        "action",
        "composition",
        "continuity",
        "style",
    ]
    score: Score
    evidence: str


class QAResult(StrictContract):
    schema_version: Literal["1.0"] = "1.0"
    qa_result_id: UUID
    project_id: UUID
    shot_id: UUID | None = None
    asset_id: UUID
    attempt: int = Field(ge=1)
    decision: Literal["pass", "fail", "human_review"]
    overall_score: Score
    checks: list[QACheck] = Field(min_length=1)
    failure_codes: list[str] = Field(default_factory=list)
    repair_instructions: list[str] = Field(default_factory=list)
    evaluator_provider: str
    evaluator_model: str
