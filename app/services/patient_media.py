"""Photos patients send: keep them, acknowledge them, tell the doctor.

A patient who sends a picture is usually sending a result -- an analysis, an
ultrasound report, a prescription -- and is waiting for a person to look at
it. Three things happen, in this order, and each is allowed to fail without
stopping the next:

1. The image is downloaded and stored. Instagram's attachment links are
   signed and expire, so a link alone would be a blank square by next week.
2. The patient is told the photo arrived and the doctor answers here, once
   per burst: three photos sent in a row are one acknowledgement, not three.
3. The doctor's Telegram gets the photo and who sent it.

The assistant never answers a photo itself. A medical image is the last thing
it should offer an opinion on (rule 3 of the answer prompt), and a reply that
pretended to have looked would be worse than none.
"""

import logging
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.instagram.client import GRAPH_API_BASE_URL
from app.core.config import get_settings
from app.core.encryption import DecryptionError, decrypt
from app.models.message import MessageSender
from app.models.patient_media import PatientMedia
from app.models.user import User
from app.repositories.channel import ChannelRepository
from app.repositories.message import MessageRepository
from app.services import message_labels
from app.services.conversation import record_outbound_message, reply_context_for
from app.services.delivery import send_reply
from app.services.language import reply_script

logger = logging.getLogger(__name__)

# Larger than any photo a phone sends, small enough that a stray video link
# cannot fill the database.
MAX_IMAGE_BYTES = 15 * 1024 * 1024

# One acknowledgement per burst of photos from the same conversation.
ACK_WINDOW_SECONDS = 120

# Said once per burst. Not "hozir ko'rib beramiz": nobody looks at once, and
# the assistant cannot come back to say so -- the photo waits on the doctor,
# who answers in this conversation.
ACKNOWLEDGEMENTS = {
    "uz-latn": "Rasmingiz keldi. Doktor ko'rib, shu yerda javob beradi.",
    "uz-cyrl": "Расмингиз келди. Доктор кўриб, шу ерда жавоб беради.",
    "ru": "Фото получили. Врач посмотрит и ответит здесь.",
}


# Instagram's CDN answers a bare HTTP client from a data-centre address with a
# 404 while serving the same link to a browser -- the photo downloaded from a
# laptop and failed from the host. So the request looks like the browser it
# stands in for, and when the link still fails the photo is asked for through
# the Graph API with the account's own token, which returns a fresh link.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Referer": "https://www.instagram.com/",
}


# Raster formats only. "image/*" also admits image/svg+xml, which can carry
# script and is served from the dashboard's own origin.
SAFE_IMAGE_TYPES = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/gif", "image/heic", "image/heif"}
)


async def download(url: str, http: httpx.AsyncClient | None = None) -> tuple[bytes, str] | None:
    """The image and its content type, or None if it cannot be had."""
    client = http or httpx.AsyncClient(timeout=30, follow_redirects=True, headers=_BROWSER_HEADERS)
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        logger.warning("patient_media_download_failed", extra={"error": type(exc).__name__})
        return None
    finally:
        if http is None:
            await client.aclose()
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    if (
        response.is_error
        or len(response.content) > MAX_IMAGE_BYTES
        or content_type not in SAFE_IMAGE_TYPES
    ):
        logger.warning(
            "patient_media_download_refused",
            extra={
                "status_code": response.status_code,
                "size": len(response.content),
                "content_type": content_type,
            },
        )
        return None
    return response.content, content_type


async def fresh_urls(access_token: str, message_id: str) -> list[str]:
    """The message's image links as the Graph API gives them now."""
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{GRAPH_API_BASE_URL}/{message_id}",
                params={"fields": "attachments", "access_token": access_token},
            )
    except httpx.HTTPError as exc:
        logger.warning("patient_media_graph_unreachable", extra={"error": type(exc).__name__})
        return []
    if response.is_error:
        logger.warning("patient_media_graph_failed", extra={"status_code": response.status_code})
        return []
    urls: list[str] = []
    for item in (response.json().get("attachments") or {}).get("data", []):
        image = item.get("image_data") or {}
        url = image.get("url") or image.get("preview_url") or item.get("file_url")
        if url:
            urls.append(url)
    return urls


async def fetch(
    url: str, *, access_token: str | None, message_id: str | None, index: int
) -> tuple[bytes, str] | None:
    """The photo, by its webhook link first and through the Graph API second."""
    fetched = await download(url)
    if fetched is not None or not (access_token and message_id):
        return fetched
    urls = await fresh_urls(access_token, message_id)
    if index < len(urls):
        fetched = await download(urls[index])
        if fetched is not None:
            logger.info("patient_media_fetched_through_graph")
    return fetched


async def channel_token(session: AsyncSession, channel_id: uuid.UUID) -> str | None:
    channel = await ChannelRepository(session).get(channel_id)
    if channel is None:
        return None
    try:
        return decrypt(channel.credentials)
    except DecryptionError:
        return None


def message_id_of(media: PatientMedia) -> tuple[str | None, int]:
    """The platform message id and attachment index stored as "mid:index"."""
    if not media.external_id or ":" not in media.external_id:
        return media.external_id, 0
    mid, _, index = media.external_id.rpartition(":")
    return mid, int(index) if index.isdigit() else 0


async def repair(session: AsyncSession, media: PatientMedia, channel_id: uuid.UUID) -> bool:
    """Try again to fetch a photo that could not be downloaded when it came."""
    mid, index = message_id_of(media)
    fetched = await fetch(
        media.source_url,
        access_token=await channel_token(session, channel_id),
        message_id=mid,
        index=index,
    )
    if fetched is None:
        return False
    media.content, media.content_type, media.size_bytes = fetched[0], fetched[1], len(fetched[0])
    await session.commit()
    return True


async def patient_script(session: AsyncSession, conversation_id: uuid.UUID) -> str:
    """Which alphabet to acknowledge in: the one the patient last wrote in."""
    for message in reversed(await MessageRepository(session).list_recent(conversation_id, 20)):
        if message.sender == MessageSender.PATIENT and not message_labels.is_label(message.content):
            return reply_script(message.content)
    return "uz-latn"


async def store(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    external_id: str | None,
    urls: list[str],
    channel_id: uuid.UUID | None = None,
) -> list[PatientMedia]:
    stored: list[PatientMedia] = []
    token = await channel_token(session, channel_id) if channel_id else None
    for index, url in enumerate(urls):
        fetched = await fetch(url, access_token=token, message_id=external_id, index=index)
        media = PatientMedia(
            tenant_id=tenant_id,
            user_id=user_id,
            conversation_id=conversation_id,
            external_id=f"{external_id}:{index}" if external_id else None,
            source_url=url,
            content=fetched[0] if fetched else None,
            content_type=fetched[1] if fetched else None,
            size_bytes=len(fetched[0]) if fetched else None,
        )
        session.add(media)
        stored.append(media)
    await session.commit()
    return stored


async def acknowledge(
    session: AsyncSession,
    redis: Any,
    *,
    channel_id: uuid.UUID,
    conversation_id: uuid.UUID,
    recipient_external_id: str,
) -> None:
    """Tell the patient somebody will look, once per burst."""
    first = await redis.set(
        f"patient_media_ack:{conversation_id}", "1", ex=ACK_WINDOW_SECONDS, nx=True
    )
    if not first:
        return
    text = ACKNOWLEDGEMENTS[await patient_script(session, conversation_id)]
    delivered = await send_reply(
        session,
        channel_id=channel_id,
        recipient_external_id=recipient_external_id,
        text=text,
        # The photo itself is the patient's latest message.
        last_user_message_at=datetime.now(UTC),
        reply_context=await reply_context_for(session, conversation_id),
    )
    if delivered is not None:
        await record_outbound_message(
            session,
            conversation_id=conversation_id,
            channel_type=delivered,
            text=text,
            sender=MessageSender.BOT,
        )
    await session.commit()


def caption(patient: User | None, count: int) -> str:
    who = "Bemor"
    if patient is not None:
        if patient.name and patient.username:
            who = f"{patient.name} (@{patient.username})"
        elif patient.username:
            who = f"@{patient.username}"
        elif patient.name:
            who = patient.name
    photos = "rasm" if count == 1 else f"{count} ta rasm"
    base = get_settings().public_base_url
    link = f"\n🖥 Dashboard → Rasm yuborganlar: {base.rstrip('/')}/admin/" if base else ""
    return f"📷 Bemor tomonidan {photos} yuborildi\n👤 {who}{link}"
