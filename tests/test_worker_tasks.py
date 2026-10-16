import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, AbstractContextManager, asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from arq.connections import ArqRedis
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.instagram.adapter import InstagramAdapter
from app.channels.instagram.client import InstagramClient
from app.core.encryption import DecryptionError, encrypt
from app.models.message import MessageSender
from app.rag.embeddings import EMBEDDING_DIMENSIONS, EmbeddingProvider
from app.rag.llm import ChatMessage, LLMProvider
from app.repositories.channel import ChannelRepository
from app.repositories.conversation import ConversationRepository
from app.repositories.knowledge_base import KnowledgeBaseRepository
from app.repositories.message import MessageRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.workers.tasks import apply_admin_rule, fire_debounce_window, process_inbound_message
from tests.conftest import Seed, isolated_settings

QUERY_VECTOR = [1.0] + [0.0] * (EMBEDDING_DIMENSIONS - 1)
SENDER = "sender-1"
MODEL_REPLY = "Model reply"


class FakeEmbeddingProvider(EmbeddingProvider):
    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector for _ in texts]


class FakeLLMProvider(LLMProvider):
    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.calls: list[tuple[str, list[ChatMessage]]] = []

    async def generate(self, system_prompt: str, messages: list[ChatMessage]) -> str:
        self.calls.append((system_prompt, messages))
        return self._reply


class FakeInstagramClient(InstagramClient):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []  # (access_token, recipient_igsid, text)
        self.comment_replies: list[tuple[str, str]] = []
        self.private_replies: list[tuple[str, str]] = []

    async def send_text(self, *, access_token: str, recipient_igsid: str, text: str) -> None:
        self.calls.append((access_token, recipient_igsid, text))

    async def fetch_username(self, *, access_token: str, igsid: str) -> str | None:
        # These tests are about answering a message, not labelling it. None
        # is also the honest default: most lookups in production return one.
        return None

    async def reply_to_comment(self, *, access_token: str, comment_id: str, text: str) -> None:
        self.comment_replies.append((comment_id, text))

    async def send_private_reply(self, *, access_token: str, comment_id: str, text: str) -> None:
        self.private_replies.append((comment_id, text))


def _fake_adapter() -> tuple[InstagramAdapter, FakeInstagramClient]:
    """The *real* Instagram adapter over a fake transport.

    Only the HTTP call is faked, so the platform rules the adapter owns —
    the placeholder-credential check and Meta's 24-hour messaging window —
    are genuinely exercised rather than stubbed out alongside it.
    """
    client = FakeInstagramClient()
    return InstagramAdapter(client=client), client


def _session_factory(
    session: AsyncSession,
) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    """Wraps this test's own transactional db_session as the factory
    process_inbound_message calls, instead of it opening a genuinely new
    connection — which couldn't see this test's uncommitted fixture data
    anyway, and would be bound to a different event loop than pytest-asyncio
    hands this test.
    """

    @asynccontextmanager
    async def _factory() -> AsyncIterator[AsyncSession]:
        yield session

    return _factory


async def test_process_inbound_message_runs_under_correct_tenant_and_logs_redacted_reply(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    reply_text = "We're open 9 to 5, Monday to Saturday."
    embedding_provider = FakeEmbeddingProvider(QUERY_VECTOR)
    llm_provider = FakeLLMProvider(reply=reply_text)
    adapter, client = _fake_adapter()

    # Seeded (and left) under tenant A's context, then the job is called
    # with NO ambient tenant set — proving it re-establishes tenant context
    # itself from the plain tenant_id argument, rather than relying on one
    # already bound in the caller's context.
    with as_tenant(seed.tenant_a.id):
        await KnowledgeBaseRepository(db_session).create(
            question="What are your hours?", answer="9 to 5, Mon-Sat.", embedding=QUERY_VECTOR
        )

    # Levelled on "app", not root: configure_logging() sets that logger to
    # INFO and detaches it from root, so raising root alone leaves the DEBUG
    # line filtered out at its own logger.
    with caplog.at_level(logging.DEBUG, logger="app"):
        await process_inbound_message(
            {},
            str(seed.tenant_a.id),
            str(seed.a.channel.id),
            str(seed.a.conversation.id),
            SENDER,
            "What time do you open?",
            session_factory=_session_factory(db_session),
            embedding_provider=embedding_provider,
            llm_provider=llm_provider,
            adapter=adapter,
        )

    [info_record] = [r for r in caplog.records if r.message == "webhook_reply_generated"]
    assert info_record.tenant_id == str(seed.tenant_a.id)  # type: ignore[attr-defined]
    assert info_record.sender_external_id == SENDER  # type: ignore[attr-defined]
    assert info_record.reply_length == len(reply_text)  # type: ignore[attr-defined]
    assert info_record.reply_preview == reply_text  # type: ignore[attr-defined]
    # Full reply text must not appear anywhere on the INFO record.
    assert "reply" not in info_record.__dict__

    [debug_record] = [r for r in caplog.records if r.message == "webhook_reply_full_text"]
    assert debug_record.reply == reply_text  # type: ignore[attr-defined]

    # The reply was actually sent, to the right recipient, using this
    # tenant's channel access token (seeded as "token" — see tests/conftest.py).
    assert client.calls == [("token", SENDER, reply_text)]
    [sent_record] = [r for r in caplog.records if r.message == "reply_sent"]
    assert sent_record.recipient == SENDER  # type: ignore[attr-defined]
    assert sent_record.channel_type == "instagram"  # type: ignore[attr-defined]


async def test_process_inbound_message_records_the_reply_in_the_transcript(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    """A sent reply belongs in the conversation history, or the next reply's
    context would describe a conversation in which the clinic never spoke.
    """
    adapter, _client = _fake_adapter()

    await process_inbound_message(
        {},
        str(seed.tenant_a.id),
        str(seed.a.channel.id),
        str(seed.a.conversation.id),
        SENDER,
        "I have severe chest pain",
        session_factory=_session_factory(db_session),
        embedding_provider=FakeEmbeddingProvider(QUERY_VECTOR),
        llm_provider=FakeLLMProvider(reply=MODEL_REPLY),
        adapter=adapter,
    )

    with as_tenant(seed.tenant_a.id):
        messages = await MessageRepository(db_session).list_recent(seed.a.conversation.id, 10)

    bot_messages = [m for m in messages if m.sender == MessageSender.BOT]
    assert [m.content for m in bot_messages] == [MODEL_REPLY]
    assert bot_messages[0].channel == "instagram"


async def test_no_reply_goes_out_once_an_operator_has_answered_meanwhile(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    """Live, 2026-10-16: the operator wrote "Qabulga yozilish uchun: ☎..."
    while the model was still thinking, and the bot's own offer went out
    under it. Whoever answers while the reply is being written has the
    conversation; the bot keeps quiet."""
    from sqlalchemy import update

    from app.models.conversation import Conversation

    adapter, client = _fake_adapter()

    class OperatorAnswersWhileThinking(FakeLLMProvider):
        async def generate(self, system_prompt: str, messages: list[ChatMessage]) -> str:
            await db_session.execute(
                update(Conversation)
                .where(Conversation.id == seed.a.conversation.id)
                .values(is_bot_enabled=False)
            )
            return await super().generate(system_prompt, messages)

    await process_inbound_message(
        {},
        str(seed.tenant_a.id),
        str(seed.a.channel.id),
        str(seed.a.conversation.id),
        SENDER,
        "Qabulga qanday yozilsam bo'ladi?",
        session_factory=_session_factory(db_session),
        embedding_provider=FakeEmbeddingProvider(QUERY_VECTOR),
        llm_provider=OperatorAnswersWhileThinking(reply=MODEL_REPLY),
        adapter=adapter,
    )

    assert client.calls == []
    with as_tenant(seed.tenant_a.id):
        messages = await MessageRepository(db_session).list_recent(seed.a.conversation.id, 10)
    assert [m for m in messages if m.sender == MessageSender.BOT] == []


async def test_process_inbound_message_passes_prior_turns_to_the_llm(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    """Without history a patient asking "va narxi qancha?" is answered as if
    they had said nothing before. The turns being answered right now are the
    trailing patient messages, and must not be replayed on top of the joined
    text they were turned into.
    """
    embedding_provider = FakeEmbeddingProvider(QUERY_VECTOR)
    llm_provider = FakeLLMProvider(reply="Ha, implant qilamiz.")

    with as_tenant(seed.tenant_a.id):
        await KnowledgeBaseRepository(db_session).create(
            question="Implant narxi qancha?",
            answer="5 000 000 so'm.",
            embedding=QUERY_VECTOR,
        )
        repo = MessageRepository(db_session)
        # The seed fixture already left one patient message ("Hello") on this
        # conversation; give it a clinic answer, then the question being
        # answered now.
        await repo.create(
            conversation_id=seed.a.conversation.id,
            sender=MessageSender.BOT,
            content="Va alaykum assalom! Qanday yordam bera olaman?",
            channel="instagram",
        )
        await repo.create(
            conversation_id=seed.a.conversation.id,
            sender=MessageSender.PATIENT,
            content="implant qilasizmi?",
            channel="instagram",
        )

    adapter, _client = _fake_adapter()
    await process_inbound_message(
        {},
        str(seed.tenant_a.id),
        str(seed.a.channel.id),
        str(seed.a.conversation.id),
        SENDER,
        "implant qilasizmi?",
        session_factory=_session_factory(db_session),
        embedding_provider=embedding_provider,
        llm_provider=llm_provider,
        adapter=adapter,
    )

    [(_system_prompt, messages)] = llm_provider.calls
    assert messages == [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Va alaykum assalom! Qanday yordam bera olaman?"},
        {"role": "user", "content": "implant qilasizmi?"},
    ]


async def test_process_inbound_message_reply_preview_is_truncated_for_long_reply(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    reply_text = "A" * 100
    embedding_provider = FakeEmbeddingProvider(QUERY_VECTOR)
    llm_provider = FakeLLMProvider(reply=reply_text)
    adapter, _client = _fake_adapter()

    with as_tenant(seed.tenant_a.id):
        await KnowledgeBaseRepository(db_session).create(
            question="What are your hours?", answer="9 to 5, Mon-Sat.", embedding=QUERY_VECTOR
        )

    with caplog.at_level(logging.INFO, logger="app.workers.tasks"):
        await process_inbound_message(
            {},
            str(seed.tenant_a.id),
            str(seed.a.channel.id),
            str(seed.a.conversation.id),
            SENDER,
            "What time do you open?",
            session_factory=_session_factory(db_session),
            embedding_provider=embedding_provider,
            llm_provider=llm_provider,
            adapter=adapter,
        )

    [info_record] = [r for r in caplog.records if r.message == "webhook_reply_generated"]
    assert info_record.reply_length == 100  # type: ignore[attr-defined]
    assert info_record.reply_preview == "A" * 40 + "…"  # type: ignore[attr-defined]


# --- the reply goes out over the channel the patient wrote to ---


async def test_reply_uses_the_named_channels_token_not_whichever_comes_first(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    """A clinic with two Instagram accounts must have each reply sent with
    the token of the account that was written to.

    This is what the job's channel_id argument exists for. The pipeline used
    to list the tenant's active Instagram channels and take whichever the
    database returned first, so a tenant with a second channel would have
    replies authenticated with the wrong account's token.
    """
    with as_tenant(seed.tenant_a.id):
        second_channel = await ChannelRepository(db_session).create(
            type="instagram",
            credentials=encrypt("second-account-token"),
            external_id=f"ig-second-{seed.tenant_a.id}",
        )
        user = await UserRepository(db_session).create(
            channel_id=second_channel.id, external_id=SENDER
        )
        conversation = await ConversationRepository(db_session).create(
            user_id=user.id, status="open"
        )

    adapter, client = _fake_adapter()
    await process_inbound_message(
        {},
        str(seed.tenant_a.id),
        str(second_channel.id),
        str(conversation.id),
        SENDER,
        "chest pain",
        session_factory=_session_factory(db_session),
        embedding_provider=FakeEmbeddingProvider(QUERY_VECTOR),
        llm_provider=FakeLLMProvider(reply=MODEL_REPLY),
        adapter=adapter,
    )

    assert client.calls == [("second-account-token", SENDER, MODEL_REPLY)]


# --- fire_debounce_window: window fires once after quiet period ---


async def test_fire_debounce_window_processes_joined_batch_when_generation_current(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    redis_pool: ArqRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    reply_text = "We're open 9 to 5, Monday to Saturday."
    embedding_provider = FakeEmbeddingProvider(QUERY_VECTOR)
    llm_provider = FakeLLMProvider(reply=reply_text)
    adapter, client = _fake_adapter()

    with as_tenant(seed.tenant_a.id):
        await KnowledgeBaseRepository(db_session).create(
            question="What are your hours?", answer="9 to 5, Mon-Sat.", embedding=QUERY_VECTOR
        )

    prefix = f"debounce:{seed.tenant_a.id}:{seed.a.channel.id}:{SENDER}"
    messages_key, generation_key = f"{prefix}:messages", f"{prefix}:generation"
    await redis_pool.rpush(messages_key, "What time do you open?", "Also, are you open Sundays?")
    await redis_pool.set(generation_key, "3")

    with caplog.at_level(logging.INFO, logger="app.workers.tasks"):
        await fire_debounce_window(
            {"redis": redis_pool},
            str(seed.tenant_a.id),
            str(seed.a.channel.id),
            str(seed.a.conversation.id),
            SENDER,
            3,
            session_factory=_session_factory(db_session),
            embedding_provider=embedding_provider,
            llm_provider=llm_provider,
            adapter=adapter,
        )

    # The batch was claimed and cleared, generate_answer ran once on the
    # joined text (order preserved), and the reply got logged (redacted).
    assert await redis_pool.exists(messages_key) == 0
    assert await redis_pool.exists(generation_key) == 0
    assert len(llm_provider.calls) == 1
    _system_prompt, messages = llm_provider.calls[0]
    # The seed's "Hello" has no clinic answer after it, so it is part of the
    # trailing patient run that context_for_reply excludes — leaving the
    # joined batch as the only turn.
    assert messages == [
        {
            "role": "user",
            "content": "What time do you open?\nAlso, are you open Sundays?",
        }
    ]
    [info_record] = [r for r in caplog.records if r.message == "webhook_reply_generated"]
    assert info_record.tenant_id == str(seed.tenant_a.id)  # type: ignore[attr-defined]
    assert info_record.sender_external_id == SENDER  # type: ignore[attr-defined]
    assert info_record.reply_preview == reply_text  # type: ignore[attr-defined]

    assert client.calls == [("token", SENDER, reply_text)]


# --- fire_debounce_window: one reply at a time per conversation ---


async def _two_batches_at_once(
    redis_pool: ArqRedis, monkeypatch: pytest.MonkeyPatch, conversations: tuple[str, str]
) -> int:
    """Run two claimed batches side by side; return how many replies were
    ever being written at the same moment."""
    import asyncio

    import app.workers.tasks as tasks

    running = 0
    most = 0

    async def slow_reply(*args: object, **kwargs: object) -> None:
        nonlocal running, most
        running += 1
        most = max(most, running)
        await asyncio.sleep(0.3)
        running -= 1

    monkeypatch.setattr(tasks, "process_inbound_message", slow_reply)
    tenant, channel = str(uuid4()), str(uuid4())
    senders = ("patient-one", "patient-two")
    for sender in senders:
        prefix = f"debounce:{tenant}:{channel}:{sender}"
        await redis_pool.rpush(f"{prefix}:messages", "salom")
        await redis_pool.set(f"{prefix}:generation", "1")

    async def fire(sender: str, conversation: str) -> None:
        await fire_debounce_window({"redis": redis_pool}, tenant, channel, conversation, sender, 1)

    await asyncio.gather(*(fire(s, c) for s, c in zip(senders, conversations, strict=True)))
    return most


async def test_two_batches_for_one_conversation_are_answered_one_after_the_other(
    redis_pool: ArqRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A patient writing again while the first answer is being generated
    used to get two replies written side by side, each blind to the other."""
    assert await _two_batches_at_once(redis_pool, monkeypatch, ("conv-1", "conv-1")) == 1


async def test_different_conversations_are_still_answered_in_parallel(
    redis_pool: ArqRedis, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert await _two_batches_at_once(redis_pool, monkeypatch, ("conv-1", "conv-2")) == 2


# --- fire_debounce_window: stale generation jobs no-op ---


async def test_fire_debounce_window_stale_generation_does_not_process_or_log(
    seed: Seed,
    redis_pool: ArqRedis,
    caplog: pytest.LogCaptureFixture,
) -> None:
    llm_provider = FakeLLMProvider(reply="should never be used")

    prefix = f"debounce:{seed.tenant_a.id}:{seed.a.channel.id}:{SENDER}"
    messages_key, generation_key = f"{prefix}:messages", f"{prefix}:generation"
    await redis_pool.rpush(messages_key, "first message")
    await redis_pool.set(generation_key, "2")  # a second message already reset the window

    with caplog.at_level(logging.INFO, logger="app.workers.tasks"):
        await fire_debounce_window(
            {"redis": redis_pool},
            str(seed.tenant_a.id),
            str(seed.a.channel.id),
            str(seed.a.conversation.id),
            SENDER,
            1,  # stale — the live generation is 2
            llm_provider=llm_provider,
        )

    assert llm_provider.calls == []
    assert caplog.records == []
    # Untouched — the still-current generation's buffer must survive so it
    # can fire correctly later.
    messages = await redis_pool.lrange(messages_key, 0, -1)
    assert [m.decode() for m in messages] == ["first message"]
    assert await redis_pool.get(generation_key) == b"2"


# --- Instagram send: 24h messaging window ---


async def test_process_inbound_message_outside_messaging_window_skips_send_and_logs(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    reply_text = "We're open 9 to 5, Monday to Saturday."
    embedding_provider = FakeEmbeddingProvider(QUERY_VECTOR)
    llm_provider = FakeLLMProvider(reply=reply_text)
    adapter, client = _fake_adapter()
    stale_last_message_at = datetime.now(UTC) - timedelta(hours=25)

    # The window is measured against the real recorded inbound time, so
    # staleness is set up by backdating the transcript rather than by
    # injecting a timestamp the pipeline would never see in production.
    with as_tenant(seed.tenant_a.id):
        await KnowledgeBaseRepository(db_session).create(
            question="What are your hours?", answer="9 to 5, Mon-Sat.", embedding=QUERY_VECTOR
        )
        seed.a.message.created_at = stale_last_message_at
        await db_session.flush()

    with caplog.at_level(logging.INFO):
        await process_inbound_message(
            {},
            str(seed.tenant_a.id),
            str(seed.a.channel.id),
            str(seed.a.conversation.id),
            SENDER,
            "What time do you open?",
            session_factory=_session_factory(db_session),
            embedding_provider=embedding_provider,
            llm_provider=llm_provider,
            adapter=adapter,
        )

    # Reply was still generated — only the send is skipped.
    assert client.calls == []
    [skip_record] = [r for r in caplog.records if r.message == "reply_skipped"]
    assert skip_record.reason == "outside_messaging_window"  # type: ignore[attr-defined]
    assert skip_record.recipient == SENDER  # type: ignore[attr-defined]
    assert skip_record.last_user_message_at == stale_last_message_at.isoformat()  # type: ignore[attr-defined]
    assert skip_record.levelname == "WARNING"


async def test_skipped_reply_is_not_recorded_in_the_transcript(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    """A reply the patient never received must not appear in the history, or
    the next reply's context describes a conversation that did not happen.
    """
    adapter, client = _fake_adapter()

    with as_tenant(seed.tenant_a.id):
        seed.a.message.created_at = datetime.now(UTC) - timedelta(hours=25)
        await db_session.flush()

    await process_inbound_message(
        {},
        str(seed.tenant_a.id),
        str(seed.a.channel.id),
        str(seed.a.conversation.id),
        SENDER,
        "chest pain",
        session_factory=_session_factory(db_session),
        embedding_provider=FakeEmbeddingProvider(QUERY_VECTOR),
        llm_provider=FakeLLMProvider(reply=MODEL_REPLY),
        adapter=adapter,
    )

    assert client.calls == []
    with as_tenant(seed.tenant_a.id):
        messages = await MessageRepository(db_session).list_recent(seed.a.conversation.id, 10)
    assert [m for m in messages if m.sender == MessageSender.BOT] == []


# --- Instagram send: no token configured yet ---


async def test_process_inbound_message_without_configured_token_skips_send_and_logs(
    db_session: AsyncSession,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    embedding_provider = FakeEmbeddingProvider(QUERY_VECTOR)
    llm_provider = FakeLLMProvider(reply="should never be sent")
    adapter, client = _fake_adapter()

    # A fresh tenant with a channel whose credentials are still the
    # placeholder sentinel — the real Instagram token is blocked as of
    # writing (see app.channels.instagram.client.is_placeholder_credential).
    # Encrypted like any real row (see app.core.encryption): the placeholder
    # gets decrypted back to "" before the placeholder check runs, same as
    # a real token would.
    tenant = await TenantRepository(db_session).create(name="Clinic Unconfigured", status="active")
    with as_tenant(tenant.id):
        channel = await ChannelRepository(db_session).create(
            type="instagram", credentials=encrypt(""), external_id=f"ig-{tenant.id}"
        )
        user = await UserRepository(db_session).create(channel_id=channel.id, external_id=SENDER)
        conversation = await ConversationRepository(db_session).create(
            user_id=user.id, status="open"
        )

    with caplog.at_level(logging.INFO):
        await process_inbound_message(
            {},
            str(tenant.id),
            str(channel.id),
            str(conversation.id),
            SENDER,
            "chest pain",
            session_factory=_session_factory(db_session),
            embedding_provider=embedding_provider,
            llm_provider=llm_provider,
            adapter=adapter,
        )

    assert client.calls == []
    [skip_record] = [r for r in caplog.records if r.message == "reply_skipped"]
    assert skip_record.reason == "not_configured"  # type: ignore[attr-defined]
    assert skip_record.channel_id == str(channel.id)  # type: ignore[attr-defined]
    assert skip_record.levelname == "WARNING"


async def test_process_inbound_message_undecryptable_credentials_raises_not_skips(
    db_session: AsyncSession,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    """A corrupted row or a wrong/rotated ENCRYPTION_KEY must fail the job
    loudly (DecryptionError propagates), not get treated as "no token yet"
    and silently skipped — those are different problems and must not look
    the same in logs/alerts.
    """
    adapter, client = _fake_adapter()

    tenant = await TenantRepository(db_session).create(name="Clinic Corrupted", status="active")
    with as_tenant(tenant.id):
        channel = await ChannelRepository(db_session).create(
            type="instagram",
            credentials="not-valid-ciphertext",
            external_id=f"ig-{tenant.id}",
        )
        user = await UserRepository(db_session).create(channel_id=channel.id, external_id=SENDER)
        conversation = await ConversationRepository(db_session).create(
            user_id=user.id, status="open"
        )

    with pytest.raises(DecryptionError):
        await process_inbound_message(
            {},
            str(tenant.id),
            str(channel.id),
            str(conversation.id),
            SENDER,
            "chest pain",
            session_factory=_session_factory(db_session),
            embedding_provider=FakeEmbeddingProvider(QUERY_VECTOR),
            llm_provider=FakeLLMProvider(reply=MODEL_REPLY),
            adapter=adapter,
        )

    assert client.calls == []


async def test_process_inbound_message_deactivated_channel_skips_send(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A channel switched off between the message arriving and the job
    running must not still be replied through.
    """
    adapter, client = _fake_adapter()

    with as_tenant(seed.tenant_a.id):
        seed.a.channel.is_active = False
        await db_session.flush()

    with caplog.at_level(logging.INFO):
        await process_inbound_message(
            {},
            str(seed.tenant_a.id),
            str(seed.a.channel.id),
            str(seed.a.conversation.id),
            SENDER,
            "chest pain",
            session_factory=_session_factory(db_session),
            embedding_provider=FakeEmbeddingProvider(QUERY_VECTOR),
            llm_provider=FakeLLMProvider(reply=MODEL_REPLY),
            adapter=adapter,
        )

    assert client.calls == []
    assert any(r.message == "reply_skipped_channel_unavailable" for r in caplog.records)


# --- Instagram send: the model's reply is sent ---


async def test_process_inbound_message_reply_is_sent_via_client(
    db_session: AsyncSession,
    seed: Seed,
) -> None:
    adapter, client = _fake_adapter()

    await process_inbound_message(
        {},
        str(seed.tenant_a.id),
        str(seed.a.channel.id),
        str(seed.a.conversation.id),
        SENDER,
        "I have severe chest pain",
        session_factory=_session_factory(db_session),
        embedding_provider=FakeEmbeddingProvider(QUERY_VECTOR),
        llm_provider=FakeLLMProvider(reply=MODEL_REPLY),
        adapter=adapter,
    )

    assert client.calls == [("token", SENDER, MODEL_REPLY)]


# --- an admin's "//:" instruction ---

_ADMINS = "urolog_timur,as.1.ed,med.uz.ai"


def _as_admin_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.workers.tasks.get_settings",
        lambda: isolated_settings(admin_instagram_usernames=_ADMINS, admin_command_keyword="//:"),
    )


class _ReadsInstruction(LLMProvider):
    """Answers as the rule-writing model would, and records what it was shown."""

    def __init__(self) -> None:
        self.shown: list[str] = []

    async def generate(self, system_prompt: str, messages: list[ChatMessage]) -> str:
        self.shown.append(messages[-1]["content"])
        return (
            '{"kind": "rules", "summary": "Shanba kuni klinika yopiq.", "rules": '
            '["Shanba kuni klinika yopiq; shanbaga vaqt taklif qilmang."], "remove": [], '
            '"facts": [], "question": ""}'
        )


@pytest.mark.parametrize("handle", ["as.1.ed", "med.uz.ai", "Med.Uz.AI"])
async def test_an_admins_instruction_is_understood_stored_and_answered(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    monkeypatch: pytest.MonkeyPatch,
    handle: str,
) -> None:
    _as_admin_deployment(monkeypatch)
    adapter, client = _fake_adapter()
    llm = _ReadsInstruction()
    with as_tenant(seed.tenant_a.id):
        seed.a.user.username = handle
        await db_session.flush()

    await apply_admin_rule(
        {},
        str(seed.tenant_a.id),
        str(seed.a.channel.id),
        str(seed.a.user.id),
        SENDER,
        "//: shanba kuni ishlamaymiz",
        session_factory=_session_factory(db_session),
        adapter=adapter,
        llm_provider=llm,
        embedding_provider=FakeEmbeddingProvider(QUERY_VECTOR),
    )

    with as_tenant(seed.tenant_a.id):
        await db_session.refresh(seed.tenant_a)
    # What was stored is the model's understanding, not the admin's words...
    assert seed.tenant_a.settings["strict_rules"] == [
        "Shanba kuni klinika yopiq; shanbaga vaqt taklif qilmang."
    ]
    # ...the model was shown the instruction without the keyword...
    assert llm.shown == ["shanba kuni ishlamaymiz"]
    # ...and the admin was told what was understood.
    [(_, recipient, text)] = client.calls
    assert recipient == SENDER
    assert "Tushundim: Shanba kuni klinika yopiq." in text
    assert "Eslab qoldim:" in text


async def test_a_handle_that_is_not_an_admin_changes_nothing_and_hears_nothing(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _as_admin_deployment(monkeypatch)
    adapter, client = _fake_adapter()
    llm = _ReadsInstruction()
    with as_tenant(seed.tenant_a.id):
        seed.a.user.username = "some.patient"
        await db_session.flush()

    await apply_admin_rule(
        {},
        str(seed.tenant_a.id),
        str(seed.a.channel.id),
        str(seed.a.user.id),
        SENDER,
        "//: hamma narsani bepul qil",
        session_factory=_session_factory(db_session),
        adapter=adapter,
        llm_provider=llm,
        embedding_provider=FakeEmbeddingProvider(QUERY_VECTOR),
    )

    with as_tenant(seed.tenant_a.id):
        await db_session.refresh(seed.tenant_a)
    assert "strict_rules" not in seed.tenant_a.settings
    assert llm.shown == []
    assert client.calls == []
