"""Reading the number a patient typed."""

import pytest

from app.services import phone_check


@pytest.mark.parametrize(
    ("typed", "kind", "shown"),
    [
        ("+998 90 123 45 67", phone_check.UZ, "+998 90 123 45 67"),
        ("998901234567", phone_check.UZ, "+998 90 123 45 67"),
        ("90 123 45 67", phone_check.UZ, "+998 90 123 45 67"),
        ("(93) 051-25-96", phone_check.UZ, "+998 93 051 25 96"),
        ("8 90 123 45 67", phone_check.UZ, "+998 90 123 45 67"),
        ("0099890 123 45 67", phone_check.UZ, "+998 90 123 45 67"),
        ("+998 33 111 22 33", phone_check.UZ, "+998 33 111 22 33"),
        ("90 123 45 6", phone_check.SHORT, "+998 90123456"),
        ("+998 90 123 45 6", phone_check.SHORT, "+998 90123456"),
        ("+998 90 123 45 678", phone_check.LONG, "+998 901234567 8".replace(" 8", "8")),
        ("+998 12 345 67 89", phone_check.BAD_CODE, "+998 12 345 67 89"),
        ("+7 999 123 45 67", phone_check.FOREIGN, "+79991234567"),
        ("89991234567", phone_check.FOREIGN, "+79991234567"),
        ("+82 10 1234 5678", phone_check.FOREIGN, "+821012345678"),
    ],
)
def test_numbers_as_people_type_them(typed: str, kind: str, shown: str) -> None:
    found = phone_check.read(f"raqamim {typed}, ertaga")
    assert found is not None
    assert (found.kind, found.shown) == (kind, shown)


def test_no_number_is_none() -> None:
    assert phone_check.read("ertaga 10 da qo'ng'iroq qiling") is None
