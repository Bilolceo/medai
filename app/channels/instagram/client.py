import logging
from abc import ABC, abstractmethod
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Meta's Send API only accepts a standard text send within 24h of the user's
# last message (the "24-hour messaging window"). Outside it, Meta rejects
# the send unless it carries a message tag (HUMAN_AGENT,
# CONFIRMED_EVENT_UPDATE, ...) or goes through a paid conversation — neither
# implemented yet, see is_within_messaging_window's caller in
# app.workers.tasks for the TODO.
MESSAGING_WINDOW = timedelta(hours=24)

# Pinned to a concrete version, not an alias, for the same reproducibility
# reason OPENAI_MODEL names a concrete model (see app/core/config.py) — an
# alias like "latest" can change Graph API behavior under us without any
# change on our side.
GRAPH_API_VERSION = "v21.0"
# graph.instagram.com, not graph.facebook.com. The channel credentials are
# Instagram Login tokens (they start with "IGAA"), which the Facebook host
# refuses outright -- it answers "Cannot parse access token" rather than a
# permission error, so a misrouted send fails in a way that reads like a
# corrupt token instead of a wrong host. Switching to a Facebook Page token
# (an "EAA" token, issued through Facebook Login) would mean moving this
# back and re-subscribing the account; the two hosts are not
# interchangeable per-request.
GRAPH_API_BASE_URL = f"https://graph.instagram.com/{GRAPH_API_VERSION}"
# ...except that Meta no longer issues Instagram Login tokens to new apps:
# an app made after mid-2026 gets "Facebook Login for Business" and an "EAA"
# token, which only graph.facebook.com accepts. The two hosts speak the same
# Instagram API, with one difference that matters here: on the Facebook host
# "/me" is the Facebook user, not the Instagram account, so a send has to
# name the account by its id ("/<IG_ID>/messages"). The token's prefix says
# which host it belongs to, so each call goes to the right one and a
# deployment with an older token sees no change.
GRAPH_FACEBOOK_BASE_URL = f"https://graph.facebook.com/{GRAPH_API_VERSION}"

# channel.credentials is NOT NULL, so a not-yet-configured channel can't be
# stored as a real NULL — these sentinel strings are what ops seed a channel
# with before the real Instagram token lands (still blocked as of writing),
# so the pipeline can tell "no real token" apart from "garbage token" and
# skip sending instead of hammering Meta with 401s.
_PLACEHOLDER_CREDENTIALS = frozenset({"", "placeholder", "todo", "changeme", "pending"})


def is_placeholder_credential(credentials: str) -> bool:
    return credentials.strip().lower() in _PLACEHOLDER_CREDENTIALS


def is_facebook_token(access_token: str) -> bool:
    """Whether this token was issued by Facebook Login (and so belongs to
    graph.facebook.com) rather than Instagram Login."""
    return access_token.startswith("EAA")


def is_within_messaging_window(
    last_user_message_at: datetime, *, now: datetime | None = None
) -> bool:
    """Whether a standard text send is still allowed under Meta's 24-hour
    messaging window. Pure function so the boundary is unit-testable without
    a real clock or a real client.
    """
    current = now or datetime.now(UTC)
    return current - last_user_message_at <= MESSAGING_WINDOW


class InstagramSendError(Exception):
    """Raised when the Instagram Graph API rejects or fails a send request."""


# What is asked of a post a comment was left under. media_url is the video
# file (or the image) and expires; thumbnail_url is the cover.
MEDIA_FIELDS = (
    "id,media_type,media_product_type,caption,permalink,thumbnail_url,media_url,timestamp"
)


class InstagramClient(ABC):
    """Abstraction over "send a text message to an Instagram user via the
    Send API", mirroring LLMProvider/EmbeddingProvider (app/rag/llm.py,
    app/rag/embeddings.py) so the transport can be swapped for a test double
    without touching callers.

    Deliberately dumb: this only knows how to make the HTTP call. The
    24-hour messaging window and token-configured checks are business rules,
    not transport concerns, so they live in the caller (app.workers.tasks),
    the same way EmbeddingProvider doesn't know about retrieval thresholds.
    """

    @abstractmethod
    async def send_text(
        self,
        *,
        access_token: str,
        recipient_igsid: str,
        text: str,
        sender_igsid: str | None = None,
    ) -> None:
        """Send a text message to recipient_igsid, authenticated with access_token.

        Raises InstagramSendError on any non-2xx response.
        """

    @abstractmethod
    async def reply_to_comment(self, *, access_token: str, comment_id: str, text: str) -> None:
        """Reply under a comment, publicly.

        Raises InstagramSendError on any non-2xx response.
        """

    @abstractmethod
    async def send_private_reply(
        self,
        *,
        access_token: str,
        comment_id: str,
        text: str,
        sender_igsid: str | None = None,
    ) -> None:
        """Answer a comment in the writer's Direct inbox.

        Instagram allows exactly one of these per comment, and only within
        seven days of it -- which is why it carries the real answer while the
        public reply says only that the answer is waiting in Direct.
        """

    async def fetch_media_caption(self, *, access_token: str, media_id: str) -> str | None:
        """The caption of the post a comment was left under, or None."""
        return None

    async def fetch_media(self, *, access_token: str, media_id: str) -> dict[str, Any] | None:
        """The post a comment was left under: caption, type, cover, link,
        time, and for a video the link to its file. None when it cannot be
        had -- a comment is answered without its post rather than not at all.
        """
        return None

    async def download(self, url: str, *, max_bytes: int) -> bytes | None:
        """The bytes behind one of Instagram's CDN links, or None.

        None past `max_bytes` as well as on any error: a video the clinic
        posted is a few megabytes, and nothing here should hold more.
        """
        return None

    @abstractmethod
    async def fetch_username(self, *, access_token: str, igsid: str) -> str | None:
        """The patient's Instagram handle, or None if it cannot be had.

        None rather than an exception, for every way this fails: the account
        has no username, the permission was not granted, the id is stale, Meta
        is having an afternoon. None of those is a reason to drop a patient's
        message, so the caller carries on without one.
        """


class GraphAPIInstagramClient(InstagramClient):
    """Real implementation: POSTs to the Graph API's /me/messages endpoint."""

    def __init__(
        self,
        *,
        base_url: str = GRAPH_API_BASE_URL,
        facebook_base_url: str = GRAPH_FACEBOOK_BASE_URL,
        timeout: float = 10.0,
    ) -> None:
        self._http = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._facebook_http = httpx.AsyncClient(base_url=facebook_base_url, timeout=timeout)

    def _client_for(self, access_token: str) -> httpx.AsyncClient:
        return self._facebook_http if is_facebook_token(access_token) else self._http

    @staticmethod
    def _own(access_token: str, sender_igsid: str | None) -> str:
        """The path segment for the account itself: "me" on the Instagram
        host; on the Facebook host "me" is somebody else, so the account's
        id is required -- and a call without one is refused here, with a
        message, rather than sent to the wrong node."""
        if is_facebook_token(access_token):
            if not sender_igsid or sender_igsid == "me":
                raise InstagramSendError(
                    "a Facebook Login token needs the Instagram account id to send"
                )
            return sender_igsid
        return sender_igsid or "me"

    async def send_text(
        self,
        *,
        access_token: str,
        recipient_igsid: str,
        text: str,
        sender_igsid: str | None = None,
    ) -> None:
        response = await self._client_for(access_token).post(
            f"/{self._own(access_token, sender_igsid)}/messages",
            params={"access_token": access_token},
            json={"recipient": {"id": recipient_igsid}, "message": {"text": text}},
        )
        if response.is_error:
            error_code: object = None
            error_subcode: object = None
            with suppress(ValueError):
                error = response.json().get("error", {})
                error_code = error.get("code")
                # The subcode is the half that says what actually happened.
                # Meta answers "no such user", "outside the 24-hour messaging
                # window" and "this app cannot message this account" all with
                # code 100, so the code alone cannot tell an operator whether
                # to fix the token, the app's permissions, or nothing at all.
                # Taken as a scalar rather than by logging the payload,
                # because an integer cannot carry a credential.
                error_subcode = error.get("error_subcode")
            # Response body withheld from the log: Meta's error payloads can
            # echo request params (including access_token) back in the copy.
            # Status + error code + subcode is enough to act on.
            logger.error(
                "instagram_send_failed",
                extra={
                    "recipient_igsid": recipient_igsid,
                    "status_code": response.status_code,
                    "error_code": error_code,
                    "error_subcode": error_subcode,
                },
            )
            raise InstagramSendError(f"Instagram send failed with status {response.status_code}")

    def _raise_for_error(self, response: httpx.Response, event: str, subject: str) -> None:
        if not response.is_error:
            return
        error_code: object = None
        error_subcode: object = None
        with suppress(ValueError):
            error = response.json().get("error", {})
            error_code = error.get("code")
            error_subcode = error.get("error_subcode")
        logger.error(
            event,
            extra={
                "subject": subject,
                "status_code": response.status_code,
                "error_code": error_code,
                "error_subcode": error_subcode,
            },
        )
        raise InstagramSendError(f"{event}: status {response.status_code}")

    async def reply_to_comment(self, *, access_token: str, comment_id: str, text: str) -> None:
        response = await self._client_for(access_token).post(
            f"/{comment_id}/replies",
            params={"access_token": access_token},
            json={"message": text},
        )
        self._raise_for_error(response, "instagram_comment_reply_failed", comment_id)

    async def send_private_reply(
        self,
        *,
        access_token: str,
        comment_id: str,
        text: str,
        sender_igsid: str | None = None,
    ) -> None:
        response = await self._client_for(access_token).post(
            f"/{self._own(access_token, sender_igsid)}/messages",
            params={"access_token": access_token},
            json={"recipient": {"comment_id": comment_id}, "message": {"text": text}},
        )
        self._raise_for_error(response, "instagram_private_reply_failed", comment_id)

    async def fetch_media_caption(self, *, access_token: str, media_id: str) -> str | None:
        # Context only: a comment answered without its post is still answered.
        try:
            response = await self._client_for(access_token).get(
                f"/{media_id}",
                params={"fields": "caption", "access_token": access_token},
            )
        except httpx.HTTPError:
            return None
        if response.is_error:
            logger.warning(
                "instagram_media_caption_failed",
                extra={"media_id": media_id, "status_code": response.status_code},
            )
            return None
        with suppress(ValueError):
            caption = response.json().get("caption")
            if isinstance(caption, str) and caption.strip():
                return caption.strip()
        return None

    async def fetch_media(self, *, access_token: str, media_id: str) -> dict[str, Any] | None:
        try:
            response = await self._client_for(access_token).get(
                f"/{media_id}",
                params={"fields": MEDIA_FIELDS, "access_token": access_token},
            )
        except httpx.HTTPError:
            return None
        if response.is_error:
            logger.warning(
                "instagram_media_fetch_failed",
                extra={"media_id": media_id, "status_code": response.status_code},
            )
            return None
        with suppress(ValueError):
            body = response.json()
            if isinstance(body, dict) and body.get("id"):
                return body
        return None

    async def download(self, url: str, *, max_bytes: int) -> bytes | None:
        # The CDN, not the Graph API: a full URL, and a longer wait than a
        # Graph call gets, because this is megabytes rather than a line.
        try:
            async with (
                httpx.AsyncClient(timeout=60.0, follow_redirects=True) as http,
                http.stream("GET", url) as response,
            ):
                if response.is_error:
                    return None
                declared = response.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > max_bytes:
                    return None
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > max_bytes:
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
        except httpx.HTTPError:
            return None

    async def fetch_username(self, *, access_token: str, igsid: str) -> str | None:
        # The sender's own node, which the messaging permission grants for
        # anybody who has written to this account. "username" is the handle;
        # "name" is the display name and is not what an operator searches by,
        # so it is not asked for.
        try:
            response = await self._client_for(access_token).get(
                f"/{igsid}",
                params={"fields": "username", "access_token": access_token},
            )
        except httpx.HTTPError as exc:
            logger.warning(
                "instagram_username_unreachable",
                extra={"igsid": igsid, "error": type(exc).__name__},
            )
            return None
        if response.is_error:
            # Logged at warning, not error: a missing username costs the
            # dashboard a nicer label and costs the patient nothing, so this
            # must never read as an outage in the logs.
            logger.warning(
                "instagram_username_failed",
                extra={"igsid": igsid, "status_code": response.status_code},
            )
            return None
        with suppress(ValueError):
            username = response.json().get("username")
            if isinstance(username, str) and username.strip():
                return username.strip().lstrip("@")
        return None


@lru_cache
def get_instagram_client() -> InstagramClient:
    return GraphAPIInstagramClient()
