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


async def test_the_receipt_is_still_awaited_after_a_few_questions_about_the_visit(
    db_session, seed, as_tenant
) -> None:
    """2026-11-22, med.uz.ai: the card, then "online or come in?", another
    time, a video -- four clinic messages later the receipt came and was
    taken for a result."""
    from datetime import UTC, datetime, timedelta

    from app.models.message import Message

    conversation = seed.a.conversation
    start = datetime.now(UTC) - timedelta(hours=1)
    later = [
        "19:00 da -- onlayn.",
        "Ertaga 09:00, 09:30 yoki 10:00 da bo'sh vaqt bor.",
        "Yaxshi, ertaga 10:00 ga yozib qo'yishim mumkin.",
        "Videongiz keldi. Doktor ko'rib, shu yerda javob beradi.",
    ]
    with as_tenant(seed.tenant_a.id):
        for position, content in enumerate([OFFER, *later]):
            db_session.add(
                Message(
                    conversation_id=conversation.id,
                    sender="assistant",
                    content=content,
                    channel="instagram",
                    created_at=start + timedelta(minutes=position),
                )
            )
        await db_session.flush()
        assert await patient_media.awaiting_payment(db_session, conversation.id) == OFFER


async def test_the_acknowledgement_is_in_the_alphabet_of_the_patients_words(
    db_session, seed, as_tenant
) -> None:
    """ "10 00" after Cyrillic is no reason to answer in Latin."""
    from datetime import UTC, datetime, timedelta

    from app.models.message import Message

    conversation = seed.a.conversation
    start = datetime.now(UTC) - timedelta(minutes=30)
    with as_tenant(seed.tenant_a.id):
        for position, content in enumerate(["Кундуз куни нечага келсам бўлади", "10 00"]):
            db_session.add(
                Message(
                    conversation_id=conversation.id,
                    sender="patient",
                    content=content,
                    channel="instagram",
                    created_at=start + timedelta(minutes=position),
                )
            )
        await db_session.flush()
        assert await patient_media.patient_script(db_session, conversation.id) == "uz-cyrl"
