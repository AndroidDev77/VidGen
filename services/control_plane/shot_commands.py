"""The replacement-shot identity every shot command shares.

A shot command that may have to start a *new* child - a regeneration, a retry
whose child has already closed, a T20 or T21 continuation, a T22 shot
remediation - needs a replacement identity that is reproducible from persisted
rows. If two creators disagreed about the sequence, two commands would mint the
same identity and silently adopt each other's child.

So the sequence lives here, is counted from every command type that can mint
one, and is stamped into the command's metadata at creation. The dispatcher
reads it back rather than recounting, which is what makes a retried dispatch
resolve to the same replacement child instead of paying for another one.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from vidgen.contracts.control_commands import ControlCommandType
from vidgen.db.control_command_models import ControlCommandRecord

#: Every command type whose dispatch can end in a replacement child workflow.
#: Counting only regenerations would let a retry and a later regeneration mint
#: the same identity.
IDENTITY_MINTING_COMMANDS: tuple[ControlCommandType, ...] = (
    ControlCommandType.SHOT_REGENERATE,
    ControlCommandType.SHOT_RETRY,
    ControlCommandType.SHOT_REVIEW_CONTINUE,
)
#: A cancelled or superseded command never reached a worker, so it never minted
#: an identity and must not consume a sequence.
_SPENT_STATUSES = ("cancelled", "superseded")

#: The metadata key the sequence is stamped under.
SEQUENCE_KEY = "regeneration_sequence"


def next_regeneration_sequence(
    session: Session,
    project_id: UUID,
    shot_id: UUID,
    *,
    exclude_command_id: UUID | None = None,
) -> int:
    """The sequence the next replacement child for this shot would take.

    ``exclude_command_id`` is for the dispatcher, which mints a sequence for a
    command that is already persisted; counting that row would skip a sequence
    on every command that did not stamp one at submission time. A route calling
    this before its command exists leaves it unset.
    """
    statement = select(ControlCommandRecord.id).where(
        ControlCommandRecord.project_id == project_id,
        ControlCommandRecord.command_type.in_(
            [command.value for command in IDENTITY_MINTING_COMMANDS]
        ),
        ControlCommandRecord.target_id == shot_id,
        ControlCommandRecord.status.notin_(_SPENT_STATUSES),
    )
    if exclude_command_id is not None:
        statement = statement.where(ControlCommandRecord.id != exclude_command_id)
    return len(session.scalars(statement).all()) + 1
