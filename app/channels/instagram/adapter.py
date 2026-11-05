"""Instagram's implementation of the ChannelAdapter contract.

Holds the two rules that are Meta's, not ours: a channel whose credential is
still a placeholder cannot send at all, and a standard text send is only
allowed inside the 24-hour messaging window. Both used to live in
app.workers.tasks, where they were reached only by the Instagram path and
would have had to be duplicated (with different constants) for Telegram.
"""

from collections.abc import Mapping
from datetime import datetime
from functools import lru_cache
from typing import Any

from app.channels.base import CHANNEL_EXTERNAL_ID, ChannelAdapter, ChannelType, DeliveryBlocked
from app.channels.instagram.client import (
    InstagramClient,
    get_instagram_client,
    is_placeholder_credential,
    is_within_messaging_window,
)


class InstagramAdapter(ChannelAdapter):
    channel_type = ChannelType.INSTAGRAM

    def __init__(self, client: InstagramClient | None = None) -> None:
        # Resolved lazily rather than at construction: the module-level
        # registration below runs at import time, and building the real
        # httpx client there would make importing app.channels open a
        # connection pool in every process that merely imports the package.
        self._client = client

    def _resolve_client(self) -> InstagramClient:
        return self._client or get_instagram_client()

    async def send_text(
        self,
        *,
        credentials: str,
        recipient_external_id: str,
        text: str,
        # Of the context only the channel's own id is read: a Facebook Login
        # token has to name the account it sends from (see
        # app.channels.instagram.client.GRAPH_FACEBOOK_BASE_URL).
        reply_context: Mapping[str, Any] | None = None,
    ) -> None:
        sender = (reply_context or {}).get(CHANNEL_EXTERNAL_ID)
        await self._resolve_client().send_text(
            access_token=credentials,
            recipient_igsid=recipient_external_id,
            text=text,
            sender_igsid=str(sender) if sender else None,
        )

    async def send_image(
        self,
        *,
        credentials: str,
        recipient_external_id: str,
        image_url: str,
        reply_context: Mapping[str, Any] | None = None,
    ) -> bool:
        sender = (reply_context or {}).get(CHANNEL_EXTERNAL_ID)
        await self._resolve_client().send_image(
            access_token=credentials,
            recipient_igsid=recipient_external_id,
            image_url=image_url,
            sender_igsid=str(sender) if sender else None,
        )
        return True

    def delivery_block_reason(
        self, *, credentials: str, last_user_message_at: datetime
    ) -> DeliveryBlocked | None:
        if is_placeholder_credential(credentials):
            return DeliveryBlocked.NOT_CONFIGURED
        if not is_within_messaging_window(last_user_message_at):
            return DeliveryBlocked.OUTSIDE_MESSAGING_WINDOW
        return None


@lru_cache
def get_instagram_adapter() -> InstagramAdapter:
    return InstagramAdapter()
