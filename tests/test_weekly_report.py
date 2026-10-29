"""app.services.weekly_report: the doctor's Monday report, the bot's side and
the operators'.

A made-up week (Mon 2 March - Sun 8 March 2026, Tashkent time) is written into
the test database conversation by conversation, and every figure in the report
is checked against what was written.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager, asynccontextmanager
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

import app.services.doctor_telegram as doctor_telegram
import app.workers.tasks as tasks
from app.models.lead import Lead, LeadStatus
from app.models.message import Message
from app.repositories.appointment import AppointmentRepository
from app.repositories.conversation import ConversationRepository
from app.repositories.user import UserRepository
from app.services import weekly_report
from app.services.appointment import CLINIC_TIMEZONE
from tests.conftest import Seed, isolated_settings

CLINIC_NUMBERS = "+998 71 200 00 01, +998 71 200 00 02"
NOW = datetime(2026, 3, 11, 7, 0, tzinfo=UTC)  # Wednesday 12:00 in Tashkent
WINDOW = weekly_report.last_full_week(NOW)


def at(day: int, hour: int, minute: int = 0, second: int = 0, month: int = 3) -> datetime:
    return datetime(2026, month, day, hour, minute, second, tzinfo=CLINIC_TIMEZONE)


class Clinic:
    """Writes conversations for the first seeded tenant."""

    def __init__(
        self, session: Any, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
    ) -> None:
        self.session, self.seed, self.as_tenant = session, seed, as_tenant

    async def talk(
        self,
        handle: str,
        script: list[tuple[datetime, str, str]],
        *,
        bot_on: bool = True,
        needs_doctor: datetime | None = None,
    ) -> Any:
        with self.as_tenant(self.seed.tenant_a.id):
            user = await UserRepository(self.session).create(
                channel_id=self.seed.a.channel.id, external_id=handle
            )
            user.username = handle
            conversation = await ConversationRepository(self.session).create(
                user_id=user.id, status="open"
            )
        conversation.is_bot_enabled = bot_on
        conversation.needs_doctor_since = needs_doctor
        conversation.created_at = script[0][0]
        for when, sender, text in script:
            self.session.add(
                Message(
                    conversation_id=conversation.id,
                    sender=sender,
                    content=text,
                    channel="instagram",
                    created_at=when,
                )
            )
        await self.session.flush()
        return conversation


@pytest.fixture
async def clinic(
    db_session: Any, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> Clinic:
    seed.tenant_a.settings = {**seed.tenant_a.settings, "clinic_phone_numbers": CLINIC_NUMBERS}
    await db_session.flush()
    return Clinic(db_session, seed, as_tenant)


async def _week(clinic: Clinic) -> None:
    """One week: a bot answer, two operator answers (one slow), a patient nobody
    answered, a reel, three Instagram auto-replies, a night answer, and one
    patient left waiting for a person."""
    talk = clinic.talk
    await talk(
        "ali",
        [
            (at(3, 10), "patient", "Narxi qancha?"),
            (at(3, 10, 0, 20), "bot", "Qabul 300 000 so'm."),
        ],
    )
    await talk(
        "vali",
        [
            (at(3, 10, 30), "patient", "Qabulga yozilsam bo'ladimi"),
            (
                at(3, 10, 40),
                "operator",
                "Ha. Qo'ng'iroq qiling: +998 93 123 45 67 yoki +998 71 200 00 01",
            ),
        ],
        bot_on=False,
    )
    await talk("sami", [(at(4, 11), "patient", "Salom, manzil qayerda?")])
    await talk("reel", [(at(5, 14), "patient", "🔗 Post yoki reels ulashdi")])
    await talk(
        "late",
        [
            (at(6, 11), "patient", "Narx?"),
            (at(6, 14), "operator", "Qabul 300 000 so'm."),
        ],
        bot_on=False,
    )
    for name in ("auto1", "auto2", "auto3"):
        await talk(
            name,
            [
                (at(5, 15), "patient", "Siz bilan qanday bog'lansam bo'ladi?"),
                (at(5, 15, 0, 5), "operator", "Murojaat uchun: ☎️+998 71 200 00 01"),
            ],
        )
    await talk(
        "night",
        [
            (at(7, 23), "patient", "Assalomu alaykum, narx?"),
            (at(7, 23, 0, 25), "bot", "Qabul 300 000 so'm."),
        ],
    )
    await talk(
        "wait",
        [(at(8, 9), "patient", "Menga qo'ng'iroq qiling")],
        bot_on=False,
        needs_doctor=at(8, 9),
    )
    # Forgotten for more than a week: counted apart, as the backlog.
    await talk(
        "old",
        [(at(2, 9), "patient", "Qo'ng'iroq qiling")],
        bot_on=False,
        needs_doctor=at(2, 9),
    )
    # The patient closed the conversation: nobody owes them an answer.
    await talk(
        "thanks",
        [(at(8, 10), "patient", "Rahmat")],
        bot_on=False,
        needs_doctor=at(8, 10),
    )


async def test_the_bots_side_of_the_week(clinic: Clinic, db_session: Any, seed: Seed) -> None:
    await _week(clinic)

    report = await weekly_report.collect(db_session, seed.tenant_a, WINDOW, NOW)
    c = report.current

    assert WINDOW.label() == "2–8 mart"
    assert c.people == 12  # ali vali sami reel late 3 autos night wait old thanks
    assert c.wrote_words == 11  # all but the reel
    assert c.unanswered == 4  # sami, wait, old and thanks: nobody wrote back
    assert c.answered == 7
    assert c.bot_replies == 2
    assert sorted(c.bot_seconds) == [20, 25]
    assert c.bot_after_hours == 1  # Saturday night
    assert c.shared_posts == 1
    assert c.topics["narx"] == 3 and c.topics["manzil"] == 1
    # The stretch nobody answered in is named.
    assert [count for _, _, count in c.unanswered_clusters] == [1, 1, 2]  # wait+thanks: 1 h


async def test_the_operators_side_of_the_week(clinic: Clinic, db_session: Any, seed: Seed) -> None:
    await _week(clinic)

    report = await weekly_report.collect(db_session, seed.tenant_a, WINDOW, NOW)
    c, snapshot = report.current, report.snapshot

    # Two people answered; the account's own "Murojaat uchun" is not one of them.
    assert c.operator_messages == 2
    assert c.automatic_replies == 3
    assert c.operator_conversations == 2
    assert sorted(c.operator_seconds) == [600, 10800]
    assert sorted(c.operator_work_seconds) == [600, 10800]
    # The clinic's own number is not counted; the other one is.
    assert c.foreign_numbers == {"931234567": 1}
    assert c.foreign_messages == 1
    # Somebody is still waiting for a person; a week-old one is backlog, and a
    # patient who only said "Rahmat" is waiting for nothing.
    assert [w.who for w in snapshot.waiting] == ["@wait"]
    assert snapshot.waiting_old == 1


async def test_call_back_numbers_that_were_never_rung_are_counted(
    clinic: Clinic, db_session: Any, seed: Seed
) -> None:
    tenant = seed.tenant_a.id

    def lead(phone: str, status: str, created: datetime, updated: datetime) -> Lead:
        return Lead(
            tenant_id=tenant, phone=phone, status=status, created_at=created, updated_at=updated
        )

    db_session.add_all(
        [
            lead("901", LeadStatus.NEW, at(4, 12), at(4, 12)),
            lead("902", LeadStatus.NEW, at(1, 12), at(1, 12)),
            lead("903", LeadStatus.CONTACTED, at(1, 9), at(5, 12)),
        ]
    )
    await db_session.flush()

    report = await weekly_report.collect(db_session, seed.tenant_a, WINDOW, NOW)

    assert report.current.leads_new == 1
    assert report.current.leads_touched == 1
    # Two are still "new" and more than three days old, the oldest from 1 March.
    assert report.snapshot.stale_leads == 2
    assert report.snapshot.oldest_stale_lead_days == 10


async def test_appointments_made_cancelled_and_missed(
    clinic: Clinic, db_session: Any, seed: Seed, as_tenant: Any
) -> None:
    with as_tenant(seed.tenant_a.id):
        repo = AppointmentRepository(db_session)
        for status, when in (
            ("scheduled", at(9, 10)),
            ("cancelled", at(4, 10)),
            ("no_show", at(3, 11)),
        ):
            await repo.create(
                user_id=seed.a.user.id, conversation_id=None, doctor_name="Dr",
                scheduled_at=when, status=status, patient_name="Ali", created_at=at(2, 12),
            )  # fmt: skip

    c = (await weekly_report.collect(db_session, seed.tenant_a, WINDOW, NOW)).current

    assert c.appointments_made == 3
    assert c.appointments_cancelled == 1
    assert c.appointments_no_show == 1


async def test_the_week_is_compared_with_the_week_before(
    clinic: Clinic, db_session: Any, seed: Seed
) -> None:
    await _week(clinic)
    # Two people the week before (23 Feb - 1 Mar).
    for handle, day in (("old1", 24), ("old2", 25)):
        await clinic.talk(handle, [(at(day, 11, month=2), "patient", "Salom")])
    report = await weekly_report.collect(db_session, seed.tenant_a, WINDOW, NOW)

    assert report.previous.people == 2
    text = "\n".join(weekly_report.render(report, NOW))
    assert "▲ 10" in text  # 12 people against 2


async def test_the_report_reads_as_the_doctor_would_want_it(
    clinic: Clinic, db_session: Any, seed: Seed
) -> None:
    await _week(clinic)
    await clinic.talk(
        "<b>evil",
        [(at(8, 8), "patient", "Qo'ng'iroq qiling")],
        bot_on=False,
        needs_doctor=at(8, 8),
    )

    messages = weekly_report.render(
        await weekly_report.collect(db_session, seed.tenant_a, WINDOW, NOW), NOW
    )
    text = "\n".join(messages)

    assert all(len(m) <= 4096 for m in messages)
    assert "Haftalik hisobot" in text and "2–8 mart" in text
    assert "👩‍💼 <b>Operatorlar</b>" in text and "🤖 <b>Bot</b>" in text
    # What the operators did that the doctor should see:
    assert "<code>+998 93 123 45 67</code> × 1" in text  # the number that is not the clinic's
    assert "Hozir javob kutayotgan chatlar: <b>2</b> (+ 1 ta eski" in text
    assert "15 daqiqadan kech: 1 ta" in text
    assert "Diqqat talab qiladi" in text
    # A patient's handle is text, never markup.
    assert "&lt;b&gt;evil" in text and "<b>evil" not in text


async def test_a_quiet_week_says_so(clinic: Clinic, db_session: Any, seed: Seed) -> None:
    messages = weekly_report.render(
        await weekly_report.collect(db_session, seed.tenant_a, WINDOW, NOW), NOW
    )

    text = "\n".join(messages)
    assert "Bu hafta operator yozmagan" in text
    assert "Diqqat talab qiladigan narsa yo'q" in text


# --- sending it ----------------------------------------------------------------


class FakeBotAPI:
    def __init__(self, fail_for: set[int] | None = None) -> None:
        self.sent: list[tuple[int, str]] = []
        self.fail_for = fail_for or set()

    async def send(self, chat_id: int, text: str, reply_markup: Any = None) -> bool:
        if chat_id in self.fail_for:
            raise RuntimeError("chat gone")
        self.sent.append((chat_id, text))
        return True


async def test_the_week_goes_out_once_and_only_when_somebody_can_read_it(
    clinic: Clinic, db_session: Any, seed: Seed
) -> None:
    tenant, api = seed.tenant_a, FakeBotAPI()

    # Nobody has opened the bot: nothing is sent and the week is NOT marked
    # sent, so it still arrives when somebody does.
    assert await doctor_telegram.deliver_weekly_report(api, db_session, tenant, WINDOW) is False
    assert api.sent == [] and doctor_telegram.WEEKLY_SENT_KEY not in tenant.settings

    tenant.settings = {**tenant.settings, doctor_telegram.CHATS_KEY: [111]}
    assert await doctor_telegram.deliver_weekly_report(api, db_session, tenant, WINDOW) is True
    first = list(api.sent)
    assert first and all(chat == 111 for chat, _ in first)
    assert tenant.settings[doctor_telegram.WEEKLY_SENT_KEY] == "2026-03-08"

    assert await doctor_telegram.deliver_weekly_report(api, db_session, tenant, WINDOW) is False
    assert api.sent == first  # not twice

    # Asked for on purpose, it goes again.
    await doctor_telegram.deliver_weekly_report(api, db_session, tenant, WINDOW, once=False)
    assert len(api.sent) == 2 * len(first)


async def test_one_unreachable_chat_does_not_stop_the_others(
    clinic: Clinic, db_session: Any, seed: Seed
) -> None:
    tenant = seed.tenant_a
    tenant.settings = {**tenant.settings, doctor_telegram.CHATS_KEY: [222, 111]}
    api = FakeBotAPI(fail_for={222})

    assert await doctor_telegram.deliver_weekly_report(api, db_session, tenant, WINDOW) is True
    assert {chat for chat, _ in api.sent} == {111}


async def test_the_report_button_sends_the_last_seven_days_to_whoever_asked(
    clinic: Clinic, db_session: Any, seed: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = isolated_settings(doctor_telegram_usernames="dr_alisher")
    monkeypatch.setattr(doctor_telegram, "get_settings", lambda: settings)
    api = FakeBotAPI()
    update = {
        "message": {
            "chat": {"id": 555},
            "from": {"id": 9, "username": "dr_alisher"},
            "text": doctor_telegram.BTN_REPORT,
        }
    }

    await doctor_telegram.handle_update(api, db_session, seed.tenant_a, update)  # type: ignore[arg-type]

    assert [chat for chat, _ in api.sent] == [555] * len(api.sent)
    assert "Hisobot tayyorlanyapti" in api.sent[0][1]
    assert "Haftalik hisobot" in "\n".join(text for _, text in api.sent)
    # A stranger gets nothing.
    stranger = {
        **update,
        "message": {**update["message"], "from": {"id": 10, "username": "someone"}},
    }
    api.sent.clear()
    await doctor_telegram.handle_update(api, db_session, seed.tenant_a, stranger)  # type: ignore[arg-type]
    assert all("Haftalik" not in text for _, text in api.sent)


# --- the schedule -----------------------------------------------------------------


def test_the_worker_checks_every_hour() -> None:
    jobs = {job.coroutine.__name__: job for job in tasks.WorkerSettings.cron_jobs}
    assert "send_weekly_report" in jobs
    assert jobs["send_weekly_report"].minute == {40}
    assert jobs["send_weekly_report"].hour is None  # every hour: it decides itself


@pytest.mark.parametrize(
    ("moment", "sends"),
    [
        (datetime(2026, 3, 9, 8, 40, tzinfo=CLINIC_TIMEZONE), False),  # Monday before nine
        (datetime(2026, 3, 9, 9, 40, tzinfo=CLINIC_TIMEZONE), True),
        (datetime(2026, 3, 11, 15, 40, tzinfo=CLINIC_TIMEZONE), True),  # Wednesday: late, not lost
    ],
)
async def test_the_report_is_sent_from_monday_nine_and_is_never_lost(
    db_session: Any, seed: Seed, monkeypatch: pytest.MonkeyPatch, moment: datetime, sends: bool
) -> None:
    class Frozen(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> "Frozen":
            return cls.fromtimestamp(moment.timestamp(), tz or UTC)  # type: ignore[return-value]

    calls: list[Any] = []

    async def deliver(api: Any, session: Any, tenant: Any, window: Any, **kw: Any) -> bool:
        calls.append(window)
        return True

    @asynccontextmanager
    async def sessions() -> Any:
        yield db_session

    settings = isolated_settings(
        doctor_telegram_bot_token="1:abc", provision_tenant_name=seed.tenant_a.name
    )
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "datetime", Frozen)
    monkeypatch.setattr(tasks, "db_session", sessions)
    monkeypatch.setattr(doctor_telegram, "get_settings", lambda: settings)
    monkeypatch.setattr(doctor_telegram, "deliver_weekly_report", deliver)

    await tasks.send_weekly_report({})

    assert bool(calls) is sends
    if sends:
        assert (calls[0].first_day, calls[0].last_day) == (
            datetime(2026, 3, 2).date(),
            datetime(2026, 3, 8).date(),
        )


async def test_no_doctor_bot_no_report(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tasks, "get_settings", lambda: isolated_settings())
    await tasks.send_weekly_report({})  # returns quietly
