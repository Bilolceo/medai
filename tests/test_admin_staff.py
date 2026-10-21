"""Staff accounts through the dashboard's own API.

The interesting cases here are not the CRUD. They are the two ways a clinic
could lock itself out of its own dashboard, and the one way an account could
reach into another clinic's staff list.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin.deps import CSRF_HEADER
from app.core.db import get_db_session
from app.core.passwords import verify_password
from app.core.session import SESSION_COOKIE_NAME, create_session_cookie
from app.main import app
from app.models.operator import Operator
from app.repositories.operator import OperatorRepository
from tests.conftest import Seed

GOOD_PASSWORD = "korrekt-ot-batareyka"


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
            name="Admin A", role="admin", username=f"admin-{seed.tenant_a.id}", password_hash="x"
        )


def _login_as(client: httpx.AsyncClient, operator: Operator) -> str:
    cookie_value, csrf_token = create_session_cookie(operator.id, operator.password_hash)
    client.cookies.set(SESSION_COOKIE_NAME, cookie_value)
    return csrf_token


# --- creating ---------------------------------------------------------------


async def test_an_admin_hires_a_receptionist(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    admin: Any,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    csrf = _login_as(client, admin)

    response = await client.post(
        "/api/admin/operators",
        json={
            "name": "Nodira Karimova",
            "username": "nodira",
            "password": GOOD_PASSWORD,
            "role": "operator",
        },
        headers={CSRF_HEADER: csrf},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["username"] == "nodira"
    assert body["role"] == "operator"
    # The password is set, never echoed: the response is what ends up in a
    # browser's network log and in whatever the dashboard renders.
    assert "password" not in body
    assert "password_hash" not in body

    with as_tenant(seed.tenant_a.id):
        created = await OperatorRepository(db_session).get(UUID(body["id"]))
    assert created is not None
    assert verify_password(GOOD_PASSWORD, created.password_hash)


async def test_a_short_password_is_refused_before_the_account_exists(
    client: httpx.AsyncClient, seed: Seed, admin: Any
) -> None:
    csrf = _login_as(client, admin)

    response = await client.post(
        "/api/admin/operators",
        json={"name": "N", "username": "shorty", "password": "1234", "role": "operator"},
        headers={CSRF_HEADER: csrf},
    )

    assert response.status_code == 422


async def test_a_taken_login_is_a_conflict_that_does_not_say_whose(
    client: httpx.AsyncClient, seed: Seed, admin: Any
) -> None:
    """Usernames are unique across every clinic because the login form has
    no clinic selector, so this can collide with a name in a clinic the
    caller cannot see. The message must not turn that into a way to probe
    for it.
    """
    csrf = _login_as(client, admin)
    taken = seed.b.operator.username

    response = await client.post(
        "/api/admin/operators",
        json={"name": "Someone", "username": taken, "password": GOOD_PASSWORD, "role": "operator"},
        headers={CSRF_HEADER: csrf},
    )

    assert response.status_code == 409
    assert taken not in response.json()["detail"]


# --- who may do this at all -------------------------------------------------


@pytest.mark.parametrize("role", ["operator", "doctor"])
async def test_only_an_admin_manages_staff(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    role: str,
) -> None:
    with as_tenant(seed.tenant_a.id):
        member = await OperatorRepository(db_session).create(
            name="Staff", role=role, username=f"{role}-{seed.tenant_a.id}", password_hash="x"
        )
    csrf = _login_as(client, member)

    assert (await client.get("/api/admin/operators")).status_code == 403
    created = await client.post(
        "/api/admin/operators",
        json={"name": "X", "username": "x-new", "password": GOOD_PASSWORD, "role": "admin"},
        headers={CSRF_HEADER: csrf},
    )
    assert created.status_code == 403


async def test_the_staff_list_stops_at_this_clinic(
    client: httpx.AsyncClient, seed: Seed, admin: Any
) -> None:
    _login_as(client, admin)

    usernames = {row["username"] for row in (await client.get("/api/admin/operators")).json()}

    assert admin.username in usernames
    assert seed.b.operator.username not in usernames


async def test_another_clinics_account_is_a_404_not_a_403(
    client: httpx.AsyncClient, seed: Seed, admin: Any
) -> None:
    """A 403 would confirm the id exists. A 404 is the same answer an id
    that does not exist gets.
    """
    csrf = _login_as(client, admin)

    response = await client.patch(
        f"/api/admin/operators/{seed.b.operator.id}",
        json={"name": "Renamed"},
        headers={CSRF_HEADER: csrf},
    )

    assert response.status_code == 404
    missing = await client.patch(
        f"/api/admin/operators/{uuid4()}", json={"name": "X"}, headers={CSRF_HEADER: csrf}
    )
    assert missing.status_code == 404


async def test_a_write_without_the_csrf_header_is_refused(
    client: httpx.AsyncClient, seed: Seed, admin: Any
) -> None:
    _login_as(client, admin)

    response = await client.post(
        "/api/admin/operators",
        json={"name": "X", "username": "x2", "password": GOOD_PASSWORD, "role": "operator"},
    )

    assert response.status_code == 403


# --- not locking the clinic out of its own dashboard ------------------------


async def test_you_cannot_demote_yourself(
    client: httpx.AsyncClient, seed: Seed, admin: Any
) -> None:
    """Holding MANAGE_STAFF is exactly the state in which one wrong click
    removes the only account that could undo it.
    """
    csrf = _login_as(client, admin)

    response = await client.patch(
        f"/api/admin/operators/{admin.id}", json={"role": "doctor"}, headers={CSRF_HEADER: csrf}
    )

    assert response.status_code == 409


async def test_you_cannot_delete_yourself(
    client: httpx.AsyncClient, seed: Seed, admin: Any
) -> None:
    csrf = _login_as(client, admin)

    response = await client.delete(f"/api/admin/operators/{admin.id}", headers={CSRF_HEADER: csrf})

    assert response.status_code == 409


async def test_the_last_admin_cannot_be_removed_by_another_admin(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    admin: Any,
) -> None:
    """Two admins, so neither is deleting themselves — then the second one
    goes, and the survivor must still be refused. A clinic with no admin
    keeps answering patients and quietly cannot change its own FAQ,
    settings or staff again.
    """
    with as_tenant(seed.tenant_a.id):
        second = await OperatorRepository(db_session).create(
            name="Admin Two", role="admin", username=f"admin2-{seed.tenant_a.id}", password_hash="x"
        )
    csrf = _login_as(client, admin)

    removed = await client.delete(f"/api/admin/operators/{second.id}", headers={CSRF_HEADER: csrf})
    assert removed.status_code == 204

    # `admin` is now the only one left, and is refused even by itself.
    response = await client.delete(f"/api/admin/operators/{admin.id}", headers={CSRF_HEADER: csrf})
    assert response.status_code == 409


async def test_a_second_admin_can_be_demoted_while_one_remains(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    admin: Any,
) -> None:
    """The mirror of the rule above: the guard is "the last one", not "any
    admin", or a clinic could never undo a promotion.
    """
    with as_tenant(seed.tenant_a.id):
        second = await OperatorRepository(db_session).create(
            name="Admin Two", role="admin", username=f"admin2-{seed.tenant_a.id}", password_hash="x"
        )
    csrf = _login_as(client, admin)

    response = await client.patch(
        f"/api/admin/operators/{second.id}", json={"role": "operator"}, headers={CSRF_HEADER: csrf}
    )

    assert response.status_code == 200
    assert response.json()["role"] == "operator"


# --- editing ----------------------------------------------------------------


async def test_resetting_a_password_does_not_touch_the_role(
    client: httpx.AsyncClient,
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    admin: Any,
) -> None:
    """A receptionist who forgot her password gets a new one, not a new job."""
    with as_tenant(seed.tenant_a.id):
        member = await OperatorRepository(db_session).create(
            name="Nodira", role="operator", username=f"nod-{seed.tenant_a.id}", password_hash="x"
        )
    csrf = _login_as(client, admin)

    response = await client.patch(
        f"/api/admin/operators/{member.id}",
        json={"password": GOOD_PASSWORD},
        headers={CSRF_HEADER: csrf},
    )

    assert response.status_code == 200
    assert response.json()["role"] == "operator"
    await db_session.refresh(member)
    assert verify_password(GOOD_PASSWORD, member.password_hash)


async def test_a_role_outside_the_three_is_refused(
    client: httpx.AsyncClient, seed: Seed, admin: Any
) -> None:
    csrf = _login_as(client, admin)

    response = await client.post(
        "/api/admin/operators",
        json={
            "name": "X",
            "username": "superuser",
            "password": GOOD_PASSWORD,
            "role": "superuser",
        },
        headers={CSRF_HEADER: csrf},
    )

    assert response.status_code == 422
