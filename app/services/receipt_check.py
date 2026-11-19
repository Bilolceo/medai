"""Is the photo sent after the card a payment receipt?

The owner, 2026-11-19: a patient who sends a photo of a car instead of the
receipt was told "Chekingiz keldi". Now the photo is looked at once by the
model -- is it a payment receipt or a transfer screenshot, for what sum, to
which card -- and a photo that is plainly not one is answered with a request
for the receipt. The booking is still confirmed by a person: the verdict goes
to the clinic's Telegram with the photo, and a doubtful or failed look falls
back to the ordinary "chekingiz keldi".

One small call per photo burst, only after the card was given -- never for an
analysis or any other picture.
"""

import base64
import json
import logging
import re
from dataclasses import dataclass

from openai import AsyncOpenAI

from app.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

_PROMPT = (
    "You check one image a patient sent right after a clinic gave them a bank card number to "
    "pay a consultation prepayment. Answer only JSON: "
    '{"receipt": true|false, "amount": "<sum as written, or null>", '
    '"card_last4": "<last 4 digits of the receiving card if visible, or null>"}. '
    '"receipt" is true for a payment receipt, a bank or payment-app transfer screenshot '
    "(Click, Payme, Uzum, a bank app) or a terminal check; false for anything else -- a "
    "photo of a person, a car, a document, an analysis, a meme, a blank screen."
)


@dataclass(frozen=True)
class Verdict:
    """What the look found. ``receipt`` is None when it could not be told."""

    receipt: bool | None
    amount: str | None = None
    card_last4: str | None = None


UNKNOWN = Verdict(receipt=None)


def _parse(text: str) -> Verdict:
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        return UNKNOWN
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return UNKNOWN
    receipt = data.get("receipt")
    if not isinstance(receipt, bool):
        return UNKNOWN
    amount = data.get("amount")
    last4 = data.get("card_last4")
    return Verdict(
        receipt=receipt,
        amount=str(amount) if amount not in (None, "", "null") else None,
        card_last4=re.sub(r"\D", "", str(last4))[-4:] if last4 not in (None, "", "null") else None,
    )


async def check(content: bytes, content_type: str, *, settings: Settings | None = None) -> Verdict:
    """The model's look at one stored image. Never raises: a failure is UNKNOWN."""
    resolved = settings or get_settings()
    if not resolved.receipt_check or not resolved.openai_api_key or not content:
        return UNKNOWN
    data_url = f"data:{content_type or 'image/jpeg'};base64,{base64.b64encode(content).decode()}"
    try:
        client = AsyncOpenAI(api_key=resolved.openai_api_key, timeout=30.0, max_retries=0)
        extra = {"reasoning_effort": "low"} if resolved.openai_model.startswith("gpt-5") else {}
        response = await client.chat.completions.create(  # type: ignore[call-overload]
            model=resolved.openai_model,
            **extra,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": _PROMPT},
                        {"type": "image_url", "image_url": {"url": data_url, "detail": "low"}},
                    ],
                }
            ],
        )
        verdict = _parse(response.choices[0].message.content or "")
    except Exception:  # noqa: BLE001 - the ordinary acknowledgement still goes
        logger.exception("receipt_check_failed")
        return UNKNOWN
    logger.info(
        "receipt_checked",
        extra={"receipt": verdict.receipt, "amount": verdict.amount, "card": verdict.card_last4},
    )
    return verdict


def note(verdict: Verdict, *, expected_last4: str | None) -> str:
    """One line for the clinic's Telegram: what the look found."""
    if verdict.receipt is None:
        return "🤖 AI: rasmni tekshira olmadi -- o'zingiz qarang."
    if not verdict.receipt:
        return "🤖 AI: bu CHEK EMAS ko'rinadi -- bemordan chek so'raldi."
    parts = ["🤖 AI: chekka o'xshaydi"]
    if verdict.amount:
        parts.append(f"summa: {verdict.amount}")
    if verdict.card_last4:
        mismatch = expected_last4 and verdict.card_last4 != expected_last4
        parts.append(
            f"karta: *{verdict.card_last4}"
            + (f" (BIZNIKI *{expected_last4} EMAS!)" if mismatch else "")
        )
    return " · ".join(parts) + " -- baribir tekshiring."
