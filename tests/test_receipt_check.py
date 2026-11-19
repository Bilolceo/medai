"""A photo sent after the card: a receipt, or not (the owner, 2026-11-19: a
photo of a car was answered "Chekingiz keldi"). No real model call here."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import Message
from app.models.patient_media import PatientMedia
from app.services import patient_media, receipt_check
from tests.conftest import Seed
from tests.test_payment_receipt import OFFER

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_the_model_s_answer_is_read() -> None:
    v = receipt_check._parse('{"receipt": true, "amount": "165 000 so\'m", "card_last4": "3446"}')
    assert v == receipt_check.Verdict(receipt=True, amount="165 000 so'm", card_last4="3446")
    assert receipt_check._parse('{"receipt": false, "amount": null}').receipt is False
    assert receipt_check._parse("not json") == receipt_check.UNKNOWN


def test_the_clinic_hears_what_was_found() -> None:
    no = receipt_check.note(receipt_check.Verdict(receipt=False), expected_last4="3446")
    assert "CHEK EMAS" in no
    other_card = receipt_check.note(
        receipt_check.Verdict(receipt=True, amount="165 000", card_last4="1111"),
        expected_last4="3446",
    )
    assert "165 000" in other_card and "*1111" in other_card and "BIZNIKI *3446 EMAS" in other_card
    caption = patient_media.caption(None, 1, OFFER, receipt_check.Verdict(receipt=False))
    assert "TO'LOV CHEKI" in caption and "CHEK EMAS" in caption


async def test_off_unless_switched_on() -> None:
    assert await receipt_check.check(PNG, "image/png") == receipt_check.UNKNOWN


class _Redis:
    def __init__(self) -> None:
        self.keys: set[str] = set()

    async def set(self, key: str, value: str, ex: int, nx: bool) -> bool:
        if key in self.keys:
            return False
        self.keys.add(key)
        return True

    async def delete(self, key: str) -> None:
        self.keys.discard(key)


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        (receipt_check.Verdict(receipt=False), patient_media.NOT_A_RECEIPT["uz-latn"]),
        (receipt_check.Verdict(receipt=True), patient_media.RECEIPT_ACKNOWLEDGEMENTS["uz-latn"]),
        (receipt_check.UNKNOWN, patient_media.RECEIPT_ACKNOWLEDGEMENTS["uz-latn"]),
    ],
)
async def test_a_photo_after_the_card_is_answered_by_what_it_is(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    monkeypatch: pytest.MonkeyPatch,
    verdict: receipt_check.Verdict,
    expected: str,
) -> None:
    sent: list[str] = []

    async def fake_check(content: bytes, content_type: str, **_: Any) -> receipt_check.Verdict:
        return verdict

    async def fake_send(session: Any, **kwargs: Any) -> None:
        sent.append(kwargs["text"])
        return None

    monkeypatch.setattr(receipt_check, "check", fake_check)
    monkeypatch.setattr(patient_media, "send_reply", fake_send)
    conversation = seed.a.conversation
    with as_tenant(seed.tenant_a.id):
        db_session.add(
            Message(
                conversation_id=conversation.id,
                sender="assistant",
                content=OFFER,
                channel="instagram",
                created_at=datetime.now(UTC) - timedelta(minutes=20),
            )
        )
        photo = PatientMedia(
            tenant_id=seed.tenant_a.id,
            user_id=seed.a.user.id,
            conversation_id=conversation.id,
            source_url="https://lookaside.fbsbx.com/x",
            content=PNG,
            content_type="image/png",
            size_bytes=len(PNG),
        )
        db_session.add(photo)
        await db_session.flush()
        redis = _Redis()
        got = await patient_media.acknowledge(
            db_session,
            redis,
            channel_id=seed.a.user.channel_id,
            conversation_id=conversation.id,
            recipient_external_id="ig-1",
            media=[photo],
        )
    assert got == verdict
    assert sent == [expected]
    # Not a receipt: the next photo is looked at again, not folded into the burst.
    assert (not redis.keys) == (verdict.receipt is False)
