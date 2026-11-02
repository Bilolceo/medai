"""Sending a reply back out over the channel the patient wrote to.

Platform-neutral by construction: it loads the channel row, decrypts that
channel's credential, and hands the text to whichever adapter serves that
channel's type (see app.channels). Adding Telegram adds an adapter, not a
second copy of this.

The channel is looked up by id — the id resolved from the account the
inbound event named. It used to be chosen by listing the tenant's active
Instagram channels and taking whichever the database returned first, which
is correct only for a tenant with exactly one channel: a clinic with two
Instagram accounts would have replies to one account sent with the other
account's token, so they either fail authentication or go out from the
wrong account. The webhook always knew which account was written to; that
identity is now carried through the queue instead of being discarded.
"""

import logging
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import CHANNEL_EXTERNAL_ID, ChannelAdapter, get_adapter
from app.core.encryption import decrypt
from app.repositories.channel import ChannelRepository

logger = logging.getLogger(__name__)


async def send_reply(
    session: AsyncSession,
    *,
    channel_id: uuid.UUID,
    recipient_external_id: str,
    text: str,
    last_user_message_at: datetime,
    reply_context: Mapping[str, Any] | None = None,
    adapter: ChannelAdapter | None = None,
) -> str | None:
    """Deliver `text` to the patient.

    Returns the channel type it went out over, which the caller stamps on
    the outbound transcript row, or None when nothing was sent.

    Returns None rather than raising for the states that are expected and
    recoverable — the channel is gone or deactivated, its credential is
    still a placeholder, or the platform's reply window has closed. Retrying
    those would not help until something outside this job changes, and
    failing the job would bury a real failure among them.

    A DecryptionError (wrong or rotated ENCRYPTION_KEY, corrupted row) is
    deliberately NOT one of those: it is a misconfiguration, not "no token
    yet", so it propagates and fails the job loudly. So does any transport
    failure from the adapter — the job should be retried in that case.

    `reply_context` is passed to the adapter untouched. Nothing here reads
    it: what a platform needs in order to answer in the right place is the
    adapter's business, and inspecting it would put platform knowledge back
    into the shared layer this module exists to keep free of it.
    """
    channel = await ChannelRepository(session).get(channel_id)
    if channel is None or not channel.is_active:
        logger.warning(
            "reply_skipped_channel_unavailable",
            extra={"channel_id": str(channel_id), "recipient": recipient_external_id},
        )
        return None

    # channel.credentials is always ciphertext at rest (app.core.encryption)
    # — even a "pending" placeholder is stored encrypted, so there is no
    # special-cased plaintext path to accidentally leave un-encrypted.
    credentials = decrypt(channel.credentials)
    resolved_adapter = adapter or get_adapter(channel.type)

    blocked = resolved_adapter.delivery_block_reason(
        credentials=credentials, last_user_message_at=last_user_message_at
    )
    if blocked is not None:
        logger.warning(
            "reply_skipped",
            extra={
                "reason": blocked.value,
                "channel_id": str(channel.id),
                "channel_type": channel.type,
                "recipient": recipient_external_id,
                "last_user_message_at": last_user_message_at.isoformat(),
            },
        )
        return None

    await resolved_adapter.send_text(
        credentials=credentials,
        recipient_external_id=recipient_external_id,
        text=text,
        # The channel's own platform id rides along with whatever the caller
        # gave: a platform that must name the sender finds it here.
        reply_context={**(reply_context or {}), CHANNEL_EXTERNAL_ID: channel.external_id},
    )
    logger.info(
        "reply_sent",
        extra={
            "channel_id": str(channel.id),
            "channel_type": channel.type,
            "recipient": recipient_external_id,
            "reply_length": len(text),
        },
    )
    return channel.type
