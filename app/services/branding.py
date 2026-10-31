"""Whose dashboard, whose sheet, whose bot: the name at the top.

One deployment serves one doctor, and the code serves every deployment, so
the doctor's name lives in the account's settings ("brand_title",
"brand_subtitle") and nowhere in the code. Until a deployment sets them, the
account's own name stands in for the title and the DOCTOR_SPECIALTY setting,
capitalised, for the line under it -- which is exactly what the first clinic
had hard-coded, so it sees no change.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings, get_settings
from app.models.tenant import Tenant


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def title(tenant: Tenant) -> str:
    return _text((tenant.settings or {}).get("brand_title")) or tenant.name


def subtitle(tenant: Tenant, settings: Settings | None = None) -> str:
    own = _text((tenant.settings or {}).get("brand_subtitle"))
    if own:
        return own
    specialty = (settings or get_settings()).doctor_specialty or ""
    return specialty[:1].upper() + specialty[1:]


async def the_tenant(session: AsyncSession) -> Tenant:
    """The one account this deployment serves -- for commands run by hand,
    which have no request to take a tenant from."""
    tenants = list((await session.execute(select(Tenant))).scalars())
    if len(tenants) != 1:
        raise ValueError(f"expected exactly one tenant, found {len(tenants)}")
    return tenants[0]
