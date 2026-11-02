"""The contract every messaging platform is reached through.

This package is the one place in the codebase allowed to know that
"Instagram" (or, once the Telegram bot merges in, "Telegram") exists as a
concrete thing. Everything under app/services and app/rag is written against
this interface instead, so adding a platform means adding an adapter here,
not touching the answer pipeline, the debounce logic, or the conversation
store.

The split mirrors the one already used for LLMProvider/EmbeddingProvider
(app/rag/llm.py, app/rag/embeddings.py): an ABC for the capability, concrete
implementations behind it, and a lookup that callers use so they never name
an implementation directly.
"""

from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar

# The reply_context key under which app.services.delivery passes the
# channel's own platform id (Channel.external_id) to every adapter.
CHANNEL_EXTERNAL_ID = "channel_external_id"


class ChannelType(StrEnum):
    """The values Channel.type may hold.

    Named here rather than as bare string literals scattered across the
    codebase because this is the join between a database column, an adapter
    registration, and a Redis key namespace — three places that must agree,
    and previously agreed only by everyone remembering to type "instagram".
    """

    INSTAGRAM = "instagram"
    # Not served by an adapter in this repository yet — the Telegram bot is
    # being prepared on its own branch. Declared because Channel.type is
    # already documented as carrying it and app.services code is written to
    # dispatch on it, so the value is part of this contract today even
    # though the implementation lands with the merge.
    TELEGRAM = "telegram"


class DeliveryBlocked(StrEnum):
    """Why a platform will not accept a send right now.

    A blocked delivery is an expected, recoverable state, not an error: the
    reply is dropped with a log line rather than raising, because retrying
    would not help until the underlying condition changes (a real token gets
    configured, or the patient writes again and reopens the window).
    """

    # The channel row carries a placeholder instead of a real credential.
    NOT_CONFIGURED = "not_configured"
    # Outside the platform's window for unsolicited replies (Meta's 24-hour
    # messaging window, and Telegram's equivalent restrictions).
    OUTSIDE_MESSAGING_WINDOW = "outside_messaging_window"


class UnknownChannelTypeError(LookupError):
    """Raised when no adapter is registered for a channel type."""


class ChannelAdapter(ABC):
    """Send-side of one messaging platform.

    Deliberately narrow: an adapter knows how to hand text to a platform and
    what that platform's own policy forbids. It knows nothing about
    retrieval, tenants, or the database — those are shared
    business logic and live in app/services, where both bots reach them.
    """

    channel_type: ClassVar[ChannelType]

    @abstractmethod
    async def send_text(
        self,
        *,
        credentials: str,
        recipient_external_id: str,
        text: str,
        reply_context: Mapping[str, Any] | None = None,
    ) -> None:
        """Deliver `text` to recipient_external_id on this platform.

        `reply_context` always carries CHANNEL_EXTERNAL_ID, the channel's own
        id on its platform (app.services.delivery puts it there), for a
        platform that has to name the sender as well as the recipient.

        `credentials` is the decrypted value from Channel.credentials, and
        `recipient_external_id` the platform's own id for the patient (an
        Instagram-scoped user id, a Telegram chat id). Raises on any
        transport or API failure, so the caller's job fails and is retried.

        `reply_context` is whatever the inbound edge captured that this
        platform needs in order to answer in the right place, carried
        through the queue untouched by everything in between. It exists
        because some platforms route a reply by more than the recipient's
        id: a Telegram conversation reached through Telegram Business must
        be answered over that same business connection, or the reply goes
        out from the bot account instead of the clinic's own. Opaque to the
        shared services, and ignored by adapters that need nothing beyond
        the recipient.
        """

    def delivery_block_reason(
        self, *, credentials: str, last_user_message_at: datetime
    ) -> DeliveryBlocked | None:
        """Whether this platform's own rules currently forbid a send, or None
        if it may proceed.

        Checked before send_text so an expected refusal costs no API call.
        Default is "nothing blocks it" — a platform with no messaging window
        and no placeholder convention needs no override.
        """
        return None


_ADAPTERS: dict[ChannelType, ChannelAdapter] = {}


def register_adapter(adapter: ChannelAdapter) -> None:
    """Make `adapter` the implementation for its channel_type.

    Re-registration overwrites rather than raising, so a test can install a
    fake for one channel type and put the real one back afterwards.
    """
    _ADAPTERS[adapter.channel_type] = adapter


def get_adapter(channel_type: str) -> ChannelAdapter:
    """The adapter serving `channel_type`, as stored in Channel.type.

    Raises rather than returning None: a channel row whose type nothing can
    deliver on is a configuration bug, and silently dropping its replies is
    exactly the failure mode that would be hardest to notice.
    """
    try:
        adapter = _ADAPTERS[ChannelType(channel_type)]
    except (KeyError, ValueError) as exc:
        registered = ", ".join(sorted(_ADAPTERS)) or "none"
        raise UnknownChannelTypeError(
            f"No channel adapter registered for type {channel_type!r} (registered: {registered})"
        ) from exc
    return adapter


def registered_channel_types() -> frozenset[ChannelType]:
    return frozenset(_ADAPTERS)
