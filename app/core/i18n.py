"""Internationalisation (i18n) core for CrisisMap.

This module is the single source of truth for:

* ``SUPPORTED_LANGUAGES`` — frozenset of every BCP-47 language code the
  system officially supports.  Adding a language requires only:
  1. Adding its code to this frozenset.
  2. Dropping a ``<code>.json`` file into ``app/i18n/``.
  No code changes elsewhere are required.

* ``get_locale`` — FastAPI dependency that resolves the caller's preferred
  language from the ``Accept-Language`` header (RFC 5646) or a ``?lang=``
  query parameter, defaulting to ``"en"``.

* ``load_messages`` — loads the bundled static translations from the
  ``app/i18n/`` directory.  Used as a fast path before delegating to the
  live translation API.

* ``LocalisedHTTPException`` — drop-in ``HTTPException`` replacement that
  translates its detail string into the caller's language before sending it.

Language negotiation order (spec §i18n)
-----------------------------------------
1. ``Accept-Language`` header (RFC 5646 quality-weighted preference list).
2. ``?lang=`` query parameter (for clients that cannot set headers).
3. Default: ``"en"``.

Supported language scope
-------------------------
The system supports **all 6 UN official languages** plus a broad set of
regional languages spanning Africa, the Middle East, South/East Asia,
and Latin America.  The architecture is fully extensible — adding a new
language requires zero code changes.

Fallback guarantee
------------------
If translation fails for any reason (network error, unsupported pair,
misconfiguration), the English source string is returned.  A translation
failure is NEVER propagated as an HTTP error.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from typing import Dict, Optional

from fastapi import HTTPException, Query, Request

from app.services.translation_service import (
    TranslationError,
    TranslationService,
    get_translation_service,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Supported language codes — single source of truth
# ---------------------------------------------------------------------------

#: All BCP-47 codes this system officially supports.
#: The 6 UN official languages are listed first, followed by African regional
#: languages, followed by other major world languages.
#: Adding a new language: add its code here AND drop a .json file in app/i18n/
SUPPORTED_LANGUAGES: frozenset[str] = frozenset(
    {
        # ── UN official languages ────────────────────────────────────────────
        "ar",  # Arabic
        "zh",  # Chinese (Simplified)
        "en",  # English
        "fr",  # French
        "ru",  # Russian
        "es",  # Spanish
        # ── East African regional languages ──────────────────────────────────
        "sw",  # Swahili       (Tanzania, Kenya, DRC, Uganda, Mozambique)
        "so",  # Somali        (Somalia, Djibouti, Ethiopia, Kenya)
        "om",  # Oromo         (Ethiopia, Kenya)
        "am",  # Amharic       (Ethiopia — official)
        "ti",  # Tigrinya      (Eritrea, Ethiopia)
        # ── West African regional languages ──────────────────────────────────
        "ha",  # Hausa         (Nigeria, Niger, Ghana, Cameroon)
        "yo",  # Yoruba        (Nigeria, Benin, Togo)
        "ig",  # Igbo          (Nigeria)
        # ── Southern African languages ────────────────────────────────────────
        "zu",  # Zulu          (South Africa, Zimbabwe)
        "xh",  # Xhosa         (South Africa)
        "st",  # Sesotho       (Lesotho, South Africa)
        # ── Central / West African languages ─────────────────────────────────
        "ln",  # Lingala       (DRC, Republic of Congo)
        "rw",  # Kinyarwanda   (Rwanda, DRC)
        # ── North African / Sahel languages ──────────────────────────────────
        "ber",  # Tamazight/Berber (Morocco, Algeria)
        # ── South / Southeast Asian languages ────────────────────────────────
        "hi",  # Hindi         (India)
        "bn",  # Bengali       (Bangladesh, India)
        "ur",  # Urdu          (Pakistan, India)
        "id",  # Indonesian    (Indonesia)
        "ms",  # Malay         (Malaysia, Brunei)
        "tl",  # Filipino/Tagalog (Philippines)
        "vi",  # Vietnamese    (Vietnam)
        # ── Other major world languages ───────────────────────────────────────
        "pt",  # Portuguese    (Brazil, Portugal, Mozambique, Angola, Cape Verde)
        "de",  # German
        "tr",  # Turkish
        "fa",  # Persian/Farsi (Iran, Afghanistan, Tajikistan)
        "ps",  # Pashto        (Afghanistan, Pakistan)
        "uk",  # Ukrainian
        "pl",  # Polish
    }
)

# Default language when no supported language can be negotiated.
_DEFAULT_LANG = "en"

# Path to bundled static translation files.
_I18N_DIR = os.path.join(os.path.dirname(__file__), "..", "i18n")


# ---------------------------------------------------------------------------
# Static message catalogue (loaded from disk once at startup)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=None)
def _load_catalogue(lang: str) -> Dict[str, str]:
    """Load the message catalogue for *lang* from ``app/i18n/<lang>.json``.

    Falls back to the English catalogue if the requested file does not exist.
    Results are cached indefinitely (LRU cache) since translation files are
    static build artefacts.

    Args:
        lang: BCP-47 language code.

    Returns:
        Dict mapping message keys to translated strings.
    """
    path = os.path.join(_I18N_DIR, f"{lang}.json")
    if not os.path.exists(path):
        if lang != _DEFAULT_LANG:
            logger.debug(
                "No static catalogue for language %r — using English fallback", lang
            )
        path = os.path.join(_I18N_DIR, f"{_DEFAULT_LANG}.json")

    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load i18n catalogue from %s: %s", path, exc)
        return {}


def load_messages(lang: str) -> Dict[str, str]:
    """Return the full message catalogue for *lang*.

    This is the public API used by tests and other modules.
    See ``_load_catalogue`` for caching semantics.

    Args:
        lang: BCP-47 language code.

    Returns:
        Dict of message keys → translated strings for that language,
        falling back to English strings for any missing keys.
    """
    catalogue = _load_catalogue(lang)
    if lang == _DEFAULT_LANG:
        return catalogue
    # Merge with English so missing keys always have a value.
    en_catalogue = _load_catalogue(_DEFAULT_LANG)
    merged = dict(en_catalogue)
    merged.update(catalogue)
    return merged


def get_message(key: str, lang: str, **kwargs: object) -> str:
    """Return the localised string for *key* in *lang*.

    Performs simple ``{placeholder}`` substitution on the translated string
    using *kwargs*.

    Args:
        key:     Message key (e.g. ``"errors.otp_locked_out"``).
        lang:    BCP-47 language code.
        **kwargs: Substitution values (e.g. ``minutes=15``).

    Returns:
        Translated and interpolated string.  If the key is missing, the key
        itself is returned so callers always get *something*.
    """
    catalogue = load_messages(lang)
    template = catalogue.get(key, key)
    try:
        return template.format(**kwargs) if kwargs else template
    except (KeyError, IndexError, ValueError):
        # Substitution failed (e.g. template from a different locale than
        # the kwargs were written for); return the raw template.
        return template


# ---------------------------------------------------------------------------
# Language negotiation
# ---------------------------------------------------------------------------


def _parse_accept_language(header: str) -> list[str]:
    """Parse an ``Accept-Language`` header into a sorted list of language codes.

    Respects ``q=`` quality values (RFC 5646 / RFC 4647).  Returns codes in
    descending quality order.

    Args:
        header: Raw ``Accept-Language`` header value.

    Returns:
        List of BCP-47 codes, highest-quality first.  Empty if *header* is
        empty or unparseable.

    Examples:
        >>> _parse_accept_language("fr-CH, fr;q=0.9, en;q=0.8, de;q=0.7")
        ['fr-ch', 'fr', 'en', 'de']
        >>> _parse_accept_language("sw;q=0.8,fr;q=0.9,en")
        ['en', 'fr', 'sw']
    """
    if not header:
        return []

    tags: list[tuple[float, str]] = []
    for item in header.split(","):
        item = item.strip()
        if not item:
            continue
        if ";q=" in item:
            lang_part, q_part = item.split(";q=", 1)
            try:
                quality = float(q_part.strip())
            except ValueError:
                quality = 1.0
        else:
            lang_part = item
            quality = 1.0
        code = lang_part.strip().lower()
        if code:
            tags.append((quality, code))

    tags.sort(key=lambda t: t[0], reverse=True)
    return [code for _, code in tags]


def _resolve_locale(codes: list[str]) -> str:
    """Return the best matching supported locale from *codes*.

    Tries exact match first, then prefix match (e.g. ``"zh-hans"`` → ``"zh"``).
    Defaults to ``"en"`` if nothing matches.

    Args:
        codes: Ordered list of language codes (highest preference first).

    Returns:
        A code present in ``SUPPORTED_LANGUAGES``, or ``"en"``.
    """
    for code in codes:
        # Exact match
        if code in SUPPORTED_LANGUAGES:
            return code
        # Prefix match: "zh-hans" → "zh", "fr-ca" → "fr"
        prefix = code.split("-")[0]
        if prefix in SUPPORTED_LANGUAGES:
            return prefix

    return _DEFAULT_LANG


async def get_locale(
    request: Request,
    lang: Optional[str] = Query(
        default=None,
        description=(
            "BCP-47 language code override "
            "(e.g. ``sw``, ``fr``, ``ar``).  "
            "Falls back to ``Accept-Language`` header, then ``en``."
        ),
    ),
) -> str:
    """FastAPI dependency that resolves the caller's preferred language.

    Negotiation order:
    1. ``?lang=`` query parameter (explicit client override).
    2. ``Accept-Language`` header (RFC 5646 quality-weighted).
    3. Default: ``"en"``.

    The resolved locale is stored on ``request.state.locale`` so that
    middleware and other handlers can read it without re-parsing headers.

    Args:
        request: FastAPI ``Request`` object (injected by FastAPI).
        lang:    Optional ``?lang=`` query parameter.

    Returns:
        BCP-47 language code from ``SUPPORTED_LANGUAGES``, or ``"en"``.
    """
    # 1. Query parameter takes priority (explicit intent from client).
    if lang:
        code = lang.strip().lower()
        if code in SUPPORTED_LANGUAGES:
            request.state.locale = code
            return code
        # Unsupported but present query param — try prefix match.
        prefix = code.split("-")[0]
        if prefix in SUPPORTED_LANGUAGES:
            request.state.locale = prefix
            return prefix
        # Unsupported code — warn and fall through.
        logger.warning(
            "Unsupported ?lang=%r — falling back to Accept-Language header", lang
        )

    # 2. Accept-Language header.
    accept_lang = request.headers.get("Accept-Language", "")
    candidates = _parse_accept_language(accept_lang)
    resolved = _resolve_locale(candidates)
    request.state.locale = resolved
    return resolved


# ---------------------------------------------------------------------------
# LocalisedHTTPException
# ---------------------------------------------------------------------------


class LocalisedHTTPException(HTTPException):  # type: ignore[misc]
    """HTTPException subclass that translates its detail string on creation.

    Usage::

        raise LocalisedHTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            message_key="errors.rate_limit_exceeded",
            lang=lang,
        )

    The translation is performed synchronously using the static catalogue.
    For keys not present in the static catalogue, a best-effort live
    translation is attempted via the configured ``TranslationService``;
    on failure the English source string is used.

    Args:
        status_code:  HTTP status code.
        message_key:  Key from ``app/i18n/en.json``.
        lang:         BCP-47 target language code (default ``"en"``).
        headers:      Optional extra response headers.
        kwargs:       Substitution values for ``{placeholder}`` in the
                      message template.
    """

    def __init__(
        self,
        status_code: int,
        message_key: str,
        lang: str = _DEFAULT_LANG,
        headers: Optional[dict[str, str]] = None,
        **kwargs: object,
    ) -> None:
        detail = _translate_message_sync(message_key, lang, **kwargs)
        super().__init__(status_code=status_code, detail=detail, headers=headers)


def _translate_message_sync(
    key: str,
    lang: str,
    **kwargs: object,
) -> str:
    """Translate a message key synchronously using the static catalogue.

    This function is intentionally synchronous so that ``LocalisedHTTPException``
    can be raised anywhere (including sync code paths).  Live API translation
    is not attempted here to avoid blocking the event loop — the static
    catalogues cover all defined message keys.

    Args:
        key:     Message key.
        lang:    BCP-47 target language.
        **kwargs: Substitution values.

    Returns:
        Translated and interpolated string.
    """
    if lang not in SUPPORTED_LANGUAGES:
        logger.warning(
            "Unsupported language %r in _translate_message_sync — using English",
            lang,
        )
        lang = _DEFAULT_LANG

    return get_message(key, lang, **kwargs)


async def translate_message(
    key: str,
    lang: str,
    translation_service: Optional[TranslationService] = None,
    **kwargs: object,
) -> str:
    """Translate a message key, falling back gracefully on any failure.

    Tries (in order):
    1. Static catalogue for *lang* (fast, no I/O).
    2. Live translation API if the static catalogue returned the key itself
       (meaning the key is not in any bundled catalogue).
    3. English source string.

    Args:
        key:                  Message key.
        lang:                 BCP-47 target language.
        translation_service:  Injected service (defaults to factory instance).
        **kwargs:             Substitution values.

    Returns:
        Translated and interpolated string.  Never raises.
    """
    # Fast path — static catalogue.
    result = get_message(key, lang, **kwargs)
    if result != key:
        # Static catalogue returned a real translation (not the key itself).
        return result

    # The key was not found in any static catalogue.  Attempt live translation
    # of the English source string.
    en_text = get_message(key, _DEFAULT_LANG, **kwargs)
    if lang == _DEFAULT_LANG or en_text == key:
        # Nothing to translate — return whatever we have.
        return en_text

    svc = translation_service or get_translation_service()
    try:
        translated = await svc.translate(en_text, _DEFAULT_LANG, lang)
        logger.debug("Live translation for key=%r lang=%r succeeded", key, lang)
        return translated
    except TranslationError as exc:
        logger.warning(
            "Translation failed for key=%r lang=%r: %s — using English fallback",
            key,
            lang,
            exc,
        )
        # Try the configured fallback provider if it's different from primary.
        primary = getattr(svc, "__class__", type(svc)).__name__
        if "LibreTranslate" in primary:
            try:
                from app.services.translation_service import ArgosTranslateService

                fallback = ArgosTranslateService()
                translated = await fallback.translate(en_text, _DEFAULT_LANG, lang)
                logger.debug(
                    "ArgosTranslate fallback succeeded for key=%r lang=%r", key, lang
                )
                return translated
            except Exception as fallback_exc:
                logger.warning(
                    "ArgosTranslate fallback also failed for key=%r lang=%r: %s",
                    key,
                    lang,
                    fallback_exc,
                )

        return en_text
