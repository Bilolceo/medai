import secrets
from collections.abc import AsyncIterator

from arq.connections import ArqRedis
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.core.db import get_db_session
from app.core.queue import get_arq_pool
from app.core.session import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    Session,
    create_session_cookie,
    password_key,
    read_session_cookie,
)
from app.core.tenant_context import reset_current_tenant, set_current_tenant
from app.models.operator import Operator
from app.services.auth import authenticate_operator
from app.services.login_rate_limit import (
    is_login_rate_limited,
    record_failed_login,
    reset_login_attempts,
)

templates = Jinja2Templates(directory="app/templates")

router = APIRouter()


class NotAuthenticatedError(Exception):
    """Raised by get_current_session/get_current_operator when there's no
    valid session — translated to a redirect-to-/login by the exception
    handler registered in app.main, rather than the default 500/401 JSON
    response FastAPI would otherwise give a browser here.
    """


async def get_current_session(request: Request) -> Session:
    cookie_value = request.cookies.get(SESSION_COOKIE_NAME)
    session = read_session_cookie(cookie_value) if cookie_value else None
    if session is None:
        raise NotAuthenticatedError()
    return session


async def get_current_operator(
    session: Session = Depends(get_current_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> AsyncIterator[Operator]:
    """Loads the Operator fresh from the DB on every request (rather than
    trusting a cached name/role/tenant_id inside the cookie) so a deleted or
    reassigned account stops working the moment the row changes, not just
    at next login. Also the single place that establishes tenant context
    for every dashboard request — set for the lifetime of the request, then
    always reset, even if the route raises.
    """
    operator = await db_session.get(Operator, session.operator_id)
    if operator is None or session.password_key != password_key(operator.password_hash):
        # Gone, or the password has changed since this session was opened.
        raise NotAuthenticatedError()
    tenant_token = set_current_tenant(operator.tenant_id)
    try:
        yield operator
    finally:
        reset_current_tenant(tenant_token)


async def verify_csrf(request: Request, session: Session = Depends(get_current_session)) -> None:
    """Double-submit CSRF check: the hidden csrf_token form field must match
    the token bound into this operator's own signed session cookie. A
    cross-site form post can't have read that cookie, so it can't supply a
    matching value.
    """
    form = await request.form()
    submitted = form.get("csrf_token")
    if not isinstance(submitted, str) or not secrets.compare_digest(submitted, session.csrf_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token")


@router.get("/login", response_class=HTMLResponse)
async def login_form(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login", response_model=None)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db_session: AsyncSession = Depends(get_db_session),
    pool: ArqRedis = Depends(get_arq_pool),
    settings: Settings = Depends(get_settings),
) -> HTMLResponse | RedirectResponse:
    if await is_login_rate_limited(pool, username):
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Too many failed attempts. Try again in a few minutes."},
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        )

    operator = await authenticate_operator(db_session, username, password)
    if operator is None:
        # Recorded against the submitted username even if it doesn't exist —
        # see login_rate_limit's module docstring for why that's deliberate.
        await record_failed_login(pool, username)
        return templates.TemplateResponse(
            request,
            "login.html",
            {"error": "Invalid username or password"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    await reset_login_attempts(pool, username)
    cookie_value, _csrf_token = create_session_cookie(operator.id, operator.password_hash)
    # /admin/, not /dashboard/appointments: the operator dashboard is the one
    # served from app/static/admin, with conversations, leads, doctors, the
    # knowledge base and clinic settings on it. /dashboard predates the merge
    # and is a single server-rendered day of appointments -- landing there
    # made the whole product look like a booking form with nothing behind it.
    response = RedirectResponse(url="/admin/", status_code=status.HTTP_303_SEE_OTHER)
    response.set_cookie(
        SESSION_COOKIE_NAME,
        cookie_value,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
    )
    return response


@router.post("/logout")
async def logout() -> RedirectResponse:
    response = RedirectResponse(url="/login", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response
