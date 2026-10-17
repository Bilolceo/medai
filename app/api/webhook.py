"""Instagram's inbound edge.

This module and app.channels.instagram are the only places that know what an
Instagram webhook payload looks like. Its job is narrow on purpose:
authenticate the delivery, parse it, work out which channel it belongs to,
and hand each genuine message to the shared services — the conversation
store, then the debounce buffer. Everything after that point (retrieval,
the answer prompt, delivery) is platform-neutral and shared with
the Telegram bot, so this file is roughly what a Telegram webhook route will
mirror rather than duplicate.
"""

import hashlib
import hmac
import logging
from contextlib import suppress
from datetime import UTC, datetime

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_db_session
from app.core.queue import get_arq_pool
from app.core.redaction import preview
from app.core.tenant_context import reset_current_tenant, set_current_tenant
from app.services import clinic_echoes, message_labels
from app.services.admin_commands import parse_rule
from app.services.conversation import register_inbound_message
from app.services.debounce import handle_inbound_message
from app.services.idempotency import claim_event
from app.services.tenant_resolution import (
    ResolvedChannel,
    bot_replies_enabled,
    debounce_window,
    resolve_instagram_channel,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class WebhookSender(BaseModel):
    id: str


class WebhookRecipient(BaseModel):
    id: str


class AttachmentPayload(BaseModel):
    url: str | None = None
    # Set on a sticker, which otherwise arrives looking like a photo.
    sticker_id: int | str | None = None


class WebhookAttachment(BaseModel):
    # "image", "video", "audio", "file", "share", "story_mention", ...
    type: str
    payload: AttachmentPayload | None = None


class WebhookMessage(BaseModel):
    # Meta's own id for this message. Parsed because it is the idempotency
    # key: Meta redelivers a payload whose 200 came back too slowly or not
    # at all, and without a claim on this id the same message is recorded
    # and answered twice. Optional because Meta does not guarantee it on
    # every event shape, and a message with no id is better handled once
    # without dedup than dropped.
    mid: str | None = None
    text: str | None = None
    is_echo: bool = False
    # The patient unsent it; Instagram tells us, there is nothing to answer.
    is_deleted: bool = False
    # Something Instagram cannot show through the API.
    is_unsupported: bool = False
    attachments: list[WebhookAttachment] = []


class WebhookReaction(BaseModel):
    mid: str | None = None
    action: str | None = None  # "react" / "unreact"
    reaction: str | None = None  # "love", ...
    emoji: str | None = None


class MessagingEvent(BaseModel):
    sender: WebhookSender
    recipient: WebhookRecipient
    message: WebhookMessage | None = None
    reaction: WebhookReaction | None = None


class CommentFrom(BaseModel):
    id: str | None = None
    username: str | None = None


class CommentMedia(BaseModel):
    id: str | None = None


class CommentValue(BaseModel):
    id: str | None = None
    text: str | None = None
    media: CommentMedia | None = None
    # Instagram sends the comment's author here; a reply to another comment
    # also carries parent_id, which is left alone -- answering a thread the
    # clinic is not part of would be talking over somebody.
    parent_id: str | None = None
    from_: CommentFrom | None = Field(default=None, alias="from")

    model_config = ConfigDict(populate_by_name=True)


class WebhookChange(BaseModel):
    field: str
    value: CommentValue | None = None


class WebhookEntry(BaseModel):
    id: str
    messaging: list[MessagingEvent] = []
    changes: list[WebhookChange] = []


class WebhookPayload(BaseModel):
    object: str
    entry: list[WebhookEntry] = []


def _verify_signature(raw_body: bytes, signature_header: str | None, app_secret: str) -> bool:
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    provided = signature_header.removeprefix("sha256=")
    return hmac.compare_digest(expected, provided)


def _signature_failure_detail(
    raw_body: bytes, signature_header: str | None, app_secret: str
) -> str:
    """Log-safe `k=v` detail explaining *why* a signature check failed.

    The app secret never lands in a log line — only its length and a
    truncated fingerprint, which is enough to tell whether the value
    deployed on the host is the one you think it is (compare fingerprints
    across environments) without disclosing it. secret_surrounding_whitespace
    catches the most common deploy mistake: a value pasted into a hosting
    dashboard with a trailing newline or wrapping quotes, which silently
    changes the HMAC while looking identical on screen.

    Returned as one preformatted string rather than a dict because the
    caller decides whether this failure is fatal and both branches log
    the same detail (see receive_webhook). New call sites should prefer
    `extra=`, which app.core.logging now renders the same way.
    """
    expected = hmac.new(app_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    fields: dict[str, object] = {
        "expected": f"sha256={expected}",
        "received": signature_header,
        "body_length": len(raw_body),
        "secret_length": len(app_secret),
        "secret_fingerprint": hashlib.sha256(app_secret.encode("utf-8")).hexdigest()[:8],
        "secret_surrounding_whitespace": app_secret != app_secret.strip(),
    }
    return " ".join(f"{key}={value}" for key, value in fields.items())


def _is_echo(event: MessagingEvent, page_id: str) -> bool:
    if event.message is not None and event.message.is_echo:
        return True
    return event.sender.id == page_id


async def _handle_event(
    session: AsyncSession,
    pool: ArqRedis,
    channel: ResolvedChannel,
    event: MessagingEvent,
    page_id: str,
) -> None:
    """One messaging event, from an already-resolved channel under an
    already-set tenant context. Returns without doing anything for every
    event shape this pipeline has nothing to say to.
    """
    if event.message is None:
        if event.reaction is not None and event.sender.id != page_id:
            await _handle_reaction(session, pool, channel, event)
        # Otherwise not a message event (e.g. read receipt, postback).
        return

    if event.message.is_deleted:
        return

    if _is_echo(event, page_id):
        # The one echo worth reading: the account's owner typing a rule from
        # the account itself. A doctor cannot direct-message their own inbox,
        # so this is the only way a rule can come from the handle that owns
        # it. Whether that handle is a nominated admin is the job's to check.
        text = event.message.text
        if text is not None and parse_rule(text, get_settings().admin_command_keyword):
            if event.message.mid is None or await claim_event(
                pool,
                tenant_id=channel.tenant_id,
                channel_type=channel.channel_type,
                event_id=event.message.mid,
            ):
                await pool.enqueue_job(
                    "apply_owner_rule",
                    str(channel.tenant_id),
                    str(channel.channel_id),
                    text,
                    event.recipient.id,
                )
            return
        logger.info(
            "webhook_echo_skipped",
            extra={"sender_id": event.sender.id, "recipient_id": event.recipient.id},
        )
        # It may be the doctor answering from the Instagram app, which is what
        # unpins a conversation waiting on them. Deferred so the assistant's
        # own sends are recorded first and can be told apart from a person's.
        sent_at = datetime.now(UTC)
        # Noted at once as well: the assistant looks here before it sends,
        # so it does not answer under somebody who already has.
        with suppress(Exception):
            await clinic_echoes.note(
                pool, str(channel.channel_id), event.recipient.id, text, sent_at
            )
        await pool.enqueue_job(
            "note_clinic_reply",
            str(channel.tenant_id),
            str(channel.channel_id),
            event.recipient.id,
            text,
            sent_at=sent_at.isoformat(),
            _defer_by=20,
        )
        return

    image_urls = [
        attachment.payload.url
        for attachment in event.message.attachments
        if attachment.type == "image"
        and attachment.payload
        and attachment.payload.url
        and attachment.payload.sticker_id is None
    ]
    if event.message.text is None and image_urls:
        await _handle_images(session, pool, channel, event, image_urls)
        return

    if event.message.text is None and any(
        attachment.type in {"audio", "voice"} for attachment in event.message.attachments
    ):
        await _handle_voice_note(session, pool, channel, event)
        return

    if event.message.text is None:
        # Everything else a patient can send without typing: recorded, so the
        # clinic sees it, and answered only where there is something to say.
        await _handle_attachment(session, pool, channel, event, _attachment_kind(event.message))
        return

    # Claimed before anything is recorded, so a redelivery cannot append a
    # second copy of the patient's message to the transcript or trigger a
    # second reply. A message Meta sent without an id is processed without
    # a claim — handling it once too often beats never handling it.
    if event.message.mid is not None and not await claim_event(
        pool,
        tenant_id=channel.tenant_id,
        channel_type=channel.channel_type,
        event_id=event.message.mid,
    ):
        logger.info(
            "webhook_duplicate_skipped",
            extra={"mid": event.message.mid, "sender_id": event.sender.id},
        )
        return

    # Full message text stays out of INFO — it is patient content that
    # should not sit in logs that may ship to external monitoring (TZ
    # section 7, personal data). Length + a short truncated preview only.
    logger.info(
        "webhook_message_received",
        extra={
            "tenant_id": str(channel.tenant_id),
            "sender_igsid": event.sender.id,
            "recipient_id": event.recipient.id,
            "message_length": len(event.message.text),
            "message_preview": preview(event.message.text),
        },
    )

    # Recorded before any decision about answering it: a patient's words
    # belong in the transcript whether the bot replies, an operator does,
    # or nothing does.
    inbound = await register_inbound_message(
        session,
        channel_id=channel.channel_id,
        channel_type=channel.channel_type,
        sender_external_id=event.sender.id,
        text=event.message.text,
    )
    await session.commit()

    # Before every early return below, because the conversations an operator
    # answers by hand are exactly the ones that most need a name on them in
    # the dashboard. Fire-and-forget: the job is cheap after the first
    # message from a patient, and nothing about this request depends on it.
    await pool.enqueue_job(
        "resolve_username",
        str(channel.tenant_id),
        str(channel.channel_id),
        str(inbound.user_id),
    )

    # A rule the clinic's admin is setting by direct message. Only the cheap
    # half of the decision happens here -- does the text start with the
    # keyword -- because knowing *who* wrote it can need a call to Meta, and
    # a webhook that waits on Meta is a webhook Meta retries. The job checks
    # the sender and drops the message if they are not a nominated admin.
    #
    # Instead of the ordinary reply, not alongside it: a command is not a
    # question, and answering "Aiadm1in: har doim shanba qabulini eslat" as
    # though a patient had asked something would put a second message under
    # every rule the admin sets. The cost is that somebody who is not an
    # admin and typed the keyword exactly gets no reply -- the cheaper of the
    # two mistakes, and it takes typing a keyword they have no reason to know.
    if parse_rule(event.message.text, get_settings().admin_command_keyword) is not None:
        await pool.enqueue_job(
            "apply_admin_rule",
            str(channel.tenant_id),
            str(channel.channel_id),
            str(inbound.user_id),
            event.sender.id,
            event.message.text,
        )
        return

    if not inbound.is_bot_enabled:
        # An operator has taken this conversation over. The bot must not
        # answer on top of a human — the message is already recorded, which
        # is the whole of what is wanted here.
        logger.info(
            "webhook_bot_disabled_for_conversation",
            extra={
                "conversation_id": str(inbound.conversation_id),
                "sender_igsid": event.sender.id,
            },
        )
        return

    if not await bot_replies_enabled(session, channel.tenant_id):
        # The clinic has switched the assistant off from the dashboard.
        # Checked here rather than in the worker so that nothing is queued
        # while it is off: a switch that let jobs accumulate would answer
        # every one of them the moment it was switched back on, which is a
        # burst of late replies to patients who have long since given up.
        logger.info(
            "webhook_bot_disabled_for_tenant",
            extra={
                "tenant_id": str(channel.tenant_id),
                "conversation_id": str(inbound.conversation_id),
            },
        )
        return

    await handle_inbound_message(
        pool,
        tenant_id=channel.tenant_id,
        channel_id=channel.channel_id,
        conversation_id=inbound.conversation_id,
        sender_external_id=event.sender.id,
        message_text=event.message.text,
        window_seconds=await debounce_window(session, channel.tenant_id),
    )


async def _handle_images(
    session: AsyncSession,
    pool: ArqRedis,
    channel: ResolvedChannel,
    event: MessagingEvent,
    image_urls: list[str],
) -> None:
    """A patient sent a photo. Record it in the transcript and hand the rest
    -- storing the image, telling the patient it will be looked at, telling
    the doctor -- to the worker, where a slow download cannot hold up Meta's
    delivery.

    Never answered by the model: a picture is not a question it can read, and
    a medical photo is the last thing an assistant should comment on.
    """
    assert event.message is not None
    if event.message.mid is not None and not await claim_event(
        pool,
        tenant_id=channel.tenant_id,
        channel_type=channel.channel_type,
        event_id=event.message.mid,
    ):
        return
    inbound = await register_inbound_message(
        session,
        channel_id=channel.channel_id,
        channel_type=channel.channel_type,
        sender_external_id=event.sender.id,
        text=(
            f"📷 Rasm yubordi ({len(image_urls)} ta)" if len(image_urls) > 1 else "📷 Rasm yubordi"
        ),
    )
    await session.commit()
    logger.info(
        "webhook_images_received",
        extra={"tenant_id": str(channel.tenant_id), "count": len(image_urls)},
    )
    await pool.enqueue_job(
        "resolve_username", str(channel.tenant_id), str(channel.channel_id), str(inbound.user_id)
    )
    await pool.enqueue_job(
        "handle_patient_media",
        str(channel.tenant_id),
        str(channel.channel_id),
        str(inbound.user_id),
        str(inbound.conversation_id),
        event.sender.id,
        event.message.mid,
        image_urls,
    )


# What Instagram calls the things a patient can send, by what we do with them.
_KIND_BY_TYPE = {
    "video": message_labels.VIDEO,
    "file": message_labels.FILE,
    "share": message_labels.SHARE,
    "ig_reel": message_labels.SHARE,
    "reel": message_labels.SHARE,
    "ig_post": message_labels.SHARE,
    "post": message_labels.SHARE,
    "template": message_labels.SHARE,
    "story_mention": message_labels.STORY,
    "sticker": message_labels.STICKER,
    "like_heart": message_labels.STICKER,
    "animated_image": message_labels.STICKER,
}
# Seen by a person, so the conversation waits on one -- as with a photo.
_FOR_A_PERSON = {message_labels.VIDEO, message_labels.FILE}

# How long past the debounce window the attachment greeting waits, so a
# question typed under a reel has been recorded before the greeting looks.
_ATTACHMENT_GREETING_DELAY_SECONDS = 2


def _attachment_kind(message: WebhookMessage) -> str | None:
    """The label for a message with no text, or None for one we cannot name."""
    if message.is_unsupported:
        return None
    for attachment in message.attachments:
        if attachment.payload is not None and attachment.payload.sticker_id is not None:
            return message_labels.STICKER
        kind = _KIND_BY_TYPE.get(attachment.type)
        if kind is not None:
            return kind
    return None


async def _handle_attachment(
    session: AsyncSession,
    pool: ArqRedis,
    channel: ResolvedChannel,
    event: MessagingEvent,
    kind: str | None,
) -> None:
    """A video, a file, a shared reel, a story mention, a sticker -- or
    something Instagram cannot show us. Recorded in the transcript as a
    label, then:

    * a video or a file is looked at by a person: the conversation waits on
      the doctor;
    * every one of them -- video, file, reel, sticker, story mention, or
      something unnamed -- is answered with a greeting and an offer of help,
      once a day (app.workers.tasks.answer_attachment), because the
      assistant cannot open it and must not pretend to.
    """
    assert event.message is not None
    if event.message.mid is not None and not await claim_event(
        pool,
        tenant_id=channel.tenant_id,
        channel_type=channel.channel_type,
        event_id=event.message.mid,
    ):
        return
    text = message_labels.TEXT[kind] if kind is not None else message_labels.UNSUPPORTED
    inbound = await register_inbound_message(
        session,
        channel_id=channel.channel_id,
        channel_type=channel.channel_type,
        sender_external_id=event.sender.id,
        text=text,
        wants_a_person=kind in _FOR_A_PERSON,
    )
    await session.commit()
    logger.info(
        "webhook_attachment_received",
        extra={
            "tenant_id": str(channel.tenant_id),
            "kind": text,
            "types": [attachment.type for attachment in event.message.attachments],
        },
    )
    await pool.enqueue_job(
        "resolve_username", str(channel.tenant_id), str(channel.channel_id), str(inbound.user_id)
    )
    # After the debounce window, not at once: a reel and the question typed
    # under it arrive as two events, and the greeting has to see the question
    # to know the patient is already being answered.
    window = await debounce_window(session, channel.tenant_id)
    if window is None:
        window = get_settings().debounce_window_seconds
    await pool.enqueue_job(
        "answer_attachment",
        str(channel.tenant_id),
        str(channel.channel_id),
        str(inbound.conversation_id),
        event.sender.id,
        kind or "",
        _defer_by=window + _ATTACHMENT_GREETING_DELAY_SECONDS,
    )


async def _handle_reaction(
    session: AsyncSession,
    pool: ArqRedis,
    channel: ResolvedChannel,
    event: MessagingEvent,
) -> None:
    """A ❤️ on one of our messages. Shown in the transcript, never answered,
    and never a reason for anybody to reply: it is how a conversation ends."""
    reaction = event.reaction
    assert reaction is not None
    if reaction.action != "react":
        return
    if reaction.mid is not None and not await claim_event(
        pool,
        tenant_id=channel.tenant_id,
        channel_type=channel.channel_type,
        event_id=f"reaction:{reaction.mid}:{reaction.emoji or reaction.reaction}",
    ):
        return
    await register_inbound_message(
        session,
        channel_id=channel.channel_id,
        channel_type=channel.channel_type,
        sender_external_id=event.sender.id,
        text=message_labels.reaction(reaction.emoji or "❤️"),
        wants_a_person=False,
    )
    await session.commit()
    logger.info("webhook_reaction_recorded", extra={"tenant_id": str(channel.tenant_id)})


async def _handle_voice_note(
    session: AsyncSession,
    pool: ArqRedis,
    channel: ResolvedChannel,
    event: MessagingEvent,
) -> None:
    """A voice message. Recorded, and answered with the one line the clinic
    wants said: the administrator does not listen to these, the doctor
    answers them. The model is never asked -- it cannot hear the message, so
    anything it wrote would be about a message it had not read."""
    assert event.message is not None
    if event.message.mid is not None and not await claim_event(
        pool,
        tenant_id=channel.tenant_id,
        channel_type=channel.channel_type,
        event_id=event.message.mid,
    ):
        return
    inbound = await register_inbound_message(
        session,
        channel_id=channel.channel_id,
        channel_type=channel.channel_type,
        sender_external_id=event.sender.id,
        text="🎤 Ovozli xabar",
    )
    await session.commit()
    logger.info("webhook_voice_note_received", extra={"tenant_id": str(channel.tenant_id)})
    await pool.enqueue_job(
        "resolve_username", str(channel.tenant_id), str(channel.channel_id), str(inbound.user_id)
    )
    await pool.enqueue_job(
        "answer_voice_note",
        str(channel.tenant_id),
        str(channel.channel_id),
        str(inbound.conversation_id),
        event.sender.id,
    )


async def _handle_comment(
    pool: ArqRedis, channel: ResolvedChannel, change: WebhookChange, page_id: str
) -> None:
    """A comment under one of the account's posts.

    Only the cheap checks happen here -- there is a comment, it has text, and
    it is not the account replying to itself. Everything else, including who
    is allowed an answer, belongs to the job.
    """
    value = change.value
    if value is None or not value.id or not value.text:
        return
    author = value.from_.id if value.from_ else None
    if author == page_id:
        return
    if not await claim_event(
        pool,
        tenant_id=channel.tenant_id,
        channel_type=channel.channel_type,
        event_id=f"comment:{value.id}",
    ):
        return
    logger.info(
        "webhook_comment_received",
        extra={
            "tenant_id": str(channel.tenant_id),
            "username": value.from_.username if value.from_ else None,
            "text_preview": preview(value.text),
        },
    )
    await pool.enqueue_job(
        "answer_comment",
        str(channel.tenant_id),
        str(channel.channel_id),
        value.id,
        author,
        value.from_.username if value.from_ else None,
        value.text,
        media_id=value.media.id if value.media else None,
    )


async def _handle_payload(session: AsyncSession, pool: ArqRedis, payload: WebhookPayload) -> None:
    for entry in payload.entry:
        # entry.id is the IG account (page) id that received the message —
        # one tenant's channel per entry, so resolution happens once per
        # entry rather than once per messaging event. The channel id it
        # returns is carried all the way to delivery, so the reply goes out
        # over the account the patient actually wrote to.
        channel = await resolve_instagram_channel(session, entry.id)
        if channel is None:
            logger.warning("webhook_unknown_ig_account", extra={"ig_account_id": entry.id})
            continue

        token = set_current_tenant(channel.tenant_id)
        try:
            for event in entry.messaging:
                await _handle_event(session, pool, channel, event, entry.id)
            for change in entry.changes:
                if change.field in {"comments", "live_comments"}:
                    await _handle_comment(pool, channel, change, entry.id)
        finally:
            reset_current_tenant(token)


@router.get("/webhook")
async def verify_webhook(
    hub_mode: str | None = Query(default=None, alias="hub.mode"),
    hub_verify_token: str | None = Query(default=None, alias="hub.verify_token"),
    hub_challenge: str | None = Query(default=None, alias="hub.challenge"),
    settings: Settings = Depends(get_settings),
) -> Response:
    if hub_mode == "subscribe" and hub_verify_token == settings.webhook_verify_token:
        return Response(content=hub_challenge or "", media_type="text/plain")
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Webhook verification failed")


@router.post("/webhook")
async def receive_webhook(
    request: Request,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_db_session),
    pool: ArqRedis = Depends(get_arq_pool),
) -> Response:
    raw_body = await request.body()
    signature_header = request.headers.get("x-hub-signature-256")

    if _verify_signature(raw_body, signature_header, settings.meta_app_secret):
        # Logged on the way past, not only on failure: while a deployment is
        # being diagnosed, "the signature matched" is the result being waited
        # for, and a check that speaks up only when it fails cannot tell a
        # fixed secret apart from traffic that stopped arriving. The
        # fingerprint identifies which secret matched without disclosing it.
        logger.info(
            "webhook_signature_ok secret_fingerprint=%s",
            hashlib.sha256(settings.meta_app_secret.encode("utf-8")).hexdigest()[:8],
        )
    else:
        detail = _signature_failure_detail(raw_body, signature_header, settings.meta_app_secret)
        if settings.webhook_signature_enforced:
            logger.warning("webhook_signature_invalid %s", detail)
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid signature")
        # Not enforcing: this payload is processed despite failing the check.
        # A distinct message from the enforced case, so a log search can
        # prove whether anything was ever let through unverified.
        logger.warning("webhook_signature_invalid_allowed %s", detail)

    try:
        payload = WebhookPayload.model_validate_json(raw_body)
    except ValueError:
        logger.warning("webhook_payload_invalid")
        return Response(status_code=status.HTTP_200_OK)

    await _handle_payload(session, pool, payload)

    return Response(status_code=status.HTTP_200_OK)
