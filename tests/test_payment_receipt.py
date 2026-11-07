"""A photo sent after the card is the payment receipt."""

from app.services import patient_media

OFFER = (
    "Yaxshi, ertaga 10:00 ga yozib qo'yishim mumkin.\n\n"
    "Konsultatsiya narxi 330 000 so'm. Qabulga yozilish uchun 50% -- 165 000 so'm oldindan "
    "to'lanadi.\n\nTo'lov uchun karta: 9860 1501 1048 3446 (Sultonbek Norkuziev)."
)


def test_the_doctor_is_told_it_is_a_receipt_and_for_what() -> None:
    text = patient_media.caption(None, 1, OFFER)
    assert text.startswith("💳 TO'LOV CHEKI -- tekshiring")
    assert "ertaga 10:00" in text and "165 000 so'm" in text


def test_an_ordinary_photo_keeps_the_ordinary_caption() -> None:
    assert patient_media.caption(None, 2).startswith("📷 Bemor tomonidan 2 ta rasm yuborildi")


def test_the_patient_hears_the_receipt_arrived() -> None:
    assert "Chekingiz keldi" in patient_media.RECEIPT_ACKNOWLEDGEMENTS["uz-latn"]


async def test_a_photo_after_the_receipt_was_acknowledged_is_not_a_receipt(
    db_session, seed, as_tenant
) -> None:
    from datetime import UTC, datetime, timedelta

    from app.models.message import Message

    conversation = seed.a.conversation
    start = datetime.now(UTC) - timedelta(hours=2)
    with as_tenant(seed.tenant_a.id):
        db_session.add(
            Message(
                conversation_id=conversation.id,
                sender="assistant",
                content=OFFER,
                channel="instagram",
                created_at=start,
            )
        )
        await db_session.flush()
        assert await patient_media.awaiting_payment(db_session, conversation.id) == OFFER
        db_session.add(
            Message(
                conversation_id=conversation.id,
                sender="assistant",
                content=patient_media.RECEIPT_ACKNOWLEDGEMENTS["uz-latn"],
                channel="instagram",
                created_at=start + timedelta(minutes=10),
            )
        )
        await db_session.flush()
        assert await patient_media.awaiting_payment(db_session, conversation.id) is None
