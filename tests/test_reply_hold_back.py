"""A written reply is held back when somebody answered, or the patient wrote
more, while the model was thinking -- and an automatic reply from the
account does not switch the assistant off.

Live, 2026-10-16: two answers to one question; the assistant's reply under
the operator's "Murojaat uchun: ☎️..."; and every patient who tapped "Siz
bilan qanday bog'lansam bo'ladi?" left with the assistant switched off by
Instagram's own automatic answer.
"""

import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.message import MessageSender
from app.rag.llm import ChatMessage
from app.repositories.message import MessageRepository
from app.services import clinic_echoes
from app.workers.tasks import note_clinic_reply, process_inbound_message
from tests.conftest import Seed
from tests.test_worker_tasks import (
    MODEL_REPLY,
    QUERY_VECTOR,
    SENDER,
    FakeEmbeddingProvider,
    FakeLLMProvider,
    _fake_adapter,
    _session_factory,
)

AsTenant = Callable[[uuid.UUID], AbstractContextManager[None]]


class FakeRedis:
    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}

    async def rpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).append(value)

    async def ltrim(self, key: str, start: int, end: int) -> None:
        self.lists[key] = self.lists.get(key, [])[start:]

    async def expire(self, key: str, seconds: int) -> None:
        return None

    async def lrange(self, key: str, start: int, end: int) -> list[str]:
        return list(self.lists.get(key, []))


async def _message(
    session: AsyncSession, seed: Seed, sender: MessageSender, text: str, at: datetime
) -> None:
    message = await MessageRepository(session).create(
        conversation_id=seed.a.conversation.id, sender=sender, content=text, channel="instagram"
    )
    message.created_at = at
    await session.flush()


async def _answer(
    session: AsyncSession, seed: Seed, llm: FakeLLMProvider, redis: Any = None
) -> list[tuple[str, str, str]]:
    adapter, client = _fake_adapter()
    await process_inbound_message(
        {"redis": redis} if redis is not None else {},
        str(seed.tenant_a.id),
        str(seed.a.channel.id),
        str(seed.a.conversation.id),
        SENDER,
        "Davosi bormi?",
        session_factory=_session_factory(session),
        embedding_provider=FakeEmbeddingProvider(QUERY_VECTOR),
        llm_provider=llm,
        adapter=adapter,
    )
    return client.calls


def _while_thinking(action: Callable[[], Any]) -> FakeLLMProvider:
    class Thinking(FakeLLMProvider):
        async def generate(self, system_prompt: str, messages: list[ChatMessage]) -> str:
            await action()
            return await super().generate(system_prompt, messages)

    return Thinking(reply=MODEL_REPLY)


async def test_a_reply_waits_when_the_patient_wrote_another_question(
    db_session: AsyncSession, seed: Seed, as_tenant: AsTenant
) -> None:
    asked = datetime.now(UTC) + timedelta(minutes=1)  # after the seeded messages
    with as_tenant(seed.tenant_a.id):
        await _message(db_session, seed, MessageSender.PATIENT, "Davosi bormi?", asked)

    async def more() -> None:
        with as_tenant(seed.tenant_a.id):
            await _message(
                db_session,
                seed,
                MessageSender.PATIENT,
                "Narxi qancha?",
                asked + timedelta(seconds=40),
            )

    assert await _answer(db_session, seed, _while_thinking(more)) == []


async def test_a_thank_you_meanwhile_does_not_hold_the_answer(
    db_session: AsyncSession, seed: Seed, as_tenant: AsTenant
) -> None:
    asked = datetime.now(UTC) + timedelta(minutes=1)  # after the seeded messages
    with as_tenant(seed.tenant_a.id):
        await _message(db_session, seed, MessageSender.PATIENT, "Davosi bormi?", asked)

    async def thanks() -> None:
        with as_tenant(seed.tenant_a.id):
            await _message(
                db_session, seed, MessageSender.PATIENT, "rahmat", asked + timedelta(seconds=40)
            )

    sent = await _answer(db_session, seed, _while_thinking(thanks))
    assert [text for _, _, text in sent] == [MODEL_REPLY]


async def test_a_reply_waits_when_the_clinic_answered_meanwhile(
    db_session: AsyncSession, seed: Seed, as_tenant: AsTenant
) -> None:
    asked = datetime.now(UTC) + timedelta(minutes=1)  # after the seeded messages
    with as_tenant(seed.tenant_a.id):
        await _message(db_session, seed, MessageSender.PATIENT, "Davosi bormi?", asked)
    redis = FakeRedis()

    async def operator_types() -> None:
        # Seen at once by the webhook, recorded by the echo job only later.
        await clinic_echoes.note(
            redis,
            str(seed.a.channel.id),
            SENDER,
            "Murojaat uchun: ☎️+998-93-051-25-96",
            asked + timedelta(seconds=10),
        )

    assert await _answer(db_session, seed, _while_thinking(operator_types), redis) == []


async def test_the_assistants_own_echo_does_not_hold_its_next_reply(
    db_session: AsyncSession, seed: Seed, as_tenant: AsTenant
) -> None:
    asked = datetime.now(UTC) + timedelta(minutes=1)  # after the seeded messages
    with as_tenant(seed.tenant_a.id):
        await _message(
            db_session, seed, MessageSender.BOT, "Avvalgi javob", asked - timedelta(seconds=5)
        )
        await _message(db_session, seed, MessageSender.PATIENT, "Davosi bormi?", asked)
    redis = FakeRedis()
    await clinic_echoes.note(redis, str(seed.a.channel.id), SENDER, "Avvalgi javob")

    sent = await _answer(db_session, seed, FakeLLMProvider(reply=MODEL_REPLY), redis)
    assert [text for _, _, text in sent] == [MODEL_REPLY]


async def test_an_automatic_reply_does_not_switch_the_assistant_off(
    db_session: AsyncSession, seed: Seed, as_tenant: AsTenant
) -> None:
    auto = "Murojaat uchun: / ☎️+998-93-051-25-96"
    asked = datetime.now(UTC) - timedelta(seconds=5)
    with as_tenant(seed.tenant_a.id):
        await _message(
            db_session, seed, MessageSender.PATIENT, "Siz bilan qanday bog'lansam bo'ladi?", asked
        )
    # The same words already sent to two other patients.
    await _same_words_elsewhere(db_session, seed, as_tenant, auto)

    await note_clinic_reply(
        {},
        str(seed.tenant_a.id),
        str(seed.a.channel.id),
        seed.a.user.external_id,
        auto,
        sent_at=(asked + timedelta(seconds=3)).isoformat(),
        session_factory=_session_factory(db_session),
    )

    conversation = await db_session.get(Conversation, seed.a.conversation.id)
    assert conversation is not None
    await db_session.refresh(conversation)
    assert conversation.is_bot_enabled is True


async def test_a_person_answering_still_switches_the_assistant_off(
    db_session: AsyncSession, seed: Seed, as_tenant: AsTenant
) -> None:
    asked = datetime.now(UTC) - timedelta(minutes=3)
    with as_tenant(seed.tenant_a.id):
        await _message(db_session, seed, MessageSender.PATIENT, "Qachon kelay?", asked)

    await note_clinic_reply(
        {},
        str(seed.tenant_a.id),
        str(seed.a.channel.id),
        seed.a.user.external_id,
        "Ertaga 10 da keling, aka",
        sent_at=(asked + timedelta(minutes=2)).isoformat(),
        session_factory=_session_factory(db_session),
    )

    conversation = await db_session.get(Conversation, seed.a.conversation.id)
    assert conversation is not None
    await db_session.refresh(conversation)
    assert conversation.is_bot_enabled is False


async def _same_words_elsewhere(
    session: AsyncSession, seed: Seed, as_tenant: AsTenant, text: str
) -> None:
    """Two other patients of the same clinic already sent `text` by the account."""
    from app.repositories.conversation import ConversationRepository
    from app.repositories.user import UserRepository

    with as_tenant(seed.tenant_a.id):
        for n in range(2):
            user = await UserRepository(session).create(
                channel_id=seed.a.channel.id, external_id=f"other-{n}"
            )
            conversation = await ConversationRepository(session).create(
                user_id=user.id, status="open"
            )
            await MessageRepository(session).create(
                conversation_id=conversation.id,
                sender=MessageSender.OPERATOR,
                content=text,
                channel="instagram",
            )
        await session.flush()
