"""Patient photos: medical images, served from the dashboard's own origin.

Who can fetch them, and what they can be once fetched.
"""

import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin.deps import CSRF_HEADER
from app.core.db import get_db_session
from app.core.session import SESSION_COOKIE_NAME, create_session_cookie
from app.main import app
from app.models.operator import Operator
from app.models.patient_media import PatientMedia
from app.models.user import User
from app.repositories.operator import OperatorRepository
from app.services import patient_media
from tests.conftest import Seed

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(document.cookie)</script></svg>'


@pytest.fixture
async def client(db_session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    async def _session() -> AsyncSession:
        return db_session

    app.dependency_overrides[get_db_session] = _session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as ac:
        yield ac
    app.dependency_overrides.pop(get_db_session, None)


@pytest.fixture
async def front_desk(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[uuid.UUID], AbstractContextManager[None]],
) -> Any:
    with as_tenant(seed.tenant_a.id):
        return await OperatorRepository(db_session).create(
            name="Front Desk", role="operator", username=f"fd-{seed.tenant_a.id}", password_hash="x"
        )


def _login(client: httpx.AsyncClient, operator: Operator) -> str:
    cookie, csrf = create_session_cookie(operator.id, operator.password_hash)
    client.cookies.set(SESSION_COOKIE_NAME, cookie)
    return csrf


async def _photo(
    session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID, content: bytes, kind: str
) -> uuid.UUID:
    media = PatientMedia(
        tenant_id=tenant_id,
        user_id=user_id,
        source_url="https://lookaside.fbsbx.com/x",
        content=content,
        content_type=kind,
        size_bytes=len(content),
    )
    session.add(media)
    await session.flush()
    return media.id


async def test_a_photo_is_served_with_headers_that_keep_it_inert(
    client: httpx.AsyncClient, db_session: AsyncSession, seed: Seed, front_desk: Any
) -> None:
    media_id = await _photo(db_session, seed.tenant_a.id, seed.a.user.id, PNG, "image/png")
    _login(client, front_desk)

    response = await client.get(f"/api/admin/media/{media_id}/file")

    assert response.status_code == 200
    assert response.content == PNG
    assert response.headers["content-type"] == "image/png"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]
    assert "private" in response.headers["cache-control"]


async def test_a_stored_svg_is_never_served_as_something_a_browser_runs(
    client: httpx.AsyncClient, db_session: AsyncSession, seed: Seed, front_desk: Any
) -> None:
    """A row from before SVGs were refused, or put there some other way."""
    media_id = await _photo(db_session, seed.tenant_a.id, seed.a.user.id, SVG, "image/svg+xml")
    _login(client, front_desk)

    response = await client.get(f"/api/admin/media/{media_id}/file")

    assert response.headers["content-type"] == "application/octet-stream"
    assert "sandbox" in response.headers["content-security-policy"]


async def test_another_clinics_photo_is_not_found(
    client: httpx.AsyncClient, db_session: AsyncSession, seed: Seed, front_desk: Any
) -> None:
    theirs = await _photo(db_session, seed.tenant_b.id, seed.b.user.id, PNG, "image/png")
    csrf = _login(client, front_desk)

    assert (await client.get(f"/api/admin/media/{theirs}/file")).status_code == 404
    changed = await client.post(
        f"/api/admin/media/{theirs}/status",
        json={"status": "reviewed"},
        headers={CSRF_HEADER: csrf},
    )
    assert changed.status_code == 404


async def test_a_photo_needs_a_session(
    client: httpx.AsyncClient, db_session: AsyncSession, seed: Seed
) -> None:
    media_id = await _photo(db_session, seed.tenant_a.id, seed.a.user.id, PNG, "image/png")

    response = await client.get(f"/api/admin/media/{media_id}/file", follow_redirects=False)

    assert response.status_code in {303, 401}
    assert response.content != PNG


async def test_a_view_only_account_cannot_open_patient_photos(
    client: httpx.AsyncClient, db_session: AsyncSession, seed: Seed
) -> None:
    media_id = await _photo(db_session, seed.tenant_a.id, seed.a.user.id, PNG, "image/png")
    _login(client, seed.a.operator)

    assert (await client.get(f"/api/admin/media/{media_id}/file")).status_code == 403


@pytest.mark.parametrize(
    ("content_type", "accepted"),
    [
        ("image/jpeg", True),
        ("image/png; charset=binary", True),
        ("IMAGE/WEBP", True),
        ("image/svg+xml", False),
        ("text/html", False),
        ("application/octet-stream", False),
        ("", False),
    ],
)
async def test_only_raster_images_are_downloaded(content_type: str, accepted: bool) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=PNG, headers={"content-type": content_type})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        result = await patient_media.download("https://lookaside.fbsbx.com/x", http=http)

    assert (result is not None) is accepted


async def test_an_oversized_image_is_not_downloaded() -> None:
    big = b"\x00" * (patient_media.MAX_IMAGE_BYTES + 1)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=big, headers={"content-type": "image/jpeg"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        assert await patient_media.download("https://lookaside.fbsbx.com/x", http=http) is None


# --- "Hammasini ko'rildi" -----------------------------------------------------

T0 = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)


async def _photo_at(
    session: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID, minutes: int
) -> uuid.UUID:
    media = PatientMedia(
        tenant_id=tenant_id,
        user_id=user_id,
        source_url="https://lookaside.fbsbx.com/x",
        content=PNG,
        content_type="image/png",
        size_bytes=len(PNG),
        created_at=T0 + timedelta(minutes=minutes),
    )
    session.add(media)
    await session.flush()
    return media.id


async def _statuses(session: AsyncSession, *ids: uuid.UUID) -> list[str]:
    rows = (
        await session.execute(
            select(PatientMedia.id, PatientMedia.status).where(PatientMedia.id.in_(ids))
        )
    ).all()
    by_id = dict(rows)
    return [by_id[i] for i in ids]


async def _second_patient(session: AsyncSession, seed: Seed) -> uuid.UUID:
    user = User(
        tenant_id=seed.tenant_a.id, channel_id=seed.a.user.channel_id, external_id="ig-second"
    )
    session.add(user)
    await session.flush()
    return user.id


async def test_every_unseen_photo_on_the_screen_is_marked_seen_at_once(
    client: httpx.AsyncClient, db_session: AsyncSession, seed: Seed, front_desk: Any
) -> None:
    other = await _second_patient(db_session, seed)
    photos = [
        await _photo_at(db_session, seed.tenant_a.id, seed.a.user.id, 0),
        await _photo_at(db_session, seed.tenant_a.id, seed.a.user.id, 1),
        await _photo_at(db_session, seed.tenant_a.id, other, 2),
    ]
    csrf = _login(client, front_desk)

    screen = (await client.get("/api/admin/media", params={"only_unseen": True})).json()
    response = await client.post(
        "/api/admin/media/seen",
        json={"user_ids": [p["user_id"] for p in screen], "up_to": screen[0]["last_at"]},
        headers={CSRF_HEADER: csrf},
    )

    assert response.status_code == 200
    assert response.json() == {"updated": 3}
    assert await _statuses(db_session, *photos) == ["reviewed"] * 3
    assert (await client.get("/api/admin/media", params={"only_unseen": True})).json() == []


async def test_a_photo_that_arrives_meanwhile_stays_new(
    client: httpx.AsyncClient, db_session: AsyncSession, seed: Seed, front_desk: Any
) -> None:
    shown = await _photo_at(db_session, seed.tenant_a.id, seed.a.user.id, 0)
    csrf = _login(client, front_desk)
    screen = (await client.get("/api/admin/media")).json()
    later = await _photo_at(db_session, seed.tenant_a.id, seed.a.user.id, 5)

    await client.post(
        "/api/admin/media/seen",
        json={"user_ids": [screen[0]["user_id"]], "up_to": screen[0]["last_at"]},
        headers={CSRF_HEADER: csrf},
    )

    assert await _statuses(db_session, shown, later) == ["reviewed", "new"]


async def test_only_the_patients_on_the_screen_are_marked(
    client: httpx.AsyncClient, db_session: AsyncSession, seed: Seed, front_desk: Any
) -> None:
    """A search narrowed the screen to one patient; the other was not seen."""
    other = await _second_patient(db_session, seed)
    shown = await _photo_at(db_session, seed.tenant_a.id, seed.a.user.id, 0)
    hidden = await _photo_at(db_session, seed.tenant_a.id, other, 1)
    csrf = _login(client, front_desk)

    await client.post(
        "/api/admin/media/seen",
        json={"user_ids": [str(seed.a.user.id)], "up_to": (T0 + timedelta(hours=1)).isoformat()},
        headers={CSRF_HEADER: csrf},
    )

    assert await _statuses(db_session, shown, hidden) == ["reviewed", "new"]


async def test_another_clinics_photos_are_never_marked(
    client: httpx.AsyncClient, db_session: AsyncSession, seed: Seed, front_desk: Any
) -> None:
    theirs = await _photo_at(db_session, seed.tenant_b.id, seed.b.user.id, 0)
    csrf = _login(client, front_desk)

    response = await client.post(
        "/api/admin/media/seen",
        json={"user_ids": [str(seed.b.user.id)], "up_to": (T0 + timedelta(hours=1)).isoformat()},
        headers={CSRF_HEADER: csrf},
    )

    assert response.json() == {"updated": 0}
    assert await _statuses(db_session, theirs) == ["new"]


async def test_marking_all_seen_needs_the_csrf_token_and_patient_access(
    client: httpx.AsyncClient, db_session: AsyncSession, seed: Seed, front_desk: Any
) -> None:
    photo = await _photo_at(db_session, seed.tenant_a.id, seed.a.user.id, 0)
    body = {"user_ids": [str(seed.a.user.id)], "up_to": (T0 + timedelta(hours=1)).isoformat()}

    _login(client, front_desk)
    no_token = await client.post("/api/admin/media/seen", json=body)
    view_only_csrf = _login(client, seed.a.operator)
    view_only = await client.post(
        "/api/admin/media/seen", json=body, headers={CSRF_HEADER: view_only_csrf}
    )

    assert no_token.status_code == 403
    assert view_only.status_code == 403
    assert await _statuses(db_session, photo) == ["new"]


async def test_nothing_on_the_screen_marks_nothing(
    client: httpx.AsyncClient, seed: Seed, front_desk: Any
) -> None:
    csrf = _login(client, front_desk)

    response = await client.post(
        "/api/admin/media/seen",
        json={"user_ids": [], "up_to": T0.isoformat()},
        headers={CSRF_HEADER: csrf},
    )

    assert response.json() == {"updated": 0}
