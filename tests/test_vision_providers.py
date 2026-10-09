"""Tests for the real vision providers' requests and response handling.

The Anthropic tests run the real ``anthropic`` SDK against a mock HTTP
transport, so the request body is exactly what would go over the wire.
The OpenAI tests use a stub ``openai`` module (the package is optional).
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import httpx2
import pytest

from app.core.config import settings
from app.services.vision_service import (
    AnthropicVisionProvider,
    OpenAIVisionProvider,
    VisionAPIError,
    _user_prompt,
)

_RESULT = {
    "quality_score": 0.8,
    "quality_flag": "usable",
    "ai_severity_prediction": "partial",
    "ai_confidence": 0.7,
}


def _anthropic_response(
    content: list[dict], stop_reason: str = "end_turn"
) -> dict[str, Any]:
    return {
        "id": "msg_test",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 10},
    }


def _run_anthropic(
    provider: AnthropicVisionProvider, response_body: dict
) -> tuple[Any, dict]:
    """Run the provider with the real SDK over a mock transport."""
    seen: dict = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, json=response_body)

    real_client = anthropic.AsyncAnthropic

    def client_factory(**kwargs: Any) -> anthropic.AsyncAnthropic:
        transport = httpx2.MockTransport(handler)
        http_client = anthropic.DefaultAsyncHttpxClient(transport=transport)
        return real_client(http_client=http_client, **kwargs)

    with (
        patch.object(settings, "ANTHROPIC_API_KEY", "test-key"),
        patch("anthropic.AsyncAnthropic", client_factory),
    ):
        try:
            result: Any = asyncio.run(
                provider.analyse_damage_image(
                    b"img",
                    "destroyed",
                    crisis_type="flood",
                    infrastructure_type="residential",
                )
            )
        except VisionAPIError as exc:
            result = exc
    return result, seen


class TestUserPrompt:
    def test_includes_context_but_not_reporter_severity(self) -> None:
        prompt = _user_prompt("flood", "residential")
        assert "flood" in prompt
        assert "residential" in prompt
        for severity in ("minimal", "partial", "destroyed"):
            assert severity not in prompt

    def test_without_context(self) -> None:
        assert "JSON" in _user_prompt()


class TestAnthropicVisionProvider:
    def test_request_uses_structured_output_and_no_prefill(self) -> None:
        body = _anthropic_response(
            [
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "text", "text": json.dumps(_RESULT)},
            ]
        )
        result, seen = _run_anthropic(
            AnthropicVisionProvider(model="claude-opus-5-5"), body
        )

        assert result.ai_severity_prediction == "partial"
        assert result.ai_confidence == pytest.approx(0.7)

        request = seen["body"]
        assert request["model"] == "claude-opus-5-5"
        # Current models reject an assistant prefill: one user turn only.
        assert [m["role"] for m in request["messages"]] == ["user"]
        assert request["output_config"]["format"]["type"] == "json_schema"
        assert request["output_config"]["effort"] == "low"
        # The reporter's own severity anchors the model; it must not be sent.
        assert "destroyed" not in json.dumps(request["messages"])
        assert "flood" in json.dumps(request["messages"])

    def test_refusal_fallback_enabled_for_supported_model(self) -> None:
        body = _anthropic_response([{"type": "text", "text": json.dumps(_RESULT)}])
        _, seen = _run_anthropic(AnthropicVisionProvider(model="claude-opus-5-5"), body)
        assert seen["body"]["fallbacks"] == "default"
        assert "server-side-fallback-2026-07-01" in seen["headers"]["anthropic-beta"]

    def test_no_fallback_for_model_without_support(self) -> None:
        body = _anthropic_response([{"type": "text", "text": json.dumps(_RESULT)}])
        _, seen = _run_anthropic(
            AnthropicVisionProvider(model="claude-haiku-5-5"), body
        )
        assert "fallbacks" not in seen["body"]
        assert "anthropic-beta" not in seen["headers"]

    def test_model_defaults_to_setting(self) -> None:
        with patch.object(settings, "ANTHROPIC_VISION_MODEL", "claude-haiku-5-5"):
            assert AnthropicVisionProvider()._model == "claude-haiku-5-5"

    def test_refusal_raises_vision_error(self) -> None:
        body = _anthropic_response([], stop_reason="refusal")
        result, _ = _run_anthropic(AnthropicVisionProvider(), body)
        assert isinstance(result, VisionAPIError)

    def test_invalid_answer_raises_vision_error(self) -> None:
        bad = dict(_RESULT, ai_severity_prediction="catastrophic")
        body = _anthropic_response([{"type": "text", "text": json.dumps(bad)}])
        result, _ = _run_anthropic(AnthropicVisionProvider(), body)
        assert isinstance(result, VisionAPIError)

    def test_missing_api_key_raises_vision_error(self) -> None:
        with patch.object(settings, "ANTHROPIC_API_KEY", ""):
            with pytest.raises(VisionAPIError):
                asyncio.run(
                    AnthropicVisionProvider().analyse_damage_image(b"x", "partial")
                )


class TestOpenAIVisionProvider:
    def test_request_uses_high_detail_and_omits_reporter_severity(self) -> None:
        create = AsyncMock(
            return_value=MagicMock(
                choices=[MagicMock(message=MagicMock(content=json.dumps(_RESULT)))]
            )
        )
        client = MagicMock()
        client.chat.completions.create = create
        stub = types.ModuleType("openai")
        stub.AsyncOpenAI = MagicMock(return_value=client)  # type: ignore[attr-defined]

        with (
            patch.dict(sys.modules, {"openai": stub}),
            patch.object(settings, "OPENAI_API_KEY", "test-key"),
        ):
            result = asyncio.run(
                OpenAIVisionProvider().analyse_damage_image(
                    b"img", "destroyed", crisis_type="flood"
                )
            )

        assert result.ai_severity_prediction == "partial"
        messages = create.await_args.kwargs["messages"]
        image_part, text_part = messages[1]["content"]
        assert image_part["image_url"]["detail"] == "high"
        assert "flood" in text_part["text"]
        # The system prompt defines the severity levels; the user turn must
        # not carry the reporter's own choice.
        assert "destroyed" not in json.dumps(messages[1])
