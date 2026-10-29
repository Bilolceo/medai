"""The doctor's Telegram bot beyond access: that a notice is told once and not
lost, who is waiting is escalated, the morning summary, and the viewer role.

A fake Bot API records what would be sent and can be told to refuse. The rest
is the real code against the test database.
"""

import uuid
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.doctor_telegram as dt
from app.models.appointment import Appointment, AppointmentStatus
from app.models.lead import Lead, LeadStatus
from app.repositories.appointment import AppointmentRepository
from app.services.appointment import CLINIC_TIMEZONE
from tests.conftest import Seed, isolated_settings

DOCTOR = "Temur_Akhmadaliev"
ASSISTANT = "Asrorbek_S"
CHAT = 111


class FakeAPI:
    def __init__(self, *, accepts: bool = True) -> None:
        self.accepts = accepts
        self.sent: list[tuple[int, str, Any]] = []
        self.edits: list[tuple[int, int, str]] = []
        self.answers: list[tuple[str, str | None]] = []
        self.fail_after: int | None = None

    async def send(self, chat_id: int, text: str, reply_markup: Any = None) -> bool:
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            return False
        if not self.accepts:
            return False
        self.sent.append((chat_id, text, reply_markup))
        return True

    async def edit(
        self, chat_id: int, message_id: int, text: str, reply_markup: Any = None
    ) -> bool:
        self.edits.append((chat_id, message_id, text))
        return True

    async def answer_callback(self, callback_id: str, text: str | None = None) -> None:
        self.answers.append((callback_id, text))

    def texts(self) -> list[str]:
        return [text for _chat, text, _markup in self.sent]


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = isolated_settings(
        doctor_telegram_usernames=f"@{DOCTOR}, 424242",
        doctor_telegram_viewers=ASSISTANT,
    )
    monkeypatch.setattr(dt, "get_settings", lambda: settings)

    async def delivered(*args: Any, **kwargs: Any) -> str:
        return "instagram"

    monkeypatch.setattr(dt, "send_reply", delivered)


def _message(user_id: int, username: str | None, text: str = "/start") -> dict[str, Any]:
    sender: dict[str, Any] = {"id": user_id}
    if username is not None:
        sender["username"] = username
    return {"message": {"chat": {"id": user_id}, "from": sender, "text": text}}


def _press(user_id: int, username: str | None, data: str, text: str = "") -> dict[str, Any]:
    sender: dict[str, Any] = {"id": user_id}
    if username is not None:
        sender["username"] = username
    return {
        "callback_query": {
            "id": "cb",
            "from": sender,
            "data": data,
            "message": {"chat": {"id": user_id}, "message_id": 7, "text": text},
        }
    }


# --- who may do what ------------------------------------------------------------


async def test_a_handle_is_tied_to_the_telegram_id_that_first_used_it(
    db_session: AsyncSession, seed: Seed
) -> None:
    api = FakeAPI()
    await dt.handle_update(api, db_session, seed.tenant_a, _message(1, DOCTOR))
    assert "faqat doktor uchun" not in api.texts()[-1]

    # Somebody else ends up with the same handle after the doctor gave it up.
    await dt.handle_update(api, db_session, seed.tenant_a, _message(2, DOCTOR))
    assert api.texts()[-1] == "🔒 Kechirasiz, bu bot faqat doktor uchun."

    # The doctor renamed themselves: still the same person.
    await dt.handle_update(api, db_session, seed.tenant_a, _message(1, "new_handle"))
    assert "faqat doktor uchun" not in api.texts()[-1]


async def test_a_numeric_id_on_the_list_is_enough(db_session: AsyncSession, seed: Seed) -> None:
    api = FakeAPI()
    await dt.handle_update(api, db_session, seed.tenant_a, _message(424242, None))
    assert "faqat doktor uchun" not in api.texts()[-1]


async def test_the_viewer_sees_the_day_but_cannot_close_it(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
) -> None:
    day = datetime.now(CLINIC_TIMEZONE).date() + timedelta(days=2)
    api = FakeAPI()

    await dt.handle_update(api, db_session, seed.tenant_a, _press(5, ASSISTANT, f"O:{day}"))
    assert api.answers[-1] == ("cb", "Bu amal faqat doktor uchun")
    await dt.handle_update(
        api, db_session, seed.tenant_a, _message(5, ASSISTANT, "bugun kelolmayman")
    )
    assert "faqat doktor uchun" in api.texts()[-1]
    await db_session.refresh(seed.tenant_a)
    assert not seed.tenant_a.settings.get("days_off")

    await dt.handle_update(api, db_session, seed.tenant_a, _press(5, ASSISTANT, f"l:{day}"))
    markup = api.edits[-1][2]
    assert "qabullar" in markup.lower()


async def test_the_doctor_sees_the_closing_buttons_the_viewer_does_not() -> None:
    today = datetime.now(CLINIC_TIMEZONE).date()
    _text, doctor = dt._list_screen(today, today, [], False, role=dt.DOCTOR)
    _text, viewer = dt._list_screen(today, today, [], False, role=dt.VIEWER)

    def labels(markup: dict[str, Any]) -> list[str]:
        return [b["text"] for row in markup["inline_keyboard"] for b in row]

    assert "🚫 Kunni yopish" in labels(doctor)
    assert "🚫 Kunni yopish" not in labels(viewer)


async def test_the_menu_is_four_buttons(db_session: AsyncSession, seed: Seed) -> None:
    api = FakeAPI()
    await dt.handle_update(api, db_session, seed.tenant_a, _message(1, DOCTOR))
    keyboard = api.sent[-1][2]["keyboard"]
    assert [b["text"] for row in keyboard for b in row] == [
        dt.BTN_LIST,
        dt.BTN_LEADS,
        dt.BTN_REPORT,
        dt.BTN_STATUS,
    ]


def test_a_long_message_is_cut_at_a_line_rather_than_refused() -> None:
    text = "\n".join(f"line {n}" for n in range(2000))
    clipped = dt._clip(text)
    assert len(clipped) <= 4096
    assert clipped.startswith("line 0\n") and "uzun" in clipped


# --- told once, and not lost ----------------------------------------------------


async def _age(session: AsyncSession, model: Any, row_id: uuid.UUID, when: datetime) -> None:
    await session.execute(
        update(model).where(model.id == row_id).values(created_at=when, updated_at=when)
    )


async def _prepare(db_session: AsyncSession, seed: Seed, since: datetime) -> None:
    """The seed's own rows are old news; the chat is open."""
    old = since - timedelta(hours=2)
    await _age(db_session, Appointment, seed.a.appointment.id, old)
    await _age(db_session, Lead, seed.a.lead.id, old)
    seed.tenant_a.settings = {
        **seed.tenant_a.settings,
        dt.CHATS_KEY: [CHAT],
        dt.NOTIFIED_UNTIL_KEY: since.isoformat(),
        dt.LEADS_NOTIFIED_UNTIL_KEY: since.isoformat(),
    }
    await db_session.commit()


async def _booking(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
    at: datetime,
    made: datetime,
    name: str,
) -> uuid.UUID:
    with as_tenant(seed.tenant_a.id):
        appointment = await AppointmentRepository(db_session).create(
            user_id=seed.a.user.id,
            conversation_id=seed.a.conversation.id,
            scheduled_at=at,
            status=AppointmentStatus.SCHEDULED,
            source="instagram",
            patient_name=name,
            doctor_name="Dr",
        )
        await db_session.flush()
    await _age(db_session, Appointment, appointment.id, made)
    return appointment.id


def _at(day: date, hour: int) -> datetime:
    return datetime.combine(day, datetime.min.time(), CLINIC_TIMEZONE).replace(hour=hour)


async def test_a_booking_the_telegram_refused_is_told_on_the_next_try_and_not_twice(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
) -> None:
    since = datetime.now(UTC) - timedelta(hours=1)
    await _prepare(db_session, seed, since)
    await _booking(
        db_session, seed, as_tenant, _at(date.today() + timedelta(days=3), 11),
        datetime.now(UTC) - timedelta(minutes=10), "Asadbek",
    )  # fmt: skip

    refusing = FakeAPI(accepts=False)
    with as_tenant(seed.tenant_a.id):
        await dt.announce_new_bookings(refusing, db_session, seed.tenant_a)
    assert dt._watermark(seed.tenant_a, dt.NOTIFIED_UNTIL_KEY) == since

    api = FakeAPI()
    with as_tenant(seed.tenant_a.id):
        await dt.announce_new_bookings(api, db_session, seed.tenant_a)
        await dt.announce_new_bookings(api, db_session, seed.tenant_a)
    assert len([t for t in api.texts() if "Yangi qabul" in t and "Asadbek" in t]) == 1


async def test_a_failure_half_way_does_not_tell_the_first_booking_again(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
) -> None:
    since = datetime.now(UTC) - timedelta(hours=1)
    await _prepare(db_session, seed, since)
    day = date.today() + timedelta(days=3)
    for hour, name, minutes in ((10, "Birinchi", 30), (11, "Ikkinchi", 20)):
        await _booking(
            db_session, seed, as_tenant, _at(day, hour),
            datetime.now(UTC) - timedelta(minutes=minutes), name,
        )  # fmt: skip

    flaky = FakeAPI()
    flaky.fail_after = 1
    with as_tenant(seed.tenant_a.id):
        await dt.announce_new_bookings(flaky, db_session, seed.tenant_a)
    assert [("Birinchi" in t) for t in flaky.texts()] == [True]

    api = FakeAPI()
    with as_tenant(seed.tenant_a.id):
        await dt.announce_new_bookings(api, db_session, seed.tenant_a)
    assert len(api.texts()) == 1 and "Ikkinchi" in api.texts()[0]


async def test_what_happened_before_anybody_opened_the_bot_arrives_when_they_do(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
) -> None:
    since = datetime.now(UTC) - timedelta(hours=1)
    await _prepare(db_session, seed, since)
    seed.tenant_a.settings = {**seed.tenant_a.settings, dt.CHATS_KEY: []}
    await _booking(
        db_session, seed, as_tenant, _at(date.today() + timedelta(days=3), 11),
        datetime.now(UTC) - timedelta(minutes=10), "Kech qolgan",
    )  # fmt: skip
    api = FakeAPI()
    with as_tenant(seed.tenant_a.id):
        await dt.announce_new_bookings(api, db_session, seed.tenant_a)
    assert api.sent == []

    seed.tenant_a.settings = {**seed.tenant_a.settings, dt.CHATS_KEY: [CHAT]}
    with as_tenant(seed.tenant_a.id):
        await dt.announce_new_bookings(api, db_session, seed.tenant_a)
    assert any("Kech qolgan" in t for t in api.texts())


async def test_a_booking_is_told_again_only_when_its_time_moves(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
) -> None:
    since = datetime.now(UTC) - timedelta(hours=1)
    await _prepare(db_session, seed, since)
    day = date.today() + timedelta(days=3)
    appointment_id = await _booking(
        db_session,
        seed,
        as_tenant,
        _at(day, 11),
        datetime.now(UTC) - timedelta(minutes=30),
        "Sardor",
    )
    api = FakeAPI()
    with as_tenant(seed.tenant_a.id):
        await dt.announce_new_bookings(api, db_session, seed.tenant_a)
    assert len(api.sent) == 1

    # Confirmed: touched, but the visit is where it was.
    await db_session.execute(
        update(Appointment)
        .where(Appointment.id == appointment_id)
        .values(
            status=AppointmentStatus.CONFIRMED, updated_at=datetime.now(UTC) - timedelta(minutes=5)
        )
    )
    with as_tenant(seed.tenant_a.id):
        await dt.announce_new_bookings(api, db_session, seed.tenant_a)
    assert len(api.sent) == 1

    await db_session.execute(
        update(Appointment)
        .where(Appointment.id == appointment_id)
        .values(scheduled_at=_at(day, 14), updated_at=datetime.now(UTC) - timedelta(minutes=2))
    )
    with as_tenant(seed.tenant_a.id):
        await dt.announce_new_bookings(api, db_session, seed.tenant_a)
    assert len(api.sent) == 2 and "vaqti o'zgardi" in api.texts()[-1]


async def test_a_booking_somebody_else_cancels_is_told(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
) -> None:
    since = datetime.now(UTC) - timedelta(hours=1)
    await _prepare(db_session, seed, since)
    appointment_id = await _booking(
        db_session, seed, as_tenant, _at(date.today() + timedelta(days=3), 11),
        datetime.now(UTC) - timedelta(minutes=30), "Dilshod",
    )  # fmt: skip
    api = FakeAPI()
    with as_tenant(seed.tenant_a.id):
        await dt.announce_new_bookings(api, db_session, seed.tenant_a)
    await db_session.execute(
        update(Appointment)
        .where(Appointment.id == appointment_id)
        .values(
            status=AppointmentStatus.CANCELLED, updated_at=datetime.now(UTC) - timedelta(minutes=1)
        )
    )
    with as_tenant(seed.tenant_a.id):
        await dt.announce_new_bookings(api, db_session, seed.tenant_a)
    assert "Qabul bekor qilindi" in api.texts()[-1] and "Dilshod" in api.texts()[-1]


# --- call-back numbers ----------------------------------------------------------


async def test_a_call_back_request_carries_the_time_and_a_button_that_marks_it_rung(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
) -> None:
    since = datetime.now(UTC) - timedelta(hours=1)
    await _prepare(db_session, seed, since)
    await db_session.execute(
        update(Lead)
        .where(Lead.id == seed.a.lead.id)
        .values(
            created_at=datetime.now(UTC) - timedelta(minutes=5),
            convenient_time="kechqurun 6 dan keyin",
            status=LeadStatus.NEW,
        )
    )
    api = FakeAPI()
    with as_tenant(seed.tenant_a.id):
        await dt.announce_new_bookings(api, db_session, seed.tenant_a)
    text = next(t for t in api.texts() if "Qo'ng'iroq qilishni so'radi" in t)
    assert "kechqurun 6 dan keyin" in text
    button = api.sent[-1][2]["inline_keyboard"][0][0]
    assert button["text"] == "✅ Bog'lanildi"

    await dt.handle_update(
        api, db_session, seed.tenant_a, _press(1, DOCTOR, button["callback_data"])
    )
    await db_session.refresh(seed.a.lead)
    assert seed.a.lead.status == LeadStatus.CONTACTED
    assert api.answers[-1] == ("cb", "Belgilandi")

    await dt.handle_update(
        api, db_session, seed.tenant_a, _press(1, DOCTOR, button["callback_data"])
    )
    assert api.answers[-1] == ("cb", "Allaqachon belgilangan")


async def test_the_numbers_screen_lists_only_those_nobody_has_rung(
    db_session: AsyncSession, seed: Seed
) -> None:
    api = FakeAPI()
    await dt.handle_update(api, db_session, seed.tenant_a, _message(1, DOCTOR, dt.BTN_LEADS))
    assert "+998 90 000 00 00" in api.texts()[-1]

    seed.a.lead.status = LeadStatus.CONTACTED
    await db_session.flush()
    await dt.handle_update(api, db_session, seed.tenant_a, _message(1, DOCTOR, dt.BTN_LEADS))
    assert "+998 90 000 00 00" not in api.texts()[-1]


# --- watching -------------------------------------------------------------------


def _wednesday(hour: int, minute: int = 0) -> datetime:
    day = datetime.now(CLINIC_TIMEZONE).date() + timedelta(days=14)
    day += timedelta(days=(2 - day.weekday()) % 7)
    return (
        datetime.combine(day, datetime.min.time(), CLINIC_TIMEZONE)
        .replace(hour=hour, minute=minute)
        .astimezone(UTC)
    )


async def test_a_patient_waiting_half_an_hour_on_a_person_is_told_to_the_doctor_once(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
) -> None:
    now = _wednesday(11)
    seed.a.conversation.needs_doctor_since = now - timedelta(minutes=40)
    await db_session.flush()
    api = FakeAPI()
    with as_tenant(seed.tenant_a.id):
        await dt.escalate_waiting(api, db_session, seed.tenant_a, [CHAT], now)
        await dt.escalate_waiting(
            api, db_session, seed.tenant_a, [CHAT], now + timedelta(minutes=5)
        )
    assert len(api.sent) == 1 and "javob kutmoqda" in api.texts()[0]
    assert "Hello" in api.texts()[0]


async def test_a_patient_who_has_waited_ten_minutes_is_left_to_the_operators(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
) -> None:
    now = _wednesday(11)
    seed.a.conversation.needs_doctor_since = now - timedelta(minutes=10)
    await db_session.flush()
    api = FakeAPI()
    with as_tenant(seed.tenant_a.id):
        await dt.escalate_waiting(api, db_session, seed.tenant_a, [CHAT], now)
    assert api.sent == []


async def test_nothing_is_escalated_at_night(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
) -> None:
    now = _wednesday(23)
    seed.a.conversation.needs_doctor_since = now - timedelta(hours=3)
    await db_session.flush()
    api = FakeAPI()
    with as_tenant(seed.tenant_a.id):
        await dt.escalate_waiting(api, db_session, seed.tenant_a, [CHAT], now)
    assert api.sent == []


async def test_the_morning_summary_comes_once_in_the_morning_and_not_on_a_sunday(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
) -> None:
    morning = _wednesday(8, 45)
    api = FakeAPI()
    with as_tenant(seed.tenant_a.id):
        await dt.morning_summary(api, db_session, seed.tenant_a, [CHAT], _wednesday(8, 0))
        assert api.sent == []  # before half past eight
        await dt.morning_summary(api, db_session, seed.tenant_a, [CHAT], _wednesday(13, 0))
        assert api.sent == []  # too late to be a morning summary
        await dt.morning_summary(api, db_session, seed.tenant_a, [CHAT], morning)
        await dt.morning_summary(
            api, db_session, seed.tenant_a, [CHAT], morning + timedelta(minutes=2)
        )
    assert len(api.sent) == 1 and "Xayrli tong" in api.texts()[0]

    sunday = morning + timedelta(days=4)
    assert sunday.astimezone(CLINIC_TIMEZONE).weekday() == 6
    with as_tenant(seed.tenant_a.id):
        await dt.morning_summary(api, db_session, seed.tenant_a, [CHAT], sunday)
    assert len(api.sent) == 1


async def test_a_summary_that_could_not_be_sent_is_tried_again(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
) -> None:
    morning = _wednesday(9)
    with as_tenant(seed.tenant_a.id):
        await dt.morning_summary(FakeAPI(accepts=False), db_session, seed.tenant_a, [CHAT], morning)
        api = FakeAPI()
        await dt.morning_summary(api, db_session, seed.tenant_a, [CHAT], morning)
    assert len(api.sent) == 1


async def test_a_bot_left_off_is_reported_after_half_an_hour_and_again_when_it_is_back(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
) -> None:
    start = _wednesday(10)
    seed.tenant_a.settings = {**seed.tenant_a.settings, "bot_replies_enabled": False}
    api = FakeAPI()
    with as_tenant(seed.tenant_a.id):
        await dt.watch_assistant(api, db_session, seed.tenant_a, [CHAT], start)
        await dt.watch_assistant(
            api, db_session, seed.tenant_a, [CHAT], start + timedelta(minutes=10)
        )
        assert api.sent == []
        await dt.watch_assistant(
            api, db_session, seed.tenant_a, [CHAT], start + timedelta(minutes=35)
        )
        await dt.watch_assistant(
            api, db_session, seed.tenant_a, [CHAT], start + timedelta(minutes=40)
        )
        assert len(api.sent) == 1 and "o'chirilgan" in api.texts()[0]

        seed.tenant_a.settings = {**seed.tenant_a.settings, "bot_replies_enabled": True}
        await dt.watch_assistant(
            api, db_session, seed.tenant_a, [CHAT], start + timedelta(minutes=50)
        )
    assert len(api.sent) == 2 and "yoqildi" in api.texts()[1]


async def test_the_status_says_who_is_waiting_and_whether_the_bot_is_on(
    db_session: AsyncSession, seed: Seed
) -> None:
    seed.a.conversation.needs_doctor_since = datetime.now(UTC) - timedelta(hours=2)
    seed.tenant_a.settings = {**seed.tenant_a.settings, "bot_replies_enabled": False}
    await db_session.flush()
    api = FakeAPI()
    await dt.handle_update(api, db_session, seed.tenant_a, _message(1, DOCTOR, dt.BTN_STATUS))
    text = api.texts()[-1]
    assert "o'chirilgan" in text and "Javob kutayotganlar: 1" in text and "Hello" in text
