"""
llm.py — Multi-backend LLM with automatic fallback (Mistral / Groq / Ollama)
"""
import asyncio
import time
import logging
from abc import ABC, abstractmethod

import httpx

import config

log = logging.getLogger("robbot.llm")

# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

class RateLimiter:
    """Simple async rate limiter — enforces a minimum interval between calls."""

    def __init__(self, max_per_second: float = 1.0):
        self.min_interval = 1.0 / max_per_second
        self.last_call = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.time()
            wait = self.min_interval - (now - self.last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self.last_call = time.time()

# ---------------------------------------------------------------------------
# Abstract backend
# ---------------------------------------------------------------------------

class LLMBackend(ABC):
    """Base class for LLM backends."""

    @abstractmethod
    async def generate(self, messages: list[dict]) -> str:
        """Send messages and return the assistant's response text."""
        ...

# ---------------------------------------------------------------------------
# Mistral (free tier: 1B tokens/month)
# ---------------------------------------------------------------------------

class MistralBackend(LLMBackend):
    API_URL = "https://api.mistral.ai/v1/chat/completions"

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    async def generate(self, messages: list[dict]) -> str:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                self.API_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": messages,
                    "max_tokens": config.LLM_MAX_TOKENS,
                    "temperature": config.LLM_TEMPERATURE,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

# ---------------------------------------------------------------------------
# Groq (free tier: rate-limited)
# ---------------------------------------------------------------------------

class GroqBackend(LLMBackend):
    API_URL = "https://api.groq.com/openai/v1/chat/completions"

    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model

    async def generate(self, messages: list[dict]) -> str:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                self.API_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    "messages": messages,
                    "max_tokens": config.LLM_MAX_TOKENS,
                    "temperature": config.LLM_TEMPERATURE,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]

# ---------------------------------------------------------------------------
# Ollama (local, fully free)
# ---------------------------------------------------------------------------

class OllamaBackend(LLMBackend):
    def __init__(self, url: str, model: str):
        self.url = url.rstrip("/")
        self.model = model

    async def generate(self, messages: list[dict]) -> str:
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self.url}/api/chat",
                json={
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                    "options": {
                        "num_predict": config.LLM_MAX_TOKENS,
                        "temperature": config.LLM_TEMPERATURE,
                    },
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data["message"]["content"]

# ---------------------------------------------------------------------------
# Router: tries primary, falls back to secondary
# ---------------------------------------------------------------------------

def _make_backend(name: str) -> LLMBackend | None:
    """Create a backend by name, or None if not configured."""
    name = name.lower()
    if name == "mistral" and config.MISTRAL_API_KEY:
        return MistralBackend(config.MISTRAL_API_KEY, config.MISTRAL_MODEL)
    if name == "groq" and config.GROQ_API_KEY:
        return GroqBackend(config.GROQ_API_KEY, config.GROQ_MODEL)
    if name == "ollama":
        return OllamaBackend(config.OLLAMA_URL, config.OLLAMA_MODEL)
    return None


class LLMRouter:
    """Try primary backend, fall back to secondary on failure."""

    def __init__(self):
        self.backends: list[tuple[str, LLMBackend]] = []
        for name in [config.LLM_PRIMARY, config.LLM_FALLBACK, "ollama"]:
            backend = _make_backend(name)
            if backend and not any(n == name for n, _ in self.backends):
                self.backends.append((name, backend))
        self.rate_limiter = RateLimiter(config.LLM_MAX_REQUESTS_PER_SECOND)

        if not self.backends:
            log.warning("No LLM backends configured! Check your .env file.")

    async def generate(self, messages: list[dict]) -> str:
        """Generate a response, trying each backend in order."""
        await self.rate_limiter.acquire()

        for name, backend in self.backends:
            try:
                log.info(f"Trying {name}...")
                result = await backend.generate(messages)
                log.info(f"{name} succeeded ({len(result)} chars)")
                return result
            except Exception as e:
                log.warning(f"{name} failed: {e}")
                continue

        return (
            "Sorry mate, my brain's a bit fried right now! \U0001F635 "
            "All my LLM backends are down. Try again in a mo — "
            "or use `/search` to find videos directly!"
        )


# Module-level singleton
router = LLMRouter()
