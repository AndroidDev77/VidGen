"""T25 YouTube connections, OAuth states, publications, upload sessions and assets.

Purely additive: six new tables and nothing touched that T01-T24 created, so
existing projects, assets, renders, approvals, QA results, completion gates,
provider attempts and cost records are preserved untouched. The tables are
created from the same ORM metadata the application uses, so the migration and
the models cannot drift.

Two of the invariants this migration installs are worth calling out, because
they are the difference between a restartable publisher and a duplicate-video
generator:

* ``publication_runs`` carries a transition CHECK generated from the same
  transition table the application enforces, plus "a state after upload names
  its video", "a published video finished processing", and "a non-private state
  required an explicit visibility decision";
* ``youtube_upload_sessions`` has a partial unique index over the active
  session per publication, and requires a completed session to have confirmed
  every byte and to name the video it produced.

The downgrade refuses to run once publication provenance exists. These rows are
the only local record of which YouTube video a render became; dropping them
would leave a published video with no lineage and would let a retry upload the
same render a second time.

Revision ID: 0019_youtube_publication
Revises: 0018_final_editorial_qa
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import inspect

from vidgen.db.publication_models import (
    PublicationAsset,
    PublicationRun,
    YouTubeConnection,
    YouTubeConnectionSecret,
    YouTubeOAuthState,
    YouTubeUploadSession,
)

revision: str = "0019_youtube_publication"
down_revision: str | None = "0018_final_editorial_qa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Creation order: connections first, then the rows that reference them.
_TABLES = (
    YouTubeConnection.__table__,
    YouTubeConnectionSecret.__table__,
    YouTubeOAuthState.__table__,
    PublicationRun.__table__,
    YouTubeUploadSession.__table__,
    PublicationAsset.__table__,
)


def upgrade() -> None:
    bind = op.get_bind()
    for table in _TABLES:
        table.create(bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    names = set(inspect(bind).get_table_names())
    populated = [
        table.name
        for table in _TABLES
        if table.name in names and bind.execute(table.select().limit(1)).first() is not None
    ]
    if populated:
        raise RuntimeError(
            "unsafe T25 downgrade: YouTube publication provenance would be destroyed. These "
            "rows are the only local record of which YouTube video a render became, and of "
            "the resumable sessions that produced it; dropping them would orphan a published "
            "video and let a retry upload the same render again. Export or delete rows from "
            + ", ".join(populated)
            + " before downgrading."
        )
    for table in reversed(_TABLES):
        table.drop(bind, checkfirst=True)
