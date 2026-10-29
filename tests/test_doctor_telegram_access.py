"""The doctor's Telegram bot cancels whole days and messages every patient
on them, so who may drive it matters more than anything else it does.

A fake Bot API records what the bot would have sent; delivery to patients is
replaced so nothing reaches Instagram. Everything else -- the bookings, the
day-off list -- is the real code against the test database.
"""

import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.doctor_telegram as doctor_telegram
from app.models.appointment import AppointmentStatus
from app.repositories.appointment import AppointmentRepository
from app.services.appointment import CLINIC_TIMEZONE, days_off_from, is_within_working_hours
from tests.conftest import Seed, isolated_settings

DOCTOR = "Temur_Akhmadaliev"


class FakeBotAPI:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.edits: list[tuple[int, int, str]] = []
        self.answers: list[tuple[str, str | None]] = []

    async def send(self, chat_id: int, text: str, reply_markup: Any = None) -> bool:
        self.sent.append((chat_id, text))
        return True

    async def edit(
        self, chat_id: int, message_id: int, text: str, reply_markup: Any = None
    ) -> None:
        self.edits.append((chat_id, message_id, text))

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        self.answers.append((callback_id, text))


@pytest.fixture(autouse=True)
def _doctor_settings(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    settings = isolated_settings(doctor_telegram_usernames=f"someone_else, @{DOCTOR}")
    monkeypatch.setattr(doctor_telegram, "get_settings", lambda: settings)
    delivered: list[str] = []

    async def no_network(*args: Any, **kwargs: Any) -> str:
        delivered.append(kwargs.get("text", ""))
        return "instagram"

    monkeypatch.setattr(doctor_telegram, "send_reply", no_network)
    return delivered


def _working_day() -> date:
    day = datetime.now(CLINIC_TIMEZONE).date() + timedelta(days=2)
    while not is_within_working_hours(
        datetime.combine(day, datetime.min.time(), CLINIC_TIMEZONE).replace(hour=11)
    ):
        day += timedelta(days=1)
    return day


async def _book(
    session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
    day: date,
) -> uuid.UUID:
    with as_tenant(seed.tenant_a.id):
        appointment = await AppointmentRepository(session).create(
            user_id=seed.a.user.id,
            conversation_id=seed.a.conversation.id,
            scheduled_at=datetime.combine(day, datetime.min.time(), CLINIC_TIMEZONE)
            .replace(hour=11)
            .astimezone(UTC),
            status=AppointmentStatus.SCHEDULED,
            source="instagram",
            patient_name="Asadbek",
            doctor_name="Dr",
        )
        await session.flush()
        return appointment.id


def _message(username: str | None, text: str = "/start") -> dict[str, Any]:
    sender = {"id": 555} if username is None else {"id": 555, "username": username}
    return {"message": {"chat": {"id": 555}, "from": sender, "text": text}}


def _press(username: str | None, data: str) -> dict[str, Any]:
    sender = {"id": 555} if username is None else {"id": 555, "username": username}
    return {
        "callback_query": {
            "id": "cb-1",
            "from": sender,
            "data": data,
            "message": {"chat": {"id": 555}, "message_id": 9},
        }
    }


async def _status(session: AsyncSession, seed: Seed, appointment_id: uuid.UUID) -> str:
    from app.models.appointment import Appointment

    row = await session.get(Appointment, appointment_id)
    assert row is not None
    await session.refresh(row)
    return str(row.status)


# --- who may use it -------------------------------------------------------------


@pytest.mark.parametrize("username", ["stranger", None, "", "Temur_Akhmadaliev_fake", "temur"])
async def test_anybody_not_on_the_list_is_turned_away(
    db_session: AsyncSession, seed: Seed, username: str | None
) -> None:
    api = FakeBotAPI()

    await doctor_telegram.handle_update(api, db_session, seed.tenant_a, _message(username))

    assert api.sent == [(555, "🔒 Kechirasiz, bu bot faqat doktor uchun.")]
    await db_session.refresh(seed.tenant_a)
    assert 555 not in (seed.tenant_a.settings.get("doctor_telegram_chats") or [])


@pytest.mark.parametrize("username", [DOCTOR, DOCTOR.lower(), DOCTOR.upper(), f"@{DOCTOR}"])
async def test_the_doctor_is_let_in_however_the_handle_is_cased(
    db_session: AsyncSession, seed: Seed, username: str
) -> None:
    api = FakeBotAPI()

    await doctor_telegram.handle_update(api, db_session, seed.tenant_a, _message(username))

    assert api.sent and "faqat doktor uchun" not in api.sent[0][1]


async def test_a_stranger_pressing_cancel_the_day_cancels_nothing(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
    _doctor_settings: list[str],
) -> None:
    """The callback data is plain text a client can forge; the check has to
    be on who pressed it, not on having been shown the button."""
    day = _working_day()
    appointment_id = await _book(db_session, seed, as_tenant, day)
    api = FakeBotAPI()

    await doctor_telegram.handle_update(
        api, db_session, seed.tenant_a, _press("stranger", f"O:{day.isoformat()}")
    )

    assert api.answers == [("cb-1", "Bu bot faqat doktor uchun")]
    assert api.edits == []
    assert await _status(db_session, seed, appointment_id) == AppointmentStatus.SCHEDULED
    await db_session.refresh(seed.tenant_a)
    assert day not in days_off_from(seed.tenant_a.settings)
    assert _doctor_settings == []


async def test_a_press_with_no_username_is_refused(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
) -> None:
    day = _working_day()
    appointment_id = await _book(db_session, seed, as_tenant, day)
    api = FakeBotAPI()

    await doctor_telegram.handle_update(
        api, db_session, seed.tenant_a, _press(None, f"O:{day.isoformat()}")
    )

    assert await _status(db_session, seed, appointment_id) == AppointmentStatus.SCHEDULED


# --- what the doctor sends it ---------------------------------------------------


@pytest.mark.parametrize("data", ["O:2020-01-01", "O:not-a-date", "O:", "O:9999-99-99"])
async def test_a_past_or_malformed_day_cancels_nothing(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
    data: str,
) -> None:
    appointment_id = await _book(db_session, seed, as_tenant, _working_day())
    api = FakeBotAPI()

    await doctor_telegram.handle_update(api, db_session, seed.tenant_a, _press(DOCTOR, data))

    assert api.answers == [("cb-1", "Bu kun o'tib ketgan")]
    assert await _status(db_session, seed, appointment_id) == AppointmentStatus.SCHEDULED


@pytest.mark.parametrize("data", ["", "zzz", "t:", "c:do", "O", ":::"])
async def test_nonsense_buttons_change_nothing(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
    data: str,
) -> None:
    appointment_id = await _book(db_session, seed, as_tenant, _working_day())
    api = FakeBotAPI()

    await doctor_telegram.handle_update(api, db_session, seed.tenant_a, _press(DOCTOR, data))

    assert await _status(db_session, seed, appointment_id) == AppointmentStatus.SCHEDULED


async def test_the_doctor_cancelling_a_day_cancels_it_and_tells_the_patient(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
    _doctor_settings: list[str],
) -> None:
    day = _working_day()
    appointment_id = await _book(db_session, seed, as_tenant, day)
    api = FakeBotAPI()

    await doctor_telegram.handle_update(
        api, db_session, seed.tenant_a, _press(DOCTOR, f"O:{day.isoformat()}")
    )

    assert await _status(db_session, seed, appointment_id) == AppointmentStatus.CANCELLED
    await db_session.refresh(seed.tenant_a)
    assert day in days_off_from(seed.tenant_a.settings)
    assert len(_doctor_settings) == 1


async def test_an_update_that_is_neither_message_nor_press_is_ignored(
    db_session: AsyncSession, seed: Seed
) -> None:
    api = FakeBotAPI()

    await doctor_telegram.handle_update(api, db_session, seed.tenant_a, {"edited_message": {}})

    assert (api.sent, api.edits, api.answers) == ([], [], [])
