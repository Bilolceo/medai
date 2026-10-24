"""instagram posts behind comments

The assistant answered a comment knowing only the post's caption. The post
is now kept: caption, cover, link, and for a video the doctor's words,
transcribed once. Only posts that were commented on are stored.

Revision ID: c9d4e7f21a63
Revises: b7e2c4f19a30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c9d4e7f21a63"
down_revision: str | Sequence[str] | None = "b7e2c4f19a30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "instagram_posts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("media_id", sa.String(length=64), nullable=False),
        sa.Column("media_type", sa.String(length=32), nullable=True),
        sa.Column("media_product_type", sa.String(length=32), nullable=True),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("permalink", sa.Text(), nullable=True),
        sa.Column("thumbnail_url", sa.Text(), nullable=True),
        sa.Column("posted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("transcript", sa.Text(), nullable=True),
        sa.Column(
            "transcript_status", sa.String(length=16), server_default="pending", nullable=False
        ),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "media_id", name="uq_instagram_posts_tenant_media"),
    )
    op.create_index("ix_instagram_posts_tenant_id", "instagram_posts", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_instagram_posts_tenant_id", table_name="instagram_posts")
    op.drop_table("instagram_posts")
