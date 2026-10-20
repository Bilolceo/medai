"""A post on the clinic's Instagram, as far as answering its comments needs it.

A comment is written about a video, and until now the assistant answered it
knowing only the caption -- usually a title. The patient asking "narxi
qancha?" under the implant reel was asked which service they meant. So the
post is kept here: its caption, its cover, where it lives, and -- for a
video -- what the doctor says in it, transcribed once and kept.

Only posts somebody commented on are fetched; the clinic's whole feed is not
mirrored. A row is a cache of Instagram's own data, refreshed when a comment
arrives and the row is older than a day.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class TranscriptStatus(StrEnum):
    """Where the video's words are.

    PENDING  -- a video, not transcribed yet (queued, or the first comment
                has only just arrived).
    READY    -- transcript is in `transcript`.
    SKIPPED  -- not a video, or a video with nothing to hear.
    FAILED   -- tried and could not; tried again on the next comment.
    """

    PENDING = "pending"
    READY = "ready"
    SKIPPED = "skipped"
    FAILED = "failed"


class InstagramPost(Base):
    __tablename__ = "instagram_posts"
    __table_args__ = (
        UniqueConstraint("tenant_id", "media_id", name="uq_instagram_posts_tenant_media"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    # Instagram's id for the post: what a comment webhook names.
    media_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # "VIDEO", "IMAGE", "CAROUSEL_ALBUM"; and "REELS", "FEED" for the product.
    media_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    media_product_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    permalink: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Instagram's CDN link for the cover; it expires, so the dashboard reads
    # the cover through the API, which refreshes the link when it has to.
    thumbnail_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # What the doctor says in the video, in his own words and language.
    transcript: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript_status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=TranscriptStatus.PENDING.value
    )
    # One or two lines the model and the dashboard read instead of the
    # whole transcript: what the video is about.
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
