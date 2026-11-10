"""The shared conversation store — the layer both bots record their traffic through."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import MessageSender
from app.repositories.base import CrossTenantAccessError
from app.repositories.conversation import ConversationRepository
from app.repositories.message import MessageRepository
from app.repositories.user import UserRepository
from app.services.conversation import (
    context_for_reply,
    hours_since_last_contact,
    last_inbound_at,
    recent_history,
    record_outbound_message,
    register_inbound_message,
    with_unanswered,
)
from tests.conftest import Seed


async def test_first_contact_creates_the_patient_conversation_and_message(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> None:
    with as_tenant(seed.tenant_a.id):
        inbound = await register_inbound_message(
            db_session,
            channel_id=seed.a.channel.id,
            channel_type="instagram",
            sender_external_id="brand-new",
            text="Assalom alaykum",
        )

        user = await UserRepository(db_session).get_by_external_id(
            channel_id=seed.a.channel.id, external_id="brand-new"
        )
        assert user is not None and user.id == inbound.user_id
        conversation = await ConversationRepository(db_session).get_open_for_user(user.id)
        assert conversation is not None and conversation.id == inbound.conversation_id
        # A brand-new conversation answers by default; takeover is an
        # operator turning it off, not something to opt into.
        assert inbound.is_bot_enabled is True

        messages = await MessageRepository(db_session).list_recent(inbound.conversation_id, 10)

    assert [(m.sender, m.content) for m in messages] == [(MessageSender.PATIENT, "Assalom alaykum")]


async def test_a_returning_patient_reuses_their_user_and_conversation(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> None:
    with as_tenant(seed.tenant_a.id):
        first = await register_inbound_message(
            db_session,
            channel_id=seed.a.channel.id,
            channel_type="instagram",
            sender_external_id="returning",
            text="Salom",
        )
        second = await register_inbound_message(
            db_session,
            channel_id=seed.a.channel.id,
            channel_type="instagram",
            sender_external_id="returning",
            text="narxi qancha?",
        )

    assert first.user_id == second.user_id
    assert first.conversation_id == second.conversation_id


async def test_the_same_platform_id_on_two_channels_is_two_patients(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> None:
    """Platform ids are unique only within their own account. Two people
    holding the same id string on two channels must not be merged into one
    patient with one transcript.
    """
    with as_tenant(seed.tenant_a.id):
        from app.repositories.channel import ChannelRepository

        second_channel = await ChannelRepository(db_session).create(
            type="telegram", credentials="tg-token", external_id=f"tg-{seed.tenant_a.id}"
        )
        on_instagram = await register_inbound_message(
            db_session,
            channel_id=seed.a.channel.id,
            channel_type="instagram",
            sender_external_id="12345",
            text="from instagram",
        )
        on_telegram = await register_inbound_message(
            db_session,
            channel_id=second_channel.id,
            channel_type="telegram",
            sender_external_id="12345",
            text="from telegram",
        )

    assert on_instagram.user_id != on_telegram.user_id
    assert on_instagram.conversation_id != on_telegram.conversation_id


async def test_losing_the_first_contact_race_reuses_the_winners_rows(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two deliveries for a patient's first two bubbles can both find no row
    and both insert; the unique constraints decide which one wins.

    A genuinely concurrent version cannot be written against this fixture —
    one AsyncSession forbids concurrent operations, and a second connection
    could not see this test's uncommitted rows — so the loser's branch is
    exercised directly instead: the rows already exist, but the lookups are
    forced to miss once each, exactly as they would for the delivery that
    read a moment before the winner inserted.
    """
    with as_tenant(seed.tenant_a.id):
        winner = await register_inbound_message(
            db_session,
            channel_id=seed.a.channel.id,
            channel_type="instagram",
            sender_external_id="racing",
            text="Salom",
        )

    real_user_lookup = UserRepository.get_by_external_id
    real_conversation_lookup = ConversationRepository.get_open_for_user
    misses = {"user": 0, "conversation": 0}

    async def _user_misses_once(self: UserRepository, **kwargs: object) -> object:
        misses["user"] += 1
        if misses["user"] == 1:
            return None
        return await real_user_lookup(self, **kwargs)  # type: ignore[arg-type]

    async def _conversation_misses_once(self: ConversationRepository, user_id: UUID) -> object:
        misses["conversation"] += 1
        if misses["conversation"] == 1:
            return None
        return await real_conversation_lookup(self, user_id)

    monkeypatch.setattr(UserRepository, "get_by_external_id", _user_misses_once)
    monkeypatch.setattr(ConversationRepository, "get_open_for_user", _conversation_misses_once)

    with as_tenant(seed.tenant_a.id):
        loser = await register_inbound_message(
            db_session,
            channel_id=seed.a.channel.id,
            channel_type="instagram",
            sender_external_id="racing",
            text="bormisiz?",
        )

    # Both deliveries land on one patient and one transcript, and neither
    # message is lost.
    assert loser.user_id == winner.user_id
    assert loser.conversation_id == winner.conversation_id
    monkeypatch.undo()
    with as_tenant(seed.tenant_a.id):
        messages = await MessageRepository(db_session).list_recent(winner.conversation_id, 10)
    assert [m.content for m in messages] == ["Salom", "bormisiz?"]


async def test_register_inbound_message_requires_a_tenant_in_context(
    db_session: AsyncSession, seed: Seed
) -> None:
    """Every write here goes through a tenant-scoped repository, so running
    unscoped must fail loudly rather than write an unattributed row.
    """
    from app.core.tenant_context import TenantContextError

    with pytest.raises(TenantContextError):
        await register_inbound_message(
            db_session,
            channel_id=seed.a.channel.id,
            channel_type="instagram",
            sender_external_id="no-tenant",
            text="Salom",
        )


async def test_cannot_record_onto_another_tenants_conversation(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> None:
    with as_tenant(seed.tenant_a.id), pytest.raises(CrossTenantAccessError):
        await record_outbound_message(
            db_session,
            conversation_id=seed.b.conversation.id,
            channel_type="instagram",
            text="wrong tenant",
        )


# --- history shaping ---


async def test_recent_history_is_oldest_first_and_maps_senders_onto_llm_roles(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> None:
    with as_tenant(seed.tenant_a.id):
        repo = MessageRepository(db_session)
        await repo.create(
            conversation_id=seed.a.conversation.id,
            sender=MessageSender.BOT,
            content="Va alaykum assalom!",
            channel="instagram",
        )
        # An operator's turn is one the clinic already took, so the model
        # must see it as its own side rather than as the patient speaking.
        await repo.create(
            conversation_id=seed.a.conversation.id,
            sender=MessageSender.OPERATOR,
            content="Ertaga 10:00 ga yozdim.",
            channel="instagram",
        )

        history = await recent_history(db_session, seed.a.conversation.id)

    assert history == [
        {"role": "user", "content": "Hello"},  # from the seed fixture
        {"role": "assistant", "content": "Va alaykum assalom!"},
        {"role": "assistant", "content": "Ertaga 10:00 ga yozdim."},
    ]


async def test_recent_history_keeps_the_newest_turns_when_over_the_limit(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> None:
    with as_tenant(seed.tenant_a.id):
        repo = MessageRepository(db_session)
        for index in range(5):
            await repo.create(
                conversation_id=seed.a.conversation.id,
                sender=MessageSender.PATIENT,
                content=f"message {index}",
                channel="instagram",
            )

        history = await recent_history(db_session, seed.a.conversation.id, limit=3)

    assert [turn["content"] for turn in history] == ["message 2", "message 3", "message 4"]


async def test_context_for_reply_drops_the_turns_being_answered(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> None:
    """The tail of the transcript at reply time is the batch about to be
    answered — passing it as context as well would show the model the same
    question twice, once split into bubbles and once joined.
    """
    with as_tenant(seed.tenant_a.id):
        repo = MessageRepository(db_session)
        await repo.create(
            conversation_id=seed.a.conversation.id,
            sender=MessageSender.BOT,
            content="Va alaykum assalom!",
            channel="instagram",
        )
        for bubble in ("implant", "qilasizmi?"):
            await repo.create(
                conversation_id=seed.a.conversation.id,
                sender=MessageSender.PATIENT,
                content=bubble,
                channel="instagram",
            )

        context = await context_for_reply(db_session, seed.a.conversation.id)

    assert context == [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Va alaykum assalom!"},
    ]


async def test_context_for_reply_is_empty_when_the_clinic_has_never_spoken(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> None:
    with as_tenant(seed.tenant_a.id):
        assert await context_for_reply(db_session, seed.a.conversation.id) == []


# --- reply window timestamp ---


async def test_last_inbound_at_ignores_the_clinics_own_messages(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> None:
    """The reply window measures how long since the *patient* wrote. A bot
    reply refreshing it would reopen a window Meta considers closed.
    """
    with as_tenant(seed.tenant_a.id):
        before = await last_inbound_at(db_session, seed.a.conversation.id)
        assert before is not None

        await record_outbound_message(
            db_session,
            conversation_id=seed.a.conversation.id,
            channel_type="instagram",
            text="a later reply",
        )
        after = await last_inbound_at(db_session, seed.a.conversation.id)

    assert after == before


async def test_last_inbound_at_is_none_for_a_conversation_with_no_patient_message(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> None:
    with as_tenant(seed.tenant_a.id):
        user = await UserRepository(db_session).create(
            channel_id=seed.a.channel.id, external_id="silent"
        )
        conversation = await ConversationRepository(db_session).create(
            user_id=user.id, status="open"
        )

        assert await last_inbound_at(db_session, conversation.id) is None


# --- how long since anyone last said anything here -----------------------


async def test_hours_since_last_contact_is_none_on_the_first_message(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> None:
    """Only the seeded patient message exists, and it is the batch about to
    be answered -- trimmed the same way context_for_reply trims it, so
    there is nothing left to measure from."""
    with as_tenant(seed.tenant_a.id):
        gap = await hours_since_last_contact(
            db_session, seed.a.conversation.id, now=datetime.now(UTC)
        )

    assert gap is None


async def test_hours_since_last_contact_measures_from_the_clinics_own_last_word(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> None:
    with as_tenant(seed.tenant_a.id):
        await MessageRepository(db_session).create(
            conversation_id=seed.a.conversation.id,
            sender=MessageSender.BOT,
            content="Va alaykum assalom!",
            channel="instagram",
            created_at=datetime.now(UTC) - timedelta(hours=30),
        )

        recent = await hours_since_last_contact(
            db_session, seed.a.conversation.id, now=datetime.now(UTC)
        )
        long_ago = await hours_since_last_contact(
            db_session,
            seed.a.conversation.id,
            now=datetime.now(UTC) + timedelta(hours=2),
        )

    assert recent == pytest.approx(30, abs=0.1)
    assert long_ago == pytest.approx(32, abs=0.1)


async def test_hours_since_last_contact_ignores_the_trailing_batch_being_answered(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> None:
    """A patient's own follow-up bubbles, sent long after the clinic's last
    reply, must not make the gap measure as zero -- they are the message
    about to be answered, not something already spoken into."""
    with as_tenant(seed.tenant_a.id):
        await MessageRepository(db_session).create(
            conversation_id=seed.a.conversation.id,
            sender=MessageSender.BOT,
            content="Va alaykum assalom!",
            channel="instagram",
            created_at=datetime.now(UTC) - timedelta(hours=48),
        )
        await MessageRepository(db_session).create(
            conversation_id=seed.a.conversation.id,
            sender=MessageSender.PATIENT,
            content="Yana bir savolim bor edi",
            channel="instagram",
        )

        gap = await hours_since_last_contact(
            db_session, seed.a.conversation.id, now=datetime.now(UTC)
        )

    assert gap == pytest.approx(48, abs=0.1)


async def test_unanswered_messages_before_the_batch_are_read_with_it(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> None:
    """Live chat 2026-11-09: "Акмал", then "Денервация нечпул" before any
    reply -- the first job superseded, the second must read both."""
    with as_tenant(seed.tenant_a.id):
        repo = MessageRepository(db_session)
        await repo.create(
            conversation_id=seed.a.conversation.id,
            sender=MessageSender.BOT,
            content="Исмингиз нима?",
            channel="instagram",
        )
        for bubble in ("Акмал", "Денервация нечпул"):
            await repo.create(
                conversation_id=seed.a.conversation.id,
                sender=MessageSender.PATIENT,
                content=bubble,
                channel="instagram",
            )
        joined = await with_unanswered(db_session, seed.a.conversation.id, "Денервация нечпул")
        same = await with_unanswered(db_session, seed.a.conversation.id, "Акмал\nДенервация нечпул")

    assert joined == "Акмал\nДенервация нечпул"
    assert same == "Акмал\nДенервация нечпул"
