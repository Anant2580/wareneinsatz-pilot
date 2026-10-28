"""Language model clients: one request in, the model's JSON reply out.

Two kinds of server, both usable for free:

- Ollama (default). Runs open models such as Qwen3-VL on a laptop or a server, nothing leaves
  the machine. Native API /api/chat; `format` takes a JSON schema (structured output), so the
  reply is always JSON with exactly the expected keys.
- Any OpenAI-compatible server: llama.cpp's llama-server, LM Studio, or a cloud API with a
  free tier. /chat/completions with response_format = json_schema.

Requests use temperature 0 and a fixed seed, so the same document gives the same answer (as
far as the server allows) and evaluation runs stay comparable.
"""

from __future__ import annotations

import base64
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

MAX_OUTPUT_TOKENS = 4096  # a 40-line invoice needs about 2,500


class LlmError(Exception):
    """The server answered with an error or an unusable reply (this document failed)."""


class LlmUnavailable(LlmError):
    """Server not reachable, model not installed or key rejected: stop instead of failing every document."""


@dataclass(frozen=True)
class LlmReply:
    content: str  # the JSON text
    model: str
    seconds: float  # wall time of the request
    prompt_tokens: int | None = None  # tokens read (text + images)
    output_tokens: int | None = None  # tokens written
    generation_seconds: float | None = None  # time spent writing, if the server reports it

    @property
    def tokens_per_second(self) -> float | None:
        if self.output_tokens and self.generation_seconds:
            return self.output_tokens / self.generation_seconds
        return None


class LlmClient(Protocol):
    model: str
    name: str  # for reports, e.g. "ollama:qwen3-vl:8b-instruct"

    def check(self) -> None: ...

    def complete(self, *, system: str, user: str, images: list[bytes], schema: dict[str, Any]) -> LlmReply: ...


class OllamaClient:
    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "qwen3-vl:8b-instruct",
        *,
        timeout: float = 600,
        num_ctx: int = 12288,  # context window: page image + text + reply fit with room to spare
        transport: httpx.BaseTransport | None = None,  # tests replace the network
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.name = f"ollama:{model}"
        self.num_ctx = num_ctx
        self.timeout = timeout
        self._http = httpx.Client(
            base_url=self.base_url, timeout=httpx.Timeout(timeout, connect=5.0), transport=transport
        )

    def check(self) -> None:
        """Is Ollama running, and is the model downloaded?"""
        try:
            response = self._http.get("/api/tags")
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise LlmUnavailable(
                f"Ollama is not reachable at {self.base_url} ({error}). Open the Ollama app or run: ollama serve"
            ) from error
        installed = {entry.get("name") for entry in response.json().get("models", [])}
        if self.model not in installed and f"{self.model}:latest" not in installed:
            raise LlmUnavailable(f"The model {self.model} is not downloaded. Run: ollama pull {self.model}")

    def complete(self, *, system: str, user: str, images: list[bytes], schema: dict[str, Any]) -> LlmReply:
        message: dict[str, Any] = {"role": "user", "content": user}
        if images:
            message["images"] = [base64.b64encode(image).decode("ascii") for image in images]
        body = {
            "model": self.model,
            "stream": False,
            "format": schema,  # structured output: the reply must match this JSON schema
            "think": False,  # no hidden reasoning tokens: faster, and the task needs reading, not thinking
            "keep_alive": "15m",  # keep the model in memory between documents
            "options": {"temperature": 0, "seed": 0, "num_ctx": self.num_ctx, "num_predict": MAX_OUTPUT_TOKENS},
            "messages": [{"role": "system", "content": system}, message],
        }
        started = time.perf_counter()
        response = _post(self._http, "/api/chat", body, self.base_url, self.timeout)
        seconds = time.perf_counter() - started
        if response.status_code == 404:
            raise LlmUnavailable(f"The model {self.model} is not downloaded. Run: ollama pull {self.model}")
        if response.status_code >= 400:
            raise LlmError(f"Ollama answered {response.status_code}: {response.text[:300]}")
        data = response.json()
        if data.get("done_reason") == "length":
            raise LlmError(f"The reply was cut off after {MAX_OUTPUT_TOKENS} tokens")
        eval_ns = data.get("eval_duration")
        return LlmReply(
            content=data["message"]["content"],
            model=self.model,
            seconds=seconds,
            prompt_tokens=data.get("prompt_eval_count"),
            output_tokens=data.get("eval_count"),
            generation_seconds=eval_ns / 1e9 if eval_ns else None,
        )


class OpenAICompatibleClient:
    """For llama-server (base URL http://localhost:8080/v1), LM Studio, or a cloud API's free tier.

    Free cloud tiers have rate limits and terms of their own; check what they do with the data
    before sending real invoices (see docs/decisions.md).
    """

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        api_key: str | None = None,
        timeout: float = 600,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.name = f"openai-compatible:{model}"
        self.timeout = timeout
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.Client(
            base_url=self.base_url, headers=headers, timeout=httpx.Timeout(timeout, connect=5.0), transport=transport
        )

    def check(self) -> None:
        try:
            response = self._http.get("/models")
        except httpx.HTTPError as error:
            raise LlmUnavailable(f"The model server is not reachable at {self.base_url} ({error})") from error
        if response.status_code in (401, 403):
            raise LlmUnavailable("The API key was rejected (LLM_API_KEY in .env)")

    def complete(self, *, system: str, user: str, images: list[bytes], schema: dict[str, Any]) -> LlmReply:
        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        for image in images:
            url = "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")
            content.append({"type": "image_url", "image_url": {"url": url}})
        body = {
            "model": self.model,
            "temperature": 0,
            "seed": 0,
            "max_tokens": MAX_OUTPUT_TOKENS,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": content}],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "invoice", "schema": schema, "strict": True},
            },
        }
        started = time.perf_counter()
        response = _post(self._http, "/chat/completions", body, self.base_url, self.timeout)
        seconds = time.perf_counter() - started
        if response.status_code in (401, 403):
            raise LlmUnavailable("The API key was rejected (LLM_API_KEY in .env)")
        if response.status_code == 429:
            raise LlmUnavailable("Rate limit reached (free tiers allow only a few requests per minute or day)")
        if response.status_code >= 400:
            raise LlmError(f"The model server answered {response.status_code}: {response.text[:300]}")
        data = response.json()
        choice = data["choices"][0]
        if choice.get("finish_reason") == "length":
            raise LlmError(f"The reply was cut off after {MAX_OUTPUT_TOKENS} tokens")
        usage = data.get("usage") or {}
        return LlmReply(
            content=choice["message"]["content"],
            model=self.model,
            seconds=seconds,
            prompt_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )


def _post(http: httpx.Client, path: str, body: dict[str, Any], base_url: str, timeout: float) -> httpx.Response:
    try:
        return http.post(path, json=body)
    except httpx.ConnectTimeout as error:  # checked first: it is also a TimeoutException
        raise LlmUnavailable(f"The model server is not reachable at {base_url} ({error})") from error
    except httpx.TimeoutException as error:
        raise LlmError(f"No answer within {timeout:.0f} s") from error
    except httpx.TransportError as error:
        raise LlmUnavailable(f"The model server is not reachable at {base_url} ({error})") from error
