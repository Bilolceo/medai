"""app.services.instagram_posts: the post behind a comment, fetched once,
its video heard once, and what the assistant is told about it."""

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.instagram.client import InstagramClient
from app.models.instagram_post import InstagramPost, TranscriptStatus
from app.rag.llm import ChatMessage, LLMProvider
from app.services import comments, instagram_posts
from tests.conftest import Seed

REEL = {
    "id": "18109399376524637",
    "media_type": "VIDEO",
    "media_product_type": "REELS",
    "caption": "Jinsiy zaiflikni dori-darmonsiz davolash",
    "permalink": "https://www.instagram.com/reel/abc/",
    "thumbnail_url": "https://cdn.example/cover.jpg",
    "media_url": "https://cdn.example/reel.mp4",
    "timestamp": "2026-10-15T09:00:00+0000",
}


class PostClient(InstagramClient):
    def __init__(self, body: dict[str, Any] | None = REEL, video: bytes | None = b"mp4") -> None:
        self.body = body
        self.video = video
        self.fetches = 0
        self.downloads: list[str] = []

    async def send_text(self, *, access_token: str, recipient_igsid: str, text: str) -> None:
        pass

    async def reply_to_comment(self, *, access_token: str, comment_id: str, text: str) -> None:
        pass

    async def send_private_reply(self, *, access_token: str, comment_id: str, text: str) -> None:
        pass

    async def fetch_username(self, *, access_token: str, igsid: str) -> str | None:
        return None

    async def fetch_media(self, *, access_token: str, media_id: str) -> dict[str, Any] | None:
        self.fetches += 1
        return dict(self.body) if self.body else None

    async def download(self, url: str, *, max_bytes: int) -> bytes | None:
        self.downloads.append(url)
        return self.video


class Summariser(LLMProvider):
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, system_prompt: str, messages: list[ChatMessage]) -> str:
        self.calls += 1
        return "Video erektil disfunksiyani dorisiz davolash haqida; doktor ko'rikka taklif qiladi."


async def _hear(monkeypatch: pytest.MonkeyPatch, text: str | None) -> None:
    async def fake(data: bytes) -> str | None:
        return text

    monkeypatch.setattr(instagram_posts, "_speech_to_text", fake)


async def test_a_video_is_fetched_heard_and_summarised_once(
    db_session: AsyncSession, seed: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _hear(monkeypatch, "Assalomu alaykum, bugun jinsiy zaiflik haqida gaplashamiz...")
    client = PostClient()
    llm = Summariser()

    post = await instagram_posts.context(
        db_session,
        tenant_id=seed.tenant_a.id,
        client=client,
        access_token="token",
        media_id=REEL["id"],
        llm_provider=llm,
    )

    assert post is not None
    assert post.transcript_status == TranscriptStatus.READY
    assert post.transcript and "jinsiy zaiflik" in post.transcript
    assert post.summary and "erektil" in post.summary
    assert post.thumbnail_url == REEL["thumbnail_url"]
    assert client.downloads == [REEL["media_url"]]

    # The second comment under the same reel reads the row: no fetch, no
    # download, no second summary.
    again = await instagram_posts.context(
        db_session,
        tenant_id=seed.tenant_a.id,
        client=client,
        access_token="token",
        media_id=REEL["id"],
        llm_provider=llm,
    )
    assert again is not None and again.id == post.id
    assert client.fetches == 1 and len(client.downloads) == 1 and llm.calls == 1
    rows = (await db_session.execute(select(InstagramPost))).scalars().all()
    assert len(rows) == 1


async def test_a_photo_is_not_sent_to_the_speech_model(
    db_session: AsyncSession, seed: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    called = []

    async def fake(data: bytes) -> str | None:
        called.append(data)
        return "never"

    monkeypatch.setattr(instagram_posts, "_speech_to_text", fake)
    client = PostClient(body={**REEL, "media_type": "IMAGE", "media_product_type": "FEED"})

    post = await instagram_posts.context(
        db_session,
        tenant_id=seed.tenant_a.id,
        client=client,
        access_token="token",
        media_id=REEL["id"],
        llm_provider=Summariser(),
    )

    assert post is not None and post.transcript_status == TranscriptStatus.SKIPPED
    assert called == [] and client.downloads == []
    assert post.summary  # from the caption alone


async def test_a_video_that_cannot_be_heard_is_tried_again_next_time(
    db_session: AsyncSession, seed: Seed, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _hear(monkeypatch, None)
    client = PostClient(video=None)
    post = await instagram_posts.context(
        db_session,
        tenant_id=seed.tenant_a.id,
        client=client,
        access_token="token",
        media_id=REEL["id"],
        llm_provider=Summariser(),
    )
    assert post is not None and post.transcript_status == TranscriptStatus.FAILED
    # Still answered: the caption is there and the summary came from it.
    assert post.summary


async def test_no_post_is_no_post(db_session: AsyncSession, seed: Seed) -> None:
    client = PostClient(body=None)
    assert (
        await instagram_posts.context(
            db_session, tenant_id=seed.tenant_a.id, client=client, access_token="t", media_id="x"
        )
        is None
    )
    assert (
        await instagram_posts.context(
            db_session, tenant_id=seed.tenant_a.id, client=client, access_token="t", media_id=None
        )
        is None
    )


def test_the_model_is_told_what_the_video_says_first() -> None:
    post = InstagramPost(
        media_id="1",
        media_type="VIDEO",
        caption="Implant haqida",
        summary="Uch komponentli implant amaliyoti; narxi taxminan 30 mln so'm.",
        transcript="...",
    )
    text = instagram_posts.describe(post)
    assert text is not None
    assert text.startswith("Video mazmuni: Uch komponentli")
    assert "Post matni: «Implant haqida»" in text
    situation = comments.situation(post.caption, text)
    assert "Izoh yozilgan post haqida:" in situation
    assert "qaysi xizmat ekanini qayta so'ramang" in situation
    # Without a post, the old caption-only line.
    assert "«Implant haqida»" in comments.situation("Implant haqida")
    assert "noma'lum" in comments.situation(None)
