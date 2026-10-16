"""A conversation the assistant hands to the doctor is pinned until a person
answers, and only a person answering unpins it.

Driven through the real turn and the real echo job against the test
database; the model is a scripted fake.
"""

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.message import MessageSender
from app.repositories.message import MessageRepository
from app.services import turn
from app.workers.tasks import note_clinic_reply
from tests.conftest import Seed
from tests.test_conversation_memory import NoEmbeddings, ScriptedLLM, _settings


async def _respond(session: AsyncSession, seed: Seed, reply: str) -> turn.TurnResult:
    llm = ScriptedLLM()
    llm.say(reply)
    return await turn.respond(
        session,
        conversation_id=seed.a.conversation.id,
        user_id=seed.a.user.id,
        message="Nega bunday bo'lyapti, doktor o'zi aytsin",
        history=[],
        source="instagram",
        settings=_settings(),  # type: ignore[arg-type]
        llm_provider=llm,
        embedding_provider=NoEmbeddings(),
    )


async def _flag_state(session: AsyncSession, seed: Seed) -> datetime | None:
    conversation = await session.get(Conversation, seed.a.conversation.id)
    assert conversation is not None
    await session.refresh(conversation)
    return conversation.needs_doctor_since


def _sessions(session: AsyncSession) -> Callable[[], AbstractContextManager[AsyncSession]]:
    @asynccontextmanager
    async def factory() -> AsyncIterator[AsyncSession]:
        yield session

    return factory  # type: ignore[return-value]


async def _echo(session: AsyncSession, seed: Seed, text: str | None) -> None:
    await note_clinic_reply(
        {},
        str(seed.tenant_a.id),
        str(seed.a.channel.id),
        seed.a.user.external_id,
        text,
        session_factory=_sessions(session),
    )


# --- raising the flag ---------------------------------------------------------


async def test_a_marked_handover_pins_the_conversation_and_hides_the_marker(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
) -> None:
    with as_tenant(seed.tenant_a.id):
        result = await _respond(db_session, seed, "Savolingizni doktorga yetkazaman. [[DOCTOR]]")
        since = await _flag_state(db_session, seed)

    assert result.reply == "Savolingizni doktorga yetkazaman."
    assert since is not None


async def test_a_malformed_marker_never_reaches_the_patient(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
) -> None:
    with as_tenant(seed.tenant_a.id):
        result = await _respond(db_session, seed, "Doktor javob beradi [[DOCTOR: sabab so'raldi]")
        since = await _flag_state(db_session, seed)

    assert "[" not in result.reply and "DOCTOR" not in result.reply
    assert since is not None


async def test_an_ordinary_reply_pins_nothing(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
) -> None:
    with as_tenant(seed.tenant_a.id):
        result = await _respond(
            db_session, seed, "Buni ko'rmasdan aytib bo'lmaydi, qabulga yozaymi?"
        )
        since = await _flag_state(db_session, seed)

    assert result.reply == "Buni ko'rmasdan aytib bo'lmaydi, qabulga yozaymi?"
    assert since is None


async def test_a_second_handover_keeps_the_time_it_first_started_waiting(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
) -> None:
    with as_tenant(seed.tenant_a.id):
        await _respond(db_session, seed, "Doktorning o'zi javob beradi. [[DOCTOR]]")
        first = await _flag_state(db_session, seed)
        await _respond(db_session, seed, "Doktorning o'zi javob beradi. [[DOCTOR]]")
        second = await _flag_state(db_session, seed)

    assert first is not None and first == second


# --- the doctor answering from the Instagram app --------------------------------


async def _pin_after_bot_said(session: AsyncSession, seed: Seed, bot_said: str) -> None:
    conversation = await session.get(Conversation, seed.a.conversation.id)
    assert conversation is not None
    conversation.needs_doctor_since = datetime.now(UTC) - timedelta(minutes=5)
    await MessageRepository(session).create(
        conversation_id=seed.a.conversation.id,
        sender=MessageSender.BOT,
        content=bot_said,
        channel="instagram",
    )
    await session.flush()


async def test_the_echo_of_the_assistants_own_message_keeps_the_pin(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
) -> None:
    with as_tenant(seed.tenant_a.id):
        await _pin_after_bot_said(db_session, seed, "Savolingizni doktorga yetkazaman.")
        await _echo(db_session, seed, "  Savolingizni doktorga   yetkazaman. ")
        assert await _flag_state(db_session, seed) is not None


async def test_the_doctor_typing_in_the_app_unpins_it(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
) -> None:
    with as_tenant(seed.tenant_a.id):
        await _pin_after_bot_said(db_session, seed, "Savolingizni doktorga yetkazaman.")
        await _echo(db_session, seed, "Assalomu alaykum, men doktor Temur. Ertaga kelib ko'ring.")
        assert await _flag_state(db_session, seed) is None


async def test_a_short_reply_contained_in_a_bot_message_still_counts_as_a_person(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
) -> None:
    """ "Ha" appears inside plenty of the assistant's sentences; it is still
    the doctor answering."""
    with as_tenant(seed.tenant_a.id):
        await _pin_after_bot_said(db_session, seed, "Ha, albatta, doktorga yetkazaman.")
        await _echo(db_session, seed, "Ha")
        assert await _flag_state(db_session, seed) is None


async def test_a_photo_from_the_app_unpins_it(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
) -> None:
    with as_tenant(seed.tenant_a.id):
        await _pin_after_bot_said(db_session, seed, "Savolingizni doktorga yetkazaman.")
        await _echo(db_session, seed, None)
        assert await _flag_state(db_session, seed) is None


async def test_a_person_writing_in_the_app_takes_the_conversation_off_the_bot(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
) -> None:
    """The doctor (2026-10-16): when I write to a patient myself, the bot
    should stop on its own. The message is kept in the transcript too."""
    with as_tenant(seed.tenant_a.id):
        await _echo(db_session, seed, "Salom, men doktor Temur. Ertaga kelavering.")
        conversation = await db_session.get(Conversation, seed.a.conversation.id)
        assert conversation is not None
        await db_session.refresh(conversation)
        assert conversation.is_bot_enabled is False
        assert await _flag_state(db_session, seed) is None
        messages = await MessageRepository(db_session).list_for_conversation(seed.a.conversation.id)
        assert any(
            m.sender == MessageSender.OPERATOR and m.content.startswith("Salom, men doktor")
            for m in messages
        )


async def test_the_bots_own_echo_leaves_it_answering(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
) -> None:
    from app.services.conversation import record_outbound_message

    with as_tenant(seed.tenant_a.id):
        await record_outbound_message(
            db_session,
            conversation_id=seed.a.conversation.id,
            channel_type="instagram",
            text="Doktor qabuli 300 000 soʻm.",
        )
        await db_session.flush()
        await _echo(db_session, seed, "Doktor qabuli  300 000 soʻm.")
        conversation = await db_session.get(Conversation, seed.a.conversation.id)
        assert conversation is not None
        await db_session.refresh(conversation)
        assert conversation.is_bot_enabled is True


async def test_an_echo_to_somebody_who_never_wrote_is_ignored(
    db_session: AsyncSession,
    seed: Seed,
) -> None:
    await note_clinic_reply(
        {},
        str(seed.tenant_a.id),
        str(seed.a.channel.id),
        "nobody-with-this-id",
        "Salom",
        session_factory=_sessions(db_session),
    )


# --- voice notes ---------------------------------------------------------------


async def _voice(session: AsyncSession, seed: Seed, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    import app.workers.tasks as tasks

    sent: list[str] = []

    async def deliver(*args: object, **kwargs: object) -> str:
        sent.append(str(kwargs.get("text")))
        return "instagram"

    monkeypatch.setattr(tasks, "send_reply", deliver)
    await tasks.answer_voice_note(
        {},
        str(seed.tenant_a.id),
        str(seed.a.channel.id),
        str(seed.a.conversation.id),
        seed.a.user.external_id,
        session_factory=_sessions(session),
    )
    return sent


async def test_a_voice_note_pins_the_conversation_for_somebody_to_listen(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with as_tenant(seed.tenant_a.id):
        sent = await _voice(db_session, seed, monkeypatch)
        since = await _flag_state(db_session, seed)

    assert len(sent) == 1
    assert since is not None


async def test_two_voice_notes_in_a_row_get_the_line_once(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A patient who sent two voice notes a minute apart got the same fixed
    line twice."""
    with as_tenant(seed.tenant_a.id):
        first = await _voice(db_session, seed, monkeypatch)
        second = await _voice(db_session, seed, monkeypatch)

    assert len(first) == 1
    assert second == []
