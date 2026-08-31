"""Pluggable LLM backends for job scoring, resume tailoring, and form analysis."""

import os
import subprocess
import json
import re
from abc import ABC, abstractmethod

import httpx


def _resolve_env(value):
    """Resolve config values written as ${ENV_VAR}."""
    if isinstance(value, str) and value.startswith("${") and value.endswith("}"):
        return os.getenv(value[2:-1], "")
    return value


class LLMBackend(ABC):
    @abstractmethod
    def ask(self, prompt: str, timeout: int = 120) -> str:
        ...

    def ask_json(self, prompt: str, timeout: int = 120) -> dict:
        full_prompt = prompt + (
            "\n\nIMPORTANT: Respond ONLY with valid JSON. "
            "No markdown fencing, no explanation, no preamble. Just the JSON object."
        )
        raw = self.ask(full_prompt, timeout=timeout)
        cleaned = raw.strip()
        cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
        cleaned = re.sub(r'\s*```$', '', cleaned).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as exc:
            raise ValueError(f"LLM didn't return valid JSON: {exc}\nRaw: {raw[:500]}")


class ClaudeCLIBackend(LLMBackend):
    def __init__(self, config: dict = None):
        self.config = config or {}
        self.default_timeout = self.config.get("timeout", 120)

    def ask(self, prompt: str, timeout: int = None) -> str:
        timeout = timeout or self.default_timeout
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = subprocess.run(
            ["claude", "-p", "--output-format", "json"],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        if result.returncode != 0:
            raise RuntimeError(f"Claude CLI error: {result.stderr.strip() or 'Unknown error'}")
        try:
            data = json.loads(result.stdout)
            return data.get("result", result.stdout)
        except json.JSONDecodeError:
            return result.stdout.strip()


class OpenAIBackend(LLMBackend):
    """OpenAI Responses API backend using an API key from the environment."""

    def __init__(self, config: dict = None):
        self.config = config or {}
        self.api_key = _resolve_env(self.config.get("api_key", "${OPENAI_API_KEY}"))
        self.model = self.config.get("model", "gpt-4o-mini")
        self.default_timeout = int(self.config.get("timeout", 120))
        if not self.api_key:
            raise RuntimeError("OpenAI backend selected but OPENAI_API_KEY is not set.")

    @staticmethod
    def _extract_text(payload: dict) -> str:
        # Responses API raw JSON contains output -> message -> content -> output_text.
        chunks = []
        for item in payload.get("output", []) or []:
            for content in item.get("content", []) or []:
                if content.get("type") in {"output_text", "text"} and content.get("text"):
                    chunks.append(content["text"])
        if chunks:
            return "\n".join(chunks).strip()
        # Defensive compatibility with wrappers that expose output_text directly.
        if isinstance(payload.get("output_text"), str):
            return payload["output_text"].strip()
        raise RuntimeError(f"OpenAI response contained no text output: {str(payload)[:500]}")

    def ask(self, prompt: str, timeout: int = None) -> str:
        timeout = timeout or self.default_timeout
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                "https://api.openai.com/v1/responses",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": self.model,
                    "input": prompt,
                },
            )
            if response.status_code >= 400:
                raise RuntimeError(
                    f"OpenAI API error {response.status_code}: {response.text[:500]}"
                )
            return self._extract_text(response.json())


_BACKENDS = {
    "claude_cli": ClaudeCLIBackend,
    "openai": OpenAIBackend,
}

_backend_cache: dict[str, LLMBackend] = {}


def get_backend(component: str, profile: dict) -> LLMBackend:
    ai_config = profile.get("ai", {})
    backend_name = ai_config.get("components", {}).get(
        component, ai_config.get("default_backend", "claude_cli")
    )
    backend_config = ai_config.get("backends", {}).get(backend_name, {})
    cache_key = f"{backend_name}:{hash(json.dumps(backend_config, sort_keys=True, default=str))}"
    if cache_key in _backend_cache:
        return _backend_cache[cache_key]

    backend_class = _BACKENDS.get(backend_name)
    if not backend_class:
        raise RuntimeError(f"Unknown LLM backend: {backend_name}")

    instance = backend_class(backend_config)
    _backend_cache[cache_key] = instance
    return instance


def clear_backend_cache():
    _backend_cache.clear()
