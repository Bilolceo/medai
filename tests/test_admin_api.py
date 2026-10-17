"""The dashboard's JSON API.

The security tests here are the point of the port: the Telegram admin API
this replaces took `tenant_id: int = Query(1)` on every endpoint, so any
authenticated operator could read and write another clinic's records by
changing a number in the URL.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

import app.services.knowledge_base as knowledge_base_service
import app.services.knowledge_files as knowledge_files_service
from app.api.admin.deps import CSRF_HEADER
from app.core.db import get_db_session
from app.core.encryption import encrypt
from app.core.session import SESSION_COOKIE_NAME, create_session_cookie
from app.main import app
from app.models.appointment import AppointmentStatus
from app.models.lead import LeadStatus
from app.models.message import MessageSender
from app.rag.embeddings import EMBEDDING_DIMENSIONS, EmbeddingProvider
from app.repositories.appointment import AppointmentRepository
from app.repositories.doctor import DoctorRepository
from app.repositories.knowledge_document import KnowledgeDocumentRepository
from app.repositories.lead import LeadRepository
from app.repositories.operator import OperatorRepository
from app.services.appointment import CLINIC_TIMEZONE, is_within_working_hours
from tests.conftest import Seed


class FakeEmbeddingProvider(EmbeddingProvider):
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * EMBEDDING_DIMENSIONS for _ in texts]


@pytest.fixture(autouse=True)
def _no_real_embeddings(monkeypatch: pytest.MonkeyPatch) -> None:
    """The FAQ endpoint embeds what it stores, and a row without an embedding
    is invisible to retrieval. Nothing here should reach a real provider.

    Patched on the module rather than through app.dependency_overrides:
    ingest_faqs resolves its provider by calling get_embedding_provider()
    itself, which FastAPI's dependency system never sees.
    """
    monkeypatch.setattr(knowledge_base_service, "get_embedding_provider", FakeEmbeddingProvider)
    monkeypatch.setattr(knowledge_files_service, "get_embedding_provider", FakeEmbeddingProvider)


@pytest.fixture
async def manager(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> Any:
    """An `admin` on tenant A: the account that may change the clinic itself.

    The seed fixture's own operator is a `doctor` — view-only by design, and
    exercised as such below — so anything testing a write needs its own
    account rather than quietly widening the shared one.
    """
    with as_tenant(seed.tenant_a.id):
        return await OperatorRepository(db_session).create(
            name="Admin A",
            role="admin",
            username=f"manager-{seed.tenant_a.id}",
            password_hash="x",
        )


@pytest.fixture
async def front_desk(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> Any:
    """An `operator` on tenant A: the front desk.

    Separate from `manager` because the boundary between them is the point
    of having three roles at all, and a boundary only one fixture ever
    crosses is not being tested.
    """
    with as_tenant(seed.tenant_a.id):
        return await OperatorRepository(db_session).create(
            name="Front Desk A",
            role="operator",
            username=f"frontdesk-{seed.tenant_a.id}",
            password_hash="x",
        )


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    async def _get_db_session_override() -> AsyncSession:
        return db_session

    app.dependency_overrides[get_db_session] = _get_db_session_override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    app.dependency_overrides.pop(get_db_session, None)


def _login_as(client: httpx.AsyncClient, operator_id: UUID) -> str:
    """Put a real signed session cookie on the client and return its CSRF
    token — the same pair a browser gets from POST /login.
    """
    cookie_value, csrf_token = create_session_cookie(operator_id)
    client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
    return csrf_token


def _next_free_slot(offset_days: int = 1) -> datetime:
    candidate = (datetime.now(UTC) + timedelta(days=offset_days)).replace(
        minute=0, second=0, microsecond=0
    )
    for _ in range(96):
        if is_within_working_hours(candidate):
            return candidate
        candidate += timedelta(minutes=30)
    raise AssertionError("no bookable slot found")


# --- authentication and tenant scoping ---


async def test_the_api_is_closed_to_anyone_without_a_session(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/admin/conversations")

    # The dashboard's exception handler turns "no session" into a redirect
    # to the login page rather than a JSON 401.
    assert response.status_code in (303, 307)
    assert "/login" in response.headers.get("location", "")


async def test_the_session_endpoint_reports_the_operators_own_tenant(
    client: httpx.AsyncClient, seed: Seed
) -> None:
    _login_as(client, seed.a.operator.id)

    body = (await client.get("/api/admin/session")).json()

    assert body["tenant_id"] == str(seed.tenant_a.id)
    assert body["tenant_name"] == "Clinic A"
    assert body["csrf_token"]


async def test_an_operator_only_ever_sees_their_own_clinics_records(
    client: httpx.AsyncClient, seed: Seed
) -> None:
    """The whole reason for the port. There is no request parameter that
    could widen this — the tenant comes from the operator's own row.
    """
    _login_as(client, seed.a.operator.id)

    conversations = (await client.get("/api/admin/conversations")).json()
    leads = (await client.get("/api/admin/leads")).json()
    doctors = (await client.get("/api/admin/doctors")).json()

    assert [c["id"] for c in conversations] == [str(seed.a.conversation.id)]
    assert str(seed.b.conversation.id) not in {c["id"] for c in conversations}
    assert str(seed.b.lead.id) not in {row["id"] for row in leads}
    assert str(seed.b.doctor.id) not in {row["id"] for row in doctors}


async def test_a_tenant_id_query_parameter_changes_nothing(
    client: httpx.AsyncClient, seed: Seed
) -> None:
    """A direct regression test for the vulnerability being replaced: the
    old endpoints took ?tenant_id= and honoured it.
    """
    _login_as(client, seed.a.operator.id)

    body = (await client.get(f"/api/admin/leads?tenant_id={seed.tenant_b.id}")).json()

    assert str(seed.b.lead.id) not in {row["id"] for row in body}
    assert str(seed.a.lead.id) in {row["id"] for row in body}


async def test_another_clinics_record_is_not_reachable_by_id(
    client: httpx.AsyncClient, seed: Seed
) -> None:
    _login_as(client, seed.a.operator.id)

    response = await client.get(f"/api/admin/conversations/{seed.b.conversation.id}")

    assert response.status_code == 404


# --- CSRF ---


async def test_a_write_without_the_csrf_header_is_refused(
    client: httpx.AsyncClient, seed: Seed, manager: Any
) -> None:
    _login_as(client, manager.id)

    response = await client.post(
        "/api/admin/doctors", json={"name": "Dr. New", "specialty": "Ortodont"}
    )

    assert response.status_code == 403
    assert CSRF_HEADER in response.json()["detail"]


async def test_a_write_with_the_wrong_csrf_token_is_refused(
    client: httpx.AsyncClient, seed: Seed, manager: Any
) -> None:
    _login_as(client, manager.id)

    response = await client.post(
        "/api/admin/doctors",
        json={"name": "Dr. New"},
        headers={CSRF_HEADER: "not-the-token"},
    )

    assert response.status_code == 403


# --- roles ---


async def test_a_doctor_account_can_read_but_not_write(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    with as_tenant(seed.tenant_a.id):
        viewer = await OperatorRepository(db_session).create(
            name="Dr. Viewer",
            role="doctor",
            username=f"viewer-{seed.tenant_a.id}",
            password_hash="x",
        )
    csrf = _login_as(client, viewer.id)

    assert (await client.get("/api/admin/appointments")).status_code == 200
    write = await client.post(
        "/api/admin/doctors", json={"name": "Dr. New"}, headers={CSRF_HEADER: csrf}
    )
    assert write.status_code == 403


# --- conversations and operator takeover ---


async def test_turning_the_bot_off_and_back_on(
    client: httpx.AsyncClient, seed: Seed, manager: Any
) -> None:
    csrf = _login_as(client, manager.id)
    url = f"/api/admin/conversations/{seed.a.conversation.id}/bot"

    off = await client.post(url, json={"is_bot_enabled": False}, headers={CSRF_HEADER: csrf})
    assert off.status_code == 200
    assert off.json()["is_bot_enabled"] is False

    on = await client.post(url, json={"is_bot_enabled": True}, headers={CSRF_HEADER: csrf})
    assert on.json()["is_bot_enabled"] is True


async def test_the_transcript_comes_back_oldest_first(
    client: httpx.AsyncClient, seed: Seed
) -> None:
    _login_as(client, seed.a.operator.id)

    body = (await client.get(f"/api/admin/conversations/{seed.a.conversation.id}")).json()

    assert body["conversation"]["id"] == str(seed.a.conversation.id)
    assert [m["content"] for m in body["messages"]] == ["Hello"]
    assert body["messages"][0]["sender"] == MessageSender.PATIENT


async def test_an_operator_reply_that_cannot_be_delivered_is_not_recorded(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    manager: Any,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    """A message in the transcript the patient never received would have the
    operator believe they had answered.

    The channel is put back to an unconfigured credential first, which is
    what makes delivery refuse without any network call — the seeded value
    is a real-looking token, and the adapter would take it to Meta.
    """
    with as_tenant(seed.tenant_a.id):
        seed.a.channel.credentials = encrypt("pending")
        await db_session.flush()

    csrf = _login_as(client, manager.id)

    response = await client.post(
        f"/api/admin/conversations/{seed.a.conversation.id}/reply",
        json={"text": "Ertaga 10:00 ga yozdim"},
        headers={CSRF_HEADER: csrf},
    )

    assert response.status_code == 502
    detail = (await client.get(f"/api/admin/conversations/{seed.a.conversation.id}")).json()
    assert [m["content"] for m in detail["messages"]] == ["Hello"]


# --- conversations waiting on the doctor ---


async def _pin(db_session: AsyncSession, seed: Seed, minutes_ago: int = 5) -> None:
    seed.a.conversation.needs_doctor_since = datetime.now(UTC) - timedelta(minutes=minutes_ago)
    await db_session.flush()


async def test_a_conversation_waiting_on_the_doctor_is_listed_first(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    from app.repositories.conversation import ConversationRepository
    from app.repositories.message import MessageRepository
    from app.repositories.user import UserRepository

    with as_tenant(seed.tenant_a.id):
        # A newer conversation, which would sort above the seeded one on
        # recency alone.
        other = await UserRepository(db_session).create(
            channel_id=seed.a.channel.id, external_id="newer-patient"
        )
        newer = await ConversationRepository(db_session).create(user_id=other.id, status="open")
        await MessageRepository(db_session).create(
            conversation_id=newer.id,
            sender=MessageSender.PATIENT,
            content="Salom",
            channel="instagram",
        )
        await _pin(db_session, seed)
    _login_as(client, seed.a.operator.id)

    rows = (await client.get("/api/admin/conversations")).json()
    assert rows[0]["id"] == str(seed.a.conversation.id)
    assert rows[0]["needs_doctor_since"] is not None
    assert all(r["needs_doctor_since"] is None for r in rows[1:])

    waiting = (await client.get("/api/admin/conversations?needs_doctor=true")).json()
    assert [r["id"] for r in waiting] == [str(seed.a.conversation.id)]


async def test_an_operator_reply_unpins_the_conversation(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    manager: Any,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.api.admin.conversations as conversations_api

    async def delivered(*args: Any, **kwargs: Any) -> str:
        return "instagram"

    monkeypatch.setattr(conversations_api, "send_reply", delivered)
    with as_tenant(seed.tenant_a.id):
        await _pin(db_session, seed)
    csrf = _login_as(client, manager.id)

    response = await client.post(
        f"/api/admin/conversations/{seed.a.conversation.id}/reply",
        json={"text": "Ertaga 10:00 da keling"},
        headers={CSRF_HEADER: csrf},
    )

    assert response.status_code == 200
    detail = (await client.get(f"/api/admin/conversations/{seed.a.conversation.id}")).json()
    assert detail["conversation"]["needs_doctor_since"] is None


async def test_a_reply_that_did_not_reach_the_patient_keeps_the_pin(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    manager: Any,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    with as_tenant(seed.tenant_a.id):
        seed.a.channel.credentials = encrypt("pending")
        await _pin(db_session, seed)
    csrf = _login_as(client, manager.id)

    response = await client.post(
        f"/api/admin/conversations/{seed.a.conversation.id}/reply",
        json={"text": "Ertaga 10:00 da keling"},
        headers={CSRF_HEADER: csrf},
    )

    assert response.status_code == 502
    detail = (await client.get(f"/api/admin/conversations/{seed.a.conversation.id}")).json()
    assert detail["conversation"]["needs_doctor_since"] is not None


async def test_marking_a_conversation_done_unpins_it(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    manager: Any,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    with as_tenant(seed.tenant_a.id):
        await _pin(db_session, seed)
    csrf = _login_as(client, manager.id)

    response = await client.post(
        f"/api/admin/conversations/{seed.a.conversation.id}/resolve", headers={CSRF_HEADER: csrf}
    )

    assert response.status_code == 200
    assert response.json()["needs_doctor_since"] is None


async def test_marking_done_needs_the_csrf_token(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    manager: Any,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    with as_tenant(seed.tenant_a.id):
        await _pin(db_session, seed)
    _login_as(client, manager.id)

    response = await client.post(f"/api/admin/conversations/{seed.a.conversation.id}/resolve")

    assert response.status_code == 403
    detail = (await client.get(f"/api/admin/conversations/{seed.a.conversation.id}")).json()
    assert detail["conversation"]["needs_doctor_since"] is not None


async def test_a_view_only_account_cannot_mark_done(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    with as_tenant(seed.tenant_a.id):
        await _pin(db_session, seed)
    csrf = _login_as(client, seed.a.operator.id)

    response = await client.post(
        f"/api/admin/conversations/{seed.a.conversation.id}/resolve", headers={CSRF_HEADER: csrf}
    )

    assert response.status_code == 403


async def test_another_clinics_conversation_cannot_be_marked_done(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    manager: Any,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    with as_tenant(seed.tenant_b.id):
        seed.b.conversation.needs_doctor_since = datetime.now(UTC)
        await db_session.flush()
    csrf = _login_as(client, manager.id)

    response = await client.post(
        f"/api/admin/conversations/{seed.b.conversation.id}/resolve", headers={CSRF_HEADER: csrf}
    )

    assert response.status_code == 404
    await db_session.refresh(seed.b.conversation)
    assert seed.b.conversation.needs_doctor_since is not None


# --- rules ---


async def _rules_fixture(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> None:
    """Two live rules, one with a knowledge-base copy, plus an old copy of a
    rule that was since replaced."""
    from app.repositories.knowledge_base import KnowledgeBaseRepository
    from app.services.knowledge_base import RULE_CATEGORY

    with as_tenant(seed.tenant_a.id):
        seed.tenant_a.settings = {
            **seed.tenant_a.settings,
            "strict_rules": ["Salomga salom bilan javob bering.", "Narxni aytmang."],
        }
        repo = KnowledgeBaseRepository(db_session)
        for position, text in ((1, "Salomga  salom bilan javob bering."), (2, "Eski qoida.")):
            await repo.create(
                question=f"Qoida {position}: {text}",
                answer=text,
                category=RULE_CATEGORY,
                embedding=[0.0] * EMBEDDING_DIMENSIONS,
                embedding_model="test",
                is_active=False,
            )
        await db_session.flush()


async def test_rules_are_numbered_online_first_then_offline(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    await _rules_fixture(db_session, seed, as_tenant)
    _login_as(client, seed.a.operator.id)

    rules = (await client.get("/api/admin/rules")).json()

    assert [(r["number"], r["text"], r["online"]) for r in rules] == [
        (1, "Salomga salom bilan javob bering.", True),
        (2, "Narxni aytmang.", True),
        (3, "Eski qoida.", False),
    ]


async def test_rule_copies_are_not_listed_as_questions_and_answers(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    await _rules_fixture(db_session, seed, as_tenant)
    _login_as(client, seed.a.operator.id)

    faqs = (await client.get("/api/admin/knowledge-base")).json()

    assert [f["id"] for f in faqs] == [str(seed.a.knowledge_base.id)]


async def test_removing_an_online_rule_takes_it_off_the_live_list_and_its_copy(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    manager: Any,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    await _rules_fixture(db_session, seed, as_tenant)
    csrf = _login_as(client, manager.id)

    response = await client.post(
        "/api/admin/rules/remove",
        json={"text": "Salomga salom bilan javob bering."},
        headers={CSRF_HEADER: csrf},
    )

    assert response.status_code == 200
    assert [(r["number"], r["text"]) for r in response.json()] == [
        (1, "Narxni aytmang."),
        (2, "Eski qoida."),
    ]
    settings = (await client.get("/api/admin/settings")).json()
    assert settings["strict_rules"] == ["Narxni aytmang."]


async def test_removing_an_offline_rule_leaves_the_live_ones_alone(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    manager: Any,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    await _rules_fixture(db_session, seed, as_tenant)
    csrf = _login_as(client, manager.id)

    response = await client.post(
        "/api/admin/rules/remove", json={"text": "Eski qoida."}, headers={CSRF_HEADER: csrf}
    )

    assert response.status_code == 200
    assert [r["online"] for r in response.json()] == [True, True]


async def test_removing_a_rule_that_does_not_exist_is_a_404(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    manager: Any,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    await _rules_fixture(db_session, seed, as_tenant)
    csrf = _login_as(client, manager.id)

    response = await client.post(
        "/api/admin/rules/remove", json={"text": "Bunday qoida yo'q"}, headers={CSRF_HEADER: csrf}
    )

    assert response.status_code == 404
    assert len((await client.get("/api/admin/rules")).json()) == 3


async def test_removing_a_rule_needs_the_csrf_token_and_a_clinic_admin(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    front_desk: Any,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    await _rules_fixture(db_session, seed, as_tenant)
    body = {"text": "Narxni aytmang."}

    csrf = _login_as(client, front_desk.id)
    assert (await client.post("/api/admin/rules/remove", json=body)).status_code == 403
    refused = await client.post("/api/admin/rules/remove", json=body, headers={CSRF_HEADER: csrf})
    assert refused.status_code == 403
    assert len((await client.get("/api/admin/rules")).json()) == 3


async def _switch(client: httpx.AsyncClient, csrf: str | None, text: str, online: bool) -> Any:
    headers = {CSRF_HEADER: csrf} if csrf else {}
    return await client.post(
        "/api/admin/rules/state", json={"text": text, "online": online}, headers=headers
    )


async def test_an_offline_rule_can_be_put_back_online(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    manager: Any,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    await _rules_fixture(db_session, seed, as_tenant)
    csrf = _login_as(client, manager.id)

    response = await _switch(client, csrf, "Eski qoida.", True)

    assert response.status_code == 200
    assert all(r["online"] for r in response.json())
    settings = (await client.get("/api/admin/settings")).json()
    assert settings["strict_rules"][-1] == "Eski qoida."


async def test_an_online_rule_taken_offline_stays_listed(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    manager: Any,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    """ "Narxni aytmang." was typed into Settings and never had a copy; it
    must not vanish from the list when switched off."""
    await _rules_fixture(db_session, seed, as_tenant)
    csrf = _login_as(client, manager.id)

    response = await _switch(client, csrf, "Narxni aytmang.", False)

    assert response.status_code == 200
    by_text = {r["text"]: r["online"] for r in response.json()}
    assert by_text == {
        "Salomga salom bilan javob bering.": True,
        "Narxni aytmang.": False,
        "Eski qoida.": False,
    }
    settings = (await client.get("/api/admin/settings")).json()
    assert settings["strict_rules"] == ["Salomga salom bilan javob bering."]

    back = await _switch(client, csrf, "Narxni aytmang.", True)
    assert {r["text"]: r["online"] for r in back.json()}["Narxni aytmang."] is True


async def test_switching_to_the_state_it_already_has_changes_nothing(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    manager: Any,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    await _rules_fixture(db_session, seed, as_tenant)
    csrf = _login_as(client, manager.id)

    response = await _switch(client, csrf, "Narxni aytmang.", True)

    assert response.status_code == 200
    settings = (await client.get("/api/admin/settings")).json()
    assert settings["strict_rules"] == ["Salomga salom bilan javob bering.", "Narxni aytmang."]


async def test_a_rule_cannot_go_online_past_the_limit(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    manager: Any,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    from app.services.knowledge_base import MAX_RULES

    await _rules_fixture(db_session, seed, as_tenant)
    with as_tenant(seed.tenant_a.id):
        seed.tenant_a.settings = {
            **seed.tenant_a.settings,
            "strict_rules": [f"Qoida {n}." for n in range(MAX_RULES)],
        }
        await db_session.flush()
    csrf = _login_as(client, manager.id)

    response = await _switch(client, csrf, "Eski qoida.", True)

    assert response.status_code == 409
    settings = (await client.get("/api/admin/settings")).json()
    assert len(settings["strict_rules"]) == MAX_RULES


async def test_switching_a_rule_that_does_not_exist_is_a_404(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    manager: Any,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    await _rules_fixture(db_session, seed, as_tenant)
    csrf = _login_as(client, manager.id)

    assert (await _switch(client, csrf, "Bunday qoida yo'q", True)).status_code == 404


async def test_switching_a_rule_needs_the_csrf_token_and_a_clinic_admin(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    front_desk: Any,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    await _rules_fixture(db_session, seed, as_tenant)

    csrf = _login_as(client, front_desk.id)
    assert (await _switch(client, None, "Eski qoida.", True)).status_code == 403
    assert (await _switch(client, csrf, "Eski qoida.", True)).status_code == 403
    rules = (await client.get("/api/admin/rules")).json()
    assert [r["online"] for r in rules] == [True, True, False]


async def test_another_clinics_rules_are_never_listed(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    await _rules_fixture(db_session, seed, as_tenant)
    _login_as(client, seed.b.operator.id)

    assert (await client.get("/api/admin/rules")).json() == []


# --- doctors ---


async def test_creating_and_editing_a_doctor(
    client: httpx.AsyncClient, seed: Seed, manager: Any
) -> None:
    csrf = _login_as(client, manager.id)

    created = await client.post(
        "/api/admin/doctors",
        json={"name": "Dr. Anvar", "specialty": "Implantolog", "phone": "+998 90 111 22 33"},
        headers={CSRF_HEADER: csrf},
    )
    assert created.status_code == 201
    doctor_id = created.json()["id"]
    # Defaults come from the schema, not from the caller.
    assert created.json()["working_hours"] == "09:00 - 18:00"

    edited = await client.patch(
        f"/api/admin/doctors/{doctor_id}",
        json={"phone": "+998 90 999 88 77"},
        headers={CSRF_HEADER: csrf},
    )
    assert edited.json()["phone"] == "+998 90 999 88 77"
    # A partial update must not blank what it did not mention.
    assert edited.json()["specialty"] == "Implantolog"


async def test_deactivating_a_doctor_removes_them_from_the_default_list(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    manager: Any,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    csrf = _login_as(client, manager.id)

    await client.patch(
        f"/api/admin/doctors/{seed.a.doctor.id}",
        json={"is_active": False},
        headers={CSRF_HEADER: csrf},
    )

    active = (await client.get("/api/admin/doctors")).json()
    everyone = (await client.get("/api/admin/doctors?include_inactive=true")).json()

    assert str(seed.a.doctor.id) not in {row["id"] for row in active}
    assert str(seed.a.doctor.id) in {row["id"] for row in everyone}
    del db_session, as_tenant


async def test_editing_another_clinics_doctor_is_a_404(
    client: httpx.AsyncClient, seed: Seed, manager: Any
) -> None:
    csrf = _login_as(client, manager.id)

    response = await client.patch(
        f"/api/admin/doctors/{seed.b.doctor.id}",
        json={"name": "hijacked"},
        headers={CSRF_HEADER: csrf},
    )

    assert response.status_code == 404


# --- leads ---


async def test_working_a_lead_through_its_statuses(
    client: httpx.AsyncClient, seed: Seed, manager: Any
) -> None:
    csrf = _login_as(client, manager.id)

    created = await client.post(
        "/api/admin/leads",
        json={"patient_name": "Aziza", "phone": "+998 90 123 45 67", "topic": "implant"},
        headers={CSRF_HEADER: csrf},
    )
    assert created.status_code == 201
    assert created.json()["status"] == LeadStatus.NEW

    updated = await client.patch(
        f"/api/admin/leads/{created.json()['id']}",
        json={"status": LeadStatus.CONTACTED, "notes": "Ertaga qayta qo'ng'iroq"},
        headers={CSRF_HEADER: csrf},
    )
    assert updated.json()["status"] == LeadStatus.CONTACTED

    only_contacted = (await client.get(f"/api/admin/leads?status={LeadStatus.CONTACTED}")).json()
    assert [row["id"] for row in only_contacted] == [created.json()["id"]]


async def test_an_unknown_lead_status_is_rejected(
    client: httpx.AsyncClient, seed: Seed, manager: Any
) -> None:
    csrf = _login_as(client, manager.id)

    response = await client.patch(
        f"/api/admin/leads/{seed.a.lead.id}",
        json={"status": "nonsense"},
        headers={CSRF_HEADER: csrf},
    )

    assert response.status_code == 422


# --- knowledge base ---


async def test_a_faq_added_here_is_embedded_so_retrieval_can_find_it(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    manager: Any,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    """A row without an embedding shows in the dashboard and is never used
    to answer anything, which is the worst of both.
    """
    csrf = _login_as(client, manager.id)

    created = await client.post(
        "/api/admin/knowledge-base",
        json={"question": "Implant narxi qancha?", "answer": "5 000 000 so'm."},
        headers={CSRF_HEADER: csrf},
    )

    assert created.status_code == 201
    from app.repositories.knowledge_base import KnowledgeBaseRepository

    with as_tenant(seed.tenant_a.id):
        faq = await KnowledgeBaseRepository(db_session).get(UUID(created.json()["id"]))
    assert faq is not None
    assert len(faq.embedding) == EMBEDDING_DIMENSIONS


async def test_deleting_a_faq_deactivates_it_rather_than_dropping_the_row(
    client: httpx.AsyncClient, seed: Seed, manager: Any
) -> None:
    csrf = _login_as(client, manager.id)

    response = await client.delete(
        f"/api/admin/knowledge-base/{seed.a.knowledge_base.id}", headers={CSRF_HEADER: csrf}
    )

    assert response.status_code == 204
    listed = (await client.get("/api/admin/knowledge-base")).json()
    [row] = [r for r in listed if r["id"] == str(seed.a.knowledge_base.id)]
    assert row["is_active"] is False


# --- settings ---


async def test_settings_merge_rather_than_replace(
    client: httpx.AsyncClient, seed: Seed, manager: Any
) -> None:
    csrf = _login_as(client, manager.id)

    await client.patch(
        "/api/admin/settings",
        json={"clinic_address": "Amir Temur 45", "debounce_seconds": 8},
        headers={CSRF_HEADER: csrf},
    )
    # A form that only edits the address must not blank the debounce window.
    after = await client.patch(
        "/api/admin/settings",
        json={"clinic_address": "Navoiy 12"},
        headers={CSRF_HEADER: csrf},
    )

    assert after.json()["clinic_address"] == "Navoiy 12"
    assert after.json()["debounce_seconds"] == 8


async def test_an_out_of_range_debounce_window_is_rejected(
    client: httpx.AsyncClient, seed: Seed, manager: Any
) -> None:
    csrf = _login_as(client, manager.id)

    response = await client.patch(
        "/api/admin/settings", json={"debounce_seconds": 9999}, headers={CSRF_HEADER: csrf}
    )

    assert response.status_code == 422


async def test_the_assistants_persona_is_saved_and_can_be_reset(
    client: httpx.AsyncClient, seed: Seed, manager: Any
) -> None:
    csrf = _login_as(client, manager.id)

    saved = await client.patch(
        "/api/admin/settings",
        json={"assistant_prompt": "Siz Nigoramisiz.", "assistant_examples": ""},
        headers={CSRF_HEADER: csrf},
    )
    assert saved.json()["assistant_prompt"] == "Siz Nigoramisiz."
    # An empty examples box is a choice, and is kept as one.
    assert saved.json()["assistant_examples"] == ""

    reset = await client.patch(
        "/api/admin/settings",
        json={"assistant_prompt": None, "assistant_examples": None},
        headers={CSRF_HEADER: csrf},
    )
    assert reset.json()["assistant_prompt"] is None
    assert reset.json()["assistant_examples"] is None


async def test_the_defaults_are_served_for_the_dashboard_to_show(
    client: httpx.AsyncClient, seed: Seed, manager: Any
) -> None:
    _login_as(client, manager.id)

    response = await client.get("/api/admin/assistant-defaults")

    assert response.status_code == 200
    assert "{klinika_malumotlari}" in response.json()["prompt"]
    assert "Bemor:" in response.json()["examples"]


async def test_an_enormous_persona_is_rejected(
    client: httpx.AsyncClient, seed: Seed, manager: Any
) -> None:
    csrf = _login_as(client, manager.id)

    response = await client.patch(
        "/api/admin/settings",
        json={"assistant_prompt": "x" * 20_001},
        headers={CSRF_HEADER: csrf},
    )

    assert response.status_code == 422


# --- files in the knowledge base ---

_PRICES = b"Xizmat,Narx\nUZI,150000\nEKG,80000"


def _upload_form(name: str = "narxlar.csv", data: bytes = _PRICES) -> dict[str, Any]:
    return {"files": {"file": (name, data, "text/csv")}}


async def test_a_file_is_uploaded_listed_and_deleted(
    client: httpx.AsyncClient, seed: Seed, manager: Any
) -> None:
    csrf = _login_as(client, manager.id)

    uploaded = await client.post(
        "/api/admin/knowledge-files", headers={CSRF_HEADER: csrf}, **_upload_form()
    )

    assert uploaded.status_code == 201
    body = uploaded.json()
    assert body["filename"] == "narxlar.csv"
    assert body["chunk_count"] == 1
    assert body["size_bytes"] == len(_PRICES)
    listed = (await client.get("/api/admin/knowledge-files")).json()
    assert [row["id"] for row in listed] == [body["id"]]

    deleted = await client.delete(
        f"/api/admin/knowledge-files/{body['id']}", headers={CSRF_HEADER: csrf}
    )
    assert deleted.status_code == 204
    assert (await client.get("/api/admin/knowledge-files")).json() == []


async def test_uploading_a_name_again_replaces_it(
    client: httpx.AsyncClient, seed: Seed, manager: Any
) -> None:
    csrf = _login_as(client, manager.id)

    await client.post("/api/admin/knowledge-files", headers={CSRF_HEADER: csrf}, **_upload_form())
    await client.post(
        "/api/admin/knowledge-files",
        headers={CSRF_HEADER: csrf},
        **_upload_form(data=b"Xizmat,Narx\nUZI,1"),
    )

    listed = (await client.get("/api/admin/knowledge-files")).json()
    assert len(listed) == 1


@pytest.mark.parametrize(
    ("name", "data", "fragment"),
    [
        ("virus.exe", b"MZ", "formati qo'llab-quvvatlanmaydi"),
        ("bosh.txt", b"", "bo'sh"),
        ("soxta.pdf", b"bu pdf emas", "PDF emas"),
    ],
)
async def test_an_unusable_file_is_refused_with_a_reason_the_operator_can_read(
    client: httpx.AsyncClient, seed: Seed, manager: Any, name: str, data: bytes, fragment: str
) -> None:
    csrf = _login_as(client, manager.id)

    response = await client.post(
        "/api/admin/knowledge-files", headers={CSRF_HEADER: csrf}, **_upload_form(name, data)
    )

    assert response.status_code == 422
    assert fragment in response.json()["detail"]
    assert (await client.get("/api/admin/knowledge-files")).json() == []


async def test_a_file_over_the_limit_is_refused_before_it_is_read(
    client: httpx.AsyncClient, seed: Seed, manager: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("app.api.admin.knowledge_files.MAX_FILE_BYTES", 10)
    csrf = _login_as(client, manager.id)

    response = await client.post(
        "/api/admin/knowledge-files",
        headers={CSRF_HEADER: csrf},
        **_upload_form(data=b"x" * 11),
    )

    assert response.status_code == 413


async def test_an_embedding_outage_is_a_retryable_error_and_stores_nothing(
    client: httpx.AsyncClient, seed: Seed, manager: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Down(EmbeddingProvider):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("down")

    monkeypatch.setattr(knowledge_files_service, "get_embedding_provider", Down)
    csrf = _login_as(client, manager.id)

    response = await client.post(
        "/api/admin/knowledge-files", headers={CSRF_HEADER: csrf}, **_upload_form()
    )

    assert response.status_code == 502
    assert "qayta urinib" in response.json()["detail"]
    assert (await client.get("/api/admin/knowledge-files")).json() == []


async def test_an_upload_without_the_csrf_header_is_refused(
    client: httpx.AsyncClient, seed: Seed, manager: Any
) -> None:
    _login_as(client, manager.id)

    response = await client.post("/api/admin/knowledge-files", **_upload_form())

    assert response.status_code == 403


async def test_the_front_desk_cannot_add_or_remove_what_the_bot_quotes(
    client: httpx.AsyncClient, seed: Seed, manager: Any, front_desk: Any
) -> None:
    manager_csrf = _login_as(client, manager.id)
    uploaded = await client.post(
        "/api/admin/knowledge-files", headers={CSRF_HEADER: manager_csrf}, **_upload_form()
    )
    csrf = _login_as(client, front_desk.id)

    upload = await client.post(
        "/api/admin/knowledge-files", headers={CSRF_HEADER: csrf}, **_upload_form("boshqa.csv")
    )
    delete = await client.delete(
        f"/api/admin/knowledge-files/{uploaded.json()['id']}", headers={CSRF_HEADER: csrf}
    )
    # ...though they may see what is there.
    listed = await client.get("/api/admin/knowledge-files")

    assert upload.status_code == 403
    assert delete.status_code == 403
    assert listed.status_code == 200
    assert len(listed.json()) == 1


async def test_another_clinics_file_is_neither_listed_nor_deletable(
    client: httpx.AsyncClient,
    seed: Seed,
    manager: Any,
    db_session: AsyncSession,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    with as_tenant(seed.tenant_b.id):
        theirs = await KnowledgeDocumentRepository(db_session).create(
            filename="ularniki.pdf", content_type=None, size_bytes=1, chunk_count=1
        )
    csrf = _login_as(client, manager.id)

    listed = (await client.get("/api/admin/knowledge-files")).json()
    deleted = await client.delete(
        f"/api/admin/knowledge-files/{theirs.id}", headers={CSRF_HEADER: csrf}
    )

    assert listed == []
    assert deleted.status_code == 404


# --- appointments and analytics ---


async def test_booking_cancelling_and_confirming_from_the_dashboard(
    client: httpx.AsyncClient, seed: Seed, manager: Any
) -> None:
    csrf = _login_as(client, manager.id)
    slot = _next_free_slot()

    created = await client.post(
        "/api/admin/appointments",
        json={
            "scheduled_at": slot.isoformat(),
            "patient_name": "Aziza",
            "patient_phone": "+998 90 123 45 67",
            "doctor_id": str(seed.a.doctor.id),
        },
        headers={CSRF_HEADER: csrf},
    )
    assert created.status_code == 201
    assert created.json()["doctor_name"] == "Dr. Smith"
    assert created.json()["source"] == "operator"
    appointment_id = created.json()["id"]

    confirmed = await client.post(
        f"/api/admin/appointments/{appointment_id}/confirm", headers={CSRF_HEADER: csrf}
    )
    assert confirmed.json()["status"] == AppointmentStatus.CONFIRMED

    cancelled = await client.post(
        f"/api/admin/appointments/{appointment_id}/cancel", headers={CSRF_HEADER: csrf}
    )
    assert cancelled.json()["status"] == AppointmentStatus.CANCELLED


async def test_double_booking_a_slot_is_refused(
    client: httpx.AsyncClient, seed: Seed, manager: Any
) -> None:
    csrf = _login_as(client, manager.id)
    slot = _next_free_slot(2)
    payload = {"scheduled_at": slot.isoformat(), "patient_name": "Aziza"}

    first = await client.post("/api/admin/appointments", json=payload, headers={CSRF_HEADER: csrf})
    second = await client.post(
        "/api/admin/appointments",
        json={**payload, "patient_name": "Boshqa bemor"},
        headers={CSRF_HEADER: csrf},
    )

    assert first.status_code == 201
    assert second.status_code == 409


async def test_booking_outside_working_hours_is_refused(
    client: httpx.AsyncClient, seed: Seed, manager: Any
) -> None:
    csrf = _login_as(client, manager.id)
    # 03:00 UTC is 08:00 in Tashkent — before the clinic opens.
    at_night = (datetime.now(UTC) + timedelta(days=1)).replace(
        hour=3, minute=0, second=0, microsecond=0
    )

    response = await client.post(
        "/api/admin/appointments",
        json={"scheduled_at": at_night.isoformat(), "patient_name": "Aziza"},
        headers={CSRF_HEADER: csrf},
    )

    assert response.status_code == 400


async def test_analytics_counts_only_this_clinic(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    with as_tenant(seed.tenant_b.id):
        # Noise in the other clinic that must not show up in A's numbers.
        await LeadRepository(db_session).create(patient_name="B lead", phone="+998 90 000 00 00")
        await DoctorRepository(db_session).create(
            name="Dr. B", specialty="Ortodont", working_hours="09:00 - 18:00"
        )
        await AppointmentRepository(db_session).create(
            doctor_name="Dr. B",
            scheduled_at=_next_free_slot(3),
            status=AppointmentStatus.SCHEDULED,
            patient_name="B bemor",
        )

    _login_as(client, seed.a.operator.id)
    body = (await client.get("/api/admin/analytics")).json()

    assert body["conversations_total"] == 1
    assert body["appointments_total"] == 1
    assert body["leads_total"] == 1
    assert body["leads_new"] == 1
    assert body["faqs_active"] == 1


# --- account: password and export ---


async def test_changing_your_own_password(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    from app.core.passwords import hash_password, verify_password

    with as_tenant(seed.tenant_a.id):
        operator = await OperatorRepository(db_session).create(
            name="Operator A",
            role="operator",
            username=f"pw-{seed.tenant_a.id}",
            password_hash=hash_password("current-password"),
        )
    csrf = _login_as(client, operator.id)

    response = await client.post(
        "/api/admin/password",
        json={"current_password": "current-password", "new_password": "a-much-longer-one"},
        headers={CSRF_HEADER: csrf},
    )

    assert response.status_code == 204
    assert verify_password("a-much-longer-one", operator.password_hash)


async def test_the_current_password_is_required_to_change_it(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    """A session alone is not enough: this is what stops a borrowed, unlocked
    browser from being turned into permanent access.
    """
    from app.core.passwords import hash_password

    with as_tenant(seed.tenant_a.id):
        operator = await OperatorRepository(db_session).create(
            name="Operator A",
            role="operator",
            username=f"pw2-{seed.tenant_a.id}",
            password_hash=hash_password("current-password"),
        )
    csrf = _login_as(client, operator.id)

    response = await client.post(
        "/api/admin/password",
        json={"current_password": "wrong", "new_password": "a-much-longer-one"},
        headers={CSRF_HEADER: csrf},
    )

    assert response.status_code == 400


async def test_a_short_new_password_is_rejected(
    client: httpx.AsyncClient, seed: Seed, manager: Any
) -> None:
    csrf = _login_as(client, manager.id)

    response = await client.post(
        "/api/admin/password",
        json={"current_password": "x", "new_password": "short"},
        headers={CSRF_HEADER: csrf},
    )

    assert response.status_code == 422


async def test_the_day_exports_as_csv(client: httpx.AsyncClient, seed: Seed, manager: Any) -> None:
    csrf = _login_as(client, manager.id)
    slot = _next_free_slot()
    await client.post(
        "/api/admin/appointments",
        json={
            "scheduled_at": slot.isoformat(),
            "patient_name": "Aziza Karimova",
            "patient_phone": "+998 90 123 45 67",
        },
        headers={CSRF_HEADER: csrf},
    )

    local_day = slot.astimezone(CLINIC_TIMEZONE).date().isoformat()
    response = await client.get(f"/api/admin/export/appointments.csv?date={local_day}")

    assert response.status_code == 200
    assert "text/csv" in response.headers["content-type"]
    assert "attachment" in response.headers["content-disposition"]
    # A BOM, so Excel on Windows does not mangle every Uzbek name.
    assert response.text.startswith("\ufeff")
    assert "Aziza Karimova" in response.text
    assert "+998 90 123 45 67" in response.text


async def test_the_export_covers_only_this_clinic(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    manager: Any,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    slot = _next_free_slot(2)
    with as_tenant(seed.tenant_b.id):
        await AppointmentRepository(db_session).create(
            doctor_name="Dr. B",
            scheduled_at=slot,
            status=AppointmentStatus.SCHEDULED,
            patient_name="Boshqa klinikaning bemori",
        )

    _login_as(client, manager.id)
    local_day = slot.astimezone(CLINIC_TIMEZONE).date().isoformat()
    response = await client.get(f"/api/admin/export/appointments.csv?date={local_day}")

    assert "Boshqa klinikaning bemori" not in response.text


# --- the boundary between the three roles ----------------------------------


async def test_the_front_desk_can_work_a_lead(
    client: httpx.AsyncClient, seed: Seed, front_desk: Any
) -> None:
    """The front desk's own job. If this ever fails, the role has been
    narrowed into uselessness and everyone will just be given `admin`.
    """
    csrf = _login_as(client, front_desk.id)

    response = await client.post(
        "/api/admin/leads",
        json={"patient_name": "Nodira", "phone": "+998901112233", "topic": "Implant"},
        headers={CSRF_HEADER: csrf},
    )

    assert response.status_code == 201


@pytest.mark.parametrize(
    ("method", "path", "payload"),
    [
        ("post", "/api/admin/doctors", {"name": "Dr. New", "specialty": "Ortodont"}),
        ("post", "/api/admin/knowledge-base", {"question": "Narx?", "answer": "3 000 000"}),
        ("patch", "/api/admin/settings", {"clinic_address": "Yangi manzil"}),
        ("patch", "/api/admin/settings", {"assistant_prompt": "Boshqa rol"}),
    ],
)
async def test_the_front_desk_cannot_change_what_the_clinic_says(
    client: httpx.AsyncClient,
    seed: Seed,
    front_desk: Any,
    method: str,
    path: str,
    payload: dict[str, Any],
) -> None:
    """A wrong answer in the knowledge base is quoted to every patient who
    asks, by a bot, with nobody reading it first — so editing it is not part
    of a front-desk shift.
    """
    csrf = _login_as(client, front_desk.id)

    response = await getattr(client, method)(path, json=payload, headers={CSRF_HEADER: csrf})

    assert response.status_code == 403


async def test_a_role_nobody_defined_cannot_be_stored_at_all(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    """The regression this whole change exists for, caught one layer lower
    than expected.

    Under the previous check (`if role == "doctor": deny`) an account with
    any other role held every right over the clinic, so a typo was an
    administrator. The permission layer now refuses an unknown role
    (tests/test_roles.py), and the table refuses to hold one in the first
    place — so this cannot be written through a script or a psql session
    either, which is where such a value would realistically come from.
    """
    with as_tenant(seed.tenant_a.id), pytest.raises(IntegrityError, match="ck_operators_role"):
        await OperatorRepository(db_session).create(
            name="Typo",
            role="operatr",
            username=f"typo-{seed.tenant_a.id}",
            password_hash="x",
        )
    await db_session.rollback()


async def test_the_csv_of_patient_names_and_phones_is_not_open_to_every_account(
    client: httpx.AsyncClient, seed: Seed
) -> None:
    """The export carries patient names and phone numbers out of the
    building, so it is gated as patient data rather than as a report. The
    seed operator is a `doctor`.
    """
    _login_as(client, seed.a.operator.id)

    response = await client.get("/api/admin/export/appointments.csv")

    assert response.status_code == 403


# --- marking a visit Keldi / Kelmadi ---


def _outcome_url(appointment_id: Any, outcome: str) -> str:
    return f"/api/admin/appointments/{appointment_id}/{outcome}"


async def test_a_visit_that_has_come_is_marked_and_a_wrong_mark_put_right(
    client: httpx.AsyncClient, seed: Seed, front_desk: Any
) -> None:
    """The seeded booking is for now: its time has come."""
    headers = {CSRF_HEADER: _login_as(client, front_desk.id)}

    came = await client.post(_outcome_url(seed.a.appointment.id, "complete"), headers=headers)
    assert came.status_code == 200, came.text
    assert came.json()["status"] == AppointmentStatus.COMPLETED

    fixed = await client.post(_outcome_url(seed.a.appointment.id, "no-show"), headers=headers)
    assert fixed.status_code == 200
    assert fixed.json()["status"] == AppointmentStatus.NO_SHOW


async def test_the_report_counts_the_marked_outcome(
    client: httpx.AsyncClient, seed: Seed, manager: Any
) -> None:
    headers = {CSRF_HEADER: _login_as(client, manager.id)}

    await client.post(_outcome_url(seed.a.appointment.id, "complete"), headers=headers)
    report = (await client.get("/api/admin/reports?days=7")).json()

    statuses = {bucket["key"]: bucket for bucket in report["appointment_status"]}
    assert statuses["completed"]["label"] == "Keldi"
    assert statuses["completed"]["count"] >= 1


async def test_a_visit_still_ahead_cannot_be_marked(
    client: httpx.AsyncClient, seed: Seed, manager: Any
) -> None:
    headers = {CSRF_HEADER: _login_as(client, manager.id)}
    created = await client.post(
        "/api/admin/appointments",
        json={"scheduled_at": _next_free_slot().isoformat(), "patient_name": "Ertangi"},
        headers=headers,
    )
    assert created.status_code == 201

    for outcome in ("complete", "no-show"):
        response = await client.post(_outcome_url(created.json()["id"], outcome), headers=headers)
        assert response.status_code == 409
        assert response.json()["detail"] == "Qabul vaqti hali kelmagan"


async def test_a_cancelled_visit_cannot_be_marked(
    client: httpx.AsyncClient, seed: Seed, manager: Any
) -> None:
    headers = {CSRF_HEADER: _login_as(client, manager.id)}
    await client.post(f"/api/admin/appointments/{seed.a.appointment.id}/cancel", headers=headers)

    response = await client.post(_outcome_url(seed.a.appointment.id, "complete"), headers=headers)

    assert response.status_code == 409
    assert response.json()["detail"] == "Bekor qilingan qabulni belgilab bo'lmaydi"


@pytest.mark.parametrize("outcome", ["complete", "no-show"])
async def test_a_view_only_doctor_account_cannot_mark(
    client: httpx.AsyncClient, seed: Seed, outcome: str
) -> None:
    """The seed's own operator is a `doctor`: it reads, it does not write."""
    headers = {CSRF_HEADER: _login_as(client, seed.a.operator.id)}

    response = await client.post(_outcome_url(seed.a.appointment.id, outcome), headers=headers)

    assert response.status_code == 403


@pytest.mark.parametrize("outcome", ["complete", "no-show"])
async def test_marking_needs_the_csrf_token(
    client: httpx.AsyncClient, seed: Seed, manager: Any, outcome: str
) -> None:
    _login_as(client, manager.id)

    response = await client.post(_outcome_url(seed.a.appointment.id, outcome))

    assert response.status_code == 403


async def test_another_clinics_visit_is_not_found(
    client: httpx.AsyncClient, seed: Seed, manager: Any
) -> None:
    headers = {CSRF_HEADER: _login_as(client, manager.id)}

    response = await client.post(_outcome_url(seed.b.appointment.id, "complete"), headers=headers)

    assert response.status_code == 404


async def test_an_unknown_visit_is_not_found(client: httpx.AsyncClient, manager: Any) -> None:
    headers = {CSRF_HEADER: _login_as(client, manager.id)}
    missing = "00000000-0000-0000-0000-000000000000"

    assert (
        await client.post(_outcome_url(missing, "complete"), headers=headers)
    ).status_code == 404
    assert (await client.post(_outcome_url("nope", "complete"), headers=headers)).status_code == 422


async def test_a_waiting_conversation_says_what_the_patient_asked(
    client: httpx.AsyncClient, db_session: AsyncSession, seed: Seed
) -> None:
    """UX audit, 2026-10-16: the task list showed the last message, usually
    "Rahmat!", so nobody could tell what a pinned conversation was about."""
    from app.models.conversation import Conversation
    from app.models.message import Message

    _login_as(client, seed.a.operator.id)
    conversation = await db_session.get(Conversation, seed.a.conversation.id)
    assert conversation is not None
    conversation.needs_doctor_since = datetime.now(UTC)
    base = datetime.now(UTC) + timedelta(minutes=1)
    for i, (sender, text) in enumerate(
        [
            ("patient", "PSA natijam 8.5 chiqdi, bu xavflimi?"),
            ("bot", "Doktor javob beradi."),
            ("patient", "Rahmat!"),
        ]
    ):
        db_session.add(
            Message(
                conversation_id=conversation.id,
                sender=sender,
                content=text,
                channel="instagram",
                created_at=base + timedelta(seconds=i),
            )
        )
    await db_session.flush()

    response = await client.get("/api/admin/conversations?needs_doctor=true")
    row = next(r for r in response.json() if r["id"] == str(conversation.id))
    assert row["waiting_reason"].startswith("PSA natijam")


async def test_the_inbox_counts_the_conversations_staff_have(
    client: httpx.AsyncClient, db_session: AsyncSession, seed: Seed
) -> None:
    """The "Operatorga o'tganlar" filter: its count and its rows agree, and
    another clinic's conversations are in neither."""
    from app.models.conversation import Conversation

    _login_as(client, seed.a.operator.id)
    before = (await client.get("/api/admin/conversations/counts")).json()
    conversation = await db_session.get(Conversation, seed.a.conversation.id)
    assert conversation is not None
    conversation.is_bot_enabled = False
    other = await db_session.get(Conversation, seed.b.conversation.id)
    assert other is not None
    other.is_bot_enabled = False
    await db_session.flush()

    counts = (await client.get("/api/admin/conversations/counts")).json()
    rows = (await client.get("/api/admin/conversations?only_taken_over=true")).json()

    assert counts["taken_over"] == before["taken_over"] + 1
    assert counts["taken_over"] == len(rows)
    assert str(conversation.id) in {r["id"] for r in rows}
    assert str(other.id) not in {r["id"] for r in rows}


async def test_the_inbox_finds_the_conversations_that_began_as_comments(
    client: httpx.AsyncClient, db_session: AsyncSession, seed: Seed
) -> None:
    """The "Izohlar" filter: people who wrote under a post, counted and listed."""
    from app.models.message import Message
    from app.services.message_labels import COMMENT_PREFIX

    _login_as(client, seed.a.operator.id)
    before = (await client.get("/api/admin/conversations/counts")).json()
    db_session.add(
        Message(
            conversation_id=seed.a.conversation.id,
            sender="patient",
            content=f"{COMMENT_PREFIX}Narxi qancha?",
            channel="instagram",
        )
    )
    await db_session.flush()

    counts = (await client.get("/api/admin/conversations/counts")).json()
    rows = (await client.get("/api/admin/conversations?from_comments=true")).json()

    assert counts["comments"] == before["comments"] + 1
    assert counts["comments"] == len(rows)
    assert {r["id"] for r in rows} == {str(seed.a.conversation.id)}
