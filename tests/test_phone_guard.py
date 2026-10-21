"""app.services.phone_guard: only the clinic's numbers leave in a reply."""

from app.services import phone_guard

CLINIC = "+998 99 899 62 83, +998 70 310 40 40"


def test_the_doctors_old_number_is_replaced_with_the_clinics() -> None:
    """Owner, 2026-10-21: +998 93 051 25 96 no longer exists and is never
    to be given, in whatever shape the model copied it."""
    for written in ("+998930512596", "+998 93 051 25 96", "+998-93-051-25-96", "93 051 25 96"):
        reply = f"Bo'lmasa shu raqamga qo'ng'iroq qiling: {written}."
        assert phone_guard.strip(reply, CLINIC) == (
            "Bo'lmasa shu raqamga qo'ng'iroq qiling: +998 99 899 62 83."
        )


def test_the_clinics_own_numbers_pass_in_any_spelling() -> None:
    reply = "Telefon: +998 99 899 62 83 yoki +998703104040."
    assert phone_guard.strip(reply, CLINIC) == reply


def test_prices_dates_and_times_are_not_numbers_anybody_can_ring() -> None:
    reply = "Qabul 300 000 so'm, implant 30 000 000 so'm; 2026-10-21 kuni 10:30 da."
    assert phone_guard.strip(reply, CLINIC) == reply


def test_a_foreign_number_next_to_a_clinic_one_does_not_double_it() -> None:
    reply = "Raqamlar: +998 99 899 62 83 yoki +998 93 051 25 96."
    assert phone_guard.strip(reply, CLINIC) == "Raqamlar: +998 99 899 62 83."


def test_with_no_clinic_numbers_the_stray_one_is_taken_out() -> None:
    assert phone_guard.strip("Raqam: +998 93 051 25 96", None) == "Raqam:"


def test_allowed_numbers_are_read_from_the_setting() -> None:
    assert phone_guard.allowed_numbers(CLINIC) == ["+998 99 899 62 83", "+998 70 310 40 40"]
    assert phone_guard.allowed_numbers("+998 70 310 40 40 yoki +998 99 899 62 83") == [
        "+998 70 310 40 40",
        "+998 99 899 62 83",
    ]
    assert phone_guard.allowed_numbers("") == []


def test_the_patients_number_inside_a_hidden_marker_is_kept() -> None:
    """The booking and call-back markers carry the patient's own number for
    the clinic; nobody sees them, and the appointment needs the number."""
    reply = (
        "Yozib qo'ydim. [[BOOK:2026-10-22T10:00|Asadbek|+998901234567|og'riq]] "
        "Savol bo'lsa: +998 93 051 25 96."
    )
    assert phone_guard.strip(reply, CLINIC) == (
        "Yozib qo'ydim. [[BOOK:2026-10-22T10:00|Asadbek|+998901234567|og'riq]] "
        "Savol bo'lsa: +998 99 899 62 83."
    )
