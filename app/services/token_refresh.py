"""Keeping Instagram channel tokens alive.

An Instagram Login token lasts sixty days. Nothing renewed them, so every
deployment carried a date, unmarked in any calendar, on which the clinic's
Instagram would stop answering -- with no error to read, because a channel
with a dead token fails the same way as one nobody has messaged.

Meta's renewal takes the current token and returns a new one good for another
sixty days, which makes this a maintenance job rather than a migration: run it
often enough and the expiry never arrives. Daily is far more often than
needed, and that is the point -- sixty days of missed runs are survivable,
where a weekly job that breaks in December is not noticed until February.

Two of Meta's conditions shape the code below. A token must be at least
twenty-four hours old to be renewable, so a channel configured this morning is
skipped rather than treated as broken. And an already-expired token cannot be
renewed at all: past that point somebody has to reconnect the account by hand,
which is why that case is logged at error rather than counted as a failure to
retry.
"""

import logging
from collections.abc import Sequence

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import ChannelType
from app.channels.instagram.client import (
    GRAPH_API_BASE_URL,
    is_facebook_token,
    is_placeholder_credential,
)
from app.core.encryption import decrypt, encrypt
from app.models.channel import Channel

logger = logging.getLogger(__name__)

# Not under the versioned path: the refresh endpoint lives at the host root.
_REFRESH_URL = GRAPH_API_BASE_URL.rsplit("/", 1)[0] + "/refresh_access_token"

_TIMEOUT_SECONDS = 20.0


class TokenRefreshRun:
    """What one pass did, for the caller to log."""

    def __init__(self) -> None:
        self.refreshed = 0
        self.skipped = 0
        self.failed = 0

    @property
    def touched(self) -> bool:
        return bool(self.refreshed or self.skipped or self.failed)


async def _instagram_channels(session: AsyncSession) -> Sequence[Channel]:
    """Every Instagram channel, across every tenant.

    Deliberately unscoped: this is a cron job with no request and no operator
    to take a tenant from, and a clinic whose token dies is not helped by the
    job having been careful about whose token it was.
    """
    stmt = select(Channel).where(Channel.type == ChannelType.INSTAGRAM)
    return (await session.execute(stmt)).scalars().all()


async def refresh_instagram_tokens(
    session: AsyncSession, *, client: httpx.AsyncClient | None = None
) -> TokenRefreshRun:
    """Renew every Instagram token that can be renewed."""
    run = TokenRefreshRun()
    channels = await _instagram_channels(session)
    if not channels:
        return run

    owned = client is None
    http = client or httpx.AsyncClient(timeout=_TIMEOUT_SECONDS)
    try:
        for channel in channels:
            token = decrypt(channel.credentials)
            if is_placeholder_credential(token):
                run.skipped += 1
                continue
            if is_facebook_token(token):
                # A Facebook Login token is not renewed at this endpoint: the
                # Instagram host does not even parse it. Its long-lived form is
                # obtained once, by exchange, when the account is connected.
                run.skipped += 1
                logger.info("instagram_token_refresh_skipped_facebook channel=%s", channel.id)
                continue
            try:
                response = await http.get(
                    _REFRESH_URL,
                    params={"grant_type": "ig_refresh_token", "access_token": token},
                )
            except httpx.HTTPError as exc:
                run.failed += 1
                logger.warning(
                    "instagram_token_refresh_unreachable channel=%s error=%s", channel.id, exc
                )
                continue

            if response.status_code != 200:
                # 400 here is usually "not yet 24 hours old" or "already
                # expired". The two need different humans, so the body goes
                # into the log rather than a tidy summary.
                run.failed += 1
                logger.error(
                    "instagram_token_refresh_refused channel=%s status=%s body=%s",
                    channel.id,
                    response.status_code,
                    response.text[:300],
                )
                continue

            body = response.json()
            fresh = body.get("access_token")
            if not fresh:
                run.failed += 1
                logger.error("instagram_token_refresh_empty channel=%s body=%s", channel.id, body)
                continue

            channel.credentials = encrypt(fresh)
            run.refreshed += 1
            logger.info(
                "instagram_token_refreshed channel=%s expires_in=%s",
                channel.id,
                body.get("expires_in"),
            )
        if run.refreshed:
            await session.commit()
    finally:
        if owned:
            await http.aclose()
    return run
