"""The post behind a comment: fetched once, its video heard once, kept.

A comment under a reel is about that reel. The assistant used to see only
the caption -- "Jinsiy zaiflikni dori-darmonsiz davolash" -- and asked
"narxi qancha?" which service was meant. Now it is given what the doctor
says in the video: the reel's sound track is transcribed (OpenAI's speech
model, which hears Uzbek and Russian) the first time somebody comments
under it, and a two-line summary of it is written for the prompt and the
dashboard. Every later comment under the same post reads the stored row.

Everything here is best effort around the answer: a post that cannot be
fetched, a video that cannot be heard, a summary that fails -- the comment
is still answered, with whatever was had. Nothing raises past `context()`.
"""

import logging
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any

from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.instagram.client import InstagramClient
from app.core.config import get_settings
from app.models.instagram_post import InstagramPost, TranscriptStatus
from app.rag.llm import LLMProvider, get_llm_provider

logger = logging.getLogger(__name__)

# Instagram's CDN links expire; a row older than this is asked for again on
# the next comment, which also picks up an edited caption.
REFRESH_AFTER = timedelta(hours=24)
# A reel the clinic posts is two to ten megabytes; nothing here should hold
# more than this in memory, and OpenAI's speech endpoint takes 25 MB at most.
MAX_VIDEO_BYTES = 24 * 1024 * 1024
# Whisper hears the first chunk best and a reel says what it has to say in
# its first minutes; the prompt reads a summary anyway.
MAX_TRANSCRIPT_CHARS = 6000
# Not whisper-1: tried on a real reel (2026-10-20), it wrote the doctor's
# Uzbek down as Kazakh. This one, told in the prompt what it is hearing,
# wrote it as he said it, in three seconds. Neither takes "uz" as a
# language code, so the prompt is how it is told.
SPEECH_MODEL = "gpt-4o-transcribe"
SPEECH_PROMPT = (
    "Urolog-androlog doktorning o‘zbek tilidagi (ba’zan rus tilidagi) tibbiy videosi: "
    "jinsiy zaiflik, tez bo‘shanish, implant, prostatit, bepushtlik, PRP terapiya."
)
_VIDEO = "VIDEO"

SUMMARY_PROMPT = """\
Quyida urolog-androlog doktorning Instagram videosidagi so'zlari (avtomatik \
yozib olingan, xatolar bo'lishi mumkin) va postning matni berilgan. Videoda \
nima haqida gap ketayotganini 1-2 jumlada, o'zbek lotin yozuvida yozing: \
qaysi kasallik yoki xizmat, doktor nimani tavsiya qilyapti, narx yoki muddat \
aytilgan bo'lsa shuni ham. Faqat videoda aytilganini yozing, o'zingizdan \
qo'shmang. Faqat xulosaning o'zini qaytaring."""


async def context(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client: InstagramClient,
    access_token: str,
    media_id: str | None,
    llm_provider: LLMProvider | None = None,
) -> InstagramPost | None:
    """The post a comment was left under, fetched, heard and summarised as
    far as that could be done; None when there is no post to know about.
    """
    if not media_id:
        return None
    try:
        post = await _fetch_or_refresh(
            session,
            tenant_id=tenant_id,
            client=client,
            access_token=access_token,
            media_id=media_id,
        )
        if post is None:
            return None
        if post.transcript_status == TranscriptStatus.PENDING:
            await _transcribe(post, client=client, access_token=access_token, media_id=media_id)
        if post.summary is None and (post.transcript or post.caption):
            await _summarise(post, llm_provider or get_llm_provider())
        await session.commit()
        return post
    except Exception:  # noqa: BLE001 - the comment is answered without its post
        logger.exception("instagram_post_context_failed", extra={"media_id": media_id})
        await session.rollback()
        return None


async def _fetch_or_refresh(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    client: InstagramClient,
    access_token: str,
    media_id: str,
) -> InstagramPost | None:
    post = (
        await session.execute(
            select(InstagramPost).where(
                InstagramPost.tenant_id == tenant_id, InstagramPost.media_id == media_id
            )
        )
    ).scalar_one_or_none()
    fresh = post is not None and datetime.now(UTC) - post.fetched_at < REFRESH_AFTER
    if fresh:
        return post
    body = await client.fetch_media(access_token=access_token, media_id=media_id)
    if body is None:
        return post
    if post is None:
        post = InstagramPost(tenant_id=tenant_id, media_id=media_id)
        session.add(post)
    _apply(post, body)
    # A transcript already made survives a refresh; one that failed is tried
    # again, which is what the next comment under the post is for.
    if post.media_type != _VIDEO:
        post.transcript_status = TranscriptStatus.SKIPPED
    elif post.transcript_status == TranscriptStatus.FAILED:
        post.transcript_status = TranscriptStatus.PENDING
    post.fetched_at = datetime.now(UTC)
    # The file link is kept only for the transcription that follows; it
    # expires within hours and is never shown to anybody.
    post._media_url = body.get("media_url")  # type: ignore[attr-defined]
    await session.flush()
    return post


def _apply(post: InstagramPost, body: dict[str, Any]) -> None:
    post.media_type = _text(body.get("media_type"), 32)
    post.media_product_type = _text(body.get("media_product_type"), 32)
    caption = _text(body.get("caption"))
    if caption != post.caption:
        post.caption = caption
        # A changed caption changes what the summary says.
        post.summary = None
    post.permalink = _text(body.get("permalink"))
    post.thumbnail_url = _text(body.get("thumbnail_url")) or _text(body.get("media_url"))
    stamp = body.get("timestamp")
    if isinstance(stamp, str):
        with suppress(ValueError):
            post.posted_at = datetime.fromisoformat(stamp.replace("+0000", "+00:00"))


def _text(value: Any, limit: int | None = None) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text: str = value.strip()
    return text[:limit] if limit else text


async def _transcribe(
    post: InstagramPost, *, client: InstagramClient, access_token: str, media_id: str
) -> None:
    url = getattr(post, "_media_url", None)
    if not url:
        body = await client.fetch_media(access_token=access_token, media_id=media_id)
        url = body.get("media_url") if body else None
    if not url:
        post.transcript_status = TranscriptStatus.FAILED
        return
    data = await client.download(url, max_bytes=MAX_VIDEO_BYTES)
    if not data:
        post.transcript_status = TranscriptStatus.FAILED
        logger.warning("instagram_video_download_failed", extra={"media_id": media_id})
        return
    text = await _speech_to_text(data)
    if text is None:
        post.transcript_status = TranscriptStatus.FAILED
        return
    post.transcript = text[:MAX_TRANSCRIPT_CHARS] or None
    post.transcript_status = TranscriptStatus.READY if post.transcript else TranscriptStatus.SKIPPED
    logger.info(
        "instagram_video_transcribed",
        extra={"media_id": media_id, "chars": len(post.transcript or "")},
    )


async def _speech_to_text(data: bytes) -> str | None:
    api_key = get_settings().openai_api_key
    if not api_key:
        return None
    openai = AsyncOpenAI(api_key=api_key, timeout=120.0, max_retries=1)
    try:
        result = await openai.audio.transcriptions.create(
            model=SPEECH_MODEL,
            file=("reel.mp4", data, "video/mp4"),
            prompt=SPEECH_PROMPT,
            response_format="text",
        )
    except Exception:  # noqa: BLE001 - logged, the comment goes on without it
        logger.exception("instagram_video_transcription_failed")
        return None
    return result if isinstance(result, str) else str(result)


async def _summarise(post: InstagramPost, provider: LLMProvider) -> None:
    parts = []
    if post.caption:
        parts.append(f"Post matni:\n{post.caption}")
    if post.transcript:
        parts.append(f"Videodagi so'zlar:\n{post.transcript}")
    try:
        summary = await provider.generate(
            SUMMARY_PROMPT, [{"role": "user", "content": "\n\n".join(parts)}]
        )
    except Exception:  # noqa: BLE001
        logger.exception("instagram_post_summary_failed", extra={"media_id": post.media_id})
        return
    post.summary = summary.strip()[:600] or None


def describe(post: InstagramPost | None) -> str | None:
    """What the model is told about the post, in the order it should trust it."""
    if post is None:
        return None
    lines = []
    if post.summary:
        lines.append(f"Video mazmuni: {post.summary}")
    if post.caption:
        lines.append(f"Post matni: «{post.caption[:800]}»")
    if post.transcript and not post.summary:
        lines.append(f"Videoda aytilgan so'zlar: «{post.transcript[:1500]}»")
    return "\n".join(lines) or None
