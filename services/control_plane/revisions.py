"""Turning a confirmed transcript or script edit into executable work.

The edit itself already does the right thing: it preserves the previous version,
computes the exact invalidation set, and requires the owner to confirm it. What
was missing was the second half - actually rebuilding what the edit invalidated.

This module is that half, and it is deliberately only a *plan*: it decides the
earliest stage a new generation run must start from, and which stages are reused
because nothing the edit touched can reach them. The run itself is started by the
control command the route creates from this plan.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from sqlalchemy.orm import Session

from services.review.invalidation import (
    script_invalidation_set,
    transcript_invalidation_set,
)
from vidgen.contracts.review import InvalidationSet
from vidgen.contracts.workflow import PROJECT_STAGE_ORDER

RevisionKind = Literal["transcript", "script"]

#: The earliest stage each revision kind must rebuild from.
#:
#: A transcript edit changes the material every generated stage was derived
#: from, so the rebuild starts at the episode analysis - the first stage that
#: reads the transcript. The source video, its processed media and the
#: transcript asset itself are unaffected and are reused.
#:
#: A script revision leaves the transcript and the episode analysis intact:
#: those are upstream of the script and nothing about them changed. The rebuild
#: starts at narration, which is also the first stage that would spend money on
#: the new script - which is why the revision must be approved before this plan
#: is turned into a command.
_ENTRY_STAGE: dict[RevisionKind, str] = {
    "transcript": "episode_analysis",
    "script": "narration",
}


@dataclass(frozen=True, slots=True)
class RevisionPlan:
    """What a confirmed revision will rebuild, and what it will reuse."""

    project_id: UUID
    kind: RevisionKind
    source_id: UUID
    entry_stage: str
    rebuilt_stages: tuple[str, ...]
    reused_stages: tuple[str, ...]
    invalidation: InvalidationSet

    @property
    def requires_confirmation(self) -> bool:
        return self.invalidation.requires_confirmation


def plan_revision(
    session: Session, *, project_id: UUID, kind: RevisionKind, source_id: UUID
) -> RevisionPlan:
    """Compute the exact rebuild a confirmed revision implies.

    The invalidation set is the existing one the edit endpoints already show, so
    the owner confirms precisely what this plan will execute - not an
    approximation of it.
    """
    invalidation = (
        transcript_invalidation_set(session, project_id)
        if kind == "transcript"
        else script_invalidation_set(session, project_id)
    )
    entry_stage = _ENTRY_STAGE[kind]
    index = PROJECT_STAGE_ORDER.index(entry_stage)
    return RevisionPlan(
        project_id=project_id,
        kind=kind,
        source_id=source_id,
        entry_stage=entry_stage,
        rebuilt_stages=PROJECT_STAGE_ORDER[index:],
        reused_stages=PROJECT_STAGE_ORDER[:index],
        invalidation=invalidation,
    )
