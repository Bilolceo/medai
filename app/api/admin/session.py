"""Who the dashboard is logged in as, and the token its writes must carry."""

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.admin.schemas import SessionInfo
from app.api.auth import get_current_operator, get_current_session
from app.core.config import get_settings
from app.core.db import get_db_session
from app.core.roles import permissions_for
from app.core.session import Session
from app.core.tenant_context import get_current_tenant
from app.models.operator import Operator
from app.models.tenant import Tenant
from app.services import branding

router = APIRouter(prefix="/api/admin", tags=["Admin — Session"])


@router.get("/session", response_model=SessionInfo)
async def current_session(
    operator: Operator = Depends(get_current_operator),
    session: Session = Depends(get_current_session),
    db_session: AsyncSession = Depends(get_db_session),
) -> SessionInfo:
    """The dashboard's first call.

    It returns the CSRF token as well as the identity, because a JSON client
    has no server-rendered form to carry a hidden field in — every mutating
    request echoes this value back in X-CSRF-Token. The token is bound into
    the signed session cookie, so a cross-site page cannot read it.

    Which clinic the operator belongs to is reported, never accepted: the
    tenant comes from their own row and is what every query here filters on.
    """
    tenant = await db_session.get(Tenant, get_current_tenant())
    assert tenant is not None  # the operator's own tenant
    return SessionInfo(
        operator_id=operator.id,
        name=operator.name,
        role=operator.role,
        # Sorted so the payload is stable between requests — an unordered
        # set would otherwise make every response look different.
        permissions=sorted(permissions_for(operator.role)),
        tenant_id=tenant.id,
        tenant_name=tenant.name,
        brand_title=branding.title(tenant),
        brand_subtitle=branding.subtitle(tenant, get_settings()),
        csrf_token=session.csrf_token,
        sheet_url=(
            f"https://docs.google.com/spreadsheets/d/{sheet_id}/edit"
            if (sheet_id := get_settings().google_sheets_spreadsheet_id)
            else None
        ),
    )
