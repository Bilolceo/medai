"""Reading patient conversations, and taking one over.

Operator takeover is the feature this makes real. `Conversation.is_bot_enabled`
existed as a column nothing read, so switching the bot off had no effect;
both inbound edges now check it before answering, and this is where a human
turns it off and starts replying themselves.
"""

import re
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import case, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin.deps import require_patient_access, verify_csrf_header
from app.api.admin.schemas import (
    AttachmentOut,
    BotToggle,
    ConversationCounts,
    ConversationDetail,
    ConversationSummary,
    MessageOut,
    OperatorReply,
    PostCommentGroup,
    PostOut,
)
from app.api.auth import get_current_operator
from app.core.db import get_db_session
from app.core.redaction import preview
from app.core.tenant_context import get_current_tenant
from app.models.channel import Channel
from app.models.conversation import Conversation
from app.models.instagram_post import InstagramPost
from app.models.lead import Lead
from app.models.message import Message, MessageSender
from app.models.operator import Operator
from app.models.patient_media import MediaKind, PatientMedia
from app.models.user import User
from app.repositories.conversation import ConversationRepository
from app.repositories.message import MessageRepository
from app.services import handoff, message_labels, search
from app.services.conversation import (
    record_outbound_message,
    reply_context_for,
)
from app.services.delivery import send_reply

# Waiting on a person for longer than this: still owed an answer, no longer
# the alarm at the top (the dashboard greys it after the same two days).
STALE_PIN = timedelta(hours=48)

router = APIRouter(prefix="/api/admin/conversations", tags=["Admin — Conversations"])

# How much of a transcript the detail view returns. Long enough to read the
# whole of a normal clinic conversation, bounded so one very long thread
# cannot make the endpoint slow for everybody.
TRANSCRIPT_LIMIT = 200


async def _summaries(
    session: AsyncSession, conversations: list[Conversation]
) -> list[ConversationSummary]:
    """One extra query for the patients and one for the last messages, not
    two per row.
    """
    if not conversations:
        return []

    user_ids = {conversation.user_id for conversation in conversations}
    users = {
        user.id: user
        for user in (await session.execute(select(User).where(User.id.in_(user_ids)))).scalars()
    }
    channel_ids = {user.channel_id for user in users.values()}
    channels = {
        channel.id: channel
        for channel in (
            await session.execute(select(Channel).where(Channel.id.in_(channel_ids)))
        ).scalars()
    }

    # The newest message per conversation, resolved in one pass rather than
    # a query per row.
    newest = (
        select(
            Message.conversation_id,
            func.max(Message.created_at).label("last_at"),
        )
        .where(Message.conversation_id.in_([c.id for c in conversations]))
        .group_by(Message.conversation_id)
        .subquery()
    )
    last_messages = {
        message.conversation_id: message
        for message in (
            await session.execute(
                select(Message).join(
                    newest,
                    (Message.conversation_id == newest.c.conversation_id)
                    & (Message.created_at == newest.c.last_at),
                )
            )
        ).scalars()
    }

    reasons = await _waiting_reasons(
        session, [c.id for c in conversations if c.needs_doctor_since is not None]
    )
    posts = await _posts_behind(session, [c.id for c in conversations])

    rows = []
    for conversation in conversations:
        user = users.get(conversation.user_id)
        channel = channels.get(user.channel_id) if user is not None else None
        last = last_messages.get(conversation.id)
        rows.append(
            ConversationSummary(
                id=conversation.id,
                status=conversation.status,
                is_bot_enabled=conversation.is_bot_enabled,
                patient_name=user.name if user is not None else None,
                patient_username=user.username if user is not None else None,
                patient_external_id=user.external_id if user is not None else "",
                channel=channel.type if channel is not None else "",
                last_message_at=last.created_at if last is not None else None,
                # Truncated in the list the same way it is in the logs: a
                # roster of conversations does not need the full text of
                # every one, and this view is often left open on a screen.
                last_message_preview=preview(last.content) if last is not None else None,
                updated_at=conversation.updated_at,
                needs_doctor_since=conversation.needs_doctor_since,
                waiting_reason=reasons.get(conversation.id),
                post_summary=_post_label(posts.get(conversation.id)),
                post_media_id=posts[conversation.id].media_id if conversation.id in posts else None,
            )
        )
    return rows


async def _posts_behind(
    session: AsyncSession, conversation_ids: list[uuid.UUID]
) -> dict[uuid.UUID, InstagramPost]:
    """For each conversation that began under a post, that post -- the one
    the newest comment in it names."""
    if not conversation_ids:
        return {}
    rows = (
        await session.execute(
            select(Message.conversation_id, Message.meta["post_media_id"].astext)
            .where(
                Message.conversation_id.in_(conversation_ids),
                Message.sender == MessageSender.PATIENT,
                Message.meta["post_media_id"].astext.is_not(None),
            )
            .order_by(Message.created_at.desc())
        )
    ).all()
    wanted: dict[uuid.UUID, str] = {}
    for conversation_id, media_id in rows:
        wanted.setdefault(conversation_id, media_id)
    if not wanted:
        return {}
    posts = {
        post.media_id: post
        for post in (
            await session.execute(
                select(InstagramPost).where(
                    InstagramPost.tenant_id == get_current_tenant(),
                    InstagramPost.media_id.in_(set(wanted.values())),
                )
            )
        ).scalars()
    }
    return {cid: posts[mid] for cid, mid in wanted.items() if mid in posts}


def _post_label(post: InstagramPost | None) -> str | None:
    """What to call the post in a list: its summary, else its caption's
    first line, else the kind of post it is."""
    if post is None:
        return None
    if post.summary:
        return post.summary
    if post.caption:
        return post.caption.strip().splitlines()[0][:140]
    return "Video" if post.media_type == "VIDEO" else "Post"


def post_out(post: InstagramPost) -> PostOut:
    return PostOut(
        media_id=post.media_id,
        media_type=post.media_type,
        caption=post.caption,
        summary=post.summary,
        permalink=post.permalink,
        posted_at=post.posted_at,
        has_cover=bool(post.thumbnail_url),
        transcript_status=post.transcript_status,
    )


# What a patient says when they are done, not when they are asking: these
# are skipped when looking for why a conversation is waiting on a person.
_CLOSING_WORDS = frozenset(
    {
        "rahmat", "raxmat", "rahmat aka", "katta rahmat", "xop", "xo'p", "hop", "ho'p", "ok",
        "okey", "ha", "xa", "mayli", "yaxshi", "tushundim", "chundim", "rahmat sizga",
        "раҳмат", "рахмат", "хоп", "хўп", "ха", "ҳа", "майли", "спасибо", "хорошо", "ок",
    }
)  # fmt: skip


def _says_something(text: str) -> bool:
    if text.startswith((message_labels.REACTION, message_labels.SHARE)):
        return False
    plain = " ".join(re.sub(r"[^\w' ]", " ", text.lower().replace("’", "'")).split())
    letters = sum(ch.isalpha() for ch in plain)
    return letters >= 3 and plain not in _CLOSING_WORDS


async def _waiting_reasons(
    session: AsyncSession, conversation_ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    """Each pinned conversation's newest patient message with substance."""
    if not conversation_ids:
        return {}
    recent = (
        await session.execute(
            select(Message.conversation_id, Message.content)
            .where(
                Message.conversation_id.in_(conversation_ids),
                Message.sender == MessageSender.PATIENT,
            )
            .order_by(Message.created_at.desc())
            .limit(40 * len(conversation_ids))
        )
    ).all()
    reasons: dict[uuid.UUID, str] = {}
    for conversation_id, content in recent:
        if conversation_id not in reasons and _says_something(content or ""):
            reasons[conversation_id] = preview(content)
    return reasons


@router.get("/counts", response_model=ConversationCounts)
async def conversation_counts(
    operator: Operator = Depends(get_current_operator),
    session: AsyncSession = Depends(get_db_session),
) -> ConversationCounts:
    """How many conversations staff have, for the filter on the inbox.

    Counted here rather than by fetching the filtered list: the inbox polls
    every few seconds, and a list of five hundred summaries to read one
    number off it would be the most expensive request on the page.
    """
    tenant_id = get_current_tenant()
    # Every status, as the list counts them: the badge and the rows it opens
    # must agree.
    open_here = (Conversation.tenant_id == tenant_id,)
    taken_over = await session.scalar(
        select(func.count())
        .select_from(Conversation)
        .where(*open_here, Conversation.is_bot_enabled.is_(False))
    )
    waiting = await session.scalar(
        select(func.count())
        .select_from(Conversation)
        .where(*open_here, Conversation.needs_doctor_since.is_not(None))
    )
    commented = await session.scalar(
        select(func.count()).select_from(Conversation).where(*open_here, _commented())
    )
    bot = await session.scalar(
        select(func.count()).select_from(Conversation).where(*open_here, _bot_alone())
    )
    return ConversationCounts(
        taken_over=taken_over or 0,
        needs_doctor=waiting or 0,
        comments=commented or 0,
        bot_only=bot or 0,
    )


def _bot_alone() -> Any:
    """Only the assistant has answered here: it is on, no person from the
    clinic ever wrote, and the patient came through Direct, not a comment.
    The "Chat bot" view -- what the bot is doing on its own (owner, 2026-10-20)."""
    return (
        Conversation.is_bot_enabled.is_(True)
        & ~exists().where(
            Message.conversation_id == Conversation.id,
            Message.sender == MessageSender.OPERATOR,
        )
        & ~_commented()
        & exists().where(
            Message.conversation_id == Conversation.id,
            Message.sender == MessageSender.BOT,
        )
    )


def _commented() -> Any:
    """The conversation has an Instagram comment from the patient in it --
    somebody who wrote under a post, whether or not they went on to Direct."""
    return exists().where(
        Message.conversation_id == Conversation.id,
        Message.sender == MessageSender.PATIENT,
        Message.content.startswith(message_labels.COMMENT_PREFIX),
    )


@router.get("/by-post", response_model=list[PostCommentGroup])
async def conversations_by_post(
    operator: Operator = Depends(get_current_operator),
    session: AsyncSession = Depends(get_db_session),
) -> list[PostCommentGroup]:
    """The posts people commented under, most commented first: which video
    is bringing the questions, and where to answer them."""
    tenant_id = get_current_tenant()
    # One expression, named once: Postgres groups by the very expression it
    # selects, and two renderings of meta->>'post_media_id' did not match
    # ("must appear in the GROUP BY clause", 2026-10-20).
    media_id = Message.meta["post_media_id"].astext.label("media_id")
    conversations = func.count(func.distinct(Message.conversation_id)).label("conversations")
    counted = (
        await session.execute(
            select(media_id, conversations)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.tenant_id == tenant_id,
                Message.sender == MessageSender.PATIENT,
                Message.meta["post_media_id"].astext.is_not(None),
            )
            .group_by(media_id)
            .order_by(conversations.desc())
        )
    ).all()
    posts = (
        {
            post.media_id: post
            for post in (
                await session.execute(
                    select(InstagramPost).where(
                        InstagramPost.tenant_id == tenant_id,
                        InstagramPost.media_id.in_([media_id for media_id, _ in counted]),
                    )
                )
            ).scalars()
        }
        if counted
        else {}
    )
    return [
        PostCommentGroup(post=post_out(posts[media_id]), comments=n)
        for media_id, n in counted
        if media_id in posts
    ]


@router.get("", response_model=list[ConversationSummary])
async def list_conversations(
    operator: Operator = Depends(get_current_operator),
    session: AsyncSession = Depends(get_db_session),
    status_filter: str | None = Query(default=None, alias="status"),
    only_taken_over: bool = Query(default=False),
    needs_doctor: bool = Query(default=False),
    from_comments: bool = Query(default=False),
    bot_only: bool = Query(default=False),
    post_media_id: str | None = Query(default=None, max_length=64),
    q: str | None = Query(default=None, max_length=100),
    limit: int = Query(default=50, ge=1, le=500),
) -> list[ConversationSummary]:
    tenant_id = get_current_tenant()
    # When each conversation last heard anything, from either side. The list
    # used to be ordered by conversations.updated_at, which nothing touches
    # when a message arrives -- only toggling the bot or closing a thread
    # moves it -- so a patient writing right now could sit halfway down the
    # screen under people who wrote yesterday. The inbox is read top-down;
    # the newest message is what belongs at the top.
    #
    # Scoped to this tenant's conversations inside the aggregate, so the
    # grouping never walks another clinic's messages.
    last_activity = (
        select(
            Message.conversation_id.label("conversation_id"),
            func.max(Message.created_at).label("last_at"),
        )
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.tenant_id == tenant_id)
        .group_by(Message.conversation_id)
        .subquery()
    )
    stmt = (
        select(Conversation)
        .outerjoin(last_activity, last_activity.c.conversation_id == Conversation.id)
        .where(Conversation.tenant_id == tenant_id)
    )
    if status_filter:
        stmt = stmt.where(Conversation.status == status_filter)
    if only_taken_over:
        stmt = stmt.where(Conversation.is_bot_enabled.is_(False))
    if needs_doctor:
        stmt = stmt.where(Conversation.needs_doctor_since.is_not(None))
    if from_comments:
        stmt = stmt.where(_commented())
    if bot_only:
        stmt = stmt.where(_bot_alone())
    if post_media_id:
        # The comments under one post: the "by video" strip on the inbox.
        stmt = stmt.where(
            exists().where(
                Message.conversation_id == Conversation.id,
                Message.sender == MessageSender.PATIENT,
                Message.meta["post_media_id"].astext == post_media_id,
            )
        )
    if q and q.strip():
        # Matched in the database rather than in the dashboard, because the
        # list is capped at `limit`: filtering what was already fetched would
        # search the most recent 50 conversations and quietly miss the patient
        # who wrote last week -- which is the one somebody is searching for.
        #
        # The handle first, since that is what the operator is typing, then
        # the display name, then the platform id: an operator who has an id
        # from somewhere else can still paste it. The leading "@" people type
        # out of habit is stripped, and matching is case-insensitive because
        # Instagram handles are lowercase and nobody types them that way.
        #
        # And by telephone, on its digits: the number is what the front desk
        # has in hand from a callback request or the spreadsheet, written
        # whichever way. It is on the patient, or on a lead they left here.
        matches = [
            search.contains(User.username, q),
            search.contains(User.name, q),
            search.contains(User.external_id, q),
        ]
        digits = search.phone_digits(q)
        if digits:
            matches += [
                search.phone_contains(User.phone, digits),
                exists().where(
                    Lead.tenant_id == tenant_id,
                    or_(
                        Lead.conversation_id == Conversation.id,
                        Lead.user_id == Conversation.user_id,
                    ),
                    search.phone_contains(Lead.phone, digits),
                ),
            ]
        stmt = stmt.join(User, User.id == Conversation.user_id).where(or_(*matches))
    # A conversation with no messages yet sorts by when it was opened, and
    # the id breaks exact ties so a refresh never shuffles two rows that
    # arrived in the same instant -- the dashboard polls this, and rows that
    # swap places on every poll read as the screen twitching.
    # Two groups, each newest first. On top, the red rows: waiting on a
    # person since less than two days ago. Below them everything else, by
    # its last message. Pinned rows used to go first oldest-first, and with
    # forty of them the inbox opened on day-old "Rahmat!" while the patient
    # writing now sat a page down (owner, 2026-10-17). A pin older than two
    # days is shown grey on the dashboard and takes its place in time.
    fresh_pin = Conversation.needs_doctor_since >= datetime.now(UTC) - STALE_PIN
    stmt = stmt.order_by(
        case((fresh_pin, 0), else_=1),
        func.coalesce(last_activity.c.last_at, Conversation.created_at).desc(),
        Conversation.id.desc(),
    ).limit(limit)

    conversations = list((await session.execute(stmt)).scalars())
    return await _summaries(session, conversations)


async def _load(session: AsyncSession, conversation_id: uuid.UUID) -> Conversation:
    conversation = await ConversationRepository(session).get(conversation_id)
    if conversation is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Suhbat topilmadi")
    return conversation


@router.get("/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: uuid.UUID,
    operator: Operator = Depends(get_current_operator),
    session: AsyncSession = Depends(get_db_session),
) -> ConversationDetail:
    conversation = await _load(session, conversation_id)
    messages = await MessageRepository(session).list_recent(conversation.id, TRANSCRIPT_LIMIT)
    [summary] = await _summaries(session, [conversation])
    media_ids = {
        m.meta.get("post_media_id") for m in messages if m.meta and m.meta.get("post_media_id")
    }
    posts = (
        {
            post.media_id: post_out(post)
            for post in (
                await session.execute(
                    select(InstagramPost).where(
                        InstagramPost.tenant_id == get_current_tenant(),
                        InstagramPost.media_id.in_(media_ids),
                    )
                )
            ).scalars()
        }
        if media_ids
        else {}
    )
    attachments = await _attachments(session, conversation.id, messages)
    return ConversationDetail(
        conversation=summary,
        messages=[
            MessageOut(
                id=message.id,
                sender=message.sender,
                content=message.content,
                channel=message.channel,
                created_at=message.created_at,
                delivery_status=message.delivery_status,
                delivery_error=message.delivery_error,
                post=posts.get(str((message.meta or {}).get("post_media_id") or "")),
                attachments=attachments.get(message.id, []),
            )
            for message in messages
        ],
    )


# How long after a "📷 Rasm yubordi" line a photo stored before attachments
# were linked to their message can have been saved and still be its photo.
_LEGACY_PHOTO_WINDOW = timedelta(minutes=10)
_PHOTO_COUNT = re.compile(r"\((\d+) ta\)")


async def _attachments(
    session: AsyncSession, conversation_id: uuid.UUID, messages: Sequence[Message]
) -> dict[uuid.UUID, list[AttachmentOut]]:
    """What each message carried, by message id.

    Since 2026-10-24 a message names its attachments in its meta. Photos from
    before then were stored on their own, linked to the conversation but not
    to the message; they are put back under the "📷 Rasm yubordi" line they
    belong to by time, so older conversations show their photos too.
    """
    rows = (
        (
            await session.execute(
                select(PatientMedia)
                .where(
                    PatientMedia.tenant_id == get_current_tenant(),
                    PatientMedia.conversation_id == conversation_id,
                )
                .order_by(PatientMedia.created_at)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return {}
    by_id = {row.id: row for row in rows}

    def out(row: PatientMedia) -> AttachmentOut:
        return AttachmentOut(
            id=row.id,
            kind=row.kind,
            stored=row.size_bytes is not None,
            content_type=row.content_type,
            title=row.title,
        )

    found: dict[uuid.UUID, list[AttachmentOut]] = {}
    linked: set[uuid.UUID] = set()
    for message in messages:
        for item in (message.meta or {}).get("attachments") or []:
            try:
                row = by_id.get(uuid.UUID(str(item.get("id"))))
            except (ValueError, AttributeError):
                row = None
            if row is not None:
                found.setdefault(message.id, []).append(out(row))
                linked.add(row.id)

    loose = [r for r in rows if r.id not in linked and r.kind == MediaKind.IMAGE]
    for message in messages:
        if not loose or message.id in found or message.sender != MessageSender.PATIENT:
            continue
        if not message.content.startswith("📷"):
            continue
        count = _PHOTO_COUNT.search(message.content)
        wanted = int(count.group(1)) if count else 1
        mine = [
            r
            for r in loose
            if message.created_at - timedelta(seconds=5)
            <= r.created_at
            <= message.created_at + _LEGACY_PHOTO_WINDOW
        ][:wanted]
        if mine:
            found[message.id] = [out(r) for r in mine]
            loose = [r for r in loose if r not in mine]
    return found


@router.post(
    "/{conversation_id}/bot",
    response_model=ConversationSummary,
    dependencies=[Depends(verify_csrf_header)],
)
async def set_bot_enabled(
    conversation_id: uuid.UUID,
    payload: BotToggle,
    operator: Operator = Depends(require_patient_access),
    session: AsyncSession = Depends(get_db_session),
) -> ConversationSummary:
    """Hand the conversation to a human, or hand it back.

    Turning the bot off does not close the conversation or stop recording
    it — the patient's messages keep reaching the transcript, which is what
    the operator reads in order to answer.
    """
    conversation = await _load(session, conversation_id)
    await ConversationRepository(session).update(
        conversation, is_bot_enabled=payload.is_bot_enabled
    )
    await session.commit()
    # updated_at is filled by the database's own onupdate, so the in-memory
    # row is stale here. Refreshed explicitly rather than left to load
    # lazily while the response is being built, which would be IO outside
    # the awaited path.
    await session.refresh(conversation)
    [summary] = await _summaries(session, [conversation])
    return summary


@router.post(
    "/{conversation_id}/resolve",
    response_model=ConversationSummary,
    dependencies=[Depends(verify_csrf_header)],
)
async def resolve_conversation(
    conversation_id: uuid.UUID,
    operator: Operator = Depends(require_patient_access),
    session: AsyncSession = Depends(get_db_session),
) -> ConversationSummary:
    """Unpin a conversation the patient was answered in some other way -- the
    doctor rang them, or they came in."""
    conversation = await _load(session, conversation_id)
    await handoff.clear(session, conversation.id)
    await session.commit()
    await session.refresh(conversation)
    [summary] = await _summaries(session, [conversation])
    return summary


@router.post(
    "/{conversation_id}/reply",
    response_model=MessageOut,
    dependencies=[Depends(verify_csrf_header)],
)
async def reply_as_operator(
    conversation_id: uuid.UUID,
    payload: OperatorReply,
    operator: Operator = Depends(require_patient_access),
    session: AsyncSession = Depends(get_db_session),
) -> MessageOut:
    """Send a message to the patient as the clinic, over whichever channel
    they wrote in on.

    Goes out through the same delivery service the bot uses, so it reaches
    Instagram or Telegram without this route knowing which, and is routed
    by the context captured from the patient's own last message — for a
    Telegram Business conversation, back over that same connection.
    """
    conversation = await _load(session, conversation_id)
    user = await session.get(User, conversation.user_id)
    if user is None:  # pragma: no cover - conversation.user_id is a FK
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Bemor topilmadi")

    delivered_over = await send_reply(
        session,
        channel_id=user.channel_id,
        recipient_external_id=user.external_id,
        text=payload.text,
        last_user_message_at=datetime.now(UTC),
        reply_context=await reply_context_for(session, conversation.id),
    )
    if delivered_over is None:
        # The platform refused or the channel is unusable. Nothing is
        # recorded: a message in the transcript the patient never received
        # would have the operator believe they had answered.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Xabar yuborilmadi — kanal sozlanmagan yoki javob oynasi yopilgan",
        )

    message = await record_outbound_message(
        session,
        conversation_id=conversation.id,
        channel_type=delivered_over,
        text=payload.text,
        sender=MessageSender.OPERATOR,
    )
    await handoff.clear(session, conversation.id)
    # A person has answered: the assistant steps back from this conversation
    # until someone presses "Botga qaytarish", so it never talks over them.
    if conversation.is_bot_enabled:
        conversation.is_bot_enabled = False
        await session.flush()
    await session.commit()
    return MessageOut(
        id=message.id,
        sender=message.sender,
        content=message.content,
        channel=message.channel,
        created_at=message.created_at,
        delivery_status=message.delivery_status,
        delivery_error=message.delivery_error,
    )
