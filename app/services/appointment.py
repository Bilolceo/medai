import logging
import uuid
from collections import Counter
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from app.channels.base import ChannelAdapter

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.tenant_context import get_current_tenant
from app.models.appointment import Appointment, AppointmentStatus
from app.models.doctor import Doctor
from app.models.tenant import Tenant
from app.repositories.appointment import AppointmentRepository

logger = logging.getLogger(__name__)

CLINIC_TIMEZONE = ZoneInfo("Asia/Tashkent")


@dataclass(frozen=True)
class Schedule:
    """When the doctor sees patients: which weekdays, from when to when, and
    how long one appointment takes. Every slot the assistant offers, every
    booking the dashboard accepts and every "after hours" figure in a report
    is measured against the same one of these."""

    slot_minutes: int = 20
    work_start: time = time(9, 0)
    work_end: time = time(17, 0)
    # date.weekday() numbers: Monday is 0, Sunday is 6.
    work_days: frozenset[int] = frozenset({0, 1, 2, 3, 4, 5})

    def is_open(self, local: datetime) -> bool:
        """Whether the clinic is seeing patients at this local moment."""
        return local.weekday() in self.work_days and (
            self.work_start <= local.time() < self.work_end
        )

    def day_slots(self, local_date: date) -> Iterator[datetime]:
        """Every slot start on local_date, tz-aware in CLINIC_TIMEZONE."""
        if local_date.weekday() not in self.work_days:
            return
        current = datetime.combine(local_date, self.work_start, tzinfo=CLINIC_TIMEZONE)
        end = datetime.combine(local_date, self.work_end, tzinfo=CLINIC_TIMEZONE)
        step = timedelta(minutes=self.slot_minutes)
        while current < end:
            yield current
            current += step

    def hours_text(self) -> str:
        return f"{self.work_start:%H:%M} – {self.work_end:%H:%M}"


# The first clinic's week, and what every clinic gets until it says otherwise:
# Monday to Saturday, 09:00-17:00, a strict 20 minutes a patient -- 09:00,
# 09:20, ... 16:40. Sunday is a day off.
DEFAULT_SCHEDULE = Schedule()
SLOT_MINUTES = DEFAULT_SCHEDULE.slot_minutes
WORK_START = DEFAULT_SCHEDULE.work_start
WORK_END = DEFAULT_SCHEDULE.work_end
WORK_DAYS = DEFAULT_SCHEDULE.work_days

# Where a clinic's own week is kept, in Tenant.settings: "work_days" as ISO
# weekday numbers (1 is Monday, 7 is Sunday -- the numbers people write),
# "work_start" and "work_end" as "HH:MM", "slot_minutes" as a number. Set by
# scripts/apply_template.py or the dashboard; an unset or unreadable value
# falls back to DEFAULT_SCHEDULE for that field alone, and says so in the log,
# so a typo in one field cannot close the clinic.
_SLOT_MINUTES_RANGE = range(5, 241)


def _parse_time(value: object) -> time | None:
    if not isinstance(value, str):
        return None
    try:
        return time.fromisoformat(value.strip())
    except ValueError:
        return None


def _parse_days(value: object) -> frozenset[int] | None:
    if isinstance(value, str):
        value = [part for part in value.replace(";", ",").split(",") if part.strip()]
    if not isinstance(value, list | tuple | set | frozenset) or not value:
        return None
    days: set[int] = set()
    for item in value:
        try:
            iso = int(str(item).strip())
        except ValueError:
            return None
        if iso not in range(1, 8):
            return None
        days.add(iso - 1)
    return frozenset(days)


def schedule_from(settings: Mapping[str, Any]) -> Schedule:
    """The clinic's week from its settings, DEFAULT_SCHEDULE where unset."""
    base = DEFAULT_SCHEDULE
    bad: list[str] = []

    start = base.work_start
    if "work_start" in settings:
        parsed = _parse_time(settings["work_start"])
        if parsed is None:
            bad.append("work_start")
        else:
            start = parsed
    end = base.work_end
    if "work_end" in settings:
        parsed = _parse_time(settings["work_end"])
        if parsed is None:
            bad.append("work_end")
        else:
            end = parsed
    if start >= end:
        bad.append("work_start>=work_end")
        start, end = base.work_start, base.work_end

    minutes = base.slot_minutes
    if "slot_minutes" in settings:
        raw = settings["slot_minutes"]
        if isinstance(raw, bool) or not isinstance(raw, int | str):
            bad.append("slot_minutes")
        else:
            try:
                candidate = int(str(raw).strip())
            except ValueError:
                candidate = -1
            if candidate in _SLOT_MINUTES_RANGE:
                minutes = candidate
            else:
                bad.append("slot_minutes")

    days = base.work_days
    if "work_days" in settings:
        parsed_days = _parse_days(settings["work_days"])
        if parsed_days is None:
            bad.append("work_days")
        else:
            days = parsed_days

    if bad:
        logger.warning("schedule_setting_ignored", extra={"fields": bad})
    return Schedule(slot_minutes=minutes, work_start=start, work_end=end, work_days=days)


def schedule_for(tenant: Tenant | None) -> Schedule:
    return schedule_from(tenant.settings or {}) if tenant is not None else DEFAULT_SCHEDULE


async def schedule(repo: AppointmentRepository) -> Schedule:
    """The current tenant's week."""
    return schedule_for(await repo.session.get(Tenant, get_current_tenant()))


# Used when a booking names no doctor at all. The clinic's real clinicians
# now live in the doctors table (app.models.doctor), so this is no longer a
# stand-in for "multi-doctor support does not exist" — it is the fallback for
# a booking taken before anyone was assigned, which an operator resolves
# later from the dashboard.
UNASSIGNED_DOCTOR_NAME = "Tayinlanmagan"
DEFAULT_SEARCH_HORIZON_DAYS = 14


class SlotAlreadyBookedError(Exception):
    """create_appointment() lost the race (or simply arrived second) for
    this exact slot. The DB's partial unique index is what actually
    enforces this — this exception is just the graceful, catchable form of
    the IntegrityError it raises.
    """

    def __init__(self, scheduled_at: datetime) -> None:
        self.scheduled_at = scheduled_at
        super().__init__(f"slot already booked: {scheduled_at.isoformat()}")


# Where the days the doctor said they cannot work are kept, as ISO dates. Set
# from the doctor's Telegram bot (app.services.doctor_telegram) and read on
# every booking, so a day the doctor has called off stays closed however the
# booking is attempted -- the dashboard, the web app or the assistant.
DAYS_OFF_KEY = "doctor_days_off"


class DoctorDayOffError(Exception):
    """scheduled_at falls on a day the doctor has said they will not work."""

    def __init__(self, scheduled_at: datetime) -> None:
        self.scheduled_at = scheduled_at
        super().__init__(f"doctor is off that day: {scheduled_at.isoformat()}")


def days_off_from(settings: dict[str, Any]) -> set[date]:
    days: set[date] = set()
    for raw in settings.get(DAYS_OFF_KEY, []) or []:
        try:
            days.add(date.fromisoformat(raw))
        except (TypeError, ValueError):
            continue
    return days


async def days_off(repo: AppointmentRepository) -> set[date]:
    tenant = await repo.session.get(Tenant, get_current_tenant())
    return days_off_from(tenant.settings) if tenant is not None else set()


class OutsideWorkingHoursError(Exception):
    """scheduled_at falls outside working hours, or isn't aligned to the slot grid."""

    def __init__(self, scheduled_at: datetime) -> None:
        self.scheduled_at = scheduled_at
        super().__init__(f"outside working hours: {scheduled_at.isoformat()}")


class NoAvailabilityError(Exception):
    """No free slot was found anywhere in the search horizon."""

    def __init__(self, from_datetime: datetime, horizon_days: int) -> None:
        self.from_datetime = from_datetime
        self.horizon_days = horizon_days
        super().__init__(
            f"no free slot found within {horizon_days} days of {from_datetime.isoformat()}"
        )


class MissingPatientIdentityError(Exception):
    """create_appointment() was given neither user_id nor patient_name — a
    booking must be identifiable as *someone's* appointment.
    """

    def __init__(self) -> None:
        super().__init__("create_appointment requires at least one of user_id or patient_name")


def _to_local(scheduled_at: datetime) -> datetime:
    return scheduled_at.astimezone(CLINIC_TIMEZONE)


def is_within_working_hours(scheduled_at: datetime, schedule: Schedule = DEFAULT_SCHEDULE) -> bool:
    """Whether scheduled_at (any tz) lands on a bookable slot: inside
    [work_start, work_end) local clinic time, and aligned to the
    slot_minutes grid measured from work_start.

    A time that's merely *inside* working hours but off-grid (e.g. 9:17) is
    not a valid slot — the bot/dashboard only ever offer grid-aligned times,
    so an off-grid scheduled_at reaching here means the caller built it
    wrong.
    """
    local = _to_local(scheduled_at)
    if not schedule.is_open(local):
        return False
    if local.second or local.microsecond:
        return False
    minutes_since_open = (local.hour * 60 + local.minute) - (
        schedule.work_start.hour * 60 + schedule.work_start.minute
    )
    return minutes_since_open % schedule.slot_minutes == 0


def day_slots(local_date: date, schedule: Schedule = DEFAULT_SCHEDULE) -> Iterator[datetime]:
    """Every bookable local slot start on local_date, tz-aware in CLINIC_TIMEZONE.

    Public because app.services.booking walks the same grid to list what is
    free for the assistant to offer — one definition of "a slot", so the
    times a patient is offered cannot drift from the times that can be
    booked.
    """
    return schedule.day_slots(local_date)


def _first_slot_on_or_after(local_dt: datetime, schedule: Schedule) -> datetime:
    """The first grid-aligned local slot start that is >= local_dt — rolls
    to the next day's opening slot if local_dt is after today's last slot.
    """
    for slot in schedule.day_slots(local_dt.date()):
        if slot >= local_dt:
            return slot
    # The next working day's opening slot -- not simply tomorrow's, which on
    # a Saturday evening is a Sunday with no slots at all.
    for offset in range(1, 8):
        opening = next(iter(schedule.day_slots(local_dt.date() + timedelta(days=offset))), None)
        if opening is not None:
            return opening
    raise ValueError("no working day in the coming week")


def slot_capacity(doctor_count: int) -> int:
    """How many bookings one time slot can hold.

    One per active doctor -- and one when the clinic has listed none at all.
    Without that floor, a practice that has not filled in its staff would be
    able to book nobody, which is a worse failure than the one it replaces.
    """
    return max(doctor_count, 1)


def first_free_doctor(
    doctors: Sequence[Doctor], taken: Collection[uuid.UUID | None]
) -> Doctor | None:
    """The first doctor with nothing at this slot, or None if all are busy.

    In listed order rather than by load: a clinic that lists its senior first
    means it, and evening out a rota is a scheduling decision the front desk
    makes, not one to bury in a booking helper.
    """
    # A booking with no doctor on it still fills one doctor's chair: without
    # counting it, a one-doctor clinic had an operator's unassigned booking
    # and the assistant's booking land on the same slot.
    busy = {doctor_id for doctor_id in taken if doctor_id is not None}
    unassigned = sum(1 for doctor_id in taken if doctor_id is None)
    free = [doctor for doctor in doctors if doctor.id not in busy]
    return free[0] if len(free) > unassigned else None


async def assign_doctor(
    repo: AppointmentRepository,
    doctors: Sequence[Doctor],
    scheduled_at: datetime,
) -> tuple[uuid.UUID | None, str]:
    """Which doctor takes this slot, and the name to record on the booking.

    Returns (None, UNASSIGNED_DOCTOR_NAME) when the clinic has listed no
    doctors -- the state every booking has been in so far, and the reason
    patients were reminded of an appointment with "Tayinlanmagan".
    """
    if not doctors:
        return None, UNASSIGNED_DOCTOR_NAME
    taken = [appt.doctor_id for appt in await repo.list_active_at(scheduled_at)]
    chosen = first_free_doctor(doctors, taken)
    if chosen is None:
        raise SlotAlreadyBookedError(scheduled_at)
    return chosen.id, chosen.name


async def check_availability(
    repo: AppointmentRepository, scheduled_at: datetime, *, capacity: int = 1
) -> bool:
    """Whether this exact slot could be booked right now.

    UX only — a True here is not a hold on the slot, so create_appointment()
    can still lose a race against a booking that lands between this check
    and the insert. That race is resolved by the DB's partial unique index,
    not by this function.
    """
    if not is_within_working_hours(scheduled_at, await schedule(repo)):
        return False
    return len(await repo.list_active_at(scheduled_at)) < capacity


async def find_next_free_slot(
    repo: AppointmentRepository,
    from_datetime: datetime,
    *,
    horizon_days: int = DEFAULT_SEARCH_HORIZON_DAYS,
    capacity: int = 1,
) -> datetime:
    """The first free, working-hours, grid-aligned slot at or after
    from_datetime, searching up to horizon_days ahead.

    Fetches the whole window's busy slots in a single query, then walks the
    local slot grid in memory — not one query per candidate slot.
    """
    week = await schedule(repo)
    local_start = _first_slot_on_or_after(_to_local(from_datetime), week)
    last_day = local_start.date() + timedelta(days=horizon_days)
    # Bounded by the *end* of the last day checked (work_end), not
    # local_start's time-of-day N days out — otherwise the busy-set query
    # would cut off mid-afternoon on the last day and a real booking late in
    # the horizon could be missed, making an actually-taken slot look free.
    window_end_local = datetime.combine(last_day, week.work_end, tzinfo=CLINIC_TIMEZONE)

    # Counted, not collected into a set: a slot is full only once every
    # doctor in it is taken.
    booked = Counter(
        appt.scheduled_at
        for appt in await repo.list_active_between(
            local_start.astimezone(UTC), window_end_local.astimezone(UTC)
        )
    )

    closed = await days_off(repo)
    for day_offset in range(horizon_days + 1):
        day = local_start.date() + timedelta(days=day_offset)
        if day in closed:
            continue
        for slot in week.day_slots(day):
            if slot < local_start:
                continue
            candidate_utc = slot.astimezone(UTC)
            if booked[candidate_utc] < capacity:
                return candidate_utc

    raise NoAvailabilityError(from_datetime, horizon_days)


async def create_appointment(
    repo: AppointmentRepository,
    *,
    scheduled_at: datetime,
    source: str,
    user_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
    patient_name: str | None = None,
    doctor_name: str = UNASSIGNED_DOCTOR_NAME,
    doctor_id: uuid.UUID | None = None,
    patient_phone: str | None = None,
    notes: str | None = None,
) -> Appointment:
    """Books scheduled_at, refusing to double-book.

    user_id is optional: a bot booking always has one (there's no
    appointment without a prior IG conversation), but an operator booking a
    walk-in or phone patient who's never messaged the clinic has no User row
    to attach — patient_name is that booking's identity instead. At least
    one of the two is required, checked here rather than left as a DB-level
    surprise.

    The is_within_working_hours check up front just gives an obviously-wrong
    caller a clear error early. The actual double-booking guard is the
    partial unique index on (tenant_id, scheduled_at): a violation surfaces
    here as IntegrityError and is translated to SlotAlreadyBookedError so
    callers (bot/operator/dashboard) never see a raw DB error.
    """
    if user_id is None and not patient_name:
        raise MissingPatientIdentityError()
    if not is_within_working_hours(scheduled_at, await schedule(repo)):
        raise OutsideWorkingHoursError(scheduled_at)
    if _to_local(scheduled_at).date() in await days_off(repo):
        raise DoctorDayOffError(scheduled_at)
    try:
        # A dedicated SAVEPOINT for just this insert attempt: on
        # IntegrityError, SQLAlchemy rolls back to it automatically before
        # re-raising, undoing only this attempt. Rolling back the session
        # itself (session.rollback()) would instead unwind everything since
        # the session's own autobegin — including any earlier, already-
        # successful work in the same request/session — which is not what a
        # "this one slot was taken" error should do.
        async with repo.session.begin_nested():
            return await repo.create(
                user_id=user_id,
                scheduled_at=scheduled_at,
                status=AppointmentStatus.SCHEDULED,
                source=source,
                conversation_id=conversation_id,
                patient_name=patient_name,
                doctor_name=doctor_name,
                doctor_id=doctor_id,
                patient_phone=patient_phone,
                notes=notes,
            )
    except IntegrityError:
        raise SlotAlreadyBookedError(scheduled_at) from None


async def cancel_appointment(repo: AppointmentRepository, appointment: Appointment) -> Appointment:
    """Cancels a booking, which — via the partial unique index — immediately
    frees its slot for rebooking.
    """
    return await repo.update(appointment, status=AppointmentStatus.CANCELLED)


async def confirm_appointment(repo: AppointmentRepository, appointment: Appointment) -> Appointment:
    """Marks a booking as confirmed with the patient.

    Still holds its slot (see ACTIVE_STATUSES) — confirming records that
    somebody spoke to the patient, it does not change what the time is
    booked for.
    """
    return await repo.update(appointment, status=AppointmentStatus.CONFIRMED)


class CancelledAppointmentError(Exception):
    """A cancelled booking has no visit to mark as attended or missed."""


class NotYetDueError(Exception):
    """The visit has not happened yet, so whether the patient came is unknown."""


# How early a visit may be marked: a patient who walks in half an hour
# before their slot has come, and the front desk marks it when they do.
OUTCOME_GRACE = timedelta(minutes=30)
OUTCOME_STATUSES = frozenset({AppointmentStatus.COMPLETED, AppointmentStatus.NO_SHOW})


async def record_outcome(
    repo: AppointmentRepository,
    appointment: Appointment,
    outcome: AppointmentStatus,
    *,
    now: datetime | None = None,
) -> Appointment:
    """Marks whether the patient came -- "Keldi" or "Kelmadi".

    A booking that is still on, or already marked the other way (so a
    wrong click can be put right), and whose time has come. Not a
    cancelled one: there was no visit to come to. Either outcome frees
    the slot, like any status outside ACTIVE_STATUSES -- it is in the past.
    """
    if outcome not in OUTCOME_STATUSES:
        raise ValueError(f"not an outcome: {outcome}")
    if appointment.status == AppointmentStatus.CANCELLED:
        raise CancelledAppointmentError
    if appointment.scheduled_at - OUTCOME_GRACE > (now or datetime.now(UTC)):
        raise NotYetDueError
    return await repo.update(appointment, status=outcome)


async def notify_patient_of_cancellation(
    session: AsyncSession,
    appointment: Appointment,
    *,
    settings: Settings,
    adapter: "ChannelAdapter | None" = None,
) -> bool:
    """Tell the patient their appointment is cancelled, over whichever
    channel they booked through. Returns whether it actually reached them.

    Written for the dashboard's own cancel button (app.api.admin.schedule),
    which used to change the row and nothing else -- a patient whose
    appointment a member of staff cancelled from the browser never heard
    about it, and found out by arriving. app.services.doctor_telegram has
    its own version of this for the doctor's own cancel flow, which also
    closes a whole day at once and needs that day's wording; this one is
    the plainer, single-slot case.

    Silently does nothing, rather than failing, when there is nobody to
    tell: a phone-only booking (operator source, no user_id) has no
    Instagram/Telegram thread to send into -- see Appointment.user_id's own
    comment for why that is a normal booking, not a broken one.
    """
    from app.models.message import MessageSender
    from app.models.user import User
    from app.services.conversation import (
        last_inbound_at,
        record_outbound_message,
        reply_context_for,
    )
    from app.services.delivery import send_reply

    if appointment.user_id is None or appointment.conversation_id is None:
        return False
    user = await session.get(User, appointment.user_id)
    if user is None:
        return False

    local = appointment.scheduled_at.astimezone(CLINIC_TIMEZONE)
    phone = settings.clinic_phone_numbers
    rebook = (
        f" Boshqa vaqtga yozilish uchun {phone} raqamiga qo'ng'iroq qiling."
        if phone
        else " Boshqa vaqtga yozilish uchun biz bilan bog'laning."
    )
    text = (
        f"Assalomu alaykum! {local:%d.%m.%Y} kuni soat {local:%H:%M} dagi "
        f"qabulingiz bekor qilindi.{rebook} Noqulaylik uchun uzr so'raymiz."
    )

    conversation_id = appointment.conversation_id
    last_seen = await last_inbound_at(session, conversation_id)
    try:
        delivered = await send_reply(
            session,
            channel_id=user.channel_id,
            recipient_external_id=user.external_id,
            text=text,
            last_user_message_at=last_seen or appointment.created_at,
            reply_context=await reply_context_for(session, conversation_id),
            adapter=adapter,
        )
    except Exception:  # noqa: BLE001 - the cancellation itself is already saved
        logger.exception(
            "cancellation_notice_failed", extra={"appointment_id": str(appointment.id)}
        )
        return False

    if delivered is not None:
        await record_outbound_message(
            session,
            conversation_id=conversation_id,
            channel_type=delivered,
            text=text,
            sender=MessageSender.BOT,
        )
    return delivered is not None
