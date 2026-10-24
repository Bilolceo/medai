"""a shared reel keeps its id and link

A shared reel's webhook link is often an HTML page, not the video, and the
conversation showed an empty box. The reel's own id (reel_video_id /
ig_post_media_id) is now kept so the Graph API can be asked for the video
or its cover, and the instagram.com link it can always be opened at.

Revision ID: e6b1c3d9a4f2
Revises: d3a9f6c2b814
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6b1c3d9a4f2"
down_revision: str | Sequence[str] | None = "d3a9f6c2b814"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "patient_media", sa.Column("platform_media_id", sa.String(length=64), nullable=True)
    )
    op.add_column("patient_media", sa.Column("permalink", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("patient_media", "permalink")
    op.drop_column("patient_media", "platform_media_id")
