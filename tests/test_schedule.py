"""app.services.appointment.Schedule: each clinic's own week, read from its
settings, with the first clinic's week as the default every clinic starts on."""

import logging
from datetime import UTC, datetime, time
from typing import Any

import pytest

from app.repositories.appointment import AppointmentRepository
from app.services import appointment
from app.services.appointment import (
    CLINIC_TIMEZONE,
    DEFAULT_SCHEDULE,
    SLOT_MINUTES,
    WORK_DAYS,
    WORK_END,
    WORK_START,
    OutsideWorkingHoursError,
    Schedule,
    create_appointment,
    is_within_working_hours,
    schedule_from,
)

# A Monday in Tashkent.
MONDAY = datetime(2026, 11, 2, tzinfo=CLINIC_TIMEZONE)
SUNDAY = datetime(2026, 11, 1, tzinfo=CLINIC_TIMEZONE)


def _at(day: datetime, hour: int, minute: int = 0) -> datetime:
    return day.replace(hour=hour, minute=minute)


# --- the default is the first clinic's week, unchanged ---------------------------


def test_the_default_week_is_monday_to_saturday_nine_to_five_in_twenty_minutes() -> None:
    first_clinic = Schedule(
        slot_minutes=20,
        work_start=time(9, 0),
        work_end=time(17, 0),
        work_days=frozenset({0, 1, 2, 3, 4, 5}),
    )
    assert first_clinic == DEFAULT_SCHEDULE
    # The module constants older code imports are the same week.
    assert first_clinic.slot_minutes == SLOT_MINUTES
    assert first_clinic.work_start == WORK_START
    assert first_clinic.work_end == WORK_END
    assert first_clinic.work_days == WORK_DAYS


def test_no_settings_at_all_is_the_default_week() -> None:
    assert schedule_from({}) == DEFAULT_SCHEDULE
    assert appointment.schedule_for(None) == DEFAULT_SCHEDULE
    # Settings about other things do not touch it.
    assert schedule_from({"bot_replies_enabled": True, "clinic_address": "x"}) == DEFAULT_SCHEDULE


# --- a clinic's own week ------------------------------------------------------


def test_a_clinics_week_is_read_from_its_settings() -> None:
    week = schedule_from(
        {
            "work_days": [1, 2, 3, 4, 5],
            "work_start": "10:00",
            "work_end": "18:00",
            "slot_minutes": 30,
        }
    )

    assert week.work_days == frozenset({0, 1, 2, 3, 4})
    assert (week.work_start, week.work_end, week.slot_minutes) == (time(10), time(18), 30)
    assert [s.strftime("%H:%M") for s in week.day_slots(MONDAY.date())] == [
        "10:00", "10:30", "11:00", "11:30", "12:00", "12:30", "13:00", "13:30",
        "14:00", "14:30", "15:00", "15:30", "16:00", "16:30", "17:00", "17:30",
    ]  # fmt: skip
    # A Saturday is not a working day for this clinic.
    assert list(week.day_slots(MONDAY.date().replace(day=7))) == []


def test_the_days_may_be_written_as_text_or_numbers() -> None:
    assert schedule_from({"work_days": "1,2,3"}).work_days == frozenset({0, 1, 2})
    assert schedule_from({"work_days": ["6", "7"]}).work_days == frozenset({5, 6})
    assert schedule_from({"work_days": "7"}).work_days == frozenset({6})


@pytest.mark.parametrize(
    "settings",
    [
        {"work_days": []},
        {"work_days": [0, 1]},  # 0 is not a day: the numbers are ISO, Monday is 1
        {"work_days": [8]},
        {"work_days": "dushanba"},
        {"work_days": 3},
        {"work_start": "9 dan"},
        {"work_start": 9},
        {"work_end": "25:00"},
        {"slot_minutes": 0},
        {"slot_minutes": 4},
        {"slot_minutes": 241},
        {"slot_minutes": "twenty"},
        {"slot_minutes": True},
        {"slot_minutes": 20.5},
    ],
)
def test_a_value_the_book_cannot_read_falls_back_to_the_default_and_says_so(
    settings: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    """A typo in one setting must not close the clinic: that field alone is
    the default, and the log names it."""
    with caplog.at_level(logging.WARNING):
        assert schedule_from(settings) == DEFAULT_SCHEDULE
    assert "schedule_setting_ignored" in caplog.text


def test_one_bad_field_leaves_the_good_ones_standing() -> None:
    week = schedule_from({"slot_minutes": "x", "work_start": "08:00", "work_days": [1, 2, 3]})
    assert week.slot_minutes == 20
    assert week.work_start == time(8)
    assert week.work_days == frozenset({0, 1, 2})


def test_a_clinic_that_closes_before_it_opens_keeps_the_default_hours() -> None:
    week = schedule_from({"work_start": "18:00", "work_end": "10:00", "slot_minutes": 15})
    assert (week.work_start, week.work_end) == (time(9), time(17))
    assert week.slot_minutes == 15


# --- what is a bookable slot, on the clinic's own week --------------------------


def test_the_slot_grid_is_measured_from_the_clinics_own_opening() -> None:
    week = schedule_from({"work_start": "08:30", "work_end": "12:30", "slot_minutes": 20})

    assert is_within_working_hours(_at(MONDAY, 8, 30), week)
    assert is_within_working_hours(_at(MONDAY, 8, 50), week)
    assert is_within_working_hours(_at(MONDAY, 12, 10), week)
    assert not is_within_working_hours(_at(MONDAY, 12, 30), week)  # closing
    assert not is_within_working_hours(_at(MONDAY, 9, 0), week)  # off this clinic's grid
    assert not is_within_working_hours(_at(MONDAY, 8, 10), week)
    # ...while the same moments against the default week behave as they always did.
    assert is_within_working_hours(_at(MONDAY, 9, 0))
    assert not is_within_working_hours(_at(MONDAY, 8, 30))


def test_a_clinic_open_on_sunday_can_book_a_sunday() -> None:
    week = schedule_from({"work_days": [7]})

    assert is_within_working_hours(_at(SUNDAY, 9, 0), week)
    assert not is_within_working_hours(_at(MONDAY, 9, 0), week)
    assert not is_within_working_hours(_at(SUNDAY, 9, 0))


def test_is_open_is_the_same_question_without_the_grid() -> None:
    assert DEFAULT_SCHEDULE.is_open(_at(MONDAY, 9, 17))
    assert not DEFAULT_SCHEDULE.is_open(_at(MONDAY, 17, 0))
    assert not DEFAULT_SCHEDULE.is_open(_at(SUNDAY, 10, 0))
    assert DEFAULT_SCHEDULE.hours_text() == "09:00 – 17:00"


# --- the booking reads the tenant's week -------------------------------------------


async def test_a_booking_is_checked_against_the_tenants_own_week(
    db_session: Any, seed: Any, as_tenant: Any
) -> None:
    repo = AppointmentRepository(db_session)
    ten_thirty = _at(MONDAY, 10, 30).astimezone(UTC)

    with as_tenant(seed.tenant_a.id):
        # On the default week 10:30 is off the 20-minute grid.
        with pytest.raises(OutsideWorkingHoursError):
            await create_appointment(repo, scheduled_at=ten_thirty, source="test", patient_name="A")

        seed.tenant_a.settings = {
            **(seed.tenant_a.settings or {}),
            "work_start": "10:00",
            "work_end": "18:00",
            "slot_minutes": 30,
        }
        await db_session.flush()

        booked = await create_appointment(
            repo, scheduled_at=ten_thirty, source="test", patient_name="A"
        )
        assert booked.scheduled_at == ten_thirty
        # What the first clinic could book, this one cannot: 09:00 is before it opens.
        with pytest.raises(OutsideWorkingHoursError):
            await create_appointment(
                repo,
                scheduled_at=_at(MONDAY, 9, 0).astimezone(UTC),
                source="test",
                patient_name="B",
            )


async def test_the_free_slots_offered_are_the_tenants_own(
    db_session: Any, seed: Any, as_tenant: Any
) -> None:
    from app.services.booking import free_slots, render

    seed.tenant_a.settings = {
        **(seed.tenant_a.settings or {}),
        "work_days": [1, 2, 3, 4, 5, 6, 7],
        "work_start": "14:00",
        "work_end": "16:00",
        "slot_minutes": 60,
    }
    await db_session.flush()
    repo = AppointmentRepository(db_session)
    now = _at(SUNDAY, 8, 0).astimezone(UTC)

    with as_tenant(seed.tenant_a.id):
        slots = await free_slots(repo, now, horizon_days=0)
        book = render(slots, now, await appointment.schedule(repo))

    assert [s.astimezone(CLINIC_TIMEZONE).strftime("%a %H:%M") for s in slots] == [
        "Sun 14:00",
        "Sun 15:00",
    ]
    assert "60 minutes each" in book
