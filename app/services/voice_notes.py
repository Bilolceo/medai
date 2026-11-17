"""Voice messages, answered by the one line the clinic wants said.

The assistant cannot hear a recording. Asked to answer one anyway it writes
something plausible about a message nobody has listened to, which is the
worst of both: the patient believes they have been understood, and nobody
has read what they sent. So a voice note gets a fixed reply instead -- the
administrator does not listen to these, the doctor does -- in whichever
alphabet the patient has been writing in.
"""

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant_context import get_current_tenant
from app.models.message import MessageSender
from app.models.tenant import Tenant
from app.repositories.message import MessageRepository
from app.services import message_labels
from app.services.language import reply_script

REPLIES = {
    "uz-latn": (
        "Ovozli xabaringiz keldi, xodimimiz eshitib shu yerda javob beradi. "
        "Savolni yozib yuborsangiz, darrov javob beraman."
    ),
    "uz-cyrl": (
        "Овозли хабарингиз келди, ходимимиз эшитиб шу ерда жавоб беради. "
        "Саволни ёзиб юборсангиз, дарров жавоб бераман."
    ),
    "ru": (
        "Голосовое сообщение получили, сотрудник клиники прослушает и ответит здесь. "
        "Если напишете вопрос текстом, я отвечу сразу."
    ),
}


async def replies(session: AsyncSession) -> dict[str, str]:
    """The fixed lines, by alphabet -- the clinic's own where its intake flow
    has one (Dr Sultonbek, 2026-11-17: "eshita olmayman, matn qilib yozing"),
    these otherwise."""
    tenant = await session.get(Tenant, get_current_tenant())
    flows = ((tenant.settings or {}) if tenant is not None else {}).get("scripted_flows") or {}
    texts = (flows.get("intake") or {}).get("texts") or {}
    own = {
        script: str(lines["voice_reply"])
        for script, lines in texts.items()
        if isinstance(lines, dict) and lines.get("voice_reply")
    }
    return {**REPLIES, **own}


async def reply_for(session: AsyncSession, conversation_id: uuid.UUID) -> str:
    """The fixed reply, in the script this patient has been writing in."""
    lines = await replies(session)
    for message in reversed(await MessageRepository(session).list_recent(conversation_id, 20)):
        if (
            message.sender == MessageSender.PATIENT
            and not message_labels.is_label(message.content)
            and not message.content.startswith("💬")
        ):
            return lines[reply_script(message.content)]
    return lines["uz-latn"]
