"""The platform-adapter boundary: registration, lookup, and dispatch.

These are the seams the Telegram bot plugs into at merge time, so they are
tested for the contract rather than for Instagram's behaviour specifically —
that lives in test_instagram_client.py and the worker tests.
"""

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import app.channels  # noqa: F401  - registers the built-in adapters
from app.channels.base import (
    CHANNEL_EXTERNAL_ID,
    ChannelAdapter,
    ChannelType,
    DeliveryBlocked,
    UnknownChannelTypeError,
    get_adapter,
    register_adapter,
)
from app.channels.instagram.adapter import InstagramAdapter
from app.core.encryption import encrypt
from app.repositories.channel import ChannelRepository
from app.services.delivery import send_reply
from tests.conftest import Seed


class RecordingAdapter(ChannelAdapter):
    """A stand-in platform, used to prove the dispatch is by channel type
    and not hardcoded to either real one.

    Registered as Telegram in the tests below because the registry is keyed
    by ChannelType and there is no third member to borrow; each of those
    tests copies the registry first so the real adapter is put back.
    """

    channel_type = ChannelType.TELEGRAM

    def __init__(self, blocked: DeliveryBlocked | None = None) -> None:
        self.calls: list[tuple[str, str, str, Any]] = []
        self._blocked = blocked

    async def send_text(
        self,
        *,
        credentials: str,
        recipient_external_id: str,
        text: str,
        reply_context: Mapping[str, Any] | None = None,
    ) -> None:
        self.calls.append((credentials, recipient_external_id, text, reply_context))

    def delivery_block_reason(
        self, *, credentials: str, last_user_message_at: datetime
    ) -> DeliveryBlocked | None:
        return self._blocked


# --- registry ---


def test_instagram_is_registered_by_importing_the_package() -> None:
    assert isinstance(get_adapter("instagram"), InstagramAdapter)


def test_unknown_channel_type_raises_rather_than_dropping_the_reply() -> None:
    """A channel row nothing can deliver on is a configuration bug. Silently
    dropping its replies is the failure that would be hardest to notice.
    """
    with pytest.raises(UnknownChannelTypeError):
        get_adapter("carrier-pigeon")


def test_every_declared_channel_type_has_an_adapter() -> None:
    """ChannelType is the contract between a database column, an adapter
    registration and a Redis key namespace. A member with no adapter would
    be a channel row whose replies silently go nowhere.
    """
    for channel_type in ChannelType:
        assert get_adapter(channel_type) is not None


def test_registering_an_adapter_makes_it_findable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.channels.base as base

    monkeypatch.setattr(base, "_ADAPTERS", dict(base._ADAPTERS))
    adapter = RecordingAdapter()
    register_adapter(adapter)

    assert get_adapter("telegram") is adapter


# --- the default block reason ---


def test_an_adapter_with_no_platform_restrictions_never_blocks() -> None:
    """The base class default is "nothing blocks it", so a platform without
    a messaging window needs no override to work.
    """
    assert (
        RecordingAdapter().delivery_block_reason(
            credentials="token", last_user_message_at=datetime.now(UTC) - timedelta(days=365)
        )
        is None
    )


# --- delivery dispatches on the channel's own type ---


async def test_send_reply_dispatches_on_the_channels_type(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.channels.base as base

    monkeypatch.setattr(base, "_ADAPTERS", dict(base._ADAPTERS))
    adapter = RecordingAdapter()
    register_adapter(adapter)

    with as_tenant(seed.tenant_a.id):
        channel = await ChannelRepository(db_session).create(
            type="telegram",
            credentials=encrypt("tg-bot-token"),
            external_id=f"tg-{seed.tenant_a.id}",
        )

        delivered_over = await send_reply(
            db_session,
            channel_id=channel.id,
            recipient_external_id="chat-42",
            text="Assalom alaykum",
            last_user_message_at=datetime.now(UTC),
        )

    assert delivered_over == "telegram"
    # The channel's own id rides along for adapters that must name the sender.
    assert adapter.calls == [
        ("tg-bot-token", "chat-42", "Assalom alaykum", {CHANNEL_EXTERNAL_ID: channel.external_id})
    ]


async def test_send_reply_sends_each_part_of_a_broken_reply_as_its_own_message(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The introduction and the question arrive as two bubbles, in order,
    the way an administrator would send them (app.services.bubbles)."""
    import app.channels.base as base
    import app.services.delivery as delivery

    monkeypatch.setattr(base, "_ADAPTERS", dict(base._ADAPTERS))
    monkeypatch.setattr(delivery, "PAUSE_BETWEEN_MESSAGES_SECONDS", 0)
    adapter = RecordingAdapter()
    register_adapter(adapter)

    with as_tenant(seed.tenant_a.id):
        channel = await ChannelRepository(db_session).create(
            type="telegram",
            credentials=encrypt("tg-bot-token"),
            external_id=f"tg-{seed.tenant_a.id}",
        )
        await send_reply(
            db_session,
            channel_id=channel.id,
            recipient_external_id="chat-42",
            text="Men administratorman, ismim Dildora. ||| Sizga qanday yordam bera olamiz?",
            last_user_message_at=datetime.now(UTC),
        )

    assert [call[2] for call in adapter.calls] == [
        "Men administratorman, ismim Dildora.",
        "Sizga qanday yordam bera olamiz?",
    ]


async def test_send_reply_returns_none_when_the_platform_blocks_it(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    adapter = RecordingAdapter(blocked=DeliveryBlocked.OUTSIDE_MESSAGING_WINDOW)

    with as_tenant(seed.tenant_a.id):
        delivered_over = await send_reply(
            db_session,
            channel_id=seed.a.channel.id,
            recipient_external_id="sender-1",
            text="too late",
            last_user_message_at=datetime.now(UTC),
            adapter=adapter,
        )

    assert delivered_over is None
    assert adapter.calls == []


async def test_send_reply_will_not_use_another_tenants_channel(
    db_session: AsyncSession,
    seed: Seed,
    as_tenant: Callable[[UUID], AbstractContextManager[None]],
) -> None:
    """The channel is loaded through the tenant-scoped repository, so a
    channel id belonging to another clinic reads as missing rather than
    lending out that clinic's token.
    """
    adapter = RecordingAdapter()

    with as_tenant(seed.tenant_a.id):
        delivered_over = await send_reply(
            db_session,
            channel_id=seed.b.channel.id,
            recipient_external_id="sender-1",
            text="cross-tenant",
            last_user_message_at=datetime.now(UTC),
            adapter=adapter,
        )

    assert delivered_over is None
    assert adapter.calls == []
