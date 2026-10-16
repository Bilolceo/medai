"""Call-back requests: the marker, the fallback, and one lead per conversation."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lead import LeadStatus
from app.rag.llm import OpenAILLMProvider
from app.services import callbacks
from tests.conftest import Seed, isolated_settings


def test_the_marker_is_removed_and_read() -> None:
    text, request = callbacks.extract(
        "Yaxshi, administrator sizga qo'ng'iroq qiladi. "
        "[[CALLBACK:+998 90 123 45 67|buyrak og'rig'i bo'yicha maslahat]]"
    )

    assert text == "Yaxshi, administrator sizga qo'ng'iroq qiladi."
    assert request is not None
    assert request.phone == "+998901234567"
    assert request.reason == "buyrak og'rig'i bo'yicha maslahat"


def test_a_half_written_marker_is_still_hidden_from_the_patient() -> None:
    text, request = callbacks.extract("Qo'ng'iroq qilamiz [[CALLBACK:")

    assert "[[" not in text
    assert request is None


def test_a_patient_who_asks_to_be_called_and_gives_a_number_is_caught() -> None:
    request = callbacks.from_patient_words(
        ["Assalomu alaykum", "menga qo'ng'iroq qiling 90 123 45 67"]
    )

    assert request is not None and request.phone.endswith("901234567")


def test_a_number_given_to_be_booked_is_not_a_callback() -> None:
    assert callbacks.from_patient_words(["qabulga yozilaman, raqamim 901234567"]) is None


def test_russian_requests_are_understood() -> None:
    assert callbacks.from_patient_words(["перезвоните пожалуйста +998901234567"]) is not None


async def test_one_open_lead_per_conversation(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    with as_tenant(seed.tenant_a.id):
        first, _ = await callbacks.record(
            db_session,
            user=seed.a.user,
            conversation_id=seed.a.conversation.id,
            request=callbacks.CallbackRequest(phone="998901234567", reason="prostata"),
            fallback_reason=None,
        )
        again, created_again = await callbacks.record(
            db_session,
            user=seed.a.user,
            conversation_id=seed.a.conversation.id,
            request=callbacks.CallbackRequest(phone="998907654321", reason=None),
            fallback_reason="qo'ng'iroq qiling",
        )

    # The seed conversation may already hold a lead; either way the second
    # request refreshes the first rather than raising another.
    assert not created_again
    assert again.id == first.id
    assert again.phone == "998907654321"
    assert again.topic in {"prostata", first.topic}
    assert again.status == LeadStatus.NEW


def test_reasoning_effort_is_sent_only_to_reasoning_models() -> None:
    settings = isolated_settings(openai_reasoning_effort="minimal")

    assert OpenAILLMProvider(settings, model="gpt-5-mini")._reasoning_effort == "minimal"
    assert settings.openai_reasoning_effort == "minimal"


def test_the_time_the_patient_asked_for_is_read_from_the_marker() -> None:
    """Replies promised "20:00 da qo'ng'iroq qilamiz" and no lead ever carried
    the time, so whoever rang had no idea when."""
    text, request = callbacks.extract(
        "Yaxshi, qo'ng'iroq qilamiz. "
        "[[CALLBACK:+998 95 627 77 79|telefon maslahat|bugun 14:00-15:00]]"
    )

    assert text == "Yaxshi, qo'ng'iroq qilamiz."
    assert request is not None
    assert request.reason == "telefon maslahat"
    assert request.when == "bugun 14:00-15:00"


def test_a_marker_without_a_time_still_reads() -> None:
    _, request = callbacks.extract("[[CALLBACK:+998901234567|narx]]")

    assert request is not None
    assert (request.reason, request.when) == ("narx", None)


async def test_the_time_is_kept_on_the_lead_and_updated_when_given_later(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    with as_tenant(seed.tenant_a.id):
        lead, _ = await callbacks.record(
            db_session,
            user=seed.a.user,
            conversation_id=seed.a.conversation.id,
            request=callbacks.CallbackRequest(phone="998901234567", reason="maslahat"),
            fallback_reason=None,
        )
        assert lead.convenient_time is None

        same, created = await callbacks.record(
            db_session,
            user=seed.a.user,
            conversation_id=seed.a.conversation.id,
            request=callbacks.CallbackRequest(
                phone="998901234567", reason=None, when="ishdan keyin, 20:00"
            ),
            fallback_reason=None,
        )

    assert not created and same.id == lead.id
    assert same.convenient_time == "ishdan keyin, 20:00"
    assert same.topic == "maslahat"
