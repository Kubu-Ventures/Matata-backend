"""Vision analysis service — Stage 3 AI image quality and damage classification.

Defines the ``VisionProvider`` Protocol and four concrete implementations:

* ``MockVisionProvider``      — deterministic, configurable via fixtures; no API calls.
* ``OpenAIVisionProvider``    — GPT-4o with ``response_format={"type": "json_object"}``.
* ``AnthropicVisionProvider`` — Claude claude-opus-4-6 vision (optional alternative).
* ``OllamaVisionProvider``    — local open-source vision model via Ollama (no API key).

A factory ``get_vision_provider()`` selects the implementation from the
``VISION_PROVIDER`` environment variable.

Design notes
------------
* All providers are ``async``; the Celery task runs them in an async event loop
  via ``asyncio.run()``.
* The model is always instructed to respond **only** in a JSON object whose
  shape matches ``ImageAnalysisResult``.  Responses are parsed with Pydantic —
  never with string manipulation.
* The quality assessment and damage classification are batched into a **single**
  vision API call to avoid double API cost (spec §8.3).
* ``VisionProvider`` is a structural Protocol so new providers can be added
  without modifying existing code.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Result schema — Pydantic model for strict parsing
# ---------------------------------------------------------------------------

_VALID_QUALITY_FLAGS = {"usable", "borderline", "unusable"}
_VALID_SEVERITIES = {"minimal", "partial", "destroyed"}


class ImageAnalysisResult(BaseModel):
    """Parsed response from any VisionProvider.

    All fields are validated by Pydantic on construction.  The task layer
    never manipulates the raw model response directly.
    """

    quality_score: float = Field(..., ge=0.0, le=1.0)
    quality_flag: Literal["usable", "borderline", "unusable"]
    ai_severity_prediction: str
    ai_confidence: float = Field(..., ge=0.0, le=1.0)
    raw_response: dict = Field(default_factory=dict)

    @field_validator("ai_severity_prediction")
    @classmethod
    def validate_severity(cls, v: str) -> str:
        if v not in _VALID_SEVERITIES:
            raise ValueError(
                f"ai_severity_prediction must be one of {_VALID_SEVERITIES}, got '{v}'"
            )
        return v


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class VisionProvider(Protocol):
    """Structural interface for vision model back-ends."""

    async def analyse_damage_image(
        self,
        image_bytes: bytes,
        reporter_severity: str,
    ) -> ImageAnalysisResult:
        """Assess image quality and classify damage in a single model call.

        Args:
            image_bytes:       Raw JPEG/PNG image binary.
            reporter_severity: The reporter's own damage classification
                               (``minimal`` / ``partial`` / ``destroyed``).
                               Provided as context — the model must never
                               simply echo it back.

        Returns:
            Validated ``ImageAnalysisResult``.

        Raises:
            VisionAPIError: On any network, rate-limit, or API error.
        """
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------


class VisionAPIError(RuntimeError):
    """Raised when a vision provider fails to analyse an image."""


# ---------------------------------------------------------------------------
# Shared prompt — used by all real providers
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """\
You are an expert structural damage assessment AI for a humanitarian crisis \
mapping system.  You will be shown a photograph submitted by a community \
reporter after a disaster event.

Your task is to evaluate the image on TWO dimensions simultaneously:

1. IMAGE QUALITY
   - Is the primary subject a building or piece of infrastructure (road, bridge, \
utility pole)?
   - Is damage (or its absence) clearly visible in the frame?
   - Is the image adequately lit and in focus?

2. DAMAGE SEVERITY
   - minimal  — Structure is standing; minor cosmetic damage only.
   - partial  — Significant structural damage but the building is still \
recognisable and partially standing.
   - destroyed — Complete or near-complete collapse; rubble or total loss.

You MUST respond ONLY with a single JSON object — no prose, no markdown, no \
code fences — that matches this exact schema:

{
  "quality_score": <float 0.0–1.0>,
  "quality_flag": "<usable|borderline|unusable>",
  "ai_severity_prediction": "<minimal|partial|destroyed>",
  "ai_confidence": <float 0.0–1.0>
}

Definitions:
  quality_score: 0.0 = completely unusable; 1.0 = perfect quality.
  quality_flag:
    usable     — quality_score >= 0.6; image supports confident assessment.
    borderline — quality_score 0.3–0.59; assessment possible but uncertain.
    unusable   — quality_score < 0.3; image cannot support any assessment.
  ai_confidence: your confidence in ai_severity_prediction (0.0–1.0).

IMPORTANT: base your ai_severity_prediction on what you SEE, not on the \
reporter's classification.  They may be wrong.
"""


def _user_prompt(reporter_severity: str) -> str:
    return (
        f"The reporter classified this image as: {reporter_severity}.\n"
        "Assess the image independently and return only the JSON object."
    )


# ---------------------------------------------------------------------------
# MockVisionProvider — unit tests / CI
# ---------------------------------------------------------------------------


class MockVisionProvider:
    """Deterministic vision provider for unit tests and CI.

    Default behaviour returns a high-quality, high-confidence result with
    ``ai_severity_prediction`` matching whatever ``reporter_severity`` was
    passed, so that tests that do NOT want to trigger divergence work without
    extra configuration.

    For tests that need to control the result precisely, construct with
    explicit keyword arguments or pass a pre-built ``ImageAnalysisResult``
    via ``fixed_result``.

    Attributes:
        fixed_result:  If set, always returned verbatim (after recording call).
        quality_score: Returned quality_score (default 0.85).
        quality_flag:  Returned quality_flag (default ``"usable"``).
        severity:      Returned ai_severity_prediction. ``None`` means
                       mirror the reporter's input (no divergence).
        confidence:    Returned ai_confidence (default 0.9).
        call_count:    Number of times ``analyse_damage_image`` was called.
        raise_on_calls: Raise ``VisionAPIError`` on the first N calls, then
                        succeed.  Used to test retry logic.
        calls:         List of ``(image_bytes_len, reporter_severity)`` tuples.
    """

    def __init__(
        self,
        *,
        fixed_result: ImageAnalysisResult | None = None,
        quality_score: float = 0.85,
        quality_flag: Literal["usable", "borderline", "unusable"] = "usable",
        severity: str | None = None,
        confidence: float = 0.90,
        raise_on_calls: int = 0,
    ) -> None:
        self.fixed_result = fixed_result
        self.quality_score = quality_score
        self.quality_flag = quality_flag
        self.severity = severity
        self.confidence = confidence
        self.raise_on_calls = raise_on_calls

        self.call_count: int = 0
        self.calls: list[tuple[int, str]] = []

    async def analyse_damage_image(
        self,
        image_bytes: bytes,
        reporter_severity: str,
    ) -> ImageAnalysisResult:
        self.calls.append((len(image_bytes), reporter_severity))
        self.call_count += 1

        if self.call_count <= self.raise_on_calls:
            raise VisionAPIError(
                f"MockVisionProvider: simulated failure (call {self.call_count})"
            )

        if self.fixed_result is not None:
            return self.fixed_result

        effective_severity = (
            self.severity if self.severity is not None else reporter_severity
        )
        # Clamp severity to valid values in case reporter sent something odd.
        if effective_severity not in _VALID_SEVERITIES:
            effective_severity = "partial"

        return ImageAnalysisResult(
            quality_score=self.quality_score,
            quality_flag=self.quality_flag,
            ai_severity_prediction=effective_severity,
            ai_confidence=self.confidence,
            raw_response={"mock": True},
        )


# ---------------------------------------------------------------------------
# OpenAIVisionProvider — GPT-4o (production)
# ---------------------------------------------------------------------------


class OpenAIVisionProvider:
    """Production vision provider backed by OpenAI GPT-4o.

    Sends the image as a base64-encoded data URI in the ``image_url`` content
    block.  ``response_format={"type": "json_object"}`` forces the model to
    return valid JSON, eliminating the need for fence-stripping.

    Requires:
        ``pip install openai``
        ``OPENAI_API_KEY`` environment variable.
    """

    def __init__(
        self,
        model: str = "gpt-4o",
        max_tokens: int = 256,
        timeout: float = 30.0,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._timeout = timeout

    async def analyse_damage_image(
        self,
        image_bytes: bytes,
        reporter_severity: str,
    ) -> ImageAnalysisResult:
        """Call GPT-4o vision API and parse the structured JSON response.

        Args:
            image_bytes:       Raw image binary (JPEG recommended).
            reporter_severity: Reporter's damage classification.

        Returns:
            Validated ``ImageAnalysisResult``.

        Raises:
            VisionAPIError: On any OpenAI or network error.
        """
        try:
            from openai import AsyncOpenAI  # type: ignore[import]
        except ImportError as exc:
            raise VisionAPIError(
                "openai package is required for VISION_PROVIDER=openai. "
                "Install it with: pip install openai"
            ) from exc

        from app.core.config import settings  # deferred to avoid import-time env check

        api_key = getattr(settings, "OPENAI_API_KEY", None)
        if not api_key:
            raise VisionAPIError(
                "OPENAI_API_KEY must be set when VISION_PROVIDER=openai."
            )

        b64 = base64.b64encode(image_bytes).decode("utf-8")
        data_uri = f"data:image/jpeg;base64,{b64}"

        client = AsyncOpenAI(api_key=api_key, timeout=self._timeout)
        try:
            response = await client.chat.completions.create(
                model=self._model,
                max_tokens=self._max_tokens,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": data_uri, "detail": "low"},
                            },
                            {"type": "text", "text": _user_prompt(reporter_severity)},
                        ],
                    },
                ],
            )
        except Exception as exc:
            raise VisionAPIError(
                f"OpenAI vision API error: {type(exc).__name__}: {exc}"
            ) from exc

        raw_text = response.choices[0].message.content or "{}"
        logger.debug("OpenAI raw response: %s", raw_text[:500])

        try:
            raw_dict = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise VisionAPIError(
                f"OpenAI returned non-JSON response: {raw_text[:200]}"
            ) from exc

        try:
            return ImageAnalysisResult(raw_response=raw_dict, **raw_dict)
        except Exception as exc:
            raise VisionAPIError(
                f"OpenAI response failed schema validation: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# AnthropicVisionProvider — Claude claude-opus-4-6 (optional alternative)
# ---------------------------------------------------------------------------


class AnthropicVisionProvider:
    """Optional vision provider backed by Anthropic Claude claude-opus-4-6.

    Uses the Messages API with a base64-encoded image content block.  Instructs
    the model to respond only in JSON via the system prompt and ``prefill``
    technique (pre-filling the assistant turn with ``{``).

    Requires:
        ``pip install anthropic``
        ``ANTHROPIC_API_KEY`` environment variable.
    """

    def __init__(
        self,
        model: str = "claude-opus-4-6-20241101",
        max_tokens: int = 256,
        timeout: float = 30.0,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._timeout = timeout

    async def analyse_damage_image(
        self,
        image_bytes: bytes,
        reporter_severity: str,
    ) -> ImageAnalysisResult:
        """Call Claude vision API and parse the structured JSON response.

        Args:
            image_bytes:       Raw image binary.
            reporter_severity: Reporter's damage classification.

        Returns:
            Validated ``ImageAnalysisResult``.

        Raises:
            VisionAPIError: On any Anthropic or network error.
        """
        try:
            import anthropic  # type: ignore[import]
        except ImportError as exc:
            raise VisionAPIError(
                "anthropic package is required for VISION_PROVIDER=anthropic. "
                "Install it with: pip install anthropic"
            ) from exc

        from app.core.config import settings

        api_key = getattr(settings, "ANTHROPIC_API_KEY", None)
        if not api_key:
            raise VisionAPIError(
                "ANTHROPIC_API_KEY must be set when VISION_PROVIDER=anthropic."
            )

        b64 = base64.b64encode(image_bytes).decode("utf-8")

        client = anthropic.AsyncAnthropic(api_key=api_key, timeout=self._timeout)
        try:
            response = await client.messages.create(
                model=self._model,
                max_tokens=self._max_tokens,
                system=_SYSTEM_PROMPT,
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": b64,
                                },
                            },
                            {"type": "text", "text": _user_prompt(reporter_severity)},
                        ],
                    },
                    # Prefill: nudge Claude to start its response with '{'
                    # so it returns raw JSON without prose preamble.
                    {"role": "assistant", "content": "{"},
                ],
            )
        except Exception as exc:
            raise VisionAPIError(
                f"Anthropic vision API error: {type(exc).__name__}: {exc}"
            ) from exc

        # Reconstruct the full JSON: prefill '{' + model continuation.
        raw_text = "{" + (response.content[0].text if response.content else "")
        logger.debug("Anthropic raw response: %s", raw_text[:500])

        try:
            raw_dict = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise VisionAPIError(
                f"Anthropic returned non-JSON response: {raw_text[:200]}"
            ) from exc

        try:
            return ImageAnalysisResult(raw_response=raw_dict, **raw_dict)
        except Exception as exc:
            raise VisionAPIError(
                f"Anthropic response failed schema validation: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Shared JSON extraction helper
# ---------------------------------------------------------------------------

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)


def _extract_json(text: str) -> dict:
    """Return the first JSON object found in *text*.

    Handles three common model output styles:
    1. Raw JSON (ideal — what the system prompt requests).
    2. JSON wrapped in a ```json … ``` code fence.
    3. JSON buried after prose — extracts the first ``{…}`` block.

    Raises:
        VisionAPIError: If no valid JSON object can be extracted.
    """
    # Strip markdown fences if present.
    fence_match = _JSON_FENCE_RE.search(text)
    candidate = fence_match.group(1).strip() if fence_match else text.strip()

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # Last resort: find the outermost {...} substring.
    start = candidate.find("{")
    end = candidate.rfind("}") + 1
    if start != -1 and end > start:
        try:
            return json.loads(candidate[start:end])
        except json.JSONDecodeError:
            pass

    raise VisionAPIError(f"Could not extract JSON from model response: {text[:300]}")


# ---------------------------------------------------------------------------
# OllamaVisionProvider — local open-source vision model (free, no API key)
# ---------------------------------------------------------------------------


class OllamaVisionProvider:
    """Vision provider backed by a local Ollama model (e.g. llava, llama3.2-vision).

    Uses Ollama's OpenAI-compatible ``/v1`` endpoint so no additional SDK is
    required beyond the ``openai`` package that is already the optional dep for
    ``OpenAIVisionProvider``.

    Recommended models (pull before use):
        ollama pull llava               # 7 B, fast
        ollama pull llava:13b           # 13 B, more accurate
        ollama pull llama3.2-vision     # Meta multimodal, strong instruction-following

    Requires:
        ``pip install openai``
        Ollama running locally — https://ollama.com
        ``OLLAMA_BASE_URL`` env var (default ``http://localhost:11434``).
        ``OLLAMA_VISION_MODEL`` env var (default ``llava``).
    """

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "llava",
        max_tokens: int = 512,
        timeout: float = 60.0,
    ) -> None:
        # Normalise: Ollama's OpenAI-compat endpoint lives at /v1.
        self._base_url = base_url.rstrip("/") + "/v1"
        self._model = model
        self._max_tokens = max_tokens
        self._timeout = timeout

    async def analyse_damage_image(
        self,
        image_bytes: bytes,
        reporter_severity: str,
    ) -> ImageAnalysisResult:
        """Call a local Ollama vision model and parse the structured JSON response.

        Args:
            image_bytes:       Raw image binary (JPEG recommended).
            reporter_severity: Reporter's damage classification.

        Returns:
            Validated ``ImageAnalysisResult``.

        Raises:
            VisionAPIError: On any connection or parsing error.
        """
        try:
            from openai import AsyncOpenAI  # type: ignore[import]
        except ImportError as exc:
            raise VisionAPIError(
                "openai package is required for VISION_PROVIDER=ollama. "
                "Install it with: pip install openai"
            ) from exc

        b64 = base64.b64encode(image_bytes).decode("utf-8")
        data_uri = f"data:image/jpeg;base64,{b64}"

        # Ollama does not enforce an API key; the openai client requires a
        # non-empty string, so we pass the conventional placeholder.
        client = AsyncOpenAI(
            base_url=self._base_url,
            api_key="ollama",
            timeout=self._timeout,
        )

        try:
            response = await client.chat.completions.create(
                model=self._model,
                max_tokens=self._max_tokens,
                messages=[
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": data_uri},
                            },
                            {"type": "text", "text": _user_prompt(reporter_severity)},
                        ],
                    },
                ],
            )
        except Exception as exc:
            raise VisionAPIError(
                f"Ollama vision error ({self._base_url}, model={self._model}): "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        raw_text = response.choices[0].message.content or "{}"
        logger.debug("Ollama raw response: %s", raw_text[:500])

        try:
            raw_dict = _extract_json(raw_text)
        except VisionAPIError:
            raise VisionAPIError(
                f"Ollama ({self._model}) returned non-JSON response: {raw_text[:200]}"
            )

        try:
            return ImageAnalysisResult(raw_response=raw_dict, **raw_dict)
        except Exception as exc:
            raise VisionAPIError(
                f"Ollama response failed schema validation: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# FallbackVisionProvider — automatic provider chain
# ---------------------------------------------------------------------------


class FallbackVisionProvider:
    """Tries a list of providers in sequence, moving to the next on failure.

    If a provider raises ``VisionAPIError`` (network issue, missing API key,
    unavailable model, etc.) the next provider in the chain is tried.
    If ALL providers fail the last ``VisionAPIError`` is re-raised.

    Used by the factory to give real providers an automatic Ollama backup
    so that image analysis never silently falls back to deterministic mock data.
    """

    def __init__(self, providers: list) -> None:
        if not providers:
            raise ValueError("FallbackVisionProvider requires at least one provider.")
        self._providers = providers

    async def analyse_damage_image(
        self,
        image_bytes: bytes,
        reporter_severity: str,
    ) -> ImageAnalysisResult:
        last_exc: VisionAPIError | None = None
        for provider in self._providers:
            try:
                return await provider.analyse_damage_image(
                    image_bytes, reporter_severity
                )
            except VisionAPIError as exc:
                logger.warning(
                    "Vision provider %s failed, trying next in chain: %s",
                    type(provider).__name__,
                    exc,
                )
                last_exc = exc
        raise VisionAPIError(
            f"All {len(self._providers)} vision providers failed. "
            f"Last error: {last_exc}"
        ) from last_exc


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_vision_provider() -> VisionProvider:
    """Return the vision provider selected by ``VISION_PROVIDER``.

    Supported values:
    * ``mock``      — ``MockVisionProvider`` (dev/CI only — returns deterministic
                      fake data; NEVER use in production).
    * ``openai``    — GPT-4o with automatic Ollama fallback.
    * ``anthropic`` — Claude claude-opus-4-6 with automatic Ollama fallback.
    * ``ollama``    — Local open-source model only (free, no API key required).

    For ``openai`` and ``anthropic``, if the primary call fails (rate limit,
    missing key, network error) the request is retried transparently against
    the local Ollama instance before propagating any error.

    Returns:
        An object satisfying the ``VisionProvider`` Protocol.

    Raises:
        ValueError: If ``VISION_PROVIDER`` is set to an unknown value.
    """
    from app.core.config import settings  # deferred — safe for test import order

    provider_name = getattr(settings, "VISION_PROVIDER", "mock").lower()

    def _ollama() -> OllamaVisionProvider:
        return OllamaVisionProvider(
            base_url=getattr(settings, "OLLAMA_BASE_URL", "http://localhost:11434"),
            model=getattr(settings, "OLLAMA_VISION_MODEL", "llava"),
        )

    if provider_name == "mock":
        return MockVisionProvider()
    if provider_name == "ollama":
        return _ollama()
    if provider_name == "openai":
        return FallbackVisionProvider([OpenAIVisionProvider(), _ollama()])
    if provider_name == "anthropic":
        return FallbackVisionProvider([AnthropicVisionProvider(), _ollama()])

    raise ValueError(
        f"Unknown VISION_PROVIDER value: '{provider_name}'. "
        "Supported options: 'mock', 'openai', 'anthropic', 'ollama'."
    )
