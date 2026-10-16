"""Patients who asked to be rung: "menga qo'ng'iroq qiling, raqamim ...".

These land on the dashboard's "Nomer" screen as leads, each with the
patient's handle, their number, and a line saying what the call is about --
so whoever rings them does not open the conversation cold.

Two ways in, in order of preference:

1. The assistant marks the request itself, in the reply that confirms the
   call: [[CALLBACK:+998901234567|buyrak og'rig'i bo'yicha maslahat]]. The
   reason is then a short summary in the patient's own language, written by
   the same model call that answered them -- no extra call, no extra cost.
2. When the reply carries no marker but the patient plainly asked to be
   called and typed a number, the lead is still raised from their own words.
   A request the model forgot to mark is a patient nobody rings, and that is
   the failure this module exists to prevent.

A patient who is being booked gives their number too; that is a booking, not
a callback, and is left to the appointment book.
"""

import logging
import re
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lead import Lead, LeadStatus
from app.models.user import User
from app.repositories.lead import LeadRepository
from app.services.conversation_signals import find_phone_number
from app.services.patient_profile import normalise_phone

logger = logging.getLogger(__name__)

CALLBACK_MARKER = re.compile(
    r"\[\[\s*CALLBACK\s*:\s*(?P<phone>[^\]|]{5,40}?)"
    r"(?:\s*\|\s*(?P<reason>[^\]|]{1,300}?))?"
    r"(?:\s*\|\s*(?P<when>[^\]|]{1,100}?))?\s*\]\]"
)
MALFORMED_MARKER = re.compile(r"\[\[\s*CALLBACK[^\]]*\]?\]?")

# "menga qo'ng'iroq qiling", "telefon qilib yuboring", "перезвоните",
# "позвоните мне", "aloqaga chiqing" -- asking to be rung, in the ways it is
# actually typed. A number alone is not a request: patients also type one to
# be booked, or to answer a question.
CALLBACK_INTENT = re.compile(
    r"(qo'?ng'?iroq\s+qil|qongiroq\s+qil|qo'?ng'?iroq\s+qilib|telefon\s+qil|"
    r"zvon(it|ite)|perezvon|aloqaga\s+chiq|bog'?lan(ib|ing|asizlar)|"
    r"қўнғироқ\s+қил|телефон\s+қил|алоқага\s+чиқ|"
    r"позвон|перезвон|свяжитесь|набери)",
    re.IGNORECASE,
)

MAX_REASON = 255


@dataclass(frozen=True)
class CallbackRequest:
    phone: str
    reason: str | None
    # When the patient said to ring them ("bugun 14:00-15:00", "20:00 dan
    # keyin"). The assistant was promising these times and nothing kept them.
    when: str | None = None


def extract(reply: str) -> tuple[str, CallbackRequest | None]:
    """The reply with any callback marker removed, and the request it made."""
    match = CALLBACK_MARKER.search(reply)
    cleaned = MALFORMED_MARKER.sub("", CALLBACK_MARKER.sub("", reply)).strip()
    if match is None:
        return cleaned, None
    raw = match.group("phone")
    # One shape, +998XXXXXXXXX, as the appointment book and the sheet keep it.
    phone = normalise_phone(raw) or find_phone_number(raw) or raw.strip()
    if not any(ch.isdigit() for ch in phone):
        return cleaned, None
    reason = (match.group("reason") or "").strip() or None
    when = (match.group("when") or "").strip() or None
    return cleaned, CallbackRequest(phone=phone, reason=reason, when=when)


def from_patient_words(patient_said: list[str]) -> CallbackRequest | None:
    """A request the model did not mark: the patient asked to be called and
    gave a number somewhere in the conversation."""
    if not any(CALLBACK_INTENT.search(text) for text in patient_said):
        return None
    phone = next(
        (
            found
            for text in patient_said
            if (found := normalise_phone(text) or find_phone_number(text))
        ),
        None,
    )
    return CallbackRequest(phone=phone, reason=None) if phone else None


async def record(
    session: AsyncSession,
    *,
    user: User | None,
    conversation_id: uuid.UUID,
    request: CallbackRequest,
    fallback_reason: str | None,
) -> tuple[Lead, bool]:
    """Raise the lead, or refresh the one this conversation already has.

    Returns the lead and whether it is new. One open lead per conversation:
    a patient who repeats their number is one call to make, not two.
    """
    repo = LeadRepository(session)
    reason = (request.reason or fallback_reason or "")[:MAX_REASON] or None
    existing = await repo.get_open_for_conversation(conversation_id)
    if existing is not None and existing.status == LeadStatus.NEW:
        await repo.update(
            existing,
            phone=request.phone,
            topic=reason or existing.topic,
            convenient_time=request.when or existing.convenient_time,
            patient_name=existing.patient_name or (user.name if user else None),
        )
        return existing, False
    lead = await repo.create(
        user_id=user.id if user else None,
        conversation_id=conversation_id,
        patient_name=user.name if user else None,
        phone=request.phone,
        topic=reason,
        convenient_time=request.when,
        status=LeadStatus.NEW,
    )
    logger.info("callback_requested", extra={"lead_id": str(lead.id)})
    return lead, True
