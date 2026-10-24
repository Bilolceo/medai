import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, Integer, LargeBinary, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, deferred, mapped_column

from app.models.base import Base


class PatientMediaStatus(StrEnum):
    NEW = "new"  # yangi -- nobody has looked yet
    REVIEWED = "reviewed"  # ko'rildi


class MediaKind(StrEnum):
    """What a patient sent. Only photos have their own screen ("Rasmlar");
    every kind is shown where it was sent, in the conversation."""

    IMAGE = "image"
    AUDIO = "audio"  # a voice message
    VIDEO = "video"  # a video the patient recorded or picked
    SHARE = "share"  # a reel or a post shared from Instagram
    STORY = "story"  # a story the patient mentioned the clinic in
    STICKER = "sticker"
    FILE = "file"


class PatientMedia(Base):
    """Something a patient sent -- a photo of an analysis result, a voice
    message, a video, a reel they shared. (The table predates everything but
    photos; `kind` says which.)

    Kept apart from the transcript and the appointment book on purpose: a
    patient who sends a picture is waiting for a person to look at it, and
    the dashboard gives those patients their own screen rather than burying
    them among bookings.

    The image bytes are stored, not just the link. Instagram's attachment URLs
    are signed and expire within days, and a result the doctor opens next
    week has to still be there.
    """

    __tablename__ = "patient_media"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    tenant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=True, index=True
    )
    # The platform's message id, so a redelivered webhook stores nothing twice.
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    # Deferred: a list of forty patients must not load forty images.
    content: Mapped[bytes | None] = deferred(mapped_column(LargeBinary, nullable=True))
    content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text(f"'{MediaKind.IMAGE}'")
    )
    # A shared reel's caption, as Instagram gives it.
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default=text(f"'{PatientMediaStatus.NEW}'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False, index=True
    )
