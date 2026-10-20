"""The dashboard's JSON API.

Ported from the Telegram side, onto the shared core and behind the session
auth the operator dashboard already used. Two things changed in the move,
both of them holes rather than features:

* The tenant is taken from the logged-in operator instead of from a
  `tenant_id` query parameter that defaulted to 1. Any authenticated
  operator could previously read and modify another clinic's patients,
  appointments and conversations by changing that number.
* Writes require the session's CSRF token in a header (see
  app.api.admin.deps), matching what the server-rendered dashboard already
  did with a hidden form field.
"""

from fastapi import APIRouter

from app.api.admin.account import router as account_router
from app.api.admin.clinic import router as clinic_router
from app.api.admin.conversations import router as conversations_router
from app.api.admin.filters import router as filters_router
from app.api.admin.knowledge_files import router as knowledge_files_router
from app.api.admin.media import router as media_router
from app.api.admin.operators import router as operators_router
from app.api.admin.posts import router as posts_router
from app.api.admin.reports import router as reports_router
from app.api.admin.schedule import router as schedule_router
from app.api.admin.session import router as session_router
from app.api.admin.suggestions import router as suggestions_router

router = APIRouter()
router.include_router(session_router)
router.include_router(account_router)
router.include_router(conversations_router)
router.include_router(clinic_router)
router.include_router(filters_router)
router.include_router(knowledge_files_router)
router.include_router(posts_router)
router.include_router(media_router)
router.include_router(operators_router)
router.include_router(reports_router)
router.include_router(schedule_router)
router.include_router(suggestions_router)

__all__ = ["router"]
