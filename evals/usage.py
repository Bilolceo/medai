"""What the evals spend on OpenAI, and a hard stop before the budget runs out.

Every chat completion's usage is counted; once the estimate passes BUDGET_USD
the next call raises, and the run ends. Prices are deliberately pessimistic
(per million tokens) so the real bill comes in under the estimate.
"""

import os
from typing import Any

from openai.resources.chat.completions import AsyncCompletions

PRICE_IN = float(os.environ.get("PRICE_IN", "1.00"))  # uncached input
PRICE_CACHED = float(os.environ.get("PRICE_CACHED", "0.15"))
PRICE_OUT = float(os.environ.get("PRICE_OUT", "6.00"))  # output, reasoning included
BUDGET_USD = float(os.environ.get("BUDGET_USD", "0"))


class BudgetSpentError(RuntimeError):
    pass


class Usage:
    calls = 0
    tokens_in = 0
    tokens_cached = 0
    tokens_out = 0

    @classmethod
    def usd(cls) -> float:
        fresh = cls.tokens_in - cls.tokens_cached
        return (
            fresh * PRICE_IN + cls.tokens_cached * PRICE_CACHED + cls.tokens_out * PRICE_OUT
        ) / 1_000_000

    @classmethod
    def line(cls) -> str:
        return (
            f"OpenAI: {cls.calls} chaqiruv, kirish {cls.tokens_in} (kesh {cls.tokens_cached}), "
            f"chiqish {cls.tokens_out} token, taxminan ${cls.usd():.3f}"
        )


def install() -> None:
    """Count every chat completion; stop at the budget."""
    if not BUDGET_USD:
        return
    original = AsyncCompletions.create

    async def counted(self: Any, *args: Any, **kwargs: Any) -> Any:
        if Usage.usd() >= BUDGET_USD:
            raise BudgetSpentError(f"budget ${BUDGET_USD} reached: {Usage.line()}")
        response = await original(self, *args, **kwargs)
        usage = getattr(response, "usage", None)
        if usage is not None:
            Usage.calls += 1
            Usage.tokens_in += usage.prompt_tokens or 0
            details = getattr(usage, "prompt_tokens_details", None)
            Usage.tokens_cached += getattr(details, "cached_tokens", 0) or 0
            Usage.tokens_out += usage.completion_tokens or 0
        return response

    AsyncCompletions.create = counted  # type: ignore[method-assign]
