"""Let a human override a soft visual-QA ``FAIL``.

One constraint change: ``visual_qa_human_reviews.decision`` now also accepts
``force_approved``, the decision recorded when a person overrules a soft
``FAIL`` rather than settling an ambiguous ``REVIEW``. Nothing else moves - no
column is added, no row is rewritten, and every decision already recorded stays
valid under the wider constraint.

A hard failure remains unoverridable. That rule lives in the review service and
the gate, not in this constraint: the constraint only bounds the vocabulary of
what a decision may say.

The downgrade narrows the constraint back, and refuses while any overridden row
exists rather than dropping the fact that a person cleared a failing shot.

Revision ID: 0023_visual_qa_force_approval
Revises: 0022_generation_routing
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0023_visual_qa_force_approval"
down_revision: str | None = "0022_generation_routing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "visual_qa_human_reviews"
_CONSTRAINT = "visual_qa_human_review_decision"
_NARROW = "decision IN ('approved','rejected')"
_WIDE = "decision IN ('approved','rejected','force_approved')"


def _replace_constraint(condition: str) -> None:
    # ``batch_alter_table`` recreates the table on SQLite, where a CHECK
    # constraint cannot be altered in place, and issues a plain DROP/ADD on
    # PostgreSQL.
    with op.batch_alter_table(_TABLE, schema=None) as batch:
        batch.drop_constraint(_CONSTRAINT, type_="check")
        batch.create_check_constraint(_CONSTRAINT, sa.text(condition))


def upgrade() -> None:
    _replace_constraint(_WIDE)


def downgrade() -> None:
    bind = op.get_bind()
    overridden = bind.execute(
        sa.text(f"SELECT count(*) FROM {_TABLE} WHERE decision = 'force_approved'")
    ).scalar_one()
    if overridden:
        raise RuntimeError(
            f"{overridden} visual-QA human review(s) record a forced approval. "
            "Downgrading would narrow the constraint past rows that are the only "
            "record of a person clearing a failing shot."
        )
    _replace_constraint(_NARROW)
