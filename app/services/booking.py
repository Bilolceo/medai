"""Letting the assistant offer a real free slot and book it.

Asked to book an appointment, the assistant used to ask for a phone number
so that somebody would call back. That is a worse answer than the clinic can
actually give: the schedule is right here, the free slots are known, and the
patient is already typing. A receptionist would say "bugun 14:00 bo'sh,
to'g'ri keladimi?" -- so this makes that answer possible.

Two halves, and the split is deliberate.

Reading: before the model is asked anything, the real free slots are looked
up and written into the prompt as facts. The model never computes
availability, never guesses a time, and cannot offer a slot that is taken --
it picks from a list it was handed. This is also what lets it answer "when
are you free?", because it is holding the whole week rather than one
suggestion.

Writing: the model marks the slot the patient accepted with
[[BOOK:YYYY-MM-DDTHH:MM]] at the end of its reply. The marker is stripped
before the patient sees anything, the slot is re-checked against the
database, and only then is the appointment created.

A marker rather than tool calling, because LLMProvider is text in, text out
and the fallback backend (Qwen through Hugging Face) speaks a different tool
protocol from OpenAI. One marker works identically on both, in a single
call.

The honest weakness is the gap between offering a slot and writing the row:
another booking can land in between. It is re-checked at write time and
never double-booked -- the database's partial unique index is the real
guard -- but by then the reply has already been written, so a lost race is
answered with a fixed correction rather than by asking the model again.
That costs a call this deployment may not have, and the race is rare in a
single clinic; the alternative, telling a patient they are booked when they
are not, is the one failure that ends with somebody standing in a waiting
room.
"""

import logging
import re
import uuid
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.models.appointment import Appointment, AppointmentStatus
from app.repositories.appointment import AppointmentRepository
from app.repositories.doctor import DoctorRepository
from app.services.appointment import (
    CLINIC_TIMEZONE,
    SLOT_MINUTES,
    SlotAlreadyBookedError,
    assign_doctor,
    create_appointment,
    day_slots,
    days_off,
    is_within_working_hours,
)
from app.services.patient_profile import looks_like_a_name, normalise_phone

logger = logging.getLogger(__name__)

# A booking still worth honouring: anything not cancelled or completed.
ACTIVE_STATUSES = (AppointmentStatus.SCHEDULED, AppointmentStatus.CONFIRMED)

# Today plus a fortnight.
#
# Three days was the window, and a patient who wrote "man kelasi seshanba
# 10:00ga yozilmoqchiman" was told next Tuesday was not in this book -- on a
# Friday, when next Tuesday is four days away and entirely free. People book
# around their own week: they ask for a named weekday far more often than
# they ask for "ertaga", and a diary that cannot see that far cannot take
# the booking they came to make.
HORIZON_DAYS = 13

# The most slots to write into the prompt. The doctor's day is 09:00-17:00 on
# a 20-minute grid -- 24 slots -- and a fortnight holds twelve working days,
# so an entirely empty diary fits and the cap is what stops anything longer
# than that crowding out the FAQ context the patient's question needs.
MAX_SLOTS = 288

# How soon a slot may be offered.
#
# The clinic is a journey away for most patients. At 15:58 the book still
# held 16:00 and the assistant offered it, which is a time nobody can keep:
# the patient either misses it or arrives to find the doctor with somebody
# else. Being able to say "the next one I can give you is 16:40" is worth
# more than being able to say "16:00" to somebody who cannot be there.
BOOKING_LEAD = timedelta(minutes=30)

# [[BOOK:2026-09-18T09:20|full name|telephone|reason]]. Everything after the
# time is optional, so a marker written before the name, number or complaint
# arrived still books the time rather than losing it.
BOOKING_MARKER = re.compile(
    r"\[\[\s*BOOK\s*:\s*(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})"
    r"(?:\s*\|\s*(?P<name>[^\]|]{1,80}?))?"
    r"(?:\s*\|\s*(?P<phone>[^\]|]{3,40}?))?"
    r"(?:\s*\|\s*(?P<reason>[^\]|]{1,300}?))?\s*\]\]"
)

# Anything that was trying to be a marker and failed -- "[[BOOK:tomorrow at
# two]]", a half-written one, a repeated one. It books nothing, but it is
# removed all the same: showing a patient "[[BOOK:" is worse than failing to
# book, because it is the one thing that says out loud that nobody is typing.
PLACEHOLDER_NAMES = frozenset(
    {
        "name",
        "your name",
        "patient",
        "patient name",
        "full name",
        "ism",
        "ismi",
        "ismingiz",
        "ism familiya",
        "ism-familiya",
        "ism sharif",
        "ism-sharif",
        "ism sharifingiz",
        "ism-sharifingiz",
        "ism familiyangiz",
        "familiya",
        "bemor",
        "bemor ismi",
        "fio",
        "f.i.o",
        "имя",
        "фио",
        "пациент",
    }
)

MALFORMED_MARKER = re.compile(r"\[\[\s*BOOK[^\]]*\]?\]?")

# Said when the slot the patient accepted was taken between the assistant
# offering it and the row being written. Fixed text, so it cannot mirror the
# patient's language; rare enough to be worth that, and far better than a
# confirmation that is not true.
SLOT_LOST_NOTICE = (
    "\n\nKechirasiz, bu vaqtni hozirgina band qilishdi. Qaysi vaqt sizga qulay bo'lardi?"
)


async def free_slots(
    repo: AppointmentRepository,
    now: datetime,
    *,
    horizon_days: int = HORIZON_DAYS,
    capacity: int = 1,
) -> list[datetime]:
    """Every free, working-hours slot from `now` to the end of the horizon.

    One query for the window's busy slots, then the grid is walked in
    memory -- the same shape as app.services.appointment.find_next_free_slot,
    which returns only the first one.
    """
    local_now = now.astimezone(CLINIC_TIMEZONE)
    last_day = local_now.date() + timedelta(days=horizon_days)
    window_end = datetime.combine(last_day, datetime.max.time(), tzinfo=CLINIC_TIMEZONE)

    # Counted rather than collected: with several doctors a slot is only
    # full once each of them is taken.
    booked = Counter(
        appointment.scheduled_at
        for appointment in await repo.list_active_between(
            local_now.astimezone(UTC), window_end.astimezone(UTC)
        )
    )

    closed = await days_off(repo)
    slots: list[datetime] = []
    for day_offset in range(horizon_days + 1):
        day = local_now.date() + timedelta(days=day_offset)
        if day in closed:
            continue
        for slot in day_slots(day):
            # Far enough ahead to be kept: offering a slot that started ten
            # minutes ago is how a patient ends up told to come at a time
            # that has already passed, and offering one that starts in two
            # minutes is barely better.
            if slot <= local_now + BOOKING_LEAD:
                continue
            if booked[slot.astimezone(UTC)] >= capacity:
                continue
            slots.append(slot)
            if len(slots) >= MAX_SLOTS:
                return slots
    return slots


def _day_label(slot: datetime, local_now: datetime) -> str:
    """How one day of the book is headed.

    "Today" and "tomorrow" are spelled out rather than left to be worked out
    from the date. Asked to do that arithmetic itself the model gets it wrong
    in the direction that costs a booking: a patient was told "ertaga
    2026-08-31" on 31 August, said no, and the slot the front desk thought was
    agreed was never written down.

    The date stays alongside the word, because the marker in rule 8 is written
    from it and a label alone would leave nothing to write.
    """
    days = (slot.date() - local_now.date()).days
    named = {0: "TODAY", 1: "TOMORROW"}.get(days)
    dated = f"{slot:%A %Y-%m-%d}"
    return f"{named} — {dated}" if named else dated


def render(slots: Sequence[datetime], now: datetime) -> str:
    """The free slots as a prompt section, in clinic-local time."""
    local_now = now.astimezone(CLINIC_TIMEZONE)
    header = (
        "\n\nTHE APPOINTMENT BOOK\n"
        f"- Right now it is {local_now:%A %Y-%m-%d %H:%M} in the clinic's own time zone."
    )
    if not slots:
        return f"{header}\n- There is nothing free in the next {HORIZON_DAYS + 1} days."

    by_day: dict[str, list[str]] = {}
    for slot in slots:
        local = slot.astimezone(CLINIC_TIMEZONE)
        by_day.setdefault(_day_label(local, local_now), []).append(f"{local:%H:%M}")

    lines = [f"- {day}: {', '.join(times)}" for day, times in by_day.items()]
    return f"{header}\n- Free slots, {SLOT_MINUTES} minutes each:\n" + "\n".join(lines)


def extract(reply: str) -> tuple[str, datetime | None, str | None]:
    """Split the patient-facing text from the booking the assistant made.

    Returns the reply with every marker removed -- including a malformed
    one, because a patient must never be shown the machinery -- the slot as
    a clinic-local aware datetime, and the patient's name if the assistant
    asked for one.

    The name is optional in the marker rather than required. A booking
    without a name is still a booking the clinic can keep; refusing to write
    one because the patient never said their name would throw away the
    appointment to protect the tidiness of a column.
    """
    match = BOOKING_MARKER.search(reply)
    cleaned = MALFORMED_MARKER.sub("", BOOKING_MARKER.sub("", reply)).strip()
    if match is None:
        return cleaned, None, None
    try:
        naive = datetime.fromisoformat(f"{match.group(1)}T{match.group(2)}")
    except ValueError:  # pragma: no cover - the regex already fixes the shape
        return cleaned, None, None
    name = (match.group("name") or "").strip()
    # The prompt shows the marker's shape as [[BOOK:...|<ism>]], and a model
    # copying the shape rather than filling it in writes the placeholder
    # itself. A row in the clinic's diary reading "Name" is worse than one
    # with no name at all: the front desk cannot tell it is a placeholder.
    candidate = " ".join(name.lower().strip("<>[]{}()").replace("_", " ").split())
    if candidate in PLACEHOLDER_NAMES or (name and not looks_like_a_name(name)):
        # A placeholder, or "Hop" -- the patient's "xo'p" taken for a name
        # (seen live 2026-10-15). No name beats a wrong one on the booking.
        name = ""
    return cleaned, naive.replace(tzinfo=CLINIC_TIMEZONE), name or None


def marker_details(reply: str) -> tuple[str | None, str | None]:
    """The telephone number and complaint from a booking marker, if given."""
    match = BOOKING_MARKER.search(reply)
    if match is None:
        return None, None
    phone = (match.group("phone") or "").strip() or None
    reason = (match.group("reason") or "").strip() or None
    # Same trap as the name: a model copying the marker's shape writes the
    # placeholder words themselves.
    if phone is not None and not any(ch.isdigit() for ch in phone):
        phone = None
    if phone is not None:
        # "998772882" and "+998 99 877 28 82" are one patient; keep one shape.
        phone = normalise_phone(phone) or phone
    if reason is not None and reason.lower() in {"reason", "sabab", "shikoyat", "причина"}:
        reason = None
    return phone, reason


def _record_details(appointment: Appointment, phone: str | None, reason: str | None) -> None:
    if phone:
        appointment.patient_phone = phone
    if reason:
        appointment.notes = reason


async def _active_for_conversation(
    repo: AppointmentRepository, conversation_id: uuid.UUID
) -> Appointment | None:
    """This conversation's live appointment, if it already has one."""
    result = await repo.session.execute(
        select(Appointment)
        .where(
            Appointment.conversation_id == conversation_id,
            Appointment.status.in_(ACTIVE_STATUSES),
        )
        .order_by(Appointment.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def _move(
    repo: AppointmentRepository,
    appointment: Appointment,
    slot: datetime,
    name: str | None,
) -> Appointment:
    """Point an existing booking at `slot`, and record a name if one arrived.

    A slot somebody else holds is left alone: the patient keeps the
    appointment they already have, which is a better answer than losing it
    to a time that was never available.
    """
    # The latest name wins, rather than only filling an empty column.
    # Placeholder-catching is a losing game -- the model has written "Name",
    # "Ismingiz" and "Ism-sharif" so far, each a form of "put the name here"
    # in a different language -- so when one slips through, the real name
    # arriving a turn later has to be able to replace it.
    if name:
        appointment.patient_name = name

    target = slot.astimezone(UTC)
    if appointment.scheduled_at == target:
        await repo.session.flush()
        return appointment

    clash = await repo.get_active_at(target)
    if clash is not None and clash.id != appointment.id:
        logger.warning(
            "booking_move_refused appointment_id=%s slot=%s taken_by=%s",
            appointment.id,
            slot,
            clash.id,
        )
        await repo.session.flush()
        return appointment

    logger.warning(
        "booking_moved appointment_id=%s from=%s to=%s",
        appointment.id,
        appointment.scheduled_at,
        target,
    )
    appointment.scheduled_at = target
    await repo.session.flush()
    return appointment


async def settle(
    session_repo: AppointmentRepository,
    reply: str,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    source: str,
    patient_name: str | None = None,
    allow_second: bool = False,
) -> tuple[str, Appointment | None]:
    """Book whatever the assistant marked, and return what the patient sees.

    The slot is validated against the database here rather than against the
    list that was offered: a time the assistant produced from somewhere else
    is fine if it is genuinely free, and a time from the list is not fine if
    it has since been taken. What matters is the schedule, not the prompt.
    """
    text, slot, marked_name = extract(reply)
    if slot is None:
        return text, None
    phone, reason = marker_details(reply)

    # Re-checked here because the model can write any time into a marker, and
    # a booking moved (below) skips create_appointment's own checks. A closed
    # day, a Sunday or a time off the 20-minute grid is
    # answered as a lost slot -- the patient is asked again rather than told
    # they are booked.
    if not is_within_working_hours(slot) or slot.date() in await days_off(session_repo):
        logger.warning("booking_slot_refused conversation_id=%s slot=%s", conversation_id, slot)
        return text + SLOT_LOST_NOTICE, None

    # One live appointment per conversation, always.
    #
    # A patient who agrees to a time and then answers "my name is ..." gets
    # a second confirmation from the assistant, marker and all -- sometimes
    # naming a different slot than the one it just booked. Creating a row
    # for each would leave that patient holding two appointments, one of
    # which nobody will come to, and the clinic looking at a diary that
    # disagrees with what it told them.
    #
    # So the existing booking is moved rather than duplicated. The reply has
    # already been written and says the new time; making the diary say the
    # same thing is the only outcome where the patient and the clinic agree.
    # A patient may hold several appointments, so a second marker is not
    # automatically a mistake -- but it usually is. The assistant re-confirms
    # a time it has already booked a turn later, when the patient answers
    # "my name is ...", and creating a row for that leaves somebody holding
    # two bookings for one visit. So a second row is written only when the
    # caller says so (allow_second). Everything else moves the booking they
    # already have.
    existing = (
        None if allow_second else await _active_for_conversation(session_repo, conversation_id)
    )
    if existing is not None:
        _record_details(existing, phone, reason)
        return text, await _move(session_repo, existing, slot, marked_name)

    # Chosen before the insert so the row carries a real name: every booking
    # so far has been recorded against "Tayinlanmagan", which is also what
    # patients were being reminded of.
    doctors = await DoctorRepository(session_repo.session).list_active()
    try:
        doctor_id, doctor_name = await assign_doctor(session_repo, doctors, slot.astimezone(UTC))
        appointment = await create_appointment(
            session_repo,
            scheduled_at=slot.astimezone(UTC),
            source=source,
            doctor_id=doctor_id,
            doctor_name=doctor_name,
            user_id=user_id,
            conversation_id=conversation_id,
            patient_name=marked_name or patient_name,
            patient_phone=phone,
            notes=reason,
        )
    except SlotAlreadyBookedError:
        # Whose booking is it? The assistant re-confirming a time it already
        # booked -- which it does, a turn later, when the patient answers
        # "my name is ..." -- lands here holding a slot this very
        # conversation owns. Telling that patient the time was just taken,
        # in the same message that confirms it, is the most confusing thing
        # this code could say, and it is the common case rather than the
        # rare one.
        existing = await session_repo.get_active_at(slot.astimezone(UTC))
        if existing is not None and existing.conversation_id == conversation_id:
            logger.info(
                "booking_already_made appointment_id=%s conversation_id=%s",
                existing.id,
                conversation_id,
            )
            # Their name may have arrived only now, with the second marker.
            if marked_name and not existing.patient_name:
                existing.patient_name = marked_name
            return text, existing
        logger.warning("booking_slot_lost conversation_id=%s slot=%s", conversation_id, slot)
        return text + SLOT_LOST_NOTICE, None
    except Exception:
        # Never fatal: the reply is already written and the patient is
        # waiting for it. A booking that did not happen is recoverable by a
        # human reading the dashboard; a message that never arrives is not.
        logger.exception("booking_failed conversation_id=%s slot=%s", conversation_id, slot)
        return text + SLOT_LOST_NOTICE, None

    logger.warning(
        "booking_created appointment_id=%s conversation_id=%s slot=%s",
        appointment.id,
        conversation_id,
        slot,
    )
    return text, appointment
