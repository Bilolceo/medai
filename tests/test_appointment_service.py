import asyncio
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from app.channels.instagram.adapter import InstagramAdapter
from app.channels.instagram.client import InstagramClient
from app.core.config import get_settings
from app.core.encryption import encrypt
from app.core.tenant_context import reset_current_tenant, set_current_tenant
from app.models.appointment import Appointment
from app.models.channel import Channel
from app.models.message import MessageSender
from app.models.tenant import Tenant
from app.models.user import User
from app.repositories.appointment import AppointmentRepository
from app.repositories.channel import ChannelRepository
from app.repositories.message import MessageRepository
from app.repositories.tenant import TenantRepository
from app.repositories.user import UserRepository
from app.services.appointment import (
    CLINIC_TIMEZONE,
    SLOT_MINUTES,
    WORK_END,
    WORK_START,
    NoAvailabilityError,
    OutsideWorkingHoursError,
    SlotAlreadyBookedError,
    cancel_appointment,
    check_availability,
    create_appointment,
    find_next_free_slot,
    first_free_doctor,
    notify_patient_of_cancellation,
    slot_capacity,
)
from tests.conftest import Seed, isolated_settings

# A fixed future date, arbitrary but deterministic — avoids "today" flakiness.
BASE_DATE = date(2026, 9, 1)


def _tashkent(local_date: date, hour: int, minute: int = 0) -> datetime:
    return datetime(
        local_date.year, local_date.month, local_date.day, hour, minute, tzinfo=CLINIC_TIMEZONE
    ).astimezone(UTC)


def _all_day_slots(local_date: date) -> list[datetime]:
    """Every bookable slot on local_date, in UTC — a hand-rolled version of
    the service's own _day_slots(), so a bug shared between this test setup
    and the implementation wouldn't hide the tests it's meant to exercise.
    """
    slots = []
    current = datetime.combine(local_date, WORK_START, tzinfo=CLINIC_TIMEZONE)
    end = datetime.combine(local_date, WORK_END, tzinfo=CLINIC_TIMEZONE)
    step = timedelta(minutes=SLOT_MINUTES)
    while current < end:
        slots.append(current.astimezone(UTC))
        current += step
    return slots


# --- check_availability ---


async def test_check_availability_true_for_free_slot(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> None:
    slot = _tashkent(BASE_DATE, 9, 0)
    with as_tenant(seed.tenant_a.id):
        assert await check_availability(AppointmentRepository(db_session), slot) is True


async def test_check_availability_false_for_taken_slot(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> None:
    slot = _tashkent(BASE_DATE, 9, 20)
    with as_tenant(seed.tenant_a.id):
        repo = AppointmentRepository(db_session)
        await create_appointment(repo, user_id=seed.a.user.id, scheduled_at=slot, source="bot")
        assert await check_availability(repo, slot) is False


async def test_check_availability_false_outside_working_hours(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> None:
    too_early = _tashkent(BASE_DATE, 7, 0)
    with as_tenant(seed.tenant_a.id):
        assert await check_availability(AppointmentRepository(db_session), too_early) is False


async def test_check_availability_ignores_other_tenants_booking(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> None:
    slot = _tashkent(BASE_DATE, 13, 0)
    with as_tenant(seed.tenant_b.id):
        await create_appointment(
            AppointmentRepository(db_session),
            user_id=seed.b.user.id,
            scheduled_at=slot,
            source="bot",
        )
    with as_tenant(seed.tenant_a.id):
        assert await check_availability(AppointmentRepository(db_session), slot) is True


# --- create_appointment ---


async def test_create_appointment_books_free_slot(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> None:
    slot = _tashkent(BASE_DATE, 10, 0)
    with as_tenant(seed.tenant_a.id):
        appt = await create_appointment(
            AppointmentRepository(db_session),
            user_id=seed.a.user.id,
            scheduled_at=slot,
            source="bot",
            conversation_id=seed.a.conversation.id,
            patient_name="Alisher",
        )
    assert appt.status == "scheduled"
    assert appt.source == "bot"
    assert appt.patient_name == "Alisher"
    assert appt.scheduled_at == slot


async def test_create_appointment_refuses_taken_slot(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> None:
    slot = _tashkent(BASE_DATE, 11, 0)
    with as_tenant(seed.tenant_a.id):
        repo = AppointmentRepository(db_session)
        await create_appointment(repo, user_id=seed.a.user.id, scheduled_at=slot, source="operator")
        with pytest.raises(SlotAlreadyBookedError):
            await create_appointment(repo, user_id=seed.a.user.id, scheduled_at=slot, source="bot")
        # The session must still be usable after the caught IntegrityError.
        assert await check_availability(repo, slot) is False


async def test_create_appointment_refuses_outside_working_hours(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> None:
    too_late = _tashkent(BASE_DATE, 20, 0)
    with as_tenant(seed.tenant_a.id), pytest.raises(OutsideWorkingHoursError):
        await create_appointment(
            AppointmentRepository(db_session),
            user_id=seed.a.user.id,
            scheduled_at=too_late,
            source="operator",
        )


# --- find_next_free_slot ---


async def test_find_next_free_slot_returns_requested_slot_when_free(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> None:
    slot = _tashkent(BASE_DATE, 9, 0)
    with as_tenant(seed.tenant_a.id):
        result = await find_next_free_slot(AppointmentRepository(db_session), slot)
    assert result == slot


async def test_find_next_free_slot_skips_taken_slot(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> None:
    first = _tashkent(BASE_DATE, 9, 0)
    second = _tashkent(BASE_DATE, 9, 20)
    with as_tenant(seed.tenant_a.id):
        repo = AppointmentRepository(db_session)
        await create_appointment(repo, user_id=seed.a.user.id, scheduled_at=first, source="bot")
        result = await find_next_free_slot(repo, first)
    assert result == second


async def test_find_next_free_slot_rolls_over_to_next_day_when_fully_booked(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> None:
    """A fully-booked day never surfaces as None or an error — the search
    just keeps going into the next working day, so the bot can offer
    tomorrow's opening slot directly ("bugun bo'sh joy yo'q, ertaga soat 9:00
    bo'sh") without a special case for "today is full".
    """
    with as_tenant(seed.tenant_a.id):
        repo = AppointmentRepository(db_session)
        for slot in _all_day_slots(BASE_DATE):
            await create_appointment(repo, user_id=seed.a.user.id, scheduled_at=slot, source="bot")
        result = await find_next_free_slot(repo, _tashkent(BASE_DATE, 9, 0))
    assert result == _tashkent(BASE_DATE + timedelta(days=1), 9, 0)


async def test_find_next_free_slot_raises_when_horizon_exhausted(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> None:
    """Once every slot within the search horizon is booked, the caller gets
    a clean, catchable NoAvailabilityError — not an infinite search and not
    a silent None. horizon_days=0 keeps this cheap: only BASE_DATE itself
    needs to be fully booked to exhaust it.
    """
    with as_tenant(seed.tenant_a.id):
        repo = AppointmentRepository(db_session)
        for slot in _all_day_slots(BASE_DATE):
            await create_appointment(repo, user_id=seed.a.user.id, scheduled_at=slot, source="bot")
        with pytest.raises(NoAvailabilityError):
            await find_next_free_slot(repo, _tashkent(BASE_DATE, 9, 0), horizon_days=0)


# --- cancel_appointment ---


async def test_cancel_appointment_frees_slot_for_rebooking(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> None:
    slot = _tashkent(BASE_DATE, 12, 0)
    with as_tenant(seed.tenant_a.id):
        repo = AppointmentRepository(db_session)
        appt = await create_appointment(
            repo, user_id=seed.a.user.id, scheduled_at=slot, source="bot"
        )
        await cancel_appointment(repo, appt)
        assert await check_availability(repo, slot) is True
        rebooked = await create_appointment(
            repo, user_id=seed.a.user.id, scheduled_at=slot, source="operator"
        )
    assert rebooked.status == "scheduled"
    assert rebooked.id != appt.id


# --- real concurrency: the DB constraint, not application code, decides the winner ---


async def test_concurrent_bookings_only_one_wins() -> None:
    """Two independent DB connections race to book the same slot.

    Deliberately doesn't use the db_session fixture: that fixture wraps
    everything in one connection's transaction, which can't model two truly
    concurrent transactions. This test commits for real, so it cleans up
    everything it creates itself instead of relying on rollback.
    """
    engine = create_async_engine(get_settings().database_url, poolclass=NullPool)
    tenant_id: UUID | None = None
    try:
        async with AsyncSession(engine) as setup:
            tenant = await TenantRepository(setup).create(name="Race Clinic", status="active")
            tenant_id = tenant.id
            token = set_current_tenant(tenant.id)
            try:
                channel = await ChannelRepository(setup).create(
                    type="instagram",
                    credentials=encrypt("token"),
                    external_id=f"ig-race-{tenant.id}",
                )
                user = await UserRepository(setup).create(
                    channel_id=channel.id, external_id="ext-race"
                )
                # Captured before this session closes below — tenant/user
                # become detached at that point, and re-touching their
                # attributes (e.g. tenant.id) from inside _attempt() would
                # raise DetachedInstanceError.
                user_id = user.id
            finally:
                reset_current_tenant(token)
            await setup.commit()

        slot = _tashkent(BASE_DATE, 14, 0)

        async def _attempt() -> bool:
            async with AsyncSession(engine) as session:
                token = set_current_tenant(tenant_id)
                try:
                    try:
                        await create_appointment(
                            AppointmentRepository(session),
                            user_id=user_id,
                            scheduled_at=slot,
                            source="bot",
                        )
                    except SlotAlreadyBookedError:
                        return False
                    await session.commit()
                    return True
                finally:
                    reset_current_tenant(token)

        results = await asyncio.gather(_attempt(), _attempt())
        assert sorted(results) == [False, True]
    finally:
        if tenant_id is not None:
            async with AsyncSession(engine) as cleanup:
                await cleanup.execute(delete(Appointment).where(Appointment.tenant_id == tenant_id))
                await cleanup.execute(delete(User).where(User.tenant_id == tenant_id))
                await cleanup.execute(delete(Channel).where(Channel.tenant_id == tenant_id))
                await cleanup.execute(delete(Tenant).where(Tenant.id == tenant_id))
                await cleanup.commit()
        await engine.dispose()


# --- a slot holds one booking per doctor, not one per clinic -----------


def test_a_clinic_with_no_doctors_listed_can_still_book() -> None:
    """Capacity follows the doctor list, and the floor is what keeps a clinic
    that has not filled its staff in from being able to book nobody at all.
    Every booking made so far is in exactly that state.
    """
    assert slot_capacity(0) == 1
    assert slot_capacity(1) == 1
    assert slot_capacity(3) == 3


def test_the_first_doctor_with_nothing_at_that_time_takes_it() -> None:
    """In listed order rather than by load: a clinic that lists its senior
    first means it, and evening out a rota is the front desk's decision.
    """
    first = SimpleNamespace(id=uuid4(), name="Dr. A")
    second = SimpleNamespace(id=uuid4(), name="Dr. B")

    assert first_free_doctor([first, second], []) is first
    assert first_free_doctor([first, second], [first.id]) is second
    assert first_free_doctor([first, second], [first.id, second.id]) is None
    assert first_free_doctor([], []) is None


def test_a_booking_with_no_doctor_still_fills_a_chair() -> None:
    """An operator's booking entered without a doctor used to block nobody, so
    a one-doctor clinic could have two patients booked into the same slot."""
    only = SimpleNamespace(id=uuid4(), name="Dr. A")
    first = SimpleNamespace(id=uuid4(), name="Dr. A")
    second = SimpleNamespace(id=uuid4(), name="Dr. B")

    assert first_free_doctor([only], [None]) is None
    assert first_free_doctor([first, second], [None]) is first
    assert first_free_doctor([first, second], [None, first.id]) is None
    assert first_free_doctor([first, second], [None, None]) is None


# --- telling the patient their appointment is cancelled -----------------


class _FakeInstagramClient(InstagramClient):
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def send_text(
        self, *, access_token: str, recipient_igsid: str, text: str, sender_igsid: str | None = None
    ) -> None:
        self.calls.append((recipient_igsid, text))

    async def fetch_username(self, *, access_token: str, igsid: str) -> str | None:
        return None

    async def reply_to_comment(self, *, access_token: str, comment_id: str, text: str) -> None:
        raise NotImplementedError

    async def send_private_reply(
        self, *, access_token: str, comment_id: str, text: str, sender_igsid: str | None = None
    ) -> None:
        raise NotImplementedError


async def test_a_patient_who_booked_through_instagram_is_told_the_visit_is_off(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    client = _FakeInstagramClient()
    adapter = InstagramAdapter(client=client)
    with as_tenant(seed.tenant_a.id):
        # Within the messaging window: the patient has to have written
        # recently for the platform to let a reply through at all.
        await MessageRepository(db_session).create(
            conversation_id=seed.a.conversation.id,
            sender=MessageSender.PATIENT,
            content="Assalomu alaykum",
            channel="instagram",
        )
        slot = _tashkent(BASE_DATE, 15, 0)
        appointment = await create_appointment(
            AppointmentRepository(db_session),
            scheduled_at=slot,
            source="instagram",
            user_id=seed.a.user.id,
            conversation_id=seed.a.conversation.id,
            patient_name="Asadbek Risqiyev",
            doctor_name="Dr. Smith",
        )
        await db_session.flush()

        told = await notify_patient_of_cancellation(
            db_session,
            appointment,
            settings=isolated_settings(clinic_phone_numbers="+998 70 310 40 40"),
            adapter=adapter,
        )

        transcript = await MessageRepository(db_session).list_recent(seed.a.conversation.id, 10)

    assert told is True
    [(recipient, text)] = client.calls
    assert recipient == seed.a.user.external_id
    assert "bekor qilindi" in text
    assert "+998 70 310 40 40" in text
    local = slot.astimezone(CLINIC_TIMEZONE)
    assert f"{local:%H:%M}" in text
    # Kept in the transcript, the same as any other reply the patient got.
    assert any(m.content == text for m in transcript)


async def test_a_phone_only_booking_has_nobody_to_tell_and_that_is_not_a_failure(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    client = _FakeInstagramClient()
    with as_tenant(seed.tenant_a.id):
        slot = _tashkent(BASE_DATE, 16, 0)
        appointment = await create_appointment(
            AppointmentRepository(db_session),
            scheduled_at=slot,
            source="operator",
            patient_name="Telefonda yozilgan bemor",
            patient_phone="+998901112233",
            doctor_name="Dr. Smith",
        )
        await db_session.flush()

        told = await notify_patient_of_cancellation(
            db_session,
            appointment,
            settings=isolated_settings(),
            adapter=InstagramAdapter(client=client),
        )

    assert told is False
    assert client.calls == []
