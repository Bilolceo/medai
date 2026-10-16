from abc import ABC, abstractmethod
from functools import lru_cache

from openai import AsyncOpenAI

from app.core.config import Settings, get_settings

# The model that makes the vectors, and how wide they are.
#
# The knowledge_base.embedding column is a single fixed-width pgvector column,
# so exactly one model's output size can be live at a time. text-embedding-3-small
# is native 1536, which also sits under pgvector's 2000-dimension hard cap for
# HNSW/IVFFlat indexes (verified against pgvector 0.8.6) -- so the vectors are
# indexable as they come, with no truncation.
#
# The name is written onto every row it embeds (KnowledgeBase.embedding_model).
# Vectors are only comparable to vectors from the same model, so a row embedded
# by something else is not a slightly worse match, it is a meaningless one --
# and knowing which model made a row is what lets ingest_faqs repair it instead
# of the clinic finding out through a bot that suddenly knows nothing.
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536


class EmbeddingProvider(ABC):
    """Abstraction over "turn text into vectors", mirroring LLMProvider so the
    concrete backend can be swapped without touching callers. Callers should
    depend on this interface, not a concrete provider directly, so tests can
    inject a fake instead of hitting the network.
    """

    @abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed each text, returning one vector per input in the same order."""


# OpenAI's embeddings endpoint takes at most this many inputs in one request.
# Roomy enough that it went unnoticed for a long time -- but the
# clinic's knowledge base has grown from 98 rows to over 1500 in a week, one
# wording of one question at a time, and ingest_faqs embeds a whole file in a
# single call. The deploy that crosses the line loses its entire seed, and
# nothing about the file says which deploy that will be.
_OPENAI_BATCH_LIMIT = 2048


class OpenAIEmbeddingProvider(EmbeddingProvider):
    def __init__(
        self,
        settings: Settings | None = None,
        model: str = EMBEDDING_MODEL,
    ) -> None:
        api_key = (settings or get_settings()).openai_api_key
        if api_key is None:
            raise ValueError("OPENAI_API_KEY is required to use OpenAIEmbeddingProvider")
        self._model = model
        self._client = AsyncOpenAI(api_key=api_key, timeout=30.0, max_retries=1)

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        # Batched, not one call per text: the endpoint accepts a list under
        # `input` and returns vectors in the same order, so this saves N-1
        # round trips per ingest -- chunked only where the API stops accepting
        # a longer list.
        vectors: list[list[float]] = []
        for start in range(0, len(texts), _OPENAI_BATCH_LIMIT):
            response = await self._client.embeddings.create(
                model=self._model,
                input=texts[start : start + _OPENAI_BATCH_LIMIT],
                dimensions=EMBEDDING_DIMENSIONS,
            )
            vectors.extend(item.embedding for item in response.data)
        return vectors


def _select_embedding_provider(settings: Settings) -> EmbeddingProvider:
    return OpenAIEmbeddingProvider(settings)


@lru_cache
def get_embedding_provider() -> EmbeddingProvider:
    return _select_embedding_provider(get_settings())
