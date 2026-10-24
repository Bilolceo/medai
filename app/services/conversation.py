"""The conversation store: who is talking to a clinic, and what was said.

Shared business logic, deliberately platform-neutral. Nothing here mentions
Instagram or Telegram — a channel arrives as an id plus its type string, and
the patient as whatever id that platform issued. Both bots record their
traffic through this one module, so a clinic's conversation history is a
single table's worth of rows however the patient reached it, and neither
adapter grows its own copy of "find or make the user, find or make the
conversation, append the message".

Before this existed the Instagram pipeline persisted nothing at all: the
User/Conversation/Message models and their repositories were written but
never called, so every reply was generated from a single message with no
history, the operator-takeover flag on Conversation could never be read, and
the reply-window check had no real timestamp to measure against.
"""

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.message import DeliveryStatus, Message, MessageSender
from app.models.patient_media import PatientMedia
from app.models.user import User
from app.rag.llm import ChatMessage
from app.repositories.conversation import OPEN_STATUS, ConversationRepository
from app.repositories.message import MessageRepository
from app.repositories.user import UserRepository

# How much of a conversation is replayed to the LLM. Counted in messages,
# both sides included.
#
# Was 10, and a real booking -- greeting, the day, the time, the name, the
# phone, the reason, the confirmation -- is already most of that on its own.
# A patient who came back ten minutes later with an unrelated question
# ("sizlarda UZI ham bormi?") pushed their own name and number clean out of
# the window, and the model, holding nothing that said otherwise, answered
# as though meeting them for the first time: greeted again, asked again.
#
# 30 comfortably holds one full booking and the question that follows it.
# The ceiling is not the token cost -- a few dozen short chat lines is
# nothing against a modern context window -- it is that a very long chat
# could in principle crowd the system prompt's own rules out of what the
# model attends to most; 30 is nowhere near that, and PatientState (see
# app.services.persona) also carries the name, the phone and whether this is
# the conversation's first message as facts the model does not have to
# recover from scrollback at all.
DEFAULT_HISTORY_LIMIT = 30


@dataclass(frozen=True)
class InboundContext:
    """What the pipeline needs to know after an inbound message is recorded."""

    conversation_id: uuid.UUID
    user_id: uuid.UUID
    # False once an operator has taken this conversation over. The message is
    # still recorded — a patient's words belong in the transcript regardless
    # of who answers them — but the bot must not reply on top of a human.
    is_bot_enabled: bool
    # What the patient attached, recorded with the message (patient_media
    # rows, bytes not yet downloaded), in the order Instagram sent them.
    media_ids: list[uuid.UUID] = field(default_factory=list)


@dataclass(frozen=True)
class Attachment:
    """One thing attached to an inbound message, as the platform gave it."""

    kind: str  # app.models.patient_media.MediaKind
    url: str
    title: str | None = None
    # A shared reel's or post's own id on Instagram (reel_video_id,
    # ig_post_media_id), for asking the Graph API about it later.
    platform_media_id: str | None = None


# Keys a patient message's meta carries that are about the message itself,
# not about how to answer it; reply_context_for leaves them out.
_NOT_ROUTING = frozenset({"attachments", "post_media_id"})


async def _get_or_create_user(
    session: AsyncSession, *, channel_id: uuid.UUID, external_id: str
) -> User:
    """The User for this platform id, created on first contact.

    Two webhook deliveries for a patient's first two message bubbles can
    arrive concurrently and both find no row. The unique constraint on
    (tenant_id, channel_id, external_id) is what actually decides which one
    wins; the loser catches the IntegrityError and re-reads the row the
    winner inserted, inside a SAVEPOINT so only the failed insert is undone
    rather than everything the caller has done so far.
    """
    repo = UserRepository(session)
    existing = await repo.get_by_external_id(channel_id=channel_id, external_id=external_id)
    if existing is not None:
        return existing
    try:
        async with session.begin_nested():
            return await repo.create(channel_id=channel_id, external_id=external_id)
    except IntegrityError:
        raced = await repo.get_by_external_id(channel_id=channel_id, external_id=external_id)
        if raced is None:
            # The constraint that fired was not the one we expected to lose
            # to — re-raising beats returning a row we never found.
            raise
        return raced


async def _get_or_create_open_conversation(
    session: AsyncSession, *, user_id: uuid.UUID
) -> Conversation:
    """This patient's open conversation, opened on first contact. Races the
    same way, and for the same reason, as _get_or_create_user.
    """
    repo = ConversationRepository(session)
    existing = await repo.get_open_for_user(user_id)
    if existing is not None:
        return existing
    try:
        async with session.begin_nested():
            return await repo.create(user_id=user_id, status=OPEN_STATUS)
    except IntegrityError:
        raced = await repo.get_open_for_user(user_id)
        if raced is None:
            raise
        return raced


async def register_inbound_message(
    session: AsyncSession,
    *,
    channel_id: uuid.UUID,
    channel_type: str,
    sender_external_id: str,
    text: str,
    reply_context: Mapping[str, Any] | None = None,
    wants_a_person: bool = True,
    attachments: Sequence[Attachment] = (),
    external_id: str | None = None,
) -> InboundContext:
    """Record one message from a patient and return the context to act on it.

    Called by every platform's inbound edge, before any answering happens:
    the transcript must contain what the patient said whether the bot
    answers, an operator does, or nothing does. Requires a tenant in context
    (see app.core.tenant_context) — every write here goes through a
    tenant-scoped repository.

    Does not commit. The caller owns the transaction, so recording the
    message and whatever it decides to do next either both happen or
    neither does.

    `reply_context` is the platform's own routing detail for answering this
    message (see ChannelAdapter.send_text). It is stored on the message
    rather than only passed to the queue so that an operator replying from
    the dashboard hours later can route their reply the same way the bot
    would have — for a Telegram Business conversation, over the same
    business connection.

    `attachments` -- a photo, a voice message, a reel -- are recorded with
    the message and named in its meta, so the conversation shows them where
    they were sent. `external_id` is the platform's message id, which the
    Graph API needs to hand out a fresh link once the original has expired.
    """
    user = await _get_or_create_user(session, channel_id=channel_id, external_id=sender_external_id)
    conversation = await _get_or_create_open_conversation(session, user_id=user.id)
    meta: dict[str, Any] = dict(reply_context) if reply_context else {}
    media: list[PatientMedia] = []
    for index, attachment in enumerate(attachments):
        row = PatientMedia(
            tenant_id=user.tenant_id,
            user_id=user.id,
            conversation_id=conversation.id,
            external_id=f"{external_id}:{index}" if external_id else None,
            source_url=attachment.url,
            kind=attachment.kind,
            title=attachment.title,
            platform_media_id=attachment.platform_media_id,
        )
        session.add(row)
        media.append(row)
    if media:
        await session.flush()
        meta["attachments"] = [{"id": str(row.id), "kind": row.kind} for row in media]
    await MessageRepository(session).create(
        conversation_id=conversation.id,
        sender=MessageSender.PATIENT,
        content=text,
        channel=channel_type,
        meta=meta,
    )
    # `wants_a_person` is False for a sticker or a ❤️: nothing in it for
    # anybody to answer, so it pins nothing.
    if (
        wants_a_person
        and not conversation.is_bot_enabled
        and conversation.needs_doctor_since is None
    ):
        # Staff have this conversation and the bot will not answer, so the
        # patient is now waiting on a person.
        conversation.needs_doctor_since = datetime.now(UTC)
    return InboundContext(
        conversation_id=conversation.id,
        user_id=user.id,
        is_bot_enabled=conversation.is_bot_enabled,
        media_ids=[row.id for row in media],
    )


async def record_outbound_message(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    channel_type: str,
    text: str,
    sender: MessageSender = MessageSender.BOT,
    status: DeliveryStatus = DeliveryStatus.SENT,
    error: str | None = None,
) -> Message:
    """Record a reply, and whether the patient actually received it.

    Every reply is recorded now, delivered or not. It used to be written
    only after a successful send, which kept the model's history honest --
    it never sees a turn the patient did not get -- but left the clinic
    looking at chats full of questions and no answers, with nothing to say
    whether the assistant had stayed silent or its answer had been refused
    by Instagram. The history stays honest a different way: recent_history
    reads only delivered messages.
    """
    return await MessageRepository(session).create(
        conversation_id=conversation_id,
        sender=sender,
        content=text,
        channel=channel_type,
        delivery_status=str(status),
        delivery_error=error[:255] if error else None,
    )


async def last_inbound_at(session: AsyncSession, conversation_id: uuid.UUID) -> datetime | None:
    return await MessageRepository(session).last_inbound_at(conversation_id)


def _to_chat_messages(messages: Sequence[Message]) -> list[ChatMessage]:
    """Map stored messages onto the LLM's two-role view.

    An operator's message is presented as "assistant": from the model's side
    it is a turn the clinic already took, and hiding it would leave the
    model contradicting a human colleague's answer in the very next reply.
    """
    return [
        ChatMessage(
            role="user" if message.sender == MessageSender.PATIENT else "assistant",
            content=message.content,
        )
        for message in messages
    ]


async def recent_history(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> list[ChatMessage]:
    """The tail of a conversation, oldest first, in the LLM's message shape."""
    messages = await MessageRepository(session).list_recent(conversation_id, limit)
    return _to_chat_messages(messages)


async def context_for_reply(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> list[ChatMessage]:
    """The conversation's earlier turns, excluding the messages about to be
    answered.

    The inbound edge records a patient's message before anything answers it,
    so the tail of the transcript at reply time is exactly the batch being
    answered — one message, or several if they arrived inside the debounce
    window and were joined. Dropping the trailing patient turns leaves the
    conversation as it stood before this question, which is what belongs in
    front of it as context; passing them as well would show the model the
    same question twice, once split up and once joined.
    """
    turns = await recent_history(session, conversation_id, limit=limit)
    while turns and turns[-1]["role"] == "user":
        turns.pop()
    return turns


async def hours_since_last_contact(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    now: datetime,
    limit: int = DEFAULT_HISTORY_LIMIT,
) -> float | None:
    """How long ago this conversation last had anything in it, before the
    batch about to be answered -- the same trimming context_for_reply does,
    so "how long since we last spoke" agrees with what the model is shown as
    history. None on the conversation's first message, where the question
    does not apply.

    This is what tells the persona a patient is not walking back into the
    middle of an exchange, but starting a new one after a real gap: a front
    desk says "Assalomu alaykum" to somebody who wrote yesterday and did not
    say it themselves today, and only stops doing that within one sitting.
    """
    messages = await MessageRepository(session).list_recent(conversation_id, limit)
    kept = list(messages)
    while kept and kept[-1].sender == MessageSender.PATIENT:
        kept.pop()
    if not kept:
        return None
    return (now - kept[-1].created_at).total_seconds() / 3600


async def reply_context_for(
    session: AsyncSession, conversation_id: uuid.UUID
) -> dict[str, Any] | None:
    """How to route a reply into this conversation, as captured from the
    patient's most recent message.

    What an operator's dashboard reply needs: the bot's own reply carries
    the context straight through the queue, but a human answering later has
    only the conversation to go on.
    """
    messages = await MessageRepository(session).list_recent(conversation_id, 20)
    for message in reversed(messages):
        if message.sender != MessageSender.PATIENT or not message.meta:
            continue
        routing = {k: v for k, v in message.meta.items() if k not in _NOT_ROUTING}
        if routing:
            return routing
    return None
