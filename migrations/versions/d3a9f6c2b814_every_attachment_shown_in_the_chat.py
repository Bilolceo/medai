"""every attachment shown in the chat

Voice messages, videos and shared reels were recorded as a label only
("🎤 Ovozli xabar") and nobody at the clinic could hear or see them; photos
had their own screen but were not in the conversation. Every attachment is
now kept in patient_media, with what kind it is.

Revision ID: d3a9f6c2b814
Revises: c9d4e7f21a63
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d3a9f6c2b814"
down_revision: str | Sequence[str] | None = "c9d4e7f21a63"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "patient_media",
        sa.Column("kind", sa.String(length=16), server_default=sa.text("'image'"), nullable=False),
    )
    op.add_column("patient_media", sa.Column("title", sa.Text(), nullable=True))
    op.create_index("ix_patient_media_conversation_id", "patient_media", ["conversation_id"])


def downgrade() -> None:
    op.drop_index("ix_patient_media_conversation_id", table_name="patient_media")
    op.drop_column("patient_media", "title")
    op.drop_column("patient_media", "kind")
