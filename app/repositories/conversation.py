import uuid
from typing import Any

from sqlalchemy import select

from app.core.tenant_context import get_current_tenant
from app.models.conversation import Conversation
from app.repositories.base import CrossTenantAccessError, TenantScopedRepository

# The status an in-progress conversation carries. A conversation is closed
# by moving it off this value, which is what makes "the open one" a
# single-row lookup rather than a guess at the most recent.
OPEN_STATUS = "open"
# Where a conversation goes when the clinic starts the patient afresh: the
# next message opens a new one, and this one stays readable as history.
CLOSED_STATUS = "closed"


class ConversationRepository(TenantScopedRepository[Conversation]):
    model = Conversation

    async def get_open_for_user(self, user_id: uuid.UUID) -> Conversation | None:
        """This patient's currently-open conversation, if any.

        Ordered newest-first and limited rather than assuming uniqueness:
        nothing at the database level forbids two open conversations for one
        user today, and a reader that raised on finding two would turn a
        recoverable data oddity into a patient getting no reply at all.
        """
        stmt = (
            select(Conversation)
            .where(
                Conversation.tenant_id == get_current_tenant(),
                Conversation.user_id == user_id,
                Conversation.status == OPEN_STATUS,
            )
            .order_by(Conversation.created_at.desc())
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def update(self, obj: Conversation, **values: Any) -> Conversation:
        """Mutates and flushes an already-loaded row (e.g. an operator taking
        the conversation over by clearing is_bot_enabled).

        Re-checks obj.tenant_id itself, the same defensive pattern as
        KnowledgeBaseRepository.update() and AppointmentRepository.update().
        """
        current_tenant_id = get_current_tenant()
        if obj.tenant_id != current_tenant_id:
            raise CrossTenantAccessError(
                f"obj.tenant_id={obj.tenant_id} does not match the current tenant "
                f"({current_tenant_id})"
            )
        for field, value in values.items():
            setattr(obj, field, value)
        await self.session.flush()
        return obj
