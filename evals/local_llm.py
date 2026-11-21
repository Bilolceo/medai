"""A local model (Ollama) in place of OpenAI for the evals -- free, offline.

    LOCAL_LLM=qwen3:8b python evals/simulate.py ...

Ollama speaks the OpenAI chat API at /v1; from the api container the host is
host.docker.internal. It tests the agent's logic and guards; an 8B model's
Uzbek is not production quality, so the wording is judged on the real model.
"""

import os
from typing import Any

import httpx

from app.rag.llm import ChatMessage, LLMProvider

# Ollama's own API (not its OpenAI one): it can switch Qwen3's thinking off,
# cap the answer and set the context -- the agent's prompt is ~7k tokens and
# Ollama's default window is 4k, which cut the rules off.
BASE_URL = os.environ.get("LOCAL_LLM_URL", "http://host.docker.internal:11434")


class LocalLLM(LLMProvider):
    calls = 0

    def __init__(self, model: str) -> None:
        self.model = model
        self.client = httpx.AsyncClient(base_url=BASE_URL, timeout=600.0)

    async def _call(self, system: str, messages: list[ChatMessage], json_mode: bool) -> str:
        LocalLLM.calls += 1
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "stream": False,
            "think": False,
            "options": {"num_ctx": 16384, "num_predict": 900, "temperature": 0.3},
        }
        if json_mode:
            body["format"] = "json"
        response = await self.client.post("/api/chat", json=body)
        response.raise_for_status()
        text = str(response.json()["message"]["content"])
        return text.split("</think>")[-1].strip()

    async def generate(self, system_prompt: str, messages: list[ChatMessage]) -> str:
        return await self._call(system_prompt, messages, json_mode=False)

    async def generate_json(self, system_prompt: str, messages: list[ChatMessage]) -> str:
        return await self._call(system_prompt, messages, json_mode=True)


def install() -> LocalLLM | None:
    """With LOCAL_LLM set: every model the app asks for is the local one."""
    model = os.environ.get("LOCAL_LLM")
    if not model:
        return None
    from app.rag import llm
    from app.services import answer

    local = LocalLLM(model)
    for module in (llm, answer):
        for name in ("get_llm_provider", "get_agent_llm_provider", "get_reader_llm_provider"):
            if hasattr(module, name):
                setattr(module, name, lambda: local)
    return local
