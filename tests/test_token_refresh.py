"""The Instagram token renewal, against each answer Meta gives.

Stubbed transport rather than the database fixtures: what is worth pinning
here is which reply leads to a token being replaced and which leaves the old
one alone, and neither needs a real channel row to be true.
"""

import uuid
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.core.encryption import decrypt, encrypt
from app.services.token_refresh import refresh_instagram_tokens


class _Session:
    """Enough of AsyncSession for the job: it selects, then may commit."""

    def __init__(self, channels: list[Any]) -> None:
        self._channels = channels
        self.committed = False

    async def execute(self, _stmt: Any) -> Any:
        channels = self._channels
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: channels))

    async def commit(self) -> None:
        self.committed = True


def _channel(token: str) -> Any:
    return SimpleNamespace(id=uuid.uuid4(), credentials=encrypt(token))


async def _run(reply: httpx.Response, channels: list[Any]) -> tuple[Any, _Session]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: reply))
    session = _Session(channels)
    try:
        return await refresh_instagram_tokens(session, client=client), session  # type: ignore[arg-type]
    finally:
        await client.aclose()


async def test_a_renewed_token_replaces_the_old_one() -> None:
    channel = _channel("IGAAold")

    run, session = await _run(
        httpx.Response(200, json={"access_token": "IGAAnew", "expires_in": 5183944}),
        [channel],
    )

    assert run.refreshed == 1
    assert decrypt(channel.credentials) == "IGAAnew"
    assert session.committed


async def test_a_channel_awaiting_its_real_token_is_left_alone() -> None:
    """A placeholder is what ops seed a channel with before the real token
    lands. Sending it to Meta would earn a 400 and a log line saying a token
    failed to renew, which is not what happened.
    """
    run, _ = await _run(httpx.Response(500), [_channel("pending")])

    assert run.skipped == 1
    assert run.failed == 0


async def test_a_facebook_login_token_is_not_sent_to_the_instagram_host() -> None:
    """New apps get Facebook Login tokens, which graph.instagram.com cannot
    parse: sending one would log a failed renewal every day for a token that
    is fine."""
    run, session = await _run(httpx.Response(400), [_channel("EAAZCTiJ5BuVkB")])

    assert run.skipped == 1
    assert run.failed == 0
    assert session.committed is False


@pytest.mark.parametrize(
    "reply",
    [
        # Meta's two refusals: younger than 24 hours, and already expired.
        httpx.Response(400, json={"error": {"message": "expired"}}),
        # A 200 that carries no token: nothing to store, and silently writing
        # an empty credential would take the channel down for good.
        httpx.Response(200, json={}),
    ],
)
async def test_a_refusal_leaves_the_working_token_in_place(reply: httpx.Response) -> None:
    channel = _channel("IGAAold")

    run, session = await _run(reply, [channel])

    assert run.failed == 1
    assert decrypt(channel.credentials) == "IGAAold"
    assert not session.committed


async def test_a_clinic_with_no_instagram_channel_is_a_quiet_no_op() -> None:
    run, session = await _run(httpx.Response(200, json={}), [])

    assert not run.touched
    assert not session.committed
