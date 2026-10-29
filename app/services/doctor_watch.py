"""What the doctor's bot has to watch: who is waiting, whether the assistant is
answering, which call-back numbers nobody has rung.

Only reads. Nothing here knows about Telegram -- app.services.doctor_telegram
turns these facts into messages and remembers which it has already said, so
the same question ("who is waiting?") has one answer for the morning summary,
the status screen and the escalation alert.
"""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.lead import Lead, LeadStatus
from app.models.message import Message, MessageSender
from app.models.tenant import Tenant
from app.models.user import User
from app.services.weekly_report import _ACKNOWLEDGEMENT, OLD_WAITING, _is_words

# How long a patient waits on a person before the doctor is told. Operators
# answer most chats inside minutes; this is the ones nobody got to.
ESCALATE_AFTER = timedelta(minutes=30)
# The assistant should have answered by now; a patient's last message sitting
# longer than this with the bot on is a sign it is not answering.
BOT_SILENT_AFTER = timedelta(minutes=10)
BOT_SILENT_BEFORE = timedelta(hours=2)
# One slow reply is a slow reply; this many at once is an outage.
BOT_SILENT_COUNT = 3


@dataclass(frozen=True)
class WaitingChat:
    conversation_id: uuid.UUID
    who: str
    since: datetime  # when the chat started needing a person
    said: str  # the patient's last words


@dataclass
class Health:
    bot_on: bool
    waiting: list[WaitingChat] = field(default_factory=list)
    waiting_old: int = 0  # the backlog nobody has touched for over a week
    silent: int = 0  # chats the assistant should have answered and did not
    last_bot_reply: datetime | None = None
    open_leads: int = 0
    oldest_lead: datetime | None = None


def _who(name: str | None, username: str | None) -> str:
    return f"@{username}" if username else (name or "ismi ko'rsatilmagan")


def _last_message_rows(tenant_id: uuid.UUID):  # type: ignore[no-untyped-def]
    """Each conversation with its newest message, joined to the patient."""
    last = (
        select(Message.conversation_id, func.max(Message.created_at).label("at"))
        .group_by(Message.conversation_id)
        .subquery()
    )
    return (
        select(
            Conversation,
            User.name,
            User.username,
            Message.sender,
            Message.content,
            Message.created_at,
        )
        .select_from(Conversation)
        .join(User, User.id == Conversation.user_id)
        .join(last, last.c.conversation_id == Conversation.id)
        .join(
            Message,
            and_(Message.conversation_id == Conversation.id, Message.created_at == last.c.at),
        )
        .where(Conversation.tenant_id == tenant_id)
    )


def _asks(sender: str, content: str) -> bool:
    """The patient spoke last, in words, and is not just saying thanks."""
    if sender != MessageSender.PATIENT or not _is_words(content):
        return False
    stripped = content.strip()
    return not (len(stripped) <= 30 and _ACKNOWLEDGEMENT.match(stripped))


async def waiting_chats(
    session: AsyncSession, tenant: Tenant, now: datetime, *, longer_than: timedelta
) -> tuple[list[WaitingChat], int]:
    """Chats waiting on a person for at least ``longer_than`` (longest first),
    and how many more are an old backlog."""
    rows = (
        await session.execute(
            _last_message_rows(tenant.id).where(Conversation.needs_doctor_since.is_not(None))
        )
    ).all()
    chats: list[WaitingChat] = []
    old = 0
    for conversation, name, username, sender, content, _at in rows:
        since = conversation.needs_doctor_since
        if since is None or now - since < longer_than or not _asks(sender, content):
            continue
        if now - since > OLD_WAITING:
            old += 1
            continue
        chats.append(WaitingChat(conversation.id, _who(name, username), since, content.strip()))
    chats.sort(key=lambda chat: chat.since)
    return chats, old


async def open_leads(session: AsyncSession, tenant: Tenant, *, limit: int = 30) -> list[Lead]:
    """Call-back numbers nobody has marked as rung, newest first."""
    return list(
        (
            await session.execute(
                select(Lead)
                .where(Lead.tenant_id == tenant.id, Lead.status == LeadStatus.NEW)
                .order_by(Lead.created_at.desc())
                .limit(limit)
            )
        ).scalars()
    )


async def health(session: AsyncSession, tenant: Tenant, now: datetime) -> Health:
    result = Health(bot_on=bool(tenant.settings.get("bot_replies_enabled", True)))
    result.waiting, result.waiting_old = await waiting_chats(
        session, tenant, now, longer_than=timedelta(0)
    )

    rows = (
        await session.execute(
            _last_message_rows(tenant.id).where(Conversation.is_bot_enabled.is_(True))
        )
    ).all()
    for conversation, _name, _username, sender, content, at in rows:
        if conversation.needs_doctor_since is not None:
            continue
        if BOT_SILENT_AFTER <= now - at <= BOT_SILENT_BEFORE and _asks(sender, content):
            result.silent += 1

    result.last_bot_reply = await session.scalar(
        select(func.max(Message.created_at))
        .join(Conversation, Conversation.id == Message.conversation_id)
        .where(Conversation.tenant_id == tenant.id, Message.sender == MessageSender.BOT)
    )
    result.open_leads = int(
        await session.scalar(
            select(func.count())
            .select_from(Lead)
            .where(Lead.tenant_id == tenant.id, Lead.status == LeadStatus.NEW)
        )
        or 0
    )
    result.oldest_lead = await session.scalar(
        select(func.min(Lead.created_at)).where(
            Lead.tenant_id == tenant.id, Lead.status == LeadStatus.NEW
        )
    )
    return result
