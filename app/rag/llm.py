from abc import ABC, abstractmethod
from functools import lru_cache
from typing import Any, Literal, TypedDict, cast

from openai import AsyncOpenAI
from openai.types.chat import ChatCompletionMessageParam

from app.core.config import Settings, get_settings


class ChatMessage(TypedDict):
    role: Literal["user", "assistant"]
    content: str


class LLMProvider(ABC):
    """Abstraction over "turn a system prompt + conversation into a reply",
    mirroring EmbeddingProvider so the backend/model can change without
    touching callers, and so tests can inject a fake instead of hitting the
    network.
    """

    @abstractmethod
    async def generate(self, system_prompt: str, messages: list[ChatMessage]) -> str:
        """Generate a reply given a system prompt and the conversation so far."""


class OpenAILLMProvider(LLMProvider):
    def __init__(self, settings: Settings | None = None, model: str | None = None) -> None:
        resolved = settings or get_settings()
        api_key = resolved.openai_api_key
        if api_key is None:
            raise ValueError("OPENAI_API_KEY is required to use OpenAILLMProvider")
        # Settings.openai_model, not a literal default here, so the model can
        # be changed from the host's dashboard: a model that retires or starts
        # writing badly is something to fix while patients are waiting, not
        # something to ship code for.
        self._model = model or resolved.openai_model
        self._reasoning_effort = resolved.openai_reasoning_effort
        # A minute, not the SDK's ten: a hung call holds a patient's answer,
        # and the job's own retry ladder is the better place to wait.
        self._client = AsyncOpenAI(api_key=api_key, timeout=90.0, max_retries=1)

    async def generate(self, system_prompt: str, messages: list[ChatMessage]) -> str:
        # ChatMessage is deliberately narrower than the SDK's message union
        # (only user/assistant; system is handled separately by this
        # abstraction), so it doesn't structurally unify with
        # ChatCompletionMessageParam under mypy strict. Both sides are
        # simple {role, content} dicts at runtime, so the cast is safe.
        payload = cast(
            "list[ChatCompletionMessageParam]",
            [{"role": "system", "content": system_prompt}, *messages],
        )
        extra: dict[str, Any] = {}
        # Only reasoning models accept the parameter; sending it to another
        # model is a 400 on every reply.
        if self._reasoning_effort and self._model.startswith(("gpt-5", "o1", "o3", "o4")):
            extra["reasoning_effort"] = self._reasoning_effort
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=payload,
            **extra,
        )
        content: str | None = response.choices[0].message.content
        if content is None:
            raise ValueError("OpenAI chat completion returned no text content")
        return content


# Hugging Face's Inference Providers router, which speaks the OpenAI chat
# completions API. Reached through AsyncOpenAI with the base URL swapped
# rather than through a Hugging Face SDK: it is the same protocol, and one
# client with two base URLs is less to keep working than two clients.
HF_ROUTER_BASE_URL = "https://router.huggingface.co/v1"


class QwenLLMProvider(LLMProvider):
    """Qwen, served through Hugging Face's OpenAI-compatible router.

    Kept as its own class rather than as an argument to OpenAILLMProvider
    because it reads a different credential and has a different default
    model, and because "which provider is this deployment on" should be
    answerable by looking at the type.
    """

    def __init__(self, settings: Settings | None = None, model: str | None = None) -> None:
        resolved = settings or get_settings()
        if resolved.hf_token is None:
            raise ValueError("HF_TOKEN is required to use QwenLLMProvider")
        self._model = model or resolved.qwen_model
        self._client = AsyncOpenAI(api_key=resolved.hf_token, base_url=HF_ROUTER_BASE_URL)

    async def generate(self, system_prompt: str, messages: list[ChatMessage]) -> str:
        payload = cast(
            "list[ChatCompletionMessageParam]",
            [{"role": "system", "content": system_prompt}, *messages],
        )
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=payload,
        )
        content = response.choices[0].message.content
        if content is None:
            raise ValueError("Qwen chat completion returned no text content")
        return content


def _select_llm_provider(settings: Settings) -> LLMProvider:
    # OpenAI unless something explicitly asks for otherwise. LLM_PROVIDER
    # moves the replies alone and leaves the embeddings where they are,
    # because moving those means re-embedding the knowledge base (see
    # Settings.llm_provider).
    if settings.llm_provider == "qwen":
        return QwenLLMProvider(settings)
    return OpenAILLMProvider(settings)


@lru_cache
def get_llm_provider() -> LLMProvider:
    return _select_llm_provider(get_settings())


@lru_cache
def get_review_llm_provider() -> LLMProvider:
    """For the nightly review: the review model if one is set, thinking hard."""
    settings = get_settings()
    return _select_llm_provider(
        settings.model_copy(
            update={
                "openai_model": settings.openai_review_model or settings.openai_model,
                "openai_reasoning_effort": "high",
            }
        )
    )


@lru_cache
def get_comment_llm_provider() -> LLMProvider:
    """The same model, thinking harder: a comment answer is read by nobody
    waiting in a chat, so the seconds a Direct reply cannot spare are free here.
    """
    settings = get_settings()
    return _select_llm_provider(settings.model_copy(update={"openai_reasoning_effort": "medium"}))
