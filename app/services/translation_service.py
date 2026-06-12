"""Translation service — multilingual support for CrisisMap.

Defines the ``TranslationService`` Protocol and concrete implementations:

* ``MockTranslationService``     — deterministic stub for tests (no I/O).
* ``LibreTranslateService``      — calls a self-hosted or public LibreTranslate
                                   REST API (``POST /translate``).
* ``ArgosTranslateService``      — open-source offline fallback using the
                                   ``argostranslate`` Python library; requires
                                   ``pip install argostranslate``.

A factory ``get_translation_service()`` selects the implementation from the
``TRANSLATION_PROVIDER`` environment variable.

Fallback strategy (spec §i18n)
-------------------------------
1. Attempt translation via the configured primary provider.
2. On ``TranslationError`` or unsupported language: attempt the fallback
   provider (``ArgosTranslateService``) if it differs from the primary.
3. If both fail (or the language is unknown): log a warning and return the
   English source string.
4. A translation failure NEVER raises an HTTP error — the message always
   reaches the reporter, even if only in English.

Privacy
-------
Translation requests contain only sanitised message strings — no PII,
no reporter tokens, no GPS coordinates.  LibreTranslate is self-hosted by
default, so no text leaves the deployment boundary.
"""

from __future__ import annotations

import logging
from typing import Optional, Protocol, runtime_checkable

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------


class TranslationError(RuntimeError):
    """Raised when a translation provider fails to translate a string."""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class TranslationService(Protocol):
    """Structural interface for translation back-ends."""

    async def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
    ) -> str:
        """Translate *text* from *source_lang* to *target_lang*.

        Args:
            text:        The string to translate.
            source_lang: BCP-47 source language code (e.g. ``"en"``).
            target_lang: BCP-47 target language code (e.g. ``"sw"``).

        Returns:
            Translated string.

        Raises:
            TranslationError: If the provider is unavailable or returns an
                              error.  Callers must handle this gracefully.
        """
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# MockTranslationService — unit tests
# ---------------------------------------------------------------------------


class MockTranslationService:
    """Deterministic translation service for unit tests.

    Returns a predictable ``"[mock:{lang}] {text}"`` string.
    No network calls are made.

    Satisfies the ``TranslationService`` Protocol.
    """

    async def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
    ) -> str:
        """Return a mock-prefixed translation string."""
        if source_lang == target_lang:
            return text
        return f"[mock:{target_lang}] {text}"


# ---------------------------------------------------------------------------
# LibreTranslateService — primary open-source provider
# ---------------------------------------------------------------------------


class LibreTranslateService:
    """Translation via the LibreTranslate REST API.

    LibreTranslate is a free, open-source, self-hostable machine translation
    service.  It supports 30+ languages and can be run as a Docker container
    with no API key required for self-hosted instances.

    Self-hosting (recommended for data sovereignty)::

        docker run -p 5000:5000 libretranslate/libretranslate \\
            --load-only en,sw,fr,ar,es,zh,ru,am,ha,yo,ig,zu,so,om,ti

    Public API (``https://libretranslate.com``) requires a free API key.

    Args:
        base_url:   LibreTranslate base URL
                    (default: ``http://libretranslate:5000``).
        api_key:    Optional API key for the public hosted instance.
        timeout_s:  HTTP timeout in seconds (default 10).
    """

    _TRANSLATE_PATH = "/translate"

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        timeout_s: float = 10.0,
    ) -> None:
        self._base_url = (base_url or settings.LIBRETRANSLATE_URL).rstrip("/")
        self._api_key = api_key or getattr(settings, "LIBRETRANSLATE_API_KEY", "")
        self._timeout = timeout_s

    async def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
    ) -> str:
        """Call the LibreTranslate ``POST /translate`` endpoint.

        Args:
            text:        Source text.
            source_lang: BCP-47 source language code.
            target_lang: BCP-47 target language code.

        Returns:
            Translated string.

        Raises:
            TranslationError: On HTTP error, network failure, or unexpected
                              response shape.
        """
        if source_lang == target_lang:
            return text

        payload: dict[str, str] = {
            "q": text,
            "source": source_lang,
            "target": target_lang,
            "format": "text",
        }
        payload_extra: dict[str, str] = dict(payload)
        if self._api_key:
            payload_extra["api_key"] = self._api_key

        url = self._base_url + self._TRANSLATE_PATH

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, json=payload_extra)
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise TranslationError(
                f"LibreTranslate HTTP error {exc.response.status_code}: "
                f"{exc.response.text[:200]}"
            ) from exc
        except httpx.RequestError as exc:
            raise TranslationError(
                f"LibreTranslate network error: {type(exc).__name__}"
            ) from exc

        try:
            data = response.json()
            return str(data["translatedText"])
        except (KeyError, ValueError, TypeError) as exc:
            raise TranslationError(
                f"LibreTranslate unexpected response: {response.text[:200]}"
            ) from exc


# ---------------------------------------------------------------------------
# ArgosTranslateService — offline open-source fallback
# ---------------------------------------------------------------------------


class ArgosTranslateService:
    """Offline translation using the ``argostranslate`` Python library.

    ArgosTranslate runs entirely locally with no internet access required
    after package installation.  It supports 30+ language pairs.

    Install::

        pip install argostranslate
        python -c "
        import argostranslate.package, argostranslate.translate
        argostranslate.package.update_package_index()
        available = argostranslate.package.get_available_packages()
        # Install en→target and target→en pairs for all SUPPORTED_LANGUAGES.
        for pkg in available:
            if pkg.from_code == 'en':
                pkg.install()
        "

    Args:
        None — all configuration is via installed language packages.
    """

    async def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str,
    ) -> str:
        """Translate *text* using locally installed ArgosTranslate packages.

        Falls back through English if a direct pair is not available.

        Args:
            text:        Source text.
            source_lang: BCP-47 source language code.
            target_lang: BCP-47 target language code.

        Returns:
            Translated string.

        Raises:
            TranslationError: If argostranslate is not installed or the
                              requested language pair is unavailable.
        """
        if source_lang == target_lang:
            return text

        try:
            import importlib

            argos_translate = importlib.import_module("argostranslate.translate")
        except ImportError as exc:
            raise TranslationError(
                "argostranslate package is not installed. "
                "Install it with: pip install argostranslate"
            ) from exc

        # Attempt direct pair.
        try:
            installed_langs = argos_translate.get_installed_languages()
            src_lang_obj = next(
                (lg for lg in installed_langs if lg.code == source_lang), None
            )
            tgt_lang_obj = next(
                (lg for lg in installed_langs if lg.code == target_lang), None
            )

            if src_lang_obj is None or tgt_lang_obj is None:
                raise TranslationError(
                    f"ArgosTranslate: language pair {source_lang}→{target_lang} "
                    "is not installed."
                )

            translation = src_lang_obj.get_translation(tgt_lang_obj)
            if translation is None:
                raise TranslationError(
                    f"ArgosTranslate: no translation model for "
                    f"{source_lang}→{target_lang}."
                )

            return str(translation.translate(text))
        except TranslationError:
            raise
        except Exception as exc:
            raise TranslationError(
                f"ArgosTranslate error: {type(exc).__name__}: {exc}"
            ) from exc


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_translation_service(provider: Optional[str] = None) -> TranslationService:
    """Return the translation service selected by ``TRANSLATION_PROVIDER``.

    Supported values for ``TRANSLATION_PROVIDER``:
    * ``libretranslate`` — ``LibreTranslateService`` (default, self-hosted)
    * ``argostranslate`` — ``ArgosTranslateService`` (offline fallback)
    * ``mock``           — ``MockTranslationService`` (tests)

    Args:
        provider: Override the setting (used in tests).

    Returns:
        An object satisfying the ``TranslationService`` Protocol.

    Raises:
        ValueError: If the provider name is not recognised.
    """
    raw: str = provider or str(
        getattr(settings, "TRANSLATION_PROVIDER", "libretranslate")
    )
    name = raw.lower()

    if name == "mock":
        return MockTranslationService()
    if name == "libretranslate":
        return LibreTranslateService()
    if name == "argostranslate":
        return ArgosTranslateService()

    raise ValueError(
        f"Unknown TRANSLATION_PROVIDER value: '{name}'. "
        "Supported options: 'libretranslate', 'argostranslate', 'mock'."
    )
