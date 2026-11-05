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
