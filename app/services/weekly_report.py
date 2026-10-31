"""The week for the doctor: what the bot did, and what the operators did.

The doctor reads a Telegram message on Monday morning rather than opening the
dashboard, so this works out the figures and writes them as messages. Two
halves, and the second is why it exists:

* **The bot** -- who wrote, who was answered, how fast, what they asked.
* **The operators** -- the chats the bot handed over: how long patients waited
  for a person, who is waiting now, which numbers the operators wrote that are
  not the clinic's, and whether the call-back numbers the bot collected were
  ever rung.

Compared with the week before, so a number is a direction and not just a
figure. Nothing is stored: it is computed from the transcript each time.

Everything is counted from what was written, which means an operator's
Instagram *automatic* reply ("Murojaat uchun: ...", a few seconds after the
patient) is not mistaken for a person answering: a text the account sends
verbatim to several patients within seconds of their message is set apart.
"""

import html
import re
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from statistics import median

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.appointment import Appointment
from app.models.conversation import Conversation
from app.models.lead import Lead, LeadStatus
from app.models.message import Message, MessageSender
from app.models.tenant import Tenant
from app.models.user import User
from app.services import message_labels, phone_guard
from app.services.appointment import CLINIC_TIMEZONE, DEFAULT_SCHEDULE, Schedule, schedule_for

TELEGRAM_LIMIT = 3800  # under Telegram's 4096, with room for the markup
# A reply to a message inside this long is "answering" it; later is a new
# conversation as far as speed goes.
BOT_REPLY_CEILING = timedelta(hours=1)
OPERATOR_REPLY_CEILING = timedelta(hours=72)
# An automatic reply arrives within seconds of the message it answers.
AUTOMATIC_WITHIN = timedelta(seconds=20)
AUTOMATIC_SEEN_IN = 3  # conversations the same text went to
SLOW_OPERATOR = timedelta(minutes=15)
STALE_LEAD = timedelta(days=3)
WAITING_AFTER = timedelta(minutes=15)
# A chat waiting longer than this is a backlog, not this week's work: said
# apart, so one forgotten fortnight does not drown what is happening now.
OLD_WAITING = timedelta(days=7)
# "Rahmat", "ok", "hop": the patient closing the conversation, not asking.
_ACKNOWLEDGEMENT = re.compile(
    r"^\W*(rahmat|raxmat|рахмат|рақмат|спасибо|thanks|ok|okey|ок|хоп|hop|mayli|майли|yaxshi|"
    r"яхши|tushund|тушунд)",
    re.IGNORECASE,
)

_MONTHS = [
    "yanvar", "fevral", "mart", "aprel", "may", "iyun",
    "iyul", "avgust", "sentabr", "oktabr", "noyabr", "dekabr",
]  # fmt: skip

# What patients ask about, by the words they use (Latin and Cyrillic); counted
# once per person. A direction, not an audit: a misspelling is missed.
_TOPICS: dict[str, str] = {
    "narx": r"(narx|qancha|necha pul|nechpul|нарх|канча|неча пул|нечпул|сколько|цена|стоит)",
    "manzil": r"(manzil|qayerda|joylash|адрес|манзил|каерда|кайерда|где находи)",
    "qabulga yozilish": r"(qabulga|yozil|yozdir|buyurtma|navbat|записа|ёзил|буюртма|навбат)",
    "telefon / bog'lanish": r"(telefon|raqam|bog'lan|bog`lan|aloqa|телефон|рақам|боғлан|алоқа)",
    "implant": r"(implant|имплант|имплан|эмплант|knopka|кнопка)",
    "tez bo'shanish": r"(tez bo'sh|tez bosh|tez boʻsh|тез бўш|тез буш|преждевремен)",
    "erektsiya": r"(erek|ereks|quvvat|kuchsiz|олат|қувват|кувват|эрекц)",
    "farzand / bepushtlik": r"(farzand|bepusht|urug|spermo|фарзанд|бепушт|уруг|сперм|бесплод)",
    "prostata": r"(prostat|простат)",
    "tomir (varikotsele)": r"(varikos|varikots|tomir|варикоц|томир)",
}
_TOPIC_RX = {name: re.compile(rx, re.IGNORECASE) for name, rx in _TOPICS.items()}


# --- the period -------------------------------------------------------------------


@dataclass(frozen=True)
class Window:
    """Whole local days, first through last inclusive."""

    first_day: date
    last_day: date

    @property
    def start(self) -> datetime:
        return datetime.combine(self.first_day, time.min, tzinfo=CLINIC_TIMEZONE).astimezone(UTC)

    @property
    def end(self) -> datetime:
        after = self.last_day + timedelta(days=1)
        return datetime.combine(after, time.min, tzinfo=CLINIC_TIMEZONE).astimezone(UTC)

    def previous(self) -> "Window":
        days = (self.last_day - self.first_day).days + 1
        return Window(self.first_day - timedelta(days=days), self.first_day - timedelta(days=1))

    def label(self) -> str:
        a, b = self.first_day, self.last_day
        if a.month == b.month:
            return f"{a.day}–{b.day} {_MONTHS[b.month - 1]}"
        return f"{a.day} {_MONTHS[a.month - 1]} – {b.day} {_MONTHS[b.month - 1]}"


def last_full_week(now: datetime) -> Window:
    """Monday to Sunday before the one ``now`` is in."""
    today = now.astimezone(CLINIC_TIMEZONE).date()
    monday = today - timedelta(days=today.weekday())
    return Window(monday - timedelta(days=7), monday - timedelta(days=1))


def rolling_week(now: datetime) -> Window:
    """The last seven days, today included: for asking in the middle of a week."""
    today = now.astimezone(CLINIC_TIMEZONE).date()
    return Window(today - timedelta(days=6), today)


def is_after_hours(moment: datetime, schedule: Schedule = DEFAULT_SCHEDULE) -> bool:
    return not schedule.is_open(moment.astimezone(CLINIC_TIMEZONE))


# --- the figures ------------------------------------------------------------------


@dataclass
class Figures:
    people: int = 0
    new_people: int = 0
    wrote_words: int = 0
    answered: int = 0
    unanswered: int = 0
    unanswered_clusters: list[tuple[datetime, datetime, int]] = field(default_factory=list)
    patient_messages: int = 0
    bot_replies: int = 0
    bot_after_hours: int = 0
    bot_seconds: list[float] = field(default_factory=list)
    operator_messages: int = 0
    automatic_replies: int = 0
    operator_conversations: int = 0
    operator_after_hours: int = 0
    operator_seconds: list[float] = field(default_factory=list)
    operator_work_seconds: list[float] = field(default_factory=list)
    foreign_numbers: Counter[str] = field(default_factory=Counter)
    foreign_messages: int = 0
    comments: int = 0
    shared_posts: int = 0
    voice_notes: int = 0
    photos: int = 0
    topics: Counter[str] = field(default_factory=Counter)
    leads_new: int = 0
    leads_touched: int = 0
    appointments_made: int = 0
    appointments_cancelled: int = 0
    appointments_no_show: int = 0


@dataclass
class Waiting:
    who: str
    since: datetime


@dataclass
class Snapshot:
    """How things stand now, not over the week."""

    waiting: list[Waiting] = field(default_factory=list)  # this week's, longest first
    waiting_old: int = 0  # the backlog older than a week
    stale_leads: int = 0
    oldest_stale_lead_days: int | None = None


@dataclass
class Report:
    window: Window
    current: Figures
    previous: Figures
    snapshot: Snapshot


def _normal(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _is_words(text: str) -> bool:
    """A patient's own words, not a label the system wrote for a reel, a voice
    message or a comment."""
    return not message_labels.is_label(text) and not text.startswith(message_labels.COMMENT_PREFIX)


def _text(value: str) -> str:
    """Text for a Telegram HTML message: the characters that are markup, and
    not the apostrophes of Uzbek words."""
    return html.escape(value, quote=False)


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def _allowed_digits(tenant: Tenant) -> set[str]:
    numbers = phone_guard.allowed_numbers((tenant.settings or {}).get("clinic_phone_numbers"))
    return {phone_guard.digits_of(number) for number in numbers}


async def _figures(
    session: AsyncSession, tenant: Tenant, window: Window, allowed: set[str]
) -> Figures:
    figures = Figures()
    week = schedule_for(tenant)
    rows = (
        await session.execute(
            select(Message.conversation_id, Message.sender, Message.content, Message.created_at)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Conversation.tenant_id == tenant.id,
                Message.created_at >= window.start,
                Message.created_at < window.end,
            )
            .order_by(Message.conversation_id, Message.created_at)
        )
    ).all()

    # Texts an operator account sent word for word to several patients: the
    # saved reply or the Instagram automation. Whether one is *automatic* also
    # depends on how fast it followed the patient (below).
    reached: dict[str, set[uuid.UUID]] = defaultdict(set)
    for row in rows:
        if row.sender == MessageSender.OPERATOR:
            reached[_normal(row.content)].add(row.conversation_id)
    repeated = {text for text, convs in reached.items() if len(convs) >= AUTOMATIC_SEEN_IN}

    texts: dict[uuid.UUID, list[str]] = defaultdict(list)
    unanswered_at: list[datetime] = []
    operator_conversations: set[uuid.UUID] = set()
    current = None
    pending: datetime | None = None
    last_patient: datetime | None = None
    first_words: datetime | None = None
    answered = False

    def close_conversation() -> None:
        if first_words is None:
            return
        figures.wrote_words += 1
        if answered:
            figures.answered += 1
        else:
            figures.unanswered += 1
            unanswered_at.append(first_words)

    people: set[uuid.UUID] = set()
    for row in rows:
        if row.conversation_id != current:
            close_conversation()
            current, pending, last_patient, first_words, answered = (
                row.conversation_id, None, None, None, False,
            )  # fmt: skip
        at = row.created_at
        if row.sender == MessageSender.PATIENT:
            people.add(row.conversation_id)
            figures.patient_messages += 1
            last_patient = at
            if row.content.startswith(message_labels.COMMENT_PREFIX):
                figures.comments += 1
            elif row.content.startswith(message_labels.SHARE):
                figures.shared_posts += 1
            elif row.content.startswith(message_labels.VOICE):
                figures.voice_notes += 1
            elif row.content.startswith(message_labels.PHOTO):
                figures.photos += 1
            if _is_words(row.content):
                texts[row.conversation_id].append(row.content)
                first_words = first_words or at
                pending = pending or at
        elif row.sender == MessageSender.BOT:
            figures.bot_replies += 1
            figures.bot_after_hours += is_after_hours(at, week)
            if pending is not None:
                gap = at - pending
                if gap <= BOT_REPLY_CEILING:
                    figures.bot_seconds.append(gap.total_seconds())
                pending = None
            answered = answered or first_words is not None
        else:  # a person at the clinic, or the account's own automation
            if (
                _normal(row.content) in repeated
                and last_patient is not None
                and at - last_patient <= AUTOMATIC_WITHIN
            ):
                figures.automatic_replies += 1
                # The patient did get something back, but it was not a person.
                pending = None
                answered = answered or first_words is not None
                continue
            figures.operator_messages += 1
            figures.operator_after_hours += is_after_hours(at, week)
            operator_conversations.add(row.conversation_id)
            strangers = phone_guard.foreign_numbers(row.content, allowed)
            figures.foreign_numbers.update(strangers)
            figures.foreign_messages += bool(strangers)
            if pending is not None:
                gap = at - pending
                if gap <= OPERATOR_REPLY_CEILING:
                    figures.operator_seconds.append(gap.total_seconds())
                    if not is_after_hours(pending, week):
                        figures.operator_work_seconds.append(gap.total_seconds())
                pending = None
            answered = answered or first_words is not None
    close_conversation()

    figures.people = len(people)
    figures.operator_conversations = len(operator_conversations)
    figures.new_people = int(
        await session.scalar(
            select(func.count())
            .select_from(Conversation)
            .where(
                Conversation.tenant_id == tenant.id,
                Conversation.created_at >= window.start,
                Conversation.created_at < window.end,
            )
        )
        or 0
    )
    for words in texts.values():
        for name, rx in _TOPIC_RX.items():
            if any(rx.search(w) for w in words):
                figures.topics[name] += 1
    figures.unanswered_clusters = _clusters(unanswered_at)

    lead_rows = (
        await session.execute(
            select(Lead.status, Lead.created_at, Lead.updated_at).where(
                Lead.tenant_id == tenant.id,
                (Lead.created_at >= window.start) & (Lead.created_at < window.end)
                | (Lead.updated_at >= window.start) & (Lead.updated_at < window.end),
            )
        )
    ).all()
    for status, created, _updated in lead_rows:
        if window.start <= created < window.end:
            figures.leads_new += 1
        elif status in (LeadStatus.CONTACTED, LeadStatus.BOOKED):
            figures.leads_touched += 1
    appointments = (
        await session.execute(
            select(Appointment.status, Appointment.created_at, Appointment.scheduled_at).where(
                Appointment.tenant_id == tenant.id,
                (Appointment.created_at >= window.start) & (Appointment.created_at < window.end)
                | (Appointment.scheduled_at >= window.start)
                & (Appointment.scheduled_at < window.end),
            )
        )
    ).all()
    for status, created, scheduled in appointments:
        if window.start <= created < window.end:
            figures.appointments_made += 1
        if window.start <= scheduled < window.end:
            figures.appointments_cancelled += status == "cancelled"
            figures.appointments_no_show += status == "no_show"
    return figures


def _clusters(times: list[datetime]) -> list[tuple[datetime, datetime, int]]:
    """People left unanswered, grouped into the stretches they wrote in: six
    hours between one and the next starts a new stretch."""
    groups: list[list[datetime]] = []
    for at in sorted(times):
        if groups and at - groups[-1][-1] <= timedelta(hours=6):
            groups[-1].append(at)
        else:
            groups.append([at])
    return [(g[0], g[-1], len(g)) for g in groups]


async def _snapshot(session: AsyncSession, tenant: Tenant, now: datetime) -> Snapshot:
    snapshot = Snapshot()
    last = (
        select(Message.conversation_id, func.max(Message.created_at).label("at"))
        .group_by(Message.conversation_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(User.name, User.username, Message.sender, Message.content, Message.created_at)
            .select_from(Conversation)
            .join(User, User.id == Conversation.user_id)
            .join(last, last.c.conversation_id == Conversation.id)
            .join(
                Message,
                and_(Message.conversation_id == Conversation.id, Message.created_at == last.c.at),
            )
            .where(
                Conversation.tenant_id == tenant.id,
                (Conversation.is_bot_enabled.is_(False))
                | (Conversation.needs_doctor_since.is_not(None)),
            )
        )
    ).all()
    for name, username, sender, content, at in rows:
        # Waiting on a person: the patient spoke last, in words, a while ago.
        if sender != MessageSender.PATIENT or not _is_words(content):
            continue
        if now - at < WAITING_AFTER:
            continue
        if len(content.strip()) <= 30 and _ACKNOWLEDGEMENT.match(content.strip()):
            continue
        if now - at > OLD_WAITING:
            snapshot.waiting_old += 1
            continue
        who = f"@{username}" if username else (name or "ismi ko'rsatilmagan")
        snapshot.waiting.append(Waiting(who, at))
    snapshot.waiting.sort(key=lambda w: w.since)

    oldest = await session.scalar(
        select(func.min(Lead.created_at)).where(
            Lead.tenant_id == tenant.id,
            Lead.status == LeadStatus.NEW,
            Lead.created_at < now - STALE_LEAD,
        )
    )
    snapshot.stale_leads = int(
        await session.scalar(
            select(func.count())
            .select_from(Lead)
            .where(
                Lead.tenant_id == tenant.id,
                Lead.status == LeadStatus.NEW,
                Lead.created_at < now - STALE_LEAD,
            )
        )
        or 0
    )
    snapshot.oldest_stale_lead_days = (now - oldest).days if oldest else None
    return snapshot


async def collect(
    session: AsyncSession, tenant: Tenant, window: Window, now: datetime | None = None
) -> Report:
    now = now or datetime.now(UTC)
    allowed = _allowed_digits(tenant)
    return Report(
        window=window,
        current=await _figures(session, tenant, window, allowed),
        previous=await _figures(session, tenant, window.previous(), allowed),
        snapshot=await _snapshot(session, tenant, now),
    )


# --- the words --------------------------------------------------------------------


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    seconds = int(round(seconds))
    if seconds < 90:
        return f"{seconds} soniya"
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"{minutes} daqiqa"
    hours, rest = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} soat" + (f" {rest} daqiqa" if rest else "")
    return f"{round(hours / 24)} kun"


def _trend(now_value: float, before: float, *, better: str = "up") -> str:
    """ "▲ 12" / "▼ 5" against the week before; nothing when it did not move."""
    diff = now_value - before
    if not diff:
        return ""
    arrow = "▲" if diff > 0 else "▼"
    return f" <i>({arrow} {abs(diff):g})</i>"


def _share(part: int, whole: int) -> str:
    return f"{100 * part / whole:.0f}%" if whole else "—"


def _when(moment: datetime) -> str:
    local = moment.astimezone(CLINIC_TIMEZONE)
    return f"{local.day}-{_MONTHS[local.month - 1][:3]} {local:%H:%M}"


def _ago(moment: datetime, now: datetime) -> str:
    return _duration((now - moment).total_seconds())


def _phone(digits: str) -> str:
    return f"+998 {digits[:2]} {digits[2:5]} {digits[5:7]} {digits[7:]}"


def _sections(report: Report, now: datetime) -> list[list[str]]:
    c, p, s = report.current, report.previous, report.snapshot
    out: list[list[str]] = []

    head = [f"📊 <b>Haftalik hisobot</b>\n<i>{report.window.label()}</i>"]
    out.append(head)

    people = [
        "👥 <b>Umumiy</b>",
        f"• Yozgan odamlar: <b>{c.people}</b>{_trend(c.people, p.people)} "
        f"(yangi: {c.new_people})",
        f"• Savol yozganlar: <b>{c.wrote_words}</b>{_trend(c.wrote_words, p.wrote_words)}; "
        f"javob olgani: <b>{c.answered}</b> ({_share(c.answered, c.wrote_words)})",
    ]
    if c.unanswered:
        people.append(f"• ⚠️ Hech kim javob bermagan: <b>{c.unanswered}</b> kishi")
        for start, end, count in c.unanswered_clusters[:3]:
            span = _when(start) if count == 1 else f"{_when(start)} – {_when(end)}"
            people.append(f"    ◦ {span}: {count} kishi")
    out.append(people)

    bot = [
        "🤖 <b>Bot</b>",
        f"• Javoblar: <b>{c.bot_replies}</b>{_trend(c.bot_replies, p.bot_replies)}; "
        f"o'rtacha tezlik: <b>{_duration(median(c.bot_seconds) if c.bot_seconds else None)}</b>",
        f"• Ish vaqtidan tashqarida berilgan: {_share(c.bot_after_hours, c.bot_replies)} "
        f"({c.bot_after_hours} ta) — bu vaqtda operator yo'q",
    ]
    extras = [
        f"reels/post {c.shared_posts}" if c.shared_posts else "",
        f"ovozli {c.voice_notes}" if c.voice_notes else "",
        f"rasm {c.photos}" if c.photos else "",
        f"izoh {c.comments}" if c.comments else "",
    ]
    if any(extras):
        bot.append("• Yuborilgan: " + ", ".join(x for x in extras if x))
    if c.topics:
        top = ", ".join(f"{name} {count}" for name, count in c.topics.most_common(5))
        bot.append(f"• Eng ko'p so'ralgan: {_text(top)}")
    out.append(bot)

    ops = ["👩‍💼 <b>Operatorlar</b>"]
    if not c.operator_messages and not s.waiting and not s.waiting_old:
        ops.append("• Bu hafta operator yozmagan.")
    else:
        ops.append(
            f"• Yozgan suhbatlar: <b>{c.operator_conversations}</b>"
            f"{_trend(c.operator_conversations, p.operator_conversations)}; "
            f"xabarlar: {c.operator_messages}"
            + (f" (+ Instagram avto-javobi {c.automatic_replies})" if c.automatic_replies else "")
        )
        if c.operator_work_seconds:
            slow = sum(1 for x in c.operator_work_seconds if x > SLOW_OPERATOR.total_seconds())
            ops.append(
                "• Ish vaqtida javob tezligi: o'rtacha "
                f"<b>{_duration(median(c.operator_work_seconds))}</b>, "
                f"90% gacha {_duration(_percentile(c.operator_work_seconds, 0.9))}; "
                f"15 daqiqadan kech: {slow} ta ({_share(slow, len(c.operator_work_seconds))})"
            )
        elif c.operator_seconds:
            ops.append(f"• Javob tezligi: o'rtacha {_duration(median(c.operator_seconds))}")
        if c.operator_after_hours:
            ops.append(f"• Ish vaqtidan tashqarida yozgan: {c.operator_after_hours} xabar")
        old = f" (+ {s.waiting_old} ta eski, 7 kundan oshgan)" if s.waiting_old else ""
        if s.waiting:
            ops.append(f"• ⏳ Hozir javob kutayotgan chatlar: <b>{len(s.waiting)}</b>{old}")
            for w in s.waiting[:5]:
                ops.append(f"    ◦ {_text(w.who)} — {_ago(w.since, now)}dan beri")
        elif s.waiting_old:
            ops.append(f"• Yangi kutayotgan chat yo'q; eski javobsizlar: {s.waiting_old} ta")
        else:
            ops.append("• Hozir javob kutayotgan chat yo'q ✅")
        if c.foreign_numbers:
            ops.append(
                f"• ⚠️ Klinikaniki bo'lmagan raqamni <b>{c.foreign_messages}</b> marta yozgan:"
            )
            for digits, count in c.foreign_numbers.most_common(3):
                ops.append(f"    ◦ <code>{_phone(digits)}</code> × {count}")
    out.append(ops)

    calls = [
        "📞 <b>Qo'ng'iroq raqamlari va qabullar</b>",
        f"• Yangi raqamlar: <b>{c.leads_new}</b>{_trend(c.leads_new, p.leads_new)}; "
        f"aloqaga chiqilgani: {c.leads_touched}",
    ]
    if s.stale_leads:
        calls.append(
            f'• ⚠️ 3 kundan beri dashboardda "yangi" bo\'lib turgan raqamlar: '
            f"<b>{s.stale_leads}</b> (eng eskisi {s.oldest_stale_lead_days} kun) — qo'ng'iroq "
            "qilingan bo'lsa ham \"bog'lanildi\" deb belgilanmagan"
        )
    calls.append(
        f"• Qabullar: yozilgan {c.appointments_made}; "
        f"bekor {c.appointments_cancelled}; kelmagan {c.appointments_no_show}"
    )
    out.append(calls)
    return out


def attention(report: Report) -> list[str]:
    """What needs the doctor's eye, in a line each."""
    c, s = report.current, report.snapshot
    found: list[str] = []
    if c.unanswered:
        found.append(f"{c.unanswered} kishi hech qanday javob olmagan")
    if c.operator_work_seconds and median(c.operator_work_seconds) > SLOW_OPERATOR.total_seconds():
        found.append(
            "operatorlar ish vaqtida o'rtacha "
            f"{_duration(median(c.operator_work_seconds))}da javob bermoqda"
        )
    if s.waiting and (datetime.now(UTC) - s.waiting[0].since) > timedelta(hours=24):
        found.append(f"{len(s.waiting)} ta chat bir sutkadan ko'p javob kutmoqda")
    if s.waiting_old:
        found.append(f"{s.waiting_old} ta eski chat (7 kundan ko'p) javobsiz qolgan")
    if c.foreign_messages:
        found.append(f"operatorlar {c.foreign_messages} marta begona raqam yozgan")
    if s.stale_leads:
        found.append(f'{s.stale_leads} ta raqam 3 kundan beri "yangi" deb turibdi (belgilanmagan)')
    return found


def render(report: Report, now: datetime | None = None) -> list[str]:
    """The report as Telegram messages, each under the length limit."""
    now = now or datetime.now(UTC)
    sections = _sections(report, now)
    flags = attention(report)
    sections.append(
        ["🔎 <b>Diqqat talab qiladi</b>", *[f"• {_text(f)}" for f in flags]]
        if flags
        else ["✅ <b>Diqqat talab qiladigan narsa yo'q</b>"]
    )
    messages: list[str] = []
    buffer = ""
    for section in sections:
        block = "\n".join(section)
        if buffer and len(buffer) + len(block) + 2 > TELEGRAM_LIMIT:
            messages.append(buffer)
            buffer = ""
        buffer = f"{buffer}\n\n{block}" if buffer else block
    if buffer:
        messages.append(buffer)
    return messages
