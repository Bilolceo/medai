from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

# Imported for the side effect of registering the built-in channel adapters
# (see app.channels), which app.services.delivery looks up by channel type.
from app import channels  # noqa: F401  - importing it registers the adapters
from app.api import (
    admin_router,
    auth_router,
    dashboard_router,
    telegram_webhook_router,
    webapp_router,
    webhook_router,
)
from app.api.auth import NotAuthenticatedError
from app.core.config import get_settings
from app.core.doctor_seeding import seed_doctors_if_configured
from app.core.faq_seeding import seed_faqs_if_configured
from app.core.logging import configure_logging
from app.core.provisioning import provision_if_configured

configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    # Web only: the worker boots from the same image but never imports this
    # module, so the two processes cannot race to insert the same channel.
    settings = get_settings()
    await provision_if_configured(settings)
    # After provisioning, not before: seeding resolves its tenant through the
    # channel row that provisioning is the thing responsible for creating.
    await seed_faqs_if_configured(settings)
    # The roster decides how many bookings a slot holds, so it wants to be
    # in place before the first webhook is answered, not after it.
    await seed_doctors_if_configured(settings)
    yield


_settings = get_settings()

app = FastAPI(
    title="Clinic Assistant",
    lifespan=lifespan,
    # None removes the route entirely rather than hiding it, so there is
    # nothing to find at the usual address.
    docs_url="/docs" if _settings.api_docs_enabled else None,
    redoc_url="/redoc" if _settings.api_docs_enabled else None,
    openapi_url="/openapi.json" if _settings.api_docs_enabled else None,
)


# Sent on every response, including the dashboard's own HTML and the error
# pages, which is why it is middleware rather than a per-route dependency.
_SECURITY_HEADERS = {
    # The dashboard shows patient names and phone numbers. Without this it
    # can be loaded invisibly inside another site and clicked through.
    "X-Frame-Options": "DENY",
    # Stops a browser deciding for itself that a JSON response is really
    # HTML and running it.
    "X-Content-Type-Options": "nosniff",
    # A referrer carrying a conversation id has no business on another site.
    "Referrer-Policy": "same-origin",
    # The dashboard loads nothing from anywhere else. Saying so means an
    # injected <script src> has nowhere to load from.
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
        # Instagram's own player, for a reel a patient shared: it arrives as
        # a link to the reel, never as a video file.
        "frame-src https://www.instagram.com; "
        "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    ),
}


@app.middleware("http")
async def _security_headers(request: Request, call_next: Any) -> Response:
    response: Response = await call_next(request)
    for header, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    # Only over HTTPS: sent on a plain-HTTP response it is ignored, and in
    # local development it would pin localhost to HTTPS for months.
    #
    # X-Forwarded-Proto before request.url.scheme, because behind a platform
    # router the app is spoken to over plain HTTP and sees "http" even though
    # the patient's browser is on TLS -- which is how the first version of
    # this sent no HSTS at all in the one place it mattered.
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    if (forwarded or request.url.scheme) == "https":
        response.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return response


app.include_router(webhook_router)
app.include_router(telegram_webhook_router)
app.include_router(auth_router)
app.include_router(dashboard_router)
app.include_router(admin_router)
app.include_router(webapp_router)

# The operator dashboard, served from the same origin as the API it calls —
# which is what lets it authenticate with the session cookie instead of
# carrying a token in JavaScript where any script on the page could read it.
# Mounted after the routers so it cannot shadow one.
_ADMIN_UI = Path("app/static/admin")
if _ADMIN_UI.is_dir():
    app.mount("/admin", StaticFiles(directory=_ADMIN_UI, html=True), name="admin")


@app.exception_handler(NotAuthenticatedError)
async def _redirect_to_login(request: Request, exc: NotAuthenticatedError) -> RedirectResponse:
    return RedirectResponse(url="/login", status_code=303)


@app.get("/", include_in_schema=False)
async def root_to_dashboard() -> RedirectResponse:
    """The front door. Everything this deployment serves a person lives under
    a path -- /admin/, /login -- and nothing was mounted at "/", so opening
    the bare domain answered {"detail":"Not Found"}. That is the link the
    host's own dashboard shows and the one a bookmark keeps, and a staff
    member who followed it got a line of JSON under the platform's error
    headers, which reads as the site being down rather than as a missing
    route. Same reasoning as /dashboard in app.api.dashboard: the operator
    dashboard is where a person opening this domain means to go.

    Temporary (302) rather than permanent, so that putting a landing page or
    a patient-facing route here later does not have to outlive a redirect
    cached in every operator's browser.
    """
    return RedirectResponse(url="/admin/", status_code=status.HTTP_302_FOUND)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


# Relative to the working directory, matching app.api.dashboard's
# Jinja2Templates(directory="app/templates"): the container runs from the
# repo root, where the Dockerfile copies both app/ and docs/.
_PRIVACY_POLICY_PAGE = Path("docs/index.html")


@app.get("/privacy")
async def privacy_policy() -> FileResponse:
    """The service's public privacy policy.

    Meta will not publish an app without a reachable privacy-policy URL,
    and an unpublished app is exactly why Instagram delivers no webhooks
    to this deployment. Served from the API itself rather than GitHub
    Pages so the URL sits on the same origin as the webhook Meta already
    calls, and needs no second thing to keep alive.
    """
    return FileResponse(_PRIVACY_POLICY_PAGE, media_type="text/html")
