"""Turning a platform's own account id into the tenant and channel it belongs to.

This runs BEFORE any tenant is known — it is what establishes tenant context
for an inbound event — so unlike the rest of the service layer it queries
Channel directly rather than through ChannelRepository /
TenantScopedRepository, both of which require get_current_tenant() to
already have a value.
"""

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.channels.base import ChannelType
from app.models.channel import Channel
from app.models.tenant import Tenant


@dataclass(frozen=True)
class ResolvedChannel:
    """Which tenant an inbound event belongs to, and over which channel.

    The channel id matters as much as the tenant: it is the account the
    patient wrote to, and therefore the only account the reply may be sent
    from. The pipeline used to carry the tenant alone and then pick a
    channel to reply through by listing the tenant's channels and taking
    whichever came back first — see app.services.delivery for what that
    cost.
    """

    tenant_id: uuid.UUID
    channel_id: uuid.UUID
    channel_type: str


async def resolve_channel(
    session: AsyncSession, *, channel_type: str, external_id: str
) -> ResolvedChannel | None:
    """The active channel with this platform account id, or None.

    None means "no channel we serve" and the event must not be processed —
    an unknown account is either a stale subscription or someone else's
    traffic, and neither has a tenant to run under.
    """
    stmt = select(Channel.tenant_id, Channel.id).where(
        Channel.type == channel_type,
        Channel.external_id == external_id,
        Channel.is_active.is_(True),
    )
    row = (await session.execute(stmt)).first()
    if row is None:
        return None
    return ResolvedChannel(tenant_id=row[0], channel_id=row[1], channel_type=channel_type)


async def resolve_instagram_channel(
    session: AsyncSession, ig_account_id: str
) -> ResolvedChannel | None:
    """resolve_channel() fixed to Instagram — what the webhook calls, so the
    channel type string lives here rather than in the route.
    """
    return await resolve_channel(
        session, channel_type=ChannelType.INSTAGRAM, external_id=ig_account_id
    )


async def bot_replies_enabled(session: AsyncSession, tenant_id: uuid.UUID) -> bool:
    """Whether this clinic's assistant is currently answering patients.

    Read from tenants.settings, the same JSONB the dashboard writes, so the
    switch on the conversations screen takes effect on the next message with
    no deploy and no restart.

    Absent means on. A clinic that has never touched the switch is a clinic
    whose bot has been answering all along, and a missing key must not read
    as "turned off" and silence it.
    """
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        return True
    return bool(tenant.settings.get("bot_replies_enabled", True))


async def comment_replies_enabled(session: AsyncSession, tenant_id: uuid.UUID) -> bool:
    """Whether the assistant answers comments under the clinic's posts.

    Its own switch beside the assistant's: a clinic may want Direct answered
    and its public comments left to people. Absent means on, as before.
    """
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        return True
    return bool(tenant.settings.get("comment_replies_enabled", True))


async def debounce_window(session: AsyncSession, tenant_id: uuid.UUID) -> int | None:
    """How long this clinic waits for a patient to finish typing, in seconds.

    The dashboard's "Kutish oynasi" (tenants.settings["debounce_seconds"]).
    It was saved and shown back for months while every reply still waited
    the deployment's DEBOUNCE_WINDOW_SECONDS, so a clinic that raised it to
    stop answers landing between a patient's bubbles saw nothing change.

    None when the clinic has not set one: the caller falls back to the
    deployment's own default. A value that is not a whole number of seconds
    within what the dashboard allows reads as unset rather than as zero.
    """
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        return None
    raw = tenant.settings.get("debounce_seconds")
    if isinstance(raw, bool) or not isinstance(raw, int) or not 0 <= raw <= 300:
        return None
    return raw


async def clinic_rules(session: AsyncSession, tenant_id: uuid.UUID) -> list[str]:
    """The clinic's own standing instructions, from tenants.settings.

    The dashboard has had a box for these since the settings screen was
    written, and until now nothing read it: an operator could type a rule,
    press save, and watch the assistant carry on exactly as before. They are
    also what an admin's Instagram command writes to, so the two ways of
    setting one end up in the same place and either can undo the other.

    Strings only, non-empty, in the order the clinic wrote them. A stored
    value of the wrong shape is ignored rather than raised on -- this is read
    on the path that answers a patient, and a malformed setting must not cost
    somebody their reply.
    """
    tenant = await session.get(Tenant, tenant_id)
    if tenant is None:
        return []
    raw = tenant.settings.get("strict_rules")
    if not isinstance(raw, list):
        return []
    return [rule.strip() for rule in raw if isinstance(rule, str) and rule.strip()]
