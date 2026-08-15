import asyncio
import json
import re
import time
from typing import Any

import httpx

from app.core.config import settings
from app.schemas.incident import IncidentAnalysis
from app.schemas.telemetry import ConfidenceScore


SYSTEM_PROMPT = (
    "You are an on-call assistant for developers. Return strict JSON with keys: "
    "root_cause, patch, confidence. Confidence must be an array of objects with label and value. "
    "Prioritize indexed_symbols, primary_symbol, graph_neighborhoods, and resolved_error when forming the answer. "
    "Explain the likely failure around the indexed symbols before proposing a patch."
)


def _prepare_model_input(context: dict[str, Any]) -> dict[str, Any]:
    indexed_symbols = []
    for symbol in context.get("indexed_symbols", [])[:5]:
        indexed_symbols.append(
            {
                "symbol_id": symbol.get("symbol_id"),
                "source": symbol.get("source"),
                "score": symbol.get("score"),
                "content": str(symbol.get("content", ""))[:400],
            }
        )

    graph_neighborhoods = []
    for neighborhood in context.get("graph_neighborhoods", [])[:3]:
        graph_neighborhoods.append(
            {
                "symbol_id": neighborhood.get("symbol_id"),
                "upstream": neighborhood.get("upstream", []),
                "downstream": neighborhood.get("downstream", []),
            }
        )

    return {
        "incident": context.get("fingerprint", {}),
        "retrieval": {
            "semantic_query": context.get("semantic_query"),
            "primary_symbol": context.get("primary_symbol"),
            "indexed_symbols": indexed_symbols,
            "graph_neighborhoods": graph_neighborhoods,
            "resolved_error": context.get("resolved_error"),
        },
        "bounds": {
            "graph_depth": context.get("graph_depth"),
            "token_budget": context.get("token_budget"),
            "deployment_window": context.get("deployment_window"),
            "confidence_threshold": context.get("confidence_threshold"),
        },
        "response_contract": {
            "root_cause": "Explain the failure using the retrieved indexed symbols.",
            "patch": "Provide a concrete Rust-oriented fix or the next validation step.",
            "confidence": "Array of label/value pairs describing retrieval, graph, and model confidence.",
        },
    }


def _fallback_analysis(context: dict[str, Any], reason: str) -> IncidentAnalysis:
    fingerprint = context.get("fingerprint", {})
    service = fingerprint.get("service", "unknown-service")
    top_frame = fingerprint.get("top_frame", "unknown-frame")
    primary_symbol = context.get("primary_symbol") or "no indexed symbol"
    return IncidentAnalysis(
        root_cause=(
            f"Initial triage suggests {service} is failing near {top_frame}. "
            f"Primary indexed symbol: {primary_symbol}. "
            f"Using deterministic fallback because model inference was unavailable: {reason}."
        ),
        patch="// TODO: add a verified Rust patch after sandbox execution is wired",
        confidence=[
            ConfidenceScore(label="Retrieval", value="0.35"),
            ConfidenceScore(label="Graph", value="0.20"),
            ConfidenceScore(label="LLM", value="Unavailable"),
        ],
        context=context,
    )


def _extract_json_payload(content: str) -> dict[str, Any]:
    if not content or not content.strip():
        raise ValueError("Empty model response")

    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    fenced_match = re.search(r"```json\s*(\{.*?\})\s*```", content, re.DOTALL)
    if fenced_match:
        return json.loads(fenced_match.group(1))

    object_match = re.search(r"(\{.*\})", content, re.DOTALL)
    if not object_match:
        raise ValueError("No JSON object found in model response")

    candidate = object_match.group(1).strip()
    candidate = re.sub(r",\s*([}\]])", r"\1", candidate)
    return json.loads(candidate)


def _build_analysis(payload: dict[str, Any], context: dict[str, Any]) -> IncidentAnalysis:
    confidence_items = []
    for item in payload.get("confidence", []):
        if isinstance(item, dict):
            confidence_items.append(
                ConfidenceScore(
                    label=str(item.get("label", "Unknown")),
                    value=str(item.get("value", "0.0")),
                )
            )

    return IncidentAnalysis(
        root_cause=str(payload.get("root_cause", "Model did not provide a root cause.")),
        patch=str(payload.get("patch", "// TODO: model did not provide a patch")),
        confidence=confidence_items or [ConfidenceScore(label="LLM", value="0.50")],
        context=context,
    )


def _chat_completions_path(base_url: str) -> tuple[str, str]:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        prefix = normalized[: -len("/chat/completions")]
        return prefix or normalized, "/chat/completions"
    return normalized, "/chat/completions"


class HostedModelClient:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._path: str | None = None

    def _get_client(self) -> httpx.AsyncClient | None:
        if not settings.litellm_base_url or not settings.litellm_api_key:
            return None

        if self._client is not None:
            return self._client

        base_url, path = _chat_completions_path(settings.litellm_base_url)
        self._path = path
        self._client = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {settings.litellm_api_key}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(connect=15.0, read=60.0, write=10.0, pool=5.0),
        )
        return self._client

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
            self._path = None

    async def test_connection(self) -> bool:
        client = self._get_client()
        if client is None or self._path is None:
            return False

        response = await client.post(
            self._path,
            json={
                "model": settings.litellm_model,
                "messages": [{"role": "user", "content": "Hello"}],
                "max_tokens": 8,
            },
            timeout=30,
        )
        response.raise_for_status()
        return True

    async def analyze(
        self,
        context: dict[str, Any],
        *,
        temperature: float = 0.0,
        max_attempts: int = 3,
        timeout_seconds: int = 60,
    ) -> IncidentAnalysis:
        client = self._get_client()
        if client is None or self._path is None:
            return _fallback_analysis(context, "missing LiteLLM configuration")

        payload = {
            "model": settings.litellm_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(_prepare_model_input(context))},
            ],
            "temperature": temperature,
            "top_p": 0.1,
            "seed": 42,
            "response_format": {"type": "json_object"},
        }

        start_time = time.monotonic()
        backoff = 1.0
        last_error = "unknown error"

        for attempt in range(1, max_attempts + 1):
            if time.monotonic() - start_time > timeout_seconds:
                return _fallback_analysis(context, f"time budget exceeded after {attempt - 1} attempts")

            try:
                response = await client.post(self._path, json=payload)
                response.raise_for_status()
                body = response.json()
                content = (
                    body.get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                )
                parsed = _extract_json_payload(content)
                return _build_analysis(parsed, context)
            except (httpx.HTTPError, json.JSONDecodeError, ValueError, KeyError, IndexError) as exc:
                last_error = str(exc)
                if attempt == max_attempts:
                    break
                await asyncio.sleep(backoff)
                backoff *= 2

        return _fallback_analysis(context, last_error)

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> str:
        client = self._get_client()
        if client is None or self._path is None:
            raise RuntimeError("missing LiteLLM configuration")

        response = await client.post(
            self._path,
            json={
                "model": settings.litellm_model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
        )
        response.raise_for_status()

        body = response.json()
        message = body.get("choices", [{}])[0].get("message", {})
        content = message.get("content")
        if not content:
            provider_specific = message.get("provider_specific_fields", {})
            content = message.get("reasoning_content") or provider_specific.get("reasoning_content")
        if not content:
            raise ValueError("model returned empty chat response")
        return str(content)


hosted_model_client = HostedModelClient()