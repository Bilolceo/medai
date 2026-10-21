"""Views the clinic saves for itself.

The point of the feature is that a new question about the clinic's own data
stops being a developer's job, so the cases worth having are the ones where
that could go wrong: a saved view outliving the control it was built on,
one clinic seeing another's, and anything that would let a saved value mean
more than a value.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin.deps import CSRF_HEADER
from app.core.db import get_db_session
from app.core.session import SESSION_COOKIE_NAME, create_session_cookie
from app.main import app
from app.models.operator import Operator
from app.repositories.appointment import AppointmentRepository
from app.repositories.lead import LeadRepository
from app.repositories.operator import OperatorRepository
from app.repositories.saved_filter import SavedFilterRepository
from app.services import search
from tests.conftest import Seed


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    async def _get_db_session_override() -> AsyncSession:
        return db_session

    app.dependency_overrides[get_db_session] = _get_db_session_override
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac
    app.dependency_overrides.pop(get_db_session, None)


@pytest.fixture
async def admin(
    db_session: AsyncSession, seed: Seed, as_tenant: Callable[[UUID], AbstractContextManager[None]]
) -> Any:
    with as_tenant(seed.tenant_a.id):
        return await OperatorRepository(db_session).create(
            name="Admin A", role="admin", username=f"fadmin-{seed.tenant_a.id}", password_hash="x"
        )


def _login_as(client: httpx.AsyncClient, operator: Operator) -> str:
    cookie_value, csrf_token = create_session_cookie(operator.id, operator.password_hash)
    client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
    return csrf_token


TODAYS_TELEGRAM = {
    "resource": "appointments",
    "name": "Bugungi Telegram qabullari",
    "params": {"source": "telegram", "status": "scheduled"},
}


# --- saving a view ----------------------------------------------------------


async def test_an_admin_saves_a_view_the_clinic_asks_for_every_morning(
    client: httpx.AsyncClient, seed: Seed, admin: Any
) -> None:
    csrf = _login_as(client, admin)

    response = await client.post(
        "/api/admin/filters", json=TODAYS_TELEGRAM, headers={CSRF_HEADER: csrf}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "Bugungi Telegram qabullari"
    assert body["params"] == {"source": "telegram", "status": "scheduled"}


async def test_a_saved_view_replays_through_the_endpoint_it_was_saved_for(
    client: httpx.AsyncClient, seed: Seed, admin: Any
) -> None:
    """The whole design: a saved filter is parameters for an endpoint that
    already exists, so applying one is the same request a person clicking
    the controls makes. If this ever fails, there are two query paths and
    one of them will drift.
    """
    csrf = _login_as(client, admin)
    saved = (
        await client.post("/api/admin/filters", json=TODAYS_TELEGRAM, headers={CSRF_HEADER: csrf})
    ).json()

    replayed = await client.get("/api/admin/appointments", params=saved["params"])

    assert replayed.status_code == 200


async def test_two_views_cannot_share_a_name_on_the_same_list(
    client: httpx.AsyncClient, seed: Seed, admin: Any
) -> None:
    """The second would be unreachable in the interface, which is a worse
    answer than refusing to save it.
    """
    csrf = _login_as(client, admin)
    await client.post("/api/admin/filters", json=TODAYS_TELEGRAM, headers={CSRF_HEADER: csrf})

    again = await client.post(
        "/api/admin/filters", json=TODAYS_TELEGRAM, headers={CSRF_HEADER: csrf}
    )

    assert again.status_code == 409


async def test_the_same_name_on_a_different_list_is_a_different_question(
    client: httpx.AsyncClient, seed: Seed, admin: Any
) -> None:
    csrf = _login_as(client, admin)
    await client.post(
        "/api/admin/filters",
        json={"resource": "appointments", "name": "Bugun", "params": {}},
        headers={CSRF_HEADER: csrf},
    )

    other = await client.post(
        "/api/admin/filters",
        json={"resource": "leads", "name": "Bugun", "params": {"status": "new"}},
        headers={CSRF_HEADER: csrf},
    )

    assert other.status_code == 201


# --- what a saved view may contain ------------------------------------------


async def test_a_parameter_the_list_does_not_understand_is_dropped_not_refused(
    client: httpx.AsyncClient, seed: Seed, admin: Any
) -> None:
    """A view saved before a list gained or lost a control should keep
    working, showing the rows it still can — not become an error the clinic
    cannot fix from the interface.
    """
    csrf = _login_as(client, admin)

    response = await client.post(
        "/api/admin/filters",
        json={
            "resource": "leads",
            "name": "Yangi lidlar",
            "params": {"status": "new", "tenant_id": str(uuid4()), "order_by": "password"},
        },
        headers={CSRF_HEADER: csrf},
    )

    assert response.status_code == 201
    assert response.json()["params"] == {"status": "new"}


async def test_a_saved_view_cannot_become_a_place_to_keep_documents(
    client: httpx.AsyncClient, seed: Seed, admin: Any
) -> None:
    csrf = _login_as(client, admin)

    response = await client.post(
        "/api/admin/filters",
        json={"resource": "leads", "name": "Katta", "params": {"status": "x" * 500}},
        headers={CSRF_HEADER: csrf},
    )

    assert response.status_code == 422


async def test_a_nested_value_is_refused(client: httpx.AsyncClient, seed: Seed, admin: Any) -> None:
    """Query parameters are flat. Anything else is somebody storing
    structure where the API only ever reads a string.
    """
    csrf = _login_as(client, admin)

    response = await client.post(
        "/api/admin/filters",
        json={"resource": "leads", "name": "Ichma-ich", "params": {"status": {"$ne": None}}},
        headers={CSRF_HEADER: csrf},
    )

    assert response.status_code == 422


async def test_a_list_nobody_serves_is_refused(
    client: httpx.AsyncClient, seed: Seed, admin: Any
) -> None:
    csrf = _login_as(client, admin)

    response = await client.post(
        "/api/admin/filters",
        json={"resource": "operators", "name": "Xodimlar", "params": {}},
        headers={CSRF_HEADER: csrf},
    )

    assert response.status_code == 422


# --- who may do what --------------------------------------------------------


async def test_the_front_desk_can_use_the_views_but_not_write_them(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    admin: Any,
) -> None:
    """A view is only worth having if the people working the lists can click
    it; deciding which views exist is the admin's.
    """
    csrf = _login_as(client, admin)
    await client.post("/api/admin/filters", json=TODAYS_TELEGRAM, headers={CSRF_HEADER: csrf})

    with as_tenant(seed.tenant_a.id):
        desk = await OperatorRepository(db_session).create(
            name="Front desk",
            role="operator",
            username=f"fdesk-{seed.tenant_a.id}",
            password_hash="x",
        )
    desk_csrf = _login_as(client, desk)

    assert len((await client.get("/api/admin/filters")).json()) == 1
    refused = await client.post(
        "/api/admin/filters",
        json={"resource": "leads", "name": "Meniki", "params": {}},
        headers={CSRF_HEADER: desk_csrf},
    )
    assert refused.status_code == 403


async def test_one_clinics_views_are_not_another_clinics(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    admin: Any,
) -> None:
    csrf = _login_as(client, admin)
    await client.post("/api/admin/filters", json=TODAYS_TELEGRAM, headers={CSRF_HEADER: csrf})

    with as_tenant(seed.tenant_b.id):
        other_admin = await OperatorRepository(db_session).create(
            name="Admin B", role="admin", username=f"fadmin-{seed.tenant_b.id}", password_hash="x"
        )
    _login_as(client, other_admin)

    assert (await client.get("/api/admin/filters")).json() == []


async def test_another_clinics_view_cannot_be_deleted(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    admin: Any,
) -> None:
    with as_tenant(seed.tenant_b.id):
        theirs = await SavedFilterRepository(db_session).create(
            resource="leads", name="Ularniki", params={}, position=0
        )
    csrf = _login_as(client, admin)

    response = await client.delete(f"/api/admin/filters/{theirs.id}", headers={CSRF_HEADER: csrf})

    assert response.status_code == 404


async def test_a_write_without_the_csrf_header_is_refused(
    client: httpx.AsyncClient, seed: Seed, admin: Any
) -> None:
    _login_as(client, admin)

    response = await client.post("/api/admin/filters", json=TODAYS_TELEGRAM)

    assert response.status_code == 403


# --- keeping them tidy ------------------------------------------------------


async def test_a_view_can_be_renamed_and_reordered(
    client: httpx.AsyncClient, seed: Seed, admin: Any
) -> None:
    csrf = _login_as(client, admin)
    saved = (
        await client.post("/api/admin/filters", json=TODAYS_TELEGRAM, headers={CSRF_HEADER: csrf})
    ).json()

    response = await client.patch(
        f"/api/admin/filters/{saved['id']}",
        json={"name": "  Telegram   qabullari  ", "position": 3},
        headers={CSRF_HEADER: csrf},
    )

    assert response.status_code == 200
    # Whitespace a person typed is not part of the name.
    assert response.json()["name"] == "Telegram qabullari"
    assert response.json()["position"] == 3


async def test_a_deleted_view_is_gone_from_the_list(
    client: httpx.AsyncClient, seed: Seed, admin: Any
) -> None:
    csrf = _login_as(client, admin)
    saved = (
        await client.post("/api/admin/filters", json=TODAYS_TELEGRAM, headers={CSRF_HEADER: csrf})
    ).json()

    removed = await client.delete(f"/api/admin/filters/{saved['id']}", headers={CSRF_HEADER: csrf})

    assert removed.status_code == 204
    assert (await client.get("/api/admin/filters")).json() == []


# --- what the lists' own filters find ----------------------------------------
#
# The dashboard used to filter leads in the browser, over the hundred newest
# it had loaded, and searched conversations by name only. These are the
# server doing it instead, over everything.

AsTenant = Callable[[UUID], AbstractContextManager[None]]


def test_wildcards_typed_into_a_search_are_taken_literally() -> None:
    assert search.like_pattern("100%") == "%100\\%%"
    assert search.like_pattern("a_b") == "%a\\_b%"
    assert search.like_pattern("@Asomov ") == "%asomov%"
    assert search.like_pattern("c:\\x") == "%c:\\\\x%"


@pytest.mark.parametrize(
    ("typed", "digits"),
    [("+998 90 123-45-67", "998901234567"), ("90 1234", "901234"), ("Aziza", None), ("12", None)],
)
def test_a_search_is_a_phone_number_once_it_has_enough_digits(
    typed: str, digits: str | None
) -> None:
    assert search.phone_digits(typed) == digits


async def _leads(db_session: AsyncSession, seed: Seed, as_tenant: AsTenant) -> None:
    with as_tenant(seed.tenant_a.id):
        repo = LeadRepository(db_session)
        await repo.create(patient_name="Bekzod Aliyev", phone="+998 90 111 22 33", topic="buyrak")
        await repo.create(
            patient_name="Olim", phone="998935554466", topic="prostatit", status="contacted"
        )
        await repo.create(patient_name="Foiz 100% chegirma", phone=None, topic="narx")
    with as_tenant(seed.tenant_b.id):
        await LeadRepository(db_session).create(patient_name="Bekzod Boshqa", phone="+998901112233")
    await db_session.flush()


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        ("bekzod", {"Bekzod Aliyev"}),
        ("BUYRAK", {"Bekzod Aliyev"}),
        ("90 111 22 33", {"Bekzod Aliyev"}),
        ("901112233", {"Bekzod Aliyev"}),
        ("+998-93-555-44-66", {"Olim"}),
        ("100%", {"Foiz 100% chegirma"}),
        # Taken literally: the one lead with a "%" in it, not every lead.
        ("%", {"Foiz 100% chegirma"}),
        ("hech kim", set()),
    ],
)
async def test_leads_are_searched_by_the_server_over_all_of_them(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    admin: Any,
    as_tenant: AsTenant,
    q: str,
    expected: set[str],
) -> None:
    await _leads(db_session, seed, as_tenant)
    _login_as(client, admin)

    found = (await client.get("/api/admin/leads", params={"q": q})).json()

    # The seed's own lead ("Aziza") and the other clinic's never come back.
    assert {lead["patient_name"] for lead in found} == expected


async def test_only_new_leads_with_a_search_is_both_conditions(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    admin: Any,
    as_tenant: AsTenant,
) -> None:
    await _leads(db_session, seed, as_tenant)
    _login_as(client, admin)

    new_olim = (await client.get("/api/admin/leads", params={"q": "olim", "status": "new"})).json()
    any_olim = (await client.get("/api/admin/leads", params={"q": "olim"})).json()

    assert new_olim == []
    assert [lead["patient_name"] for lead in any_olim] == ["Olim"]


async def test_the_lead_list_can_be_asked_for_more_than_a_hundred(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    admin: Any,
    as_tenant: AsTenant,
) -> None:
    with as_tenant(seed.tenant_a.id):
        repo = LeadRepository(db_session)
        for n in range(120):
            await repo.create(patient_name=f"Bemor {n}", phone=f"+99890{n:07d}")
    await db_session.flush()
    _login_as(client, admin)

    first = (await client.get("/api/admin/leads", params={"status": "new"})).json()
    more = (await client.get("/api/admin/leads", params={"status": "new", "limit": 200})).json()

    assert len(first) == 100
    assert len(more) == 121  # and the seed's own
    assert (await client.get("/api/admin/leads", params={"limit": 501})).status_code == 422


async def test_conversations_are_found_by_the_patients_phone_however_it_is_written(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    admin: Any,
    as_tenant: AsTenant,
) -> None:
    with as_tenant(seed.tenant_a.id):
        seed.a.user.phone = "+998 97 765 43 21"
        await LeadRepository(db_session).create(
            patient_name="X", phone="+998 33 123 45 67", conversation_id=seed.a.conversation.id
        )
    with as_tenant(seed.tenant_b.id):
        seed.b.user.phone = "+998977654321"
    await db_session.flush()
    _login_as(client, admin)

    async def ids(q: str) -> list[str]:
        rows = (await client.get("/api/admin/conversations", params={"q": q})).json()
        return [row["id"] for row in rows]

    mine = [str(seed.a.conversation.id)]
    assert await ids("977654321") == mine  # on the patient
    assert await ids("97-765-43-21") == mine
    assert await ids("33 123 45 67") == mine  # on a lead they left
    assert await ids("1234 9999") == []
    assert await ids("_") == []  # a wildcard, taken literally


async def test_the_conversation_list_can_be_asked_for_more(
    client: httpx.AsyncClient, seed: Seed, admin: Any
) -> None:
    _login_as(client, admin)

    assert (await client.get("/api/admin/conversations", params={"limit": 500})).status_code == 200
    assert (await client.get("/api/admin/conversations", params={"limit": 501})).status_code == 422


async def test_the_instagram_source_includes_bookings_written_before_channels_were(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    admin: Any,
    as_tenant: AsTenant,
) -> None:
    """The bot wrote "bot" before it wrote "instagram"; the report already
    counts both as Instagram, and the filter has to agree with it."""
    when = datetime.now(UTC).replace(microsecond=0)
    with as_tenant(seed.tenant_a.id):
        repo = AppointmentRepository(db_session)
        for minutes, source in ((5, "instagram"), (6, "bot"), (7, "telegram"), (8, "operator")):
            await repo.create(
                scheduled_at=when + timedelta(minutes=minutes),
                doctor_name="Test",
                patient_name=f"Manba {source}",
                source=source,
                status="scheduled",
            )
    await db_session.flush()
    _login_as(client, admin)

    async def names(source: str) -> set[str]:
        rows = (await client.get("/api/admin/appointments", params={"source": source})).json()
        return {r["patient_name"] for r in rows if (r["patient_name"] or "").startswith("Manba")}

    assert await names("instagram") == {"Manba instagram", "Manba bot"}
    assert await names("telegram") == {"Manba telegram"}
    assert await names("operator") == {"Manba operator"}


async def test_a_view_keeps_what_the_screen_shows(
    client: httpx.AsyncClient, seed: Seed, admin: Any
) -> None:
    """Days from today rather than a date, and the search box -- the two
    things a saved view used to lose."""
    csrf = _login_as(client, admin)

    appointments = await client.post(
        "/api/admin/filters",
        json={"resource": "appointments", "name": "Ertangi", "params": {"offset": 1, "days": "1"}},
        headers={CSRF_HEADER: csrf},
    )
    chats = await client.post(
        "/api/admin/filters",
        json={
            "resource": "conversations",
            "name": "Doktor kutayotganlar",
            "params": {"q": "asomov", "needs_doctor": True},
        },
        headers={CSRF_HEADER: csrf},
    )
    leads = await client.post(
        "/api/admin/filters",
        json={"resource": "leads", "name": "Buyrak", "params": {"q": "buyrak", "status": "new"}},
        headers={CSRF_HEADER: csrf},
    )

    assert appointments.json()["params"] == {"offset": 1, "days": "1"}
    assert chats.json()["params"] == {"q": "asomov", "needs_doctor": True}
    assert leads.json()["params"] == {"q": "buyrak", "status": "new"}
    replay = await client.get("/api/admin/conversations", params=chats.json()["params"])
    assert replay.status_code == 200
