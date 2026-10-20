"""The clinic's Instagram posts, as far as the dashboard shows them.

One endpoint: the cover of a post somebody commented under, fetched from
Instagram's CDN on the way through. The CDN link is never handed to the
browser -- it expires, and it is the clinic's token that fetched it.
"""

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin.deps import require_patient_access
from app.channels.instagram.client import get_instagram_client
from app.core.db import get_db_session
from app.core.encryption import decrypt
from app.core.tenant_context import get_current_tenant
from app.models.channel import Channel
from app.models.instagram_post import InstagramPost
from app.models.operator import Operator

router = APIRouter(prefix="/api/admin/posts", tags=["Admin — Posts"])

_MAX_COVER_BYTES = 4 * 1024 * 1024


@router.get("/{media_id}/cover")
async def cover(
    media_id: str,
    operator: Operator = Depends(require_patient_access),
    session: AsyncSession = Depends(get_db_session),
) -> Response:
    tenant_id = get_current_tenant()
    post = (
        await session.execute(
            select(InstagramPost).where(
                InstagramPost.tenant_id == tenant_id, InstagramPost.media_id == media_id
            )
        )
    ).scalar_one_or_none()
    if post is None or not post.thumbnail_url:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rasm yo'q")
    client = get_instagram_client()
    data = await client.download(post.thumbnail_url, max_bytes=_MAX_COVER_BYTES)
    if data is None:
        # The link expired: ask Instagram for the post again, once.
        channel = (
            (
                await session.execute(
                    select(Channel).where(
                        Channel.tenant_id == tenant_id, Channel.type == "instagram"
                    )
                )
            )
            .scalars()
            .first()
        )
        if channel is not None:
            body = await client.fetch_media(
                access_token=decrypt(channel.credentials), media_id=media_id
            )
            if body and isinstance(body.get("thumbnail_url"), str):
                post.thumbnail_url = body["thumbnail_url"]
                await session.commit()
                data = await client.download(post.thumbnail_url, max_bytes=_MAX_COVER_BYTES)
    if data is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Rasm yuklanmadi")
    return Response(
        content=data,
        media_type="image/jpeg",
        headers={
            "Cache-Control": "private, max-age=86400",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; img-src 'self'; sandbox",
        },
    )
