"""Turning a patient's message into a reply.

The model is given the clinic's persona (written in the dashboard, see
app.services.persona), the facts that go with it, and the conversation, and
writes the reply itself. Nothing it says is checked or rewritten here.

What this module adds to the persona is the part that is not the clinic's to
edit: the markers the rest of the system reads ([[BOOK:...]], [[CALLBACK:...]],
[[DOCTOR]])
and the appointment book they refer to.
"""

import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.tenant_context import get_current_tenant
from app.models.tenant import Tenant
from app.rag.embeddings import EmbeddingProvider
from app.rag.llm import ChatMessage, LLMProvider, get_llm_provider
from app.rag.retrieval import retrieve_knowledge
from app.repositories.appointment import AppointmentRepository
from app.repositories.doctor import DoctorRepository
from app.repositories.knowledge_base import KnowledgeBaseMatch
from app.repositories.knowledge_document import ChunkMatch
from app.services import booking_offer, medical_safety, price_guard, repetition, spelling
from app.services import persona as persona_service
from app.services.language import (
    clear_script,
    conversation_script,
    reply_fits,
    writes_english,
)
from app.services.patient_profile import Profile, asks_for_language
from app.services.persona import ClinicFacts, PatientState, Persona
from app.services.tenant_resolution import clinic_rules as clinic_rules_for

logger = logging.getLogger(__name__)

_CALLBACK_CONTRACT = """\
If the patient asks to be called and gives a number, end your reply with \
[[CALLBACK:<their number>|<why they want the call, at most twelve words>|<when \
they said to call, in their words, or nothing>]]. The patient never sees it; \
it puts them on the list of people to ring, with the time they asked for. \
Write it once per request, and again with the time if they give one later. \
Say that the clinic will call. When they gave a time, say that time back \
("ertaga 14:30 larda qo'ng'iroq qilishadi") -- the operators ring at the time \
on the list; when they gave none, ask once when suits them. Never choose a \
time yourself."""

_DOCTOR_CONTRACT = """\
When a person from the clinic has to answer this patient themselves -- you \
told them the doctor will reply or you will pass their question to the \
doctor, the facts you were given do not answer what they asked and a person \
must, or they insist on the doctor personally or ask to talk to a real \
person -- end your reply with [[DOCTOR]]. The patient never sees it; it puts \
the conversation at the top of the clinic's inbox until somebody answers. \
Do not write it for anything you answered yourself, for a booking, or for \
the usual advice to come in for an examination.
You only ever speak when the patient writes; you cannot come back to them \
later. So never say "bir daqiqa", "hozir aniqlab beraman", "hozir ko'rib \
beramiz" or anything else that promises you will follow up. Say instead that \
the doctor or the administrator will answer them here, and mark it."""

_MEDICAL_BOUNDS_CONTRACT = """\
You are the clinic's administrator, not a clinician. Beyond never naming a \
medicine or a dose: give no home treatment, exercises or techniques to try, \
no vitamins or supplements, and no opinion on whether a medicine or spray the \
patient already uses is safe, harmful or fine to keep using -- those are the \
doctor's, at an examination. Do not explain what causes a condition or how it \
develops, however many times the patient asks: say the doctor will find the \
cause at the examination. Ask nothing about the symptoms themselves -- how \
long, where, what kind of pain, fever -- the doctor asks that. If you do not \
yet know why they are writing, one general question ("what is bothering \
you?") is enough. Show brief sympathy. When they ask whether the clinic treats \
a condition its doctors work with, say yes. Once in the whole conversation, \
after you have answered what they asked, you may offer to book them or to \
have the clinic call them -- asking for their name, telephone number and a \
convenient time to call, so the clinic can give them the details. Never \
repeat that offer, never put it in an emergency reply, and never make it to a \
patient who has said no."""

_SERVICE_CONTRACT = """\
Answer every question in the patient's message -- a patient who asks whether \
it is treated, what it costs and when to come gets all three answered before \
you ask anything back. The knowledge base shows the records closest to the \
message, not necessarily what the patient meant: when they name a kind of \
service without saying which one ("UZI", "analiz", "tahlil", "УЗИ"), do not \
assume the specific one a record happens to be about -- ask which one they \
need."""

# Here and not only in the persona: a clinic that saved its own copy of the
# persona before the exception existed still forbids every price, and its
# rules telling the assistant to quote one would lose to it.
_PRICE_CONTRACT = """\
Prices: never state one from the knowledge base or the uploaded files. The \
only prices you may state are the ones the clinic's own rules ("Klinikaning \
qo'shimcha qoidalari") tell you to state, and only when the patient asks the \
price of that service -- not when they ask about something else, not \
unprompted, and not for a different service. Asking whether the clinic \
does it, how it goes, or to book it is not asking its price. Say it as the rule does \
(approximate if the rule says approximate), and invent no discount, range or \
other figure, whatever the patient offers or insists on. This holds over \
anything above that forbids all prices."""

_BOOKING_CONTRACT = """\
You can book appointments yourself, from THE APPOINTMENT BOOK below. Only \
times listed there exist. When the patient has agreed to one specific listed \
time and you have their name, telephone number and reason for the visit, \
confirm it in one short message -- saying an operator will call them to \
confirm the time -- and end that same message with \
[[BOOK:YYYY-MM-DDTHH:MM|full name|telephone|reason]], using the date and \
time exactly as the book gives them. The patient never sees the marker; it \
is what writes the appointment down. Never write it before they have agreed \
to a time, and never tell a patient they are booked in a message that does \
not carry it. The reason can be as general as what they already said \
("urolog ko'rigi", "konsultatsiya"); if they never said one, write \
"konsultatsiya" yourself. Never hold a booking up to ask for the reason -- \
once they have agreed a time and given a name and number, book, in whatever \
language the conversation is in. Say the day \
the way a person would ("bugun", "ertaga", "1-sentabr"), never as 2026-09-01."""

_LANGUAGE_CONTRACT = (
    'Reply only in the language and alphabet given as "Javob tili" above: '
    "Russian to a patient writing Russian, Uzbek Cyrillic to one writing Uzbek "
    "in Cyrillic, Uzbek Latin to one writing Latin. Never mix alphabets in one "
    "reply. The clinic's rules and examples sometimes quote exact wording in "
    "another language or alphabet: say what they mean, in the reply's own "
    "language and alphabet, rather than copying them. When the patient's "
    "language is unclear, reply in {default_language}."
)

_SCRIPT_REWRITES = 2

_REPLY_LANGUAGE_LINE = (
    "THIS REPLY: write it entirely in {name} -- every word, the greeting too -- "
    "whatever language the instructions, examples or knowledge above are in."
)

_SCRIPT_NAMES = {
    "uz-latn": "Uzbek, in the Latin alphabet",
    "uz-cyrl": "Uzbek, in the Cyrillic alphabet",
    "ru": "Russian",
}

_WRONG_SCRIPT = """

YOUR LAST REPLY WAS IN THE WRONG LANGUAGE OR ALPHABET
This patient must be answered in {name}. Write the same answer again, \
entirely in {name} -- every word, including any greeting. Keep usernames, \
telephone numbers and any [[...]] marker exactly as they were. Send only the \
corrected reply."""


def _setting(stored: dict[str, Any], key: str, fallback: str | None = None) -> str | None:
    value = stored.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return fallback


def _clinic_facts(
    tenant: Tenant | None,
    settings: Settings,
    doctors: Sequence[Any],
    rules: Sequence[str],
) -> ClinicFacts:
    """The dashboard's own settings first, the deployment's environment second.

    The dashboard has had boxes for the address, the telephone numbers and the
    hours from the start, and nothing read them: a clinic could type its
    address, press save, and watch the bot say it did not know.
    """
    stored = tenant.settings if tenant is not None else {}
    lead = (
        f"{settings.doctor_name}, {settings.doctor_specialty}"
        if settings.doctor_name and settings.doctor_specialty
        else None
    )
    return ClinicFacts(
        name=tenant.name if tenant is not None else None,
        address=_setting(stored, "clinic_address", settings.clinic_address),
        landmark=_setting(stored, "clinic_landmark"),
        phone_numbers=_setting(stored, "clinic_phone_numbers", settings.clinic_phone_numbers),
        work_hours=_setting(stored, "clinic_work_hours", settings.clinic_work_hours),
        doctors=[(d.name, d.specialty, d.working_hours) for d in doctors],
        lead_doctor=lead,
        lead_doctor_background=settings.doctor_background_text if lead else None,
        rules=rules,
    )


def _source(match: ChunkMatch) -> str:
    """Where a piece of an uploaded file came from: "narxlar.pdf, 3-bet"."""
    label = match.chunk.label
    return f"{match.filename}, {label}" if label else match.filename


def _build_system_prompt(
    persona: Persona,
    *,
    facts: ClinicFacts,
    matches: Sequence[KnowledgeBaseMatch],
    state: PatientState,
    default_language: str,
    appointment_book: str | None = None,
    chunks: Sequence[ChunkMatch] = (),
) -> str:
    prompt = persona_service.render(
        persona,
        facts=facts,
        knowledge=[(m.knowledge_base.question, m.knowledge_base.answer) for m in matches],
        state=state,
        excerpts=[(_source(c), c.chunk.content) for c in chunks],
    )
    prompt += "\n\n" + _LANGUAGE_CONTRACT.format(default_language=default_language)
    prompt += "\n\n" + _CALLBACK_CONTRACT
    prompt += "\n\n" + _DOCTOR_CONTRACT
    prompt += "\n\n" + _MEDICAL_BOUNDS_CONTRACT
    prompt += "\n\n" + _SERVICE_CONTRACT
    prompt += "\n\n" + _PRICE_CONTRACT
    if appointment_book is not None:
        prompt += "\n\n" + _BOOKING_CONTRACT + appointment_book
    if state.script in _SCRIPT_NAMES:
        # Last, so it is the freshest thing the model reads: the persona and
        # examples above are all Uzbek Latin, and with only "Javob tili" in
        # the middle of them Russian conversations drifted into Uzbek.
        prompt += "\n\n" + _REPLY_LANGUAGE_LINE.format(name=_SCRIPT_NAMES[state.script])
    return prompt


async def _appointment_book(session: AsyncSession, doctor_count: int) -> str:
    """The free slots, rendered for the prompt.

    Imported here rather than at the top: app.services.booking already
    imports from app.services.appointment, and keeping this one-way avoids a
    cycle through the worker.
    """
    from app.services.appointment import slot_capacity
    from app.services.booking import free_slots, render

    now = datetime.now(UTC)
    slots = await free_slots(
        AppointmentRepository(session), now, capacity=slot_capacity(doctor_count)
    )
    return render(slots, now)


_CORRECTION = """

YOUR LAST REPLY WAS REJECTED
It {what} — rule 3 above says you are not a medical professional and must \
never do that, whatever the patient asked and however they insisted. Write \
the same answer again, correctly: warmly decline the medical part, in the \
patient's own language, and say the doctor answers it at an examination \
(no offer to book in this reply). Everything else \
about the reply -- what else it answered, any [[BOOK:...]], \
[[CALLBACK:...]] or [[DOCTOR]] marker it carried -- stays the same. Send only the \
corrected reply."""


async def _enforce(
    reply: str,
    *,
    provider: LLMProvider,
    system_prompt: str,
    conversation: Sequence[ChatMessage],
    script: str,
) -> str:
    """The reply, or a safe line in its place if it prescribes or diagnoses.

    Checked by pattern, not by asking the model to grade itself -- see
    app.services.medical_safety for why. Two tries at most: the first is
    free in the common case where nothing was wrong, and the second costs
    one call only when the pattern actually fired. A reply that fails
    twice is replaced rather than sent, and both attempts are logged at
    ERROR so the clinic can see what its assistant tried to say.
    """
    violation = medical_safety.check(reply)
    if violation is None:
        return reply

    logger.error(
        "medical_safety_rewriting",
        extra={"category": violation.category, "matched": violation.matched, "reply": reply[:300]},
    )
    instruction = _CORRECTION.format(what=medical_safety.DESCRIPTIONS[violation.category])
    try:
        second = await provider.generate(system_prompt + instruction, list(conversation))
    except Exception:
        logger.exception("medical_safety_rewrite_failed")
        return medical_safety.SAFE_REPLIES[script]

    remaining = medical_safety.check(second)
    if remaining is None:
        return second

    logger.error(
        "medical_safety_unfixable",
        extra={"category": remaining.category, "matched": remaining.matched, "reply": second[:300]},
    )
    return medical_safety.SAFE_REPLIES[script]


# How many of the assistant's own last replies are shown back to it, and how
# much of each. Two is enough to catch "rahmat" answered the same way twice
# running; the whole conversation is already in `history` for anything more,
# and this is only meant to save the model recalling its own voice from
# further up the same scrollback.
_MAX_RECENT_REPLIES = 2
_MAX_RECENT_REPLY_CHARS = 220


def _recent_replies(history: Sequence[ChatMessage] | None) -> tuple[str, ...]:
    """The assistant's own last couple of turns, oldest first."""
    if not history:
        return ()
    own = [turn["content"] for turn in history if turn.get("role") == "assistant"]
    return tuple(text[:_MAX_RECENT_REPLY_CHARS] for text in own[-_MAX_RECENT_REPLIES:])


async def generate_answer(
    session: AsyncSession,
    user_message: str,
    embedding_provider: EmbeddingProvider | None = None,
    llm_provider: LLMProvider | None = None,
    settings: Settings | None = None,
    history: Sequence[ChatMessage] | None = None,
    patient: Profile | None = None,
    long_gap: bool = False,
    situation: str | None = None,
) -> str:
    """Ask the model to answer the patient, with the clinic's facts in hand.

    `situation` is added to the system prompt as it stands, for a message
    that did not arrive as an ordinary chat line (a comment under a post).

    `history` is the conversation's earlier turns, oldest first. Callers get
    it from app.services.conversation.context_for_reply, which already
    excludes the messages being answered right now, so appending
    user_message here cannot repeat them.

    `long_gap` is whether it has been 24+ hours since anything was last said
    in this conversation (app.services.conversation.hours_since_last_contact)
    -- see PatientState.long_gap for what it changes.
    """
    resolved_settings = settings or get_settings()
    tenant_id = get_current_tenant()
    tenant = await session.get(Tenant, tenant_id)

    knowledge = await retrieve_knowledge(
        session, user_message, embedding_provider=embedding_provider
    )
    doctors = await DoctorRepository(session).list_active()

    # A language the patient asked for outranks a message that does not say
    # which language it is in: somebody who said "по-русски" and then typed
    # "ok" was being answered in Uzbek again. It does not outrank a whole
    # sentence plainly written in another language -- a patient whose saved
    # choice was Russian and who wrote "Assalomu alaykum, qabulga
    # yozilmoqchiman" was answered in Russian.
    asked = patient.language if patient else None
    own = clear_script(user_message)
    if asked and (own is None or asks_for_language(user_message) is not None):
        script = asked
    else:
        script = conversation_script(history, user_message)
    state = PatientState(
        name=patient.name if patient else None,
        phone=patient.phone if patient else None,
        script=script,
        # Whether there was anything to send as history: context_for_reply
        # has already dropped the trailing patient turns being answered
        # right now, so an empty list here means, reliably, that this is
        # the first thing this patient has ever said.
        continuing=bool(history),
        recent_replies=_recent_replies(history),
        long_gap=long_gap,
    )
    rules = await clinic_rules_for(session, tenant_id)
    system_prompt = _build_system_prompt(
        persona_service.from_settings(tenant.settings if tenant is not None else None),
        facts=_clinic_facts(tenant, resolved_settings, doctors, rules),
        matches=knowledge.faqs,
        chunks=knowledge.chunks,
        state=state,
        default_language=resolved_settings.default_reply_language,
        appointment_book=(
            await _appointment_book(session, len(doctors))
            if resolved_settings.booking_enabled
            else None
        ),
    )
    if situation:
        system_prompt += "\n\n" + situation
    provider = llm_provider or get_llm_provider()
    conversation: list[ChatMessage] = [
        *(history or []),
        ChatMessage(role="user", content=user_message),
    ]
    reply = await provider.generate(system_prompt, conversation)
    reply = await _enforce(
        reply,
        provider=provider,
        system_prompt=system_prompt,
        conversation=conversation,
        script=script,
    )
    # The rules' prices are said only to a patient asking one; any other
    # message is answered with no sum at all.
    previous = [turn["content"] for turn in history or [] if turn.get("role") == "user"]
    quotable = rules if price_guard.asks_price(user_message, previous) else []
    reply = await _enforce_price(
        reply,
        provider=provider,
        system_prompt=system_prompt,
        conversation=conversation,
        script=script,
        rules=quotable,
    )
    reply = await _enforce_script(
        reply,
        provider=provider,
        system_prompt=system_prompt,
        conversation=conversation,
        script=script,
        rules=quotable,
    )
    reply = await _enforce_fresh(
        reply,
        provider=provider,
        system_prompt=system_prompt,
        conversation=conversation,
        script=script,
        rules=quotable,
        earlier=[turn["content"] for turn in history or [] if turn.get("role") == "assistant"],
        asked=user_message,
    )
    # Last, so a rewrite above cannot put it back: the offer to book is made
    # once a conversation and never after a no (see app.services.booking_offer).
    patient_turns = [turn["content"] for turn in history or [] if turn.get("role") == "user"]
    own_turns = [turn["content"] for turn in history or [] if turn.get("role") == "assistant"]
    # The introduction is made once; a second "Men doktor Temurning
    # administratoriman" in the same conversation reads as a machine.
    if booking_offer.introduced_before(own_turns):
        reply = booking_offer.drop_intro(reply)
    reply = repetition.drop_echoed_opening(reply, own_turns)
    reply = spelling.fix(reply)
    if not booking_offer.wants_to_book(user_message, patient_turns) and (
        booking_offer.offered_before(own_turns)
        or booking_offer.declined([*patient_turns, user_message])
        # The doctor: answer first, then offer. A bare hello has asked nothing.
        or booking_offer.only_hello(user_message)
    ):
        stripped = booking_offer.strip(reply)
        if stripped != reply:
            logger.info("booking_offer_removed", extra={"reply": reply[:300]})
        reply = stripped
    return reply


_PRICE_CORRECTION = """

YOUR LAST REPLY STATED A PRICE THE CLINIC HAS NOT GIVEN YOU
It said {written}. The only sums you may ever write are the ones in the \
clinic's own rules, only for the service they name, and only when the \
patient has asked its price. Write the reply \
again without that sum -- tell the patient the exact price is given by \
telephone, with the clinic's number -- keeping everything else, and any \
[[...]] marker, the same. Send only the corrected reply."""


async def _enforce_price(
    reply: str,
    *,
    provider: LLMProvider,
    system_prompt: str,
    conversation: Sequence[ChatMessage],
    script: str,
    rules: Sequence[str],
) -> str:
    """The reply, with no sum of money the clinic's rules do not give.

    The prompt alone let the knowledge base's price list through two times in
    eight once a rule allowed one price -- see app.services.price_guard. One
    rewrite, like the medical guard; a second miss is replaced by a line that
    sends the patient to the telephone, never sent.
    """
    wrong = price_guard.check(reply, rules)
    if not wrong:
        return reply
    logger.warning("price_not_allowed", extra={"amounts": wrong, "reply": reply[:300]})
    instruction = _PRICE_CORRECTION.format(written=", ".join(wrong))
    try:
        second = await provider.generate(system_prompt + instruction, list(conversation))
    except Exception:
        logger.exception("price_rewrite_failed")
        return price_guard.SAFE_REPLIES.get(script, price_guard.SAFE_REPLIES["uz-latn"])
    if medical_safety.check(second) is None and not price_guard.check(second, rules):
        return second
    logger.error("price_not_allowed_again", extra={"reply": second[:300]})
    return price_guard.SAFE_REPLIES.get(script, price_guard.SAFE_REPLIES["uz-latn"])


async def _enforce_script(
    reply: str,
    *,
    provider: LLMProvider,
    system_prompt: str,
    conversation: Sequence[ChatMessage],
    script: str,
    rules: Sequence[str] = (),
) -> str:
    """The reply, rewritten once if it came back in the wrong language or
    alphabet.

    Told in the prompt, the model still answered one reply in six in the
    wrong one on real conversations -- Latin to a patient writing Cyrillic,
    Russian to one writing Uzbek, a word of each in one sentence. Checked
    here the same way the medical rules are: free when the reply is right,
    one more call when it is not. A second miss is sent rather than
    withheld; an answer in the wrong alphabet still beats silence.
    """
    allow_english = writes_english(conversation[-1]["content"]) if conversation else False
    if script not in _SCRIPT_NAMES or reply_fits(reply, script, allow_english=allow_english):
        return reply
    logger.warning("reply_wrong_script", extra={"script": script, "reply": reply[:200]})
    instruction = _WRONG_SCRIPT.format(name=_SCRIPT_NAMES[script])
    # Two rewrites, not one: on mixed Russian-Uzbek conversations the first
    # rewrite still came back half in the other language about one time in
    # three, and that half-and-half reply was what the patient got.
    best = reply
    for _ in range(_SCRIPT_REWRITES):
        try:
            second = await provider.generate(system_prompt + instruction, list(conversation))
        except Exception:
            logger.exception("reply_script_rewrite_failed")
            return best
        if medical_safety.check(second) is not None or price_guard.check(second, rules):
            continue
        best = second
        if reply_fits(second, script, allow_english=allow_english):
            return second
    logger.warning("reply_wrong_script_again", extra={"script": script})
    return best


_SAID_BEFORE = """

YOUR LAST REPLY REPEATED WHAT YOU HAD ALREADY SAID
These were already sent to this patient earlier in the conversation:
{lines}
Think again about what they are asking now, and answer that -- in new words, \
with something they have not been told yet, or by asking what they mean if \
the question is the same as before. Keep any [[...]] marker as it was. Send \
only the new reply."""


async def _enforce_fresh(
    reply: str,
    *,
    provider: LLMProvider,
    system_prompt: str,
    conversation: Sequence[ChatMessage],
    script: str,
    rules: Sequence[str],
    earlier: Sequence[str],
    asked: str = "",
) -> str:
    """The reply, rewritten once if it says again what was already said.

    See app.services.repetition. A rewrite that breaks a medical, price or
    alphabet rule, or still repeats, is dropped and the first reply sent: a
    repeat is a smaller fault than any of those.
    """
    lines = repetition.repeated(reply, earlier, asked)
    if not lines:
        return reply
    logger.info("reply_repeated", extra={"lines": lines[:3]})
    instruction = _SAID_BEFORE.format(lines="\n".join(f"- {line}" for line in lines[:4]))
    try:
        second = await provider.generate(system_prompt + instruction, list(conversation))
    except Exception:
        logger.exception("reply_fresh_rewrite_failed")
        return reply
    if (
        medical_safety.check(second) is not None
        or price_guard.check(second, rules)
        or (script in _SCRIPT_NAMES and not reply_fits(second, script))
        or repetition.repeated(second, earlier, asked)
    ):
        return reply
    return second
