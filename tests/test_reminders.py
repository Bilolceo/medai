"""Appointment reminders.

Most of these are about the ways the version this replaces could silently
fail to remind somebody: marking a reminder sent that never went out, using
one clinic's bot token for everybody, and losing a reminder outright if the
worker was down when its fixed window passed.
"""

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import ChannelAdapter, ChannelType, DeliveryBlocked
from app.core.encryption import encrypt
from app.models.appointment import AppointmentStatus
from app.models.message import MessageSender
from app.repositories.appointment import AppointmentRepository
from app.repositories.channel import ChannelRepository
from app.repositories.conversation import ConversationRepository
from app.repositories.message import MessageRepository
from app.repositories.user import UserRepository
from app.services.appointment import UNASSIGNED_DOCTOR_NAME
from app.services.reminders import REMINDER_WINDOWS, _message, send_due_reminders
from app.workers.tasks import send_appointment_reminders
from tests.conftest import Seed

NOW = datetime(2026, 9, 1, 6, 0, tzinfo=UTC)


class RecordingAdapter(ChannelAdapter):
    """Stands in for both platforms — reminders are not platform-specific,
    and the adapter is what decides whether a send is allowed at all.
    """

    channel_type = ChannelType.INSTAGRAM

    def __init__(self, blocked: DeliveryBlocked | None = None) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self._blocked = blocked

    async def send_text(
        self,
        *,
        credentials: str,
        recipient_external_id: str,
        text: str,
        reply_context: Mapping[str, Any] | None = None,
    ) -> None:
        self.calls.append((credentials, recipient_external_id, text))

    def delivery_block_reason(
        self, *, credentials: str, last_user_message_at: datetime
    ) -> DeliveryBlocked | None:
        return self._blocked


@pytest.fixture
async def booking_factory(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> Any:
    """Makes an appointment for the seeded patient at a chosen offset from
    NOW, with a channel that carries a real-looking credential.
    """

    async def _make(
        *,
        hours_ahead: float,
        tenant_id: UUID | None = None,
        with_conversation: bool = True,
        status: str = AppointmentStatus.SCHEDULED,
        **overrides: Any,
    ) -> Any:
        tenant = tenant_id or seed.tenant_a.id
        side = seed.a if tenant == seed.tenant_a.id else seed.b
        # The seeded channel already carries an encrypted, non-placeholder
        # credential, so delivery is allowed. Left alone here so a test can
        # set its own token and have it survive.
        with as_tenant(tenant):
            return await AppointmentRepository(db_session).create(
                user_id=side.user.id,
                conversation_id=side.conversation.id if with_conversation else None,
                doctor_name="Dr. Smith",
                scheduled_at=NOW + timedelta(hours=hours_ahead),
                status=status,
                patient_name="Aziza",
                **overrides,
            )

    return _make


# --- what gets reminded, and when ---


async def test_a_booking_a_day_out_gets_the_day_before_reminder(
    db_session: AsyncSession, booking_factory: Any
) -> None:
    booking = await booking_factory(hours_ahead=23)
    adapter = RecordingAdapter()

    run = await send_due_reminders(db_session, now=NOW, adapter=adapter)

    assert run.sent == 1
    assert booking.reminder_24h_sent is True
    assert booking.reminder_2h_sent is False
    [(_credentials, recipient, text)] = adapter.calls
    assert recipient == "ext-" + str(booking.tenant_id)
    assert "ertaga" in text


async def test_a_booking_further_out_than_the_longest_window_is_left_alone(
    db_session: AsyncSession, booking_factory: Any
) -> None:
    booking = await booking_factory(hours_ahead=48)
    adapter = RecordingAdapter()

    run = await send_due_reminders(db_session, now=NOW, adapter=adapter)

    assert run == type(run)(sent=0, failed=0, skipped=0)
    assert adapter.calls == []
    assert booking.reminder_24h_sent is False


async def test_a_booking_in_the_past_is_never_reminded(
    db_session: AsyncSession, booking_factory: Any
) -> None:
    await booking_factory(hours_ahead=-1)
    adapter = RecordingAdapter()

    run = await send_due_reminders(db_session, now=NOW, adapter=adapter)

    assert run.sent == 0
    assert adapter.calls == []


async def test_a_cancelled_booking_is_never_reminded(
    db_session: AsyncSession, booking_factory: Any
) -> None:
    await booking_factory(hours_ahead=23, status=AppointmentStatus.CANCELLED)
    adapter = RecordingAdapter()

    assert (await send_due_reminders(db_session, now=NOW, adapter=adapter)).sent == 0
    assert adapter.calls == []


async def test_a_confirmed_booking_is_still_reminded(
    db_session: AsyncSession, booking_factory: Any
) -> None:
    """Confirming records that somebody spoke to the patient; it does not
    mean they no longer need reminding the day before.
    """
    await booking_factory(hours_ahead=23, status=AppointmentStatus.CONFIRMED)
    adapter = RecordingAdapter()

    assert (await send_due_reminders(db_session, now=NOW, adapter=adapter)).sent == 1


async def test_a_short_notice_booking_gets_one_reminder_not_two(
    db_session: AsyncSession, booking_factory: Any
) -> None:
    """Booked ninety minutes ahead, it is inside both windows at once. The
    day-before reminder's moment passed before the booking existed, so it is
    written off rather than arriving right after the two-hour one.
    """
    booking = await booking_factory(hours_ahead=1.5)
    adapter = RecordingAdapter()

    run = await send_due_reminders(db_session, now=NOW, adapter=adapter)

    assert (run.sent, run.skipped) == (1, 1)
    assert len(adapter.calls) == 1
    assert "bugun" in adapter.calls[0][2]
    assert booking.reminder_24h_sent is True
    assert booking.reminder_2h_sent is True


async def test_a_missed_window_is_caught_up_rather_than_lost(
    db_session: AsyncSession, booking_factory: Any
) -> None:
    """The version this replaces only sent between 22 and 26 hours out, so a
    worker that was down across that window never reminded anybody. Here the
    reminder is still owed and goes out late.
    """
    booking = await booking_factory(hours_ahead=20)
    adapter = RecordingAdapter()

    run = await send_due_reminders(db_session, now=NOW, adapter=adapter)

    assert run.sent == 1
    assert booking.reminder_24h_sent is True


async def test_the_second_reminder_follows_on_a_later_run(
    db_session: AsyncSession, booking_factory: Any
) -> None:
    booking = await booking_factory(hours_ahead=23)
    adapter = RecordingAdapter()

    await send_due_reminders(db_session, now=NOW, adapter=adapter)
    await send_due_reminders(db_session, now=NOW + timedelta(hours=22), adapter=adapter)

    assert booking.reminder_24h_sent is True
    assert booking.reminder_2h_sent is True
    assert len(adapter.calls) == 2
    assert "ertaga" in adapter.calls[0][2]
    assert "bugun" in adapter.calls[1][2]


async def test_a_reminder_is_not_sent_twice(db_session: AsyncSession, booking_factory: Any) -> None:
    await booking_factory(hours_ahead=23)
    adapter = RecordingAdapter()

    await send_due_reminders(db_session, now=NOW, adapter=adapter)
    second_run = await send_due_reminders(db_session, now=NOW, adapter=adapter)

    assert second_run.sent == 0
    assert len(adapter.calls) == 1


# --- a reminder that could not be delivered ---


async def test_an_undeliverable_reminder_is_not_recorded_as_sent(
    db_session: AsyncSession, booking_factory: Any
) -> None:
    """The bug this port exists to fix. The original set the flag whether or
    not the message went out, so a patient it could not reach was recorded as
    reminded and never tried again.
    """
    booking = await booking_factory(hours_ahead=23)
    blocked = RecordingAdapter(blocked=DeliveryBlocked.NOT_CONFIGURED)

    run = await send_due_reminders(db_session, now=NOW, adapter=blocked)

    assert (run.sent, run.failed) == (0, 1)
    assert blocked.calls == []
    assert booking.reminder_24h_sent is False

    # And the next run, once the channel works, still delivers it.
    working = RecordingAdapter()
    assert (await send_due_reminders(db_session, now=NOW, adapter=working)).sent == 1
    assert booking.reminder_24h_sent is True


async def test_a_walk_in_booking_with_no_patient_row_is_skipped_quietly(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    """An operator booking somebody who phoned the clinic has no User row and
    no channel to be reached on. It must not stall the run or be recorded as
    reminded.
    """
    with as_tenant(seed.tenant_a.id):
        booking = await AppointmentRepository(db_session).create(
            doctor_name="Dr. Smith",
            scheduled_at=NOW + timedelta(hours=23),
            status=AppointmentStatus.SCHEDULED,
            patient_name="Telefon orqali",
        )
    adapter = RecordingAdapter()

    run = await send_due_reminders(db_session, now=NOW, adapter=adapter)

    assert run.sent == 0
    assert adapter.calls == []
    assert booking.reminder_24h_sent is False


# --- multi-tenant ---


async def test_each_clinic_is_reminded_with_its_own_credentials(
    db_session: AsyncSession,
    seed: Seed,
    booking_factory: Any,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    """The original sent every reminder with one global bot token, which is
    wrong the moment a second clinic exists.
    """
    with as_tenant(seed.tenant_a.id):
        seed.a.channel.credentials = encrypt("clinic-a-token")
    with as_tenant(seed.tenant_b.id):
        seed.b.channel.credentials = encrypt("clinic-b-token")
    await db_session.flush()

    await booking_factory(hours_ahead=23)
    await booking_factory(hours_ahead=23, tenant_id=seed.tenant_b.id)
    adapter = RecordingAdapter()

    run = await send_due_reminders(db_session, now=NOW, adapter=adapter)

    assert run.sent == 2
    assert {credentials for credentials, _, _ in adapter.calls} == {
        "clinic-a-token",
        "clinic-b-token",
    }


async def test_a_reminder_reaches_a_patient_on_any_channel(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    """The original was Telegram-only; an Instagram patient was never
    reminded at all.
    """
    with as_tenant(seed.tenant_a.id):
        telegram = await ChannelRepository(db_session).create(
            type=ChannelType.TELEGRAM,
            external_id="8100000001",
            credentials=encrypt("telegram-token"),
            config={"webhook_secret": "s"},
        )
        patient = await UserRepository(db_session).create(
            channel_id=telegram.id, external_id="tg-chat-1"
        )
        conversation = await ConversationRepository(db_session).create(
            user_id=patient.id, status="open"
        )
        await AppointmentRepository(db_session).create(
            user_id=patient.id,
            conversation_id=conversation.id,
            doctor_name="Dr. Smith",
            scheduled_at=NOW + timedelta(hours=23),
            status=AppointmentStatus.SCHEDULED,
            patient_name="Aziza",
        )
    adapter = RecordingAdapter()

    run = await send_due_reminders(db_session, now=NOW, adapter=adapter)

    assert run.sent == 1
    assert adapter.calls[0][0] == "telegram-token"
    assert adapter.calls[0][1] == "tg-chat-1"


# --- the transcript ---


async def test_a_sent_reminder_appears_in_the_conversation(
    db_session: AsyncSession,
    seed: Seed,
    booking_factory: Any,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    """So an operator reading the conversation sees what the clinic has
    already said, instead of repeating it.
    """
    await booking_factory(hours_ahead=23)

    await send_due_reminders(db_session, now=NOW, adapter=RecordingAdapter())

    with as_tenant(seed.tenant_a.id):
        messages = await MessageRepository(db_session).list_recent(seed.a.conversation.id, 10)
    bot_messages = [m for m in messages if m.sender == MessageSender.BOT]
    assert len(bot_messages) == 1
    assert "ertaga" in bot_messages[0].content


async def test_a_booking_with_no_conversation_still_gets_its_reminder(
    db_session: AsyncSession, booking_factory: Any
) -> None:
    """A dashboard booking for a patient who has messaged before has a user
    but no conversation attached; there is nowhere to record the reminder,
    which must not stop it being sent.
    """
    booking = await booking_factory(hours_ahead=23, with_conversation=False)
    adapter = RecordingAdapter()

    run = await send_due_reminders(db_session, now=NOW, adapter=adapter)

    assert run.sent == 1
    assert booking.reminder_24h_sent is True


# --- the cron entry point ---


async def test_the_cron_job_runs_the_same_pass(
    db_session: AsyncSession, booking_factory: Any
) -> None:
    from contextlib import asynccontextmanager

    booking = await booking_factory(hours_ahead=23)
    adapter = RecordingAdapter()

    @asynccontextmanager
    async def _factory() -> Any:
        yield db_session

    await send_appointment_reminders({}, session_factory=_factory, now=NOW, adapter=adapter)

    assert booking.reminder_24h_sent is True
    assert len(adapter.calls) == 1


def test_the_windows_are_ordered_longest_lead_first() -> None:
    """_due_windows relies on it: the last due window is the most urgent."""
    leads = [window.lead_time for window in REMINDER_WINDOWS]
    assert leads == sorted(leads, reverse=True)


def test_a_reminder_names_no_doctor_when_none_was_assigned() -> None:
    """The assistant books a time, not a person, so most appointments carry
    the placeholder. Rendered into the doctor template it reached a real
    patient as "Tayinlanmagan qabulida" -- "at the Unassigned's clinic".
    """
    when = datetime(2026, 8, 30, 4, 0, tzinfo=UTC)

    for window in REMINDER_WINDOWS:
        unassigned = _message(
            SimpleNamespace(scheduled_at=when, doctor_name=UNASSIGNED_DOCTOR_NAME),
            window,
        )
        blank = _message(SimpleNamespace(scheduled_at=when, doctor_name=None), window)
        named = _message(SimpleNamespace(scheduled_at=when, doctor_name="Dr. Aliyev A.A."), window)

        assert UNASSIGNED_DOCTOR_NAME not in unassigned
        assert "09:00" in unassigned
        assert unassigned == blank
        # A doctor who is named is still named.
        assert "Dr. Aliyev A.A." in named


@pytest.mark.parametrize(
    ("written", "opening"),
    [
        ("Ертага 9.00 да келаман, катта раҳмат", "Эслатма: эртага"),
        ("Завтра в 9 приду, спасибо", "Напоминание: завтра"),
        ("Ertaga 9 da kelaman, rahmat", "Eslatma: ertaga"),
    ],
)
async def test_a_reminder_is_written_in_the_patients_own_alphabet(
    db_session: AsyncSession,
    seed: Seed,
    booking_factory: Any,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    written: str,
    opening: str,
) -> None:
    """A patient who wrote Cyrillic all along got a Latin reminder and
    answered "Лотинча тушунмадим" (live, 2026-10-20). The patient's own
    messages in the conversation decide the alphabet, Russian included."""
    with as_tenant(seed.tenant_a.id):
        await MessageRepository(db_session).create(
            conversation_id=seed.a.conversation.id,
            sender=MessageSender.PATIENT,
            content=written,
            channel="instagram",
        )
    await booking_factory(hours_ahead=23)
    adapter = RecordingAdapter()

    await send_due_reminders(db_session, now=NOW, adapter=adapter)

    assert len(adapter.calls) == 1
    assert adapter.calls[0][2].startswith(opening)
    assert "Dr. Smith" in adapter.calls[0][2]


async def test_a_booking_without_a_conversation_follows_the_noted_language(
    db_session: AsyncSession,
    seed: Seed,
    booking_factory: Any,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    seed.a.user.preferred_language = "uz-cyrl"
    await db_session.flush()
    await booking_factory(hours_ahead=23, with_conversation=False)
    adapter = RecordingAdapter()

    await send_due_reminders(db_session, now=NOW, adapter=adapter)

    assert adapter.calls[0][2].startswith("Эслатма: эртага")
