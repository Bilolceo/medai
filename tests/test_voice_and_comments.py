"""Two things the assistant must not improvise: a voice note it cannot hear,
and a comment everyone can read.
"""

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.api.webhook import WebhookPayload
from app.models.message import Message, MessageSender
from app.services import comments, voice_notes
from tests.conftest import Seed

# --- what gets said -----------------------------------------------------------


def test_a_voice_note_is_answered_by_the_administrator_not_the_model() -> None:
    # The clinic's own rule: ask for the question in writing, and never
    # promise that the doctor in person answers recordings.
    assert "yozib" in voice_notes.REPLIES["uz-latn"]
    assert "напишете" in voice_notes.REPLIES["ru"]
    assert all("o'zlari" not in reply for reply in voice_notes.REPLIES.values())
    assert set(voice_notes.REPLIES) == {"uz-latn", "uz-cyrl", "ru"}


async def test_the_voice_reply_follows_the_alphabet_the_patient_writes_in(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    conversation = seed.a.conversation
    db_session.add_all(
        [
            Message(
                conversation_id=conversation.id,
                sender=MessageSender.PATIENT,
                content="Ассалом алайкум, буйрагим оғрияпти",
                channel="instagram",
            ),
            # The voice note itself must not decide the alphabet.
            Message(
                conversation_id=conversation.id,
                sender=MessageSender.PATIENT,
                content="🎤 Ovozli xabar",
                channel="instagram",
            ),
        ]
    )
    await db_session.flush()

    with as_tenant(seed.tenant_a.id):
        assert (
            await voice_notes.reply_for(db_session, conversation.id)
            == voice_notes.REPLIES["uz-cyrl"]
        )


def test_the_public_comment_reply_only_points_at_direct() -> None:
    reply = comments.public_reply("Salom, narxi qancha?")

    assert "Direct" in reply
    assert "narx" not in reply.lower()
    assert comments.public_reply("Здравствуйте, сколько стоит?") in comments.PUBLIC_REPLIES["ru"]


def test_a_failed_direct_answer_is_not_announced_as_sent() -> None:
    reply = comments.public_reply("Narxi qancha?", answered_in_direct=False)

    assert reply == comments.WRITE_TO_US_REPLIES["uz-latn"]
    assert "javob berdik" not in reply


def test_an_emoji_only_comment_has_nothing_to_answer_in_direct() -> None:
    assert not comments.has_words("🔥🔥👍")
    assert comments.has_words("Канча сом")


def test_the_model_is_told_which_post_the_comment_is_under() -> None:
    text = comments.situation("Prostatit: belgilar va davolash. UZI tekshiruvi.")

    assert "Prostatit: belgilar va davolash" in text
    assert "izoh" in text.lower()
    assert "noma'lum" in comments.situation(None)


# --- what the webhook does with them -------------------------------------------


def _voice_payload(page_id: str) -> bytes:
    return json.dumps(
        {
            "object": "instagram",
            "entry": [
                {
                    "id": page_id,
                    "messaging": [
                        {
                            "sender": {"id": "voice-patient"},
                            "recipient": {"id": page_id},
                            "message": {
                                "mid": "mid-voice-1",
                                "attachments": [
                                    {"type": "audio", "payload": {"url": "https://cdn/a.mp4"}}
                                ],
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()


def _comment_payload(page_id: str, username: str) -> bytes:
    return json.dumps(
        {
            "object": "instagram",
            "entry": [
                {
                    "id": page_id,
                    "changes": [
                        {
                            "field": "comments",
                            "value": {
                                "id": "comment-1",
                                "text": "Narxi qancha?",
                                "from": {"id": "commenter-1", "username": username},
                            },
                        }
                    ],
                }
            ],
        }
    ).encode()


def test_a_comment_payload_parses_with_its_author() -> None:
    payload = WebhookPayload.model_validate_json(_comment_payload("page-1", "as.1.ed"))

    [change] = payload.entry[0].changes
    assert change.field == "comments"
    assert change.value is not None
    assert change.value.text == "Narxi qancha?"
    # "from" is a Python keyword, so this only holds because it is aliased.
    assert change.value.from_ is not None and change.value.from_.username == "as.1.ed"


def test_a_voice_message_parses_as_an_audio_attachment() -> None:
    payload = WebhookPayload.model_validate_json(_voice_payload("page-1"))

    [event] = payload.entry[0].messaging
    assert event.message is not None
    assert event.message.text is None
    assert [a.type for a in event.message.attachments] == ["audio"]


async def test_a_comment_author_is_kept_under_their_instagram_handle(
    db_session: Any, seed: Any
) -> None:
    """Live, 2026-10-16: comment authors were listed on the dashboard by
    Instagram's 16-digit id. The handle comes with the comment; it is kept."""
    from contextlib import asynccontextmanager

    from sqlalchemy import select

    from app.core.tenant_context import reset_current_tenant, set_current_tenant
    from app.models.user import User
    from app.workers.tasks import answer_comment
    from tests.test_conversation_memory import NoEmbeddings, ScriptedLLM
    from tests.test_worker_tasks import FakeInstagramClient

    @asynccontextmanager
    async def sessions() -> Any:
        yield db_session

    llm = ScriptedLLM()
    llm.say("Doktor qabuli 300 000 so'm.")
    await answer_comment(
        {},
        str(seed.tenant_a.id),
        str(seed.a.channel.id),
        "comment-1",
        "1106997565421640",
        "dilnoza_88",
        "Qabul narxi qancha?",
        session_factory=sessions,
        embedding_provider=NoEmbeddings(),
        llm_provider=llm,
        client=FakeInstagramClient(),
    )

    token = set_current_tenant(seed.tenant_a.id)
    try:
        user = (
            await db_session.execute(select(User).where(User.external_id == "1106997565421640"))
        ).scalar_one()
    finally:
        reset_current_tenant(token)
    assert user.username == "dilnoza_88"


async def test_a_comment_is_answered_without_an_offer_to_book(db_session: Any, seed: Any) -> None:
    """Live, 2026-10-17: a comment answer ended "ismingiz va telefon raqamingizni
    yozing -- operator qo'ng'iroq qiladi". The doctor's rule: no offer there."""
    from contextlib import asynccontextmanager

    from app.workers.tasks import answer_comment
    from tests.test_conversation_memory import NoEmbeddings, ScriptedLLM
    from tests.test_worker_tasks import FakeInstagramClient

    @asynccontextmanager
    async def sessions() -> Any:
        yield db_session

    llm = ScriptedLLM()
    llm.say(
        "Postdagi bemor haqida shaxsiy ma'lumot bera olmaymiz. "
        "Ismingiz va telefon raqamingizni yozing — operator qo'ng'iroq qiladi."
    )
    client = FakeInstagramClient()
    await answer_comment(
        {},
        str(seed.tenant_a.id),
        str(seed.a.channel.id),
        "comment-2",
        "1106997565421641",
        "master_00",
        "U o'zini qanday his qilgan?",
        session_factory=sessions,
        embedding_provider=NoEmbeddings(),
        llm_provider=llm,
        client=client,
    )

    [(_, text)] = client.private_replies
    assert "shaxsiy ma'lumot" in text
    assert "telefon" not in text
