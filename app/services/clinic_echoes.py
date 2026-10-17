"""What the clinic's own account sent a patient while the assistant was thinking.

Instagram echoes every message the account sends. The job that reads an
echo as "a person from the clinic wrote" runs twenty seconds late on
purpose (see app.workers.tasks.note_clinic_reply), so the assistant's own
send is recorded first and not mistaken for a person's -- and for those
twenty seconds the assistant did not know. Live, 2026-10-16: an operator's
"Murojaat uchun: ☎️..." and then, the same minute, the assistant's own
answer under it, again and again.

So the webhook notes each echo here the moment it arrives, and the
assistant looks before it sends: anything the account sent since the
patient's last message that is not one of the assistant's own replies
means somebody else has answered.
"""

import json
from datetime import UTC, datetime
from typing import Any

_TTL_SECONDS = 15 * 60
_KEEP = 20


def _key(channel_id: str, patient_id: str) -> str:
    return f"clinic_echo:{channel_id}:{patient_id}"


async def note(
    redis: Any, channel_id: str, patient_id: str, text: str | None, at: datetime | None = None
) -> None:
    """Remember that the account sent this patient `text` (None: not text)."""
    key = _key(channel_id, patient_id)
    entry = json.dumps({"at": (at or datetime.now(UTC)).timestamp(), "text": text})
    await redis.rpush(key, entry)
    await redis.ltrim(key, -_KEEP, -1)
    await redis.expire(key, _TTL_SECONDS)


async def since(redis: Any, channel_id: str, patient_id: str, after: datetime) -> list[str | None]:
    """Texts the account sent this patient after `after`, oldest first."""
    raw = await redis.lrange(_key(channel_id, patient_id), 0, -1)
    out: list[str | None] = []
    for item in raw or []:
        try:
            entry = json.loads(item)
        except (TypeError, ValueError):
            continue
        if float(entry.get("at", 0)) > after.timestamp():
            out.append(entry.get("text"))
    return out
