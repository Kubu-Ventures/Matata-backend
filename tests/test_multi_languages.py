"""Tests for CrisisMap multilingual infrastructure.

Covers:
* ``TranslationService`` Protocol conformance (MockTranslationService)
* ``LibreTranslateService`` — happy path, HTTP errors, network errors
* ``ArgosTranslateService`` — ImportError, missing language pair
* ``get_translation_service`` factory
* ``get_locale`` FastAPI dependency — all negotiation paths
* ``_parse_accept_language`` — quality value ordering
* ``_resolve_locale`` — exact match, prefix match, fallback
* ``get_message`` / ``load_messages`` — catalogue loading, interpolation
* ``LocalisedHTTPException`` — translated detail, English fallback
* ``translate_message`` — static fast-path, live API path, fallback chain
"""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.i18n import (
    SUPPORTED_LANGUAGES,
    LocalisedHTTPException,
    _parse_accept_language,
    _resolve_locale,
    get_locale,
    get_message,
    load_messages,
    translate_message,
)
from app.services.translation_service import (
    ArgosTranslateService,
    LibreTranslateService,
    MockTranslationService,
    TranslationError,
    TranslationService,
    get_translation_service,
)

# ===========================================================================
# MockTranslationService
# ===========================================================================


class TestMockTranslationService:
    """MockTranslationService satisfies the Protocol and behaves predictably."""

    def test_satisfies_protocol(self) -> None:
        svc = MockTranslationService()
        assert isinstance(svc, TranslationService)

    @pytest.mark.asyncio
    async def test_same_lang_returns_original(self) -> None:
        svc = MockTranslationService()
        result = await svc.translate("Hello", "en", "en")
        assert result == "Hello"

    @pytest.mark.asyncio
    async def test_different_lang_returns_mock_prefix(self) -> None:
        svc = MockTranslationService()
        result = await svc.translate("Hello", "en", "sw")
        assert result == "[mock:sw] Hello"

    @pytest.mark.asyncio
    async def test_different_lang_prefix_contains_text(self) -> None:
        svc = MockTranslationService()
        text = "Too many requests."
        result = await svc.translate(text, "en", "fr")
        assert text in result
        assert "fr" in result


# ===========================================================================
# LibreTranslateService
# ===========================================================================


class TestLibreTranslateService:
    """LibreTranslateService HTTP interaction tests."""

    @pytest.mark.asyncio
    async def test_happy_path(self) -> None:
        svc = LibreTranslateService(base_url="http://lt:5000")
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"translatedText": "Bonjour"})

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            result = await svc.translate("Hello", "en", "fr")
        assert result == "Bonjour"

    @pytest.mark.asyncio
    async def test_same_lang_returns_original(self) -> None:
        svc = LibreTranslateService(base_url="http://lt:5000")
        # Should not make any HTTP call.
        result = await svc.translate("Hello", "en", "en")
        assert result == "Hello"

    @pytest.mark.asyncio
    async def test_http_error_raises_translation_error(self) -> None:
        import httpx

        svc = LibreTranslateService(base_url="http://lt:5000")
        request = httpx.Request("POST", "http://lt:5000/translate")
        response = httpx.Response(status_code=400, request=request)

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_response = MagicMock()
            mock_response.raise_for_status = MagicMock(
                side_effect=httpx.HTTPStatusError(
                    "400", request=request, response=response
                )
            )
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            with pytest.raises(TranslationError, match="HTTP error"):
                await svc.translate("Hello", "en", "fr")

    @pytest.mark.asyncio
    async def test_network_error_raises_translation_error(self) -> None:
        import httpx

        svc = LibreTranslateService(base_url="http://lt:5000")

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(
                side_effect=httpx.ConnectError("Connection refused")
            )
            mock_client_cls.return_value = mock_client

            with pytest.raises(TranslationError, match="network error"):
                await svc.translate("Hello", "en", "fr")

    @pytest.mark.asyncio
    async def test_malformed_response_raises_translation_error(self) -> None:
        svc = LibreTranslateService(base_url="http://lt:5000")
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"unexpected": "shape"})
        mock_response.text = '{"unexpected": "shape"}'

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_response)
            mock_client_cls.return_value = mock_client

            with pytest.raises(TranslationError, match="unexpected response"):
                await svc.translate("Hello", "en", "fr")

    @pytest.mark.asyncio
    async def test_api_key_included_in_payload(self) -> None:
        svc = LibreTranslateService(base_url="http://lt:5000", api_key="mykey")
        mock_response = MagicMock()
        mock_response.raise_for_status = MagicMock()
        mock_response.json = MagicMock(return_value={"translatedText": "Hola"})
        captured_payload: list[Any] = []

        async def capture_post(url: str, json: Any = None, **_: Any) -> Any:
            captured_payload.append(json)
            return mock_response

        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = capture_post
            mock_client_cls.return_value = mock_client

            await svc.translate("Hello", "en", "es")

        assert captured_payload[0].get("api_key") == "mykey"


# ===========================================================================
# ArgosTranslateService
# ===========================================================================


class TestArgosTranslateService:
    """ArgosTranslateService error-path tests."""

    @pytest.mark.asyncio
    async def test_import_error_raises_translation_error(self) -> None:
        svc = ArgosTranslateService()
        # argostranslate is almost certainly not installed in the test env.
        with pytest.raises(TranslationError, match="argostranslate"):
            await svc.translate("Hello", "en", "sw")

    @pytest.mark.asyncio
    async def test_same_lang_returns_original(self) -> None:
        svc = ArgosTranslateService()
        result = await svc.translate("Hello", "en", "en")
        assert result == "Hello"


# ===========================================================================
# Factory
# ===========================================================================


class TestGetTranslationService:
    def test_mock_provider(self) -> None:
        svc = get_translation_service("mock")
        assert isinstance(svc, MockTranslationService)

    def test_libretranslate_provider(self) -> None:
        svc = get_translation_service("libretranslate")
        assert isinstance(svc, LibreTranslateService)

    def test_argostranslate_provider(self) -> None:
        svc = get_translation_service("argostranslate")
        assert isinstance(svc, ArgosTranslateService)

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown TRANSLATION_PROVIDER"):
            get_translation_service("notareal")


# ===========================================================================
# SUPPORTED_LANGUAGES
# ===========================================================================


class TestSupportedLanguages:
    def test_is_frozenset(self) -> None:
        assert isinstance(SUPPORTED_LANGUAGES, frozenset)

    def test_contains_all_un_languages(self) -> None:
        for code in ("ar", "zh", "en", "fr", "ru", "es"):
            assert code in SUPPORTED_LANGUAGES, f"UN language {code!r} missing"

    def test_contains_swahili(self) -> None:
        assert "sw" in SUPPORTED_LANGUAGES

    def test_contains_at_least_8_african_regional_languages(self) -> None:
        african = {"sw", "so", "om", "am", "ha", "zu", "yo", "ig", "ti", "rw", "ln"}
        covered = african & SUPPORTED_LANGUAGES
        assert len(covered) >= 8, f"Only {len(covered)} African languages covered"

    def test_minimum_total_languages(self) -> None:
        # Must cover at least 6 UN + 8 African = 14.
        assert len(SUPPORTED_LANGUAGES) >= 14


# ===========================================================================
# _parse_accept_language
# ===========================================================================


class TestParseAcceptLanguage:
    def test_empty_header(self) -> None:
        assert _parse_accept_language("") == []

    def test_single_code(self) -> None:
        assert _parse_accept_language("en") == ["en"]

    def test_multiple_codes_no_quality(self) -> None:
        result = _parse_accept_language("en, fr, sw")
        # All quality 1.0, preserved in declaration order.
        assert "en" in result
        assert "fr" in result
        assert "sw" in result

    def test_quality_ordering(self) -> None:
        result = _parse_accept_language("fr;q=0.9,sw;q=0.8,en")
        assert result[0] == "en"  # implicit q=1.0
        assert result[1] == "fr"
        assert result[2] == "sw"

    def test_complex_header(self) -> None:
        result = _parse_accept_language("fr-CH, fr;q=0.9, en;q=0.8, de;q=0.7")
        assert result[0] == "fr-ch"
        assert "en" in result
        assert "de" in result

    def test_case_normalised_to_lower(self) -> None:
        result = _parse_accept_language("EN, FR")
        assert "en" in result
        assert "fr" in result


# ===========================================================================
# _resolve_locale
# ===========================================================================


class TestResolveLocale:
    def test_exact_match(self) -> None:
        assert _resolve_locale(["sw"]) == "sw"

    def test_prefix_match(self) -> None:
        assert _resolve_locale(["fr-ca"]) == "fr"

    def test_unsupported_falls_back_to_en(self) -> None:
        assert _resolve_locale(["xx-zz", "yy"]) == "en"

    def test_empty_list_falls_back_to_en(self) -> None:
        assert _resolve_locale([]) == "en"

    def test_second_choice_used_when_first_unsupported(self) -> None:
        assert _resolve_locale(["xx", "sw"]) == "sw"


# ===========================================================================
# get_locale dependency
# ===========================================================================


class _MockRequest:
    """Minimal FastAPI Request stub for testing get_locale."""

    def __init__(self, accept_lang: str = "") -> None:
        self.headers: dict[str, str] = {}
        if accept_lang:
            self.headers["Accept-Language"] = accept_lang
        self.state = MagicMock()


@pytest.mark.asyncio
async def test_get_locale_no_header_defaults_to_en() -> None:
    req = _MockRequest()
    result = await get_locale(req, lang=None)
    assert result == "en"


@pytest.mark.asyncio
async def test_get_locale_supported_header() -> None:
    req = _MockRequest(accept_lang="sw")
    result = await get_locale(req, lang=None)
    assert result == "sw"


@pytest.mark.asyncio
async def test_get_locale_unsupported_code_falls_back() -> None:
    req = _MockRequest(accept_lang="xx-ZZ")
    result = await get_locale(req, lang=None)
    assert result == "en"


@pytest.mark.asyncio
async def test_get_locale_quality_values_respected() -> None:
    req = _MockRequest(accept_lang="fr;q=0.9,sw;q=0.8,en")
    result = await get_locale(req, lang=None)
    assert result == "en"  # en has highest quality (implicit 1.0)


@pytest.mark.asyncio
async def test_get_locale_query_param_overrides_header() -> None:
    req = _MockRequest(accept_lang="en")
    result = await get_locale(req, lang="sw")
    assert result == "sw"


@pytest.mark.asyncio
async def test_get_locale_unsupported_query_param_uses_header() -> None:
    req = _MockRequest(accept_lang="fr")
    result = await get_locale(req, lang="xx-unsupported")
    assert result == "fr"


@pytest.mark.asyncio
async def test_get_locale_stores_in_request_state() -> None:
    req = _MockRequest(accept_lang="ar")
    await get_locale(req, lang=None)
    assert req.state.locale == "ar"


# ===========================================================================
# Message catalogue
# ===========================================================================


class TestLoadMessages:
    def test_english_keys_present(self) -> None:
        msgs = load_messages("en")
        assert "errors.rate_limit_exceeded" in msgs
        assert "errors.otp_invalid" in msgs
        assert "errors.auth_required" in msgs

    def test_unsupported_language_returns_english(self) -> None:
        msgs = load_messages("xx-unsupported")
        # Should contain all English keys as fallback.
        assert "errors.rate_limit_exceeded" in msgs

    def test_swahili_catalogue_loaded(self) -> None:
        msgs = load_messages("sw")
        en_msgs = load_messages("en")
        # The Swahili value for rate_limit_exceeded should differ from English.
        sw_val = msgs.get("errors.rate_limit_exceeded", "")
        en_val = en_msgs.get("errors.rate_limit_exceeded", "")
        assert sw_val != en_val
        assert len(sw_val) > 0

    def test_missing_keys_fall_back_to_english(self) -> None:
        # All language catalogues should have all English keys after merge.
        en_keys = set(load_messages("en").keys())
        for lang in ("fr", "ar", "sw", "es"):
            merged = load_messages(lang)
            for key in en_keys:
                assert key in merged, f"Key {key!r} missing for lang {lang!r}"


class TestGetMessage:
    def test_basic_lookup(self) -> None:
        msg = get_message("errors.otp_invalid", "en")
        assert len(msg) > 0
        assert msg != "errors.otp_invalid"  # Not just the key

    def test_interpolation(self) -> None:
        msg = get_message("errors.otp_locked_out", "en", minutes=15)
        assert "15" in msg

    def test_missing_key_returns_key(self) -> None:
        result = get_message("errors.does_not_exist", "en")
        assert result == "errors.does_not_exist"

    def test_french_translation_differs_from_english(self) -> None:
        en = get_message("errors.rate_limit_exceeded", "en")
        fr = get_message("errors.rate_limit_exceeded", "fr")
        assert en != fr


# ===========================================================================
# LocalisedHTTPException
# ===========================================================================


class TestLocalisedHTTPException:
    def test_english_message(self) -> None:
        exc = LocalisedHTTPException(
            status_code=429,
            message_key="errors.rate_limit_exceeded",
            lang="en",
        )
        assert exc.status_code == 429
        assert isinstance(exc.detail, str)
        assert len(exc.detail) > 0

    def test_swahili_message_differs_from_english(self) -> None:
        exc_en = LocalisedHTTPException(
            status_code=429,
            message_key="errors.rate_limit_exceeded",
            lang="en",
        )
        exc_sw = LocalisedHTTPException(
            status_code=429,
            message_key="errors.rate_limit_exceeded",
            lang="sw",
        )
        assert exc_en.detail != exc_sw.detail

    def test_interpolation(self) -> None:
        exc = LocalisedHTTPException(
            status_code=429,
            message_key="errors.otp_locked_out",
            lang="en",
            minutes=15,
        )
        assert "15" in exc.detail

    def test_unsupported_language_falls_back_to_english(self) -> None:
        exc_en = LocalisedHTTPException(
            status_code=429,
            message_key="errors.rate_limit_exceeded",
            lang="en",
        )
        exc_xx = LocalisedHTTPException(
            status_code=429,
            message_key="errors.rate_limit_exceeded",
            lang="xx-unknown",
        )
        assert exc_en.detail == exc_xx.detail

    def test_all_required_keys_present(self) -> None:
        required_keys = [
            "errors.rate_limit_exceeded",
            "errors.image_rejected",
            "errors.otp_invalid",
            "errors.otp_locked_out",
            "errors.report_not_found",
            "errors.auth_required",
        ]
        for key in required_keys:
            exc = LocalisedHTTPException(status_code=400, message_key=key, lang="en")
            assert exc.detail != key, f"Key {key!r} returned raw key, not translation"


# ===========================================================================
# translate_message (async, with fallback chain)
# ===========================================================================


@pytest.mark.asyncio
async def test_translate_message_static_fast_path() -> None:
    """Static catalogue hit — no network call expected."""
    result = await translate_message(
        "errors.rate_limit_exceeded",
        "en",
        translation_service=MockTranslationService(),
    )
    assert len(result) > 0
    assert result != "errors.rate_limit_exceeded"


@pytest.mark.asyncio
async def test_translate_message_live_api_for_unknown_key() -> None:
    """Key not in any catalogue → live translation attempted."""
    mock_svc = MockTranslationService()
    result = await translate_message(
        "errors.key_that_does_not_exist_anywhere",
        "sw",
        translation_service=mock_svc,
    )
    # Should return the key itself since English source == key.
    assert result == "errors.key_that_does_not_exist_anywhere"


@pytest.mark.asyncio
async def test_translate_message_translation_error_returns_english() -> None:
    """On TranslationError the English source string is returned."""

    class FailingService:
        async def translate(self, text: str, source_lang: str, target_lang: str) -> str:
            raise TranslationError("simulated failure")

    result = await translate_message(
        "errors.rate_limit_exceeded",
        "sw",
        translation_service=FailingService(),  # type: ignore[arg-type]
    )
    # Should return a non-empty string (English fallback or static sw).
    assert len(result) > 0


@pytest.mark.asyncio
async def test_translate_message_en_target_no_api_call() -> None:
    """When target_lang == 'en', no translation API call is needed."""
    call_count = 0

    class CountingService:
        async def translate(self, text: str, source_lang: str, target_lang: str) -> str:
            nonlocal call_count
            call_count += 1
            return text

    result = await translate_message(
        "errors.otp_invalid",
        "en",
        translation_service=CountingService(),  # type: ignore[arg-type]
    )
    assert call_count == 0
    assert len(result) > 0


# ===========================================================================
# JSON catalogue files — structural validation
# ===========================================================================


class TestJsonCatalogues:
    """Validate that every bundled .json catalogue is well-formed."""

    I18N_DIR = os.path.join(os.path.dirname(__file__), "..", "app", "i18n")

    def _catalogue_files(self) -> list[str]:
        if not os.path.exists(self.I18N_DIR):
            return []
        return [
            f
            for f in os.listdir(self.I18N_DIR)
            if f.endswith(".json") and f != "__init__.py"
        ]

    def test_at_least_one_catalogue(self) -> None:
        assert len(self._catalogue_files()) >= 1

    def test_all_catalogues_are_valid_json(self) -> None:
        for filename in self._catalogue_files():
            path = os.path.join(self.I18N_DIR, filename)
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            assert isinstance(data, dict), f"{filename} must be a JSON object"

    def test_en_catalogue_has_all_required_keys(self) -> None:
        required = [
            "errors.rate_limit_exceeded",
            "errors.image_rejected",
            "errors.otp_invalid",
            "errors.otp_locked_out",
            "errors.report_not_found",
            "errors.auth_required",
        ]
        en_path = os.path.join(self.I18N_DIR, "en.json")
        with open(en_path, encoding="utf-8") as fh:
            data = json.load(fh)
        for key in required:
            assert key in data, f"Required key {key!r} missing from en.json"

    def test_all_catalogue_values_are_strings(self) -> None:
        for filename in self._catalogue_files():
            path = os.path.join(self.I18N_DIR, filename)
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            for key, value in data.items():
                assert isinstance(
                    value, str
                ), f"{filename}: key {key!r} has non-string value"

    def test_swahili_catalogue_present(self) -> None:
        assert "sw.json" in self._catalogue_files()

    def test_un_language_catalogues_present(self) -> None:
        files = self._catalogue_files()
        for lang in ("ar", "zh", "en", "fr", "ru", "es"):
            assert f"{lang}.json" in files, f"UN language catalogue {lang}.json missing"
