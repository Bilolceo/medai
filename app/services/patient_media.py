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
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlsplit

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.instagram.client import GRAPH_API_BASE_URL
from app.core.config import get_settings
from app.core.encryption import DecryptionError, decrypt
from app.models.message import MessageSender
from app.models.patient_media import MediaKind, PatientMedia
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
# A minute-long voice message is under a megabyte; a patient's own video or a
# shared reel is a few. Anything larger is left as a link.
MAX_MEDIA_BYTES = 60 * 1024 * 1024

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
# What a voice message, a video or a reel arrives as. Instagram serves voice
# messages as audio/mp4 and, from some CDN nodes, as video/mp4.
SAFE_AV_TYPES = frozenset(
    {
        "audio/mp4", "audio/mpeg", "audio/aac", "audio/ogg", "audio/wav", "audio/x-wav",
        "audio/webm", "audio/x-m4a", "audio/m4a",
        "video/mp4", "video/quicktime", "video/webm", "video/3gpp",
    }
)  # fmt: skip
SAFE_FILE_TYPES = frozenset({"application/pdf"})
SAFE_TYPES = SAFE_IMAGE_TYPES | SAFE_AV_TYPES | SAFE_FILE_TYPES

# Downloaded as soon as they arrive: Instagram's links expire within days.
# Shared reels and stories are not -- a patient shares dozens, mostly the
# clinic's own -- and are fetched the first time somebody opens one.
DOWNLOAD_ON_ARRIVAL = frozenset({MediaKind.AUDIO, MediaKind.VIDEO, MediaKind.FILE})


def accepts(kind: str) -> tuple[frozenset[str], int]:
    """The content types and the size a kind of attachment may be stored at."""
    if kind in (MediaKind.IMAGE, MediaKind.STICKER):
        return SAFE_IMAGE_TYPES, MAX_IMAGE_BYTES
    if kind == MediaKind.FILE:
        return SAFE_FILE_TYPES, MAX_MEDIA_BYTES
    # A shared post can be a photo; a story is either.
    return SAFE_IMAGE_TYPES | SAFE_AV_TYPES, MAX_MEDIA_BYTES


# Where Instagram serves its media and its pages from. A link read out of a
# page (og:video, og:image) is followed only to one of these: the page came
# from Instagram, but a URL written in it is still somebody's text.
_META_HOST_SUFFIXES = (
    "instagram.com",
    "cdninstagram.com",
    "fbcdn.net",
    "fbsbx.com",
    "facebook.com",
)
_INSTAGRAM_HOSTS = frozenset({"instagram.com", "www.instagram.com", "m.instagram.com"})
_PERMALINK_PATH = re.compile(r"^/(?:[\w.]+/)?(?:reels?|p|tv|stories)/[\w.-]+/?")

# A shared reel's link answers with a web page often enough that the page is
# read -- this much of it, for its og: tags -- rather than refused unread.
_PAGE_TYPES = frozenset({"text/html", "application/xhtml+xml"})
MAX_PAGE_BYTES = 1024 * 1024
_SHARED_KINDS = frozenset({MediaKind.SHARE, MediaKind.STORY})
# A reel or a story may be a video: say so, rather than asking for an image.
_SHARED_ACCEPT = "video/mp4,video/*;q=0.9,image/*;q=0.8,*/*;q=0.5"


def host_of(url: str | None) -> str:
    """The host alone: what is logged of a link. Instagram's links are signed,
    and the signature in a log is a working link to a patient's file."""
    try:
        return (urlsplit(url or "").hostname or "").lower()
    except ValueError:
        return ""


def is_meta_url(url: str | None) -> bool:
    """An https link on one of Instagram's or Facebook's own hosts."""
    if not url:
        return False
    try:
        scheme = urlsplit(url).scheme
    except ValueError:
        return False
    host = host_of(url)
    return scheme == "https" and any(
        host == suffix or host.endswith("." + suffix) for suffix in _META_HOST_SUFFIXES
    )


def instagram_permalink(url: str | None) -> str | None:
    """The instagram.com page of a reel, post or story, without its query
    (Instagram's share links carry tracking there), or None for any other
    link."""
    try:
        parts = urlsplit(url or "")
    except ValueError:
        return None
    if parts.scheme not in ("https", "http") or host_of(url) not in _INSTAGRAM_HOSTS:
        return None
    if not _PERMALINK_PATH.match(parts.path):
        return None
    return f"https://www.instagram.com{parts.path}"


@dataclass
class SharedMedia:
    """What could be learned about a shared reel or post besides its bytes:
    where to open it, its caption, its cover."""

    permalink: str | None = None
    title: str | None = None
    thumbnail_url: str | None = None
    video_url: str | None = None


class _OpenGraph(HTMLParser):
    """The og:/twitter: meta tags of a page, first value of each."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: dict[str, str] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "meta":
            return
        values = {name.lower(): value for name, value in attrs if value is not None}
        key = (values.get("property") or values.get("name") or "").lower()
        content = values.get("content")
        if content and key.startswith(("og:", "twitter:")):
            self.found.setdefault(key, content.strip())


def _read_page(page: bytes, final_url: str, shared: SharedMedia) -> None:
    """Note what an Instagram page says about the reel it shows."""
    parser = _OpenGraph()
    try:
        parser.feed(page.decode("utf-8", errors="replace"))
        parser.close()
    except Exception:  # noqa: BLE001 -- a page we cannot read tells us nothing
        return
    tags = parser.found
    shared.permalink = (
        shared.permalink
        or instagram_permalink(tags.get("og:url"))
        or instagram_permalink(final_url)
    )
    shared.title = shared.title or tags.get("og:title") or tags.get("og:description")
    for key in ("og:video:secure_url", "og:video:url", "og:video", "twitter:player:stream"):
        if is_meta_url(tags.get(key)):
            shared.video_url = shared.video_url or tags[key]
            break
    for key in ("og:image:secure_url", "og:image", "twitter:image"):
        if is_meta_url(tags.get(key)):
            shared.thumbnail_url = shared.thumbnail_url or tags[key]
            break


async def download(
    url: str,
    http: httpx.AsyncClient | None = None,
    *,
    kind: str = MediaKind.IMAGE,
    shared: SharedMedia | None = None,
) -> tuple[bytes, str] | None:
    """The file and its content type, or None if it cannot be had.

    Streamed and cut off at the kind's size, so a link to something enormous
    is refused without being held in memory first.

    With `shared` (a reel or a post), a web page where the file should have
    been is read for what it says about the reel -- its instagram.com link,
    caption, cover and, when the page names one, the video itself, which is
    then downloaded instead.
    """
    allowed, max_bytes = accepts(kind)
    client = http or httpx.AsyncClient(timeout=60, follow_redirects=True, headers=_BROWSER_HEADERS)
    headers = {"Accept": _SHARED_ACCEPT} if kind in _SHARED_KINDS else None
    status_code, content_type, body, page = 0, "", bytearray(), bytearray()
    final_url, redirects = url, 0
    try:
        async with client.stream("GET", url, headers=headers) as response:
            status_code = response.status_code
            final_url, redirects = str(response.url), len(response.history)
            content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
            if not response.is_error and content_type in allowed:
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > max_bytes:
                        break
            elif shared is not None and not response.is_error and content_type in _PAGE_TYPES:
                async for chunk in response.aiter_bytes():
                    page.extend(chunk)
                    if len(page) >= MAX_PAGE_BYTES:
                        break
    except httpx.HTTPError as exc:
        logger.warning(
            "patient_media_download_failed",
            extra={"error": type(exc).__name__, "kind": kind, "host": host_of(url)},
        )
        return None
    finally:
        if http is None:
            await client.aclose()
    if status_code >= 400 or content_type not in allowed or not body or len(body) > max_bytes:
        if page and shared is not None:
            _read_page(bytes(page), final_url, shared)
        logger.warning(
            "patient_media_download_refused",
            extra={
                "status_code": status_code,
                "size": len(body),
                "content_type": content_type,
                "kind": kind,
                # Hosts only: never the signed link.
                "host": host_of(url),
                "final_host": host_of(final_url),
                "redirects": redirects,
                "page_bytes": len(page),
                "page_had_video": bool(shared and shared.video_url),
                "page_had_permalink": bool(shared and shared.permalink),
            },
        )
        if shared is not None and shared.video_url and shared.video_url != url:
            # Once, and not reading pages again: one hop, not a crawl.
            video_url, shared.video_url = shared.video_url, None
            fetched = await download(video_url, http, kind=kind)
            if fetched is not None:
                logger.info("patient_media_fetched_through_page")
            return fetched
        return None
    return bytes(body), content_type


async def fresh_urls(access_token: str, message_id: str) -> list[str]:
    """The message's attachment links as the Graph API gives them now."""
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
        logger.warning(
            "patient_media_graph_failed",
            extra={"status_code": response.status_code, **_graph_error(response)},
        )
        return []
    urls: list[str] = []
    for item in (response.json().get("attachments") or {}).get("data", []):
        image = item.get("image_data") or {}
        video = item.get("video_data") or {}
        url = (
            image.get("url")
            or video.get("url")
            or item.get("file_url")
            or image.get("preview_url")
            or video.get("preview_url")
        )
        if url:
            urls.append(url)
    return urls


def _graph_error(response: httpx.Response) -> dict[str, Any]:
    """Meta's error code and type, for a log: never its message, which can
    quote the request back."""
    try:
        error = response.json().get("error") or {}
    except ValueError:
        return {}
    return {
        "graph_error_code": error.get("code"),
        "graph_error_subcode": error.get("error_subcode"),
        "graph_error_type": error.get("type"),
    }


# What the Graph API is asked about a shared reel or post by its id. It
# answers for media the account itself published (the clinic's own reels,
# which are most of what patients share); for anybody else's it refuses, and
# media_url is left out even for some of the clinic's own (licensed music) --
# Meta's advice is to fall back to permalink or thumbnail_url, which is what
# repair does.
_MEDIA_FIELDS = "media_type,media_url,thumbnail_url,permalink,caption"


async def graph_media(access_token: str, media_id: str) -> dict[str, Any] | None:
    """The Graph API's IG Media object for this id, or None."""
    if not media_id.isdigit():
        return None
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                f"{GRAPH_API_BASE_URL}/{media_id}",
                params={"fields": _MEDIA_FIELDS, "access_token": access_token},
            )
    except httpx.HTTPError as exc:
        logger.warning("shared_media_graph_unreachable", extra={"error": type(exc).__name__})
        return None
    if response.is_error:
        logger.warning(
            "shared_media_graph_failed",
            extra={"status_code": response.status_code, **_graph_error(response)},
        )
        return None
    try:
        found = response.json()
    except ValueError:
        return None
    return found if isinstance(found, dict) else None


async def fetch(
    url: str,
    *,
    access_token: str | None,
    message_id: str | None,
    index: int,
    kind: str = MediaKind.IMAGE,
    shared: SharedMedia | None = None,
) -> tuple[bytes, str] | None:
    """The file, by its webhook link first and through the Graph API second."""
    fetched = await download(url, kind=kind, shared=shared)
    if fetched is not None or not (access_token and message_id):
        return fetched
    urls = await fresh_urls(access_token, message_id)
    if index < len(urls):
        fetched = await download(urls[index], kind=kind, shared=shared)
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
    """Fetch now what was not downloaded when it came: a photo whose link
    failed then, or a reel nobody had opened before."""
    token = await channel_token(session, channel_id)
    if media.kind in _SHARED_KINDS:
        fetched = await _fetch_shared(media, token)
    else:
        mid, index = message_id_of(media)
        fetched = await fetch(
            media.source_url, access_token=token, message_id=mid, index=index, kind=media.kind
        )
    if fetched is None:
        # What was learnt of a shared reel -- its link, its caption -- is
        # kept even when the video itself could not be had.
        await session.commit()
        return False
    media.content, media.content_type, media.size_bytes = fetched[0], fetched[1], len(fetched[0])
    await session.commit()
    return True


async def _fetch_shared(media: PatientMedia, token: str | None) -> tuple[bytes, str] | None:
    """A shared reel or post: the video if Instagram will hand it over, its
    cover if not -- and, either way, its caption and instagram.com link
    written onto the row, so the conversation is never left an empty box.

    In order: the webhook's own link (followed through a web page to the
    video it names, if it is a page); the message's attachments through the
    Graph API; the reel itself by its id through the Graph API (media_url);
    and last its cover (thumbnail_url, or the page's og:image).
    """
    shared = SharedMedia()
    mid, index = message_id_of(media)
    fetched = await fetch(
        media.source_url,
        access_token=token,
        message_id=mid,
        index=index,
        kind=media.kind,
        shared=shared,
    )
    graph_asked = False
    if token and media.platform_media_id and (fetched is None or not shared.permalink):
        graph_asked = True
        info = await graph_media(token, media.platform_media_id)
        if info is not None:
            shared.permalink = shared.permalink or instagram_permalink(info.get("permalink"))
            caption = info.get("caption")
            if isinstance(caption, str) and caption.strip():
                shared.title = shared.title or caption
            thumbnail = info.get("thumbnail_url")
            if isinstance(thumbnail, str) and thumbnail:
                shared.thumbnail_url = shared.thumbnail_url or thumbnail
            media_url = info.get("media_url")
            if fetched is None and isinstance(media_url, str) and media_url:
                fetched = await download(media_url, kind=media.kind)
    outcome = "media" if fetched is not None else "none"
    if fetched is None and shared.thumbnail_url and is_meta_url(shared.thumbnail_url):
        # The cover, held to the photo rules.
        fetched = await download(shared.thumbnail_url, kind=MediaKind.IMAGE)
        if fetched is not None:
            outcome = "thumbnail"
    if not media.permalink and shared.permalink:
        media.permalink = shared.permalink
    if not media.title and shared.title and shared.title.strip():
        media.title = shared.title.strip()[:500]
    logger.info(
        "shared_media_resolved",
        extra={
            "kind": media.kind,
            "outcome": outcome,
            "content_type": fetched[1] if fetched else None,
            "has_media_id": media.platform_media_id is not None,
            "graph_asked": graph_asked,
            "has_permalink": media.permalink is not None,
            "has_title": media.title is not None,
        },
    )
    return fetched


def open_link(media: PatientMedia) -> str | None:
    """Where the clinic can open a shared reel or story on Instagram itself:
    its instagram.com page once known, otherwise the link Instagram sent
    (in a browser signed in to Instagram it opens the reel)."""
    if media.kind not in _SHARED_KINDS:
        return None
    if media.permalink:
        return media.permalink
    return media.source_url if is_meta_url(media.source_url) else None


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
    media_ids: list[uuid.UUID] | None = None,
) -> list[PatientMedia]:
    token = await channel_token(session, channel_id) if channel_id else None
    if media_ids:
        # Recorded with the message already (app.services.conversation); only
        # the bytes are missing.
        return await fill(session, media_ids, token=token)
    stored: list[PatientMedia] = []
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


async def fill(
    session: AsyncSession, media_ids: list[uuid.UUID], *, token: str | None
) -> list[PatientMedia]:
    """Download what these recorded attachments point at, where not done."""
    rows: list[PatientMedia] = []
    for media_id in media_ids:
        media = await session.get(PatientMedia, media_id)
        if media is None:
            continue
        rows.append(media)
        if media.size_bytes is not None:
            continue
        mid, index = message_id_of(media)
        fetched = await fetch(
            media.source_url, access_token=token, message_id=mid, index=index, kind=media.kind
        )
        if fetched is not None:
            media.content, media.content_type = fetched
            media.size_bytes = len(fetched[0])
    await session.commit()
    return rows


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
