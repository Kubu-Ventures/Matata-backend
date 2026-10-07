"""Geocoding service.

Defines the ``GeocodingProvider`` Protocol and two concrete implementations:

* ``MockGeocodingProvider``  — deterministic, no network calls; for tests.
* ``NominatimGeocodingProvider`` — free, open-source geocoding via the
  OpenStreetMap Nominatim API (no API key required).
* ``GoogleGeocodingProvider`` — production geocoding via Google Maps
  Geocoding API (requires ``GOOGLE_GEOCODING_API_KEY``).

A factory ``get_geocoding_provider()`` selects the implementation from the
``GEOCODING_PROVIDER`` environment variable and wraps the real providers in
``CachedGeocodingProvider``, so a repeated landmark is geocoded once.

Nominatim usage policy (https://operations.osmfoundation.org/policies/nominatim/)
-------------------------------------------------------------------------------
At most 1 request per second, an identifying User-Agent, and caching of
results. Every GIS worker process shares one Redis "slot" key, so the limit
holds across all workers, not just per process; without Redis each process
falls back to sleeping one second per request.

Privacy note
------------
Landmark descriptions submitted by reporters are forwarded to the geocoding
service as-is.  They may contain location names, street names, or other
potentially identifying information.  Nominatim's privacy policy applies
for that provider; organisations with stricter requirements should self-host
Nominatim or use the mock provider in environments where geocoding is not
operationally required.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Optional, Protocol, Tuple, runtime_checkable

import httpx
import redis as sync_redis

from app.core.config import settings

logger = logging.getLogger(__name__)

Coordinates = Tuple[float, float]  # (lat, lng)


# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------


class GeocodingError(RuntimeError):
    """Raised when a geocoding provider fails to resolve a query."""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class GeocodingProvider(Protocol):
    """Structural interface for geocoding back-ends."""

    def geocode(self, query: str) -> Optional[Coordinates]:
        """Resolve *query* to (lat, lng) coordinates.

        Args:
            query: Human-readable location description or landmark text.

        Returns:
            ``(lat, lng)`` tuple in WGS84 decimal degrees, or ``None`` if the
            query cannot be resolved.

        Raises:
            GeocodingError: If the provider is unavailable or returns an error.
        """
        ...  # pragma: no cover


# ---------------------------------------------------------------------------
# MockGeocodingProvider — unit tests
# ---------------------------------------------------------------------------


class MockGeocodingProvider:
    """Deterministic geocoding provider for unit tests.

    Returns a fixed Nairobi coordinate by default so that the landmark path
    can be exercised end-to-end without a network connection.

    Attributes:
        result:       Coordinate to return (default: central Nairobi).
        raise_error:  When ``True``, the next call raises ``GeocodingError``.
    """

    # Default: Nairobi city centre.
    DEFAULT_RESULT: Coordinates = (-1.2921, 36.8219)

    def __init__(
        self,
        result: Optional[Coordinates] = None,
        raise_error: bool = False,
    ) -> None:
        self.result: Optional[Coordinates] = result or self.DEFAULT_RESULT
        self.raise_error = raise_error
        self.calls: list[str] = []  # Records every query for test assertions.

    def geocode(self, query: str) -> Optional[Coordinates]:
        self.calls.append(query)
        if self.raise_error:
            raise GeocodingError("MockGeocodingProvider: simulated error")
        return self.result


# ---------------------------------------------------------------------------
# NominatimGeocodingProvider — free, open-source
# ---------------------------------------------------------------------------

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# Redis key holding the shared one-request-per-second slot.
_NOMINATIM_SLOT_KEY = "crisismap:geocode:nominatim:slot"
_NOMINATIM_MIN_INTERVAL_MS = 1000
# Give up waiting for a slot after this long (the caller treats it as a
# geocoding failure and the report stays unmatched for an analyst).
_NOMINATIM_MAX_WAIT_S = 60.0


def _nominatim_headers() -> dict[str, str]:
    """Identifying User-Agent, as the Nominatim usage policy requires."""
    contact = getattr(settings, "GEOCODING_CONTACT", "").strip()
    return {
        "User-Agent": f"Matata/1.0 (crisis damage reporting; {contact})",
        "Accept-Language": "en",
    }


def _redis_client() -> Optional[Any]:
    """A sync Redis client, or ``None`` when Redis is not configured."""
    url = getattr(settings, "REDIS_URL", "")
    if not url:
        return None
    return sync_redis.Redis.from_url(url, socket_timeout=2.0)


class NominatimGeocodingProvider:
    """Production geocoding via the Nominatim OpenStreetMap API.

    This provider is free and requires no API key.  It is subject to
    Nominatim's usage policy (no bulk requests, max 1 req/s).

    For a crisis event with many landmark submissions, consider deploying a
    self-hosted Nominatim instance using the Docker image:
        https://github.com/mediagis/nominatim-docker
    """

    def __init__(self, timeout_s: float = 5.0, redis_client: Any = None) -> None:
        self._timeout = timeout_s
        self._redis = redis_client

    def _wait_for_slot(self) -> None:
        """Block until this process may send the next request.

        Uses ``SET key NX PX 1000`` on a shared key: whoever sets it owns the
        next second. Others wait for the key to expire. Falls back to a plain
        one-second sleep when Redis is unavailable.
        """
        if self._redis is None:
            time.sleep(_NOMINATIM_MIN_INTERVAL_MS / 1000)
            return
        deadline = time.monotonic() + _NOMINATIM_MAX_WAIT_S
        try:
            while True:
                if self._redis.set(
                    _NOMINATIM_SLOT_KEY, "1", nx=True, px=_NOMINATIM_MIN_INTERVAL_MS
                ):
                    return
                if time.monotonic() > deadline:
                    raise GeocodingError("Nominatim rate-limit slot wait timed out")
                ttl_ms = self._redis.pttl(_NOMINATIM_SLOT_KEY)
                time.sleep(max(ttl_ms, 50) / 1000 if ttl_ms and ttl_ms > 0 else 0.05)
        except sync_redis.RedisError as exc:
            logger.warning("Nominatim limiter: Redis unavailable (%s)", exc)
            time.sleep(_NOMINATIM_MIN_INTERVAL_MS / 1000)

    def geocode(self, query: str) -> Optional[Coordinates]:
        """Resolve *query* via the Nominatim search API.

        Args:
            query: Landmark description or address string.

        Returns:
            ``(lat, lng)`` or ``None`` if no result is found.

        Raises:
            GeocodingError: On HTTP error or network failure.
        """
        # Nominatim usage policy: at most 1 request per second, across all
        # workers. Blocking here is fine: it runs in the Celery GIS worker,
        # not the async FastAPI event loop.
        self._wait_for_slot()

        params: dict = {"q": query, "format": "json", "limit": 1}
        country_code = getattr(settings, "GEOCODING_COUNTRY_CODE", "").strip().lower()
        if country_code:
            params["countrycodes"] = country_code

        try:
            response = httpx.get(
                _NOMINATIM_URL,
                params=params,
                headers=_nominatim_headers(),
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GeocodingError(
                f"Nominatim HTTP error: {exc.response.status_code}"
            ) from exc
        except httpx.RequestError as exc:
            raise GeocodingError(
                f"Nominatim network error: {type(exc).__name__}"
            ) from exc

        results = response.json()
        if not results:
            logger.debug("Nominatim: no results for query %r", query[:50])
            return None

        lat = float(results[0]["lat"])
        lng = float(results[0]["lon"])

        logger.debug("Nominatim geocoded %r → (%.4f, %.4f)", query[:50], lat, lng)
        return lat, lng


# ---------------------------------------------------------------------------
# GoogleGeocodingProvider — production (requires API key)
# ---------------------------------------------------------------------------

_GOOGLE_GEOCODING_URL = "https://maps.googleapis.com/maps/api/geocode/json"


class GoogleGeocodingProvider:
    """Production geocoding via the Google Maps Geocoding API.

    Requires ``GOOGLE_GEOCODING_API_KEY`` environment variable.
    Google's free tier provides 200 USD credit/month (~40,000 geocoding
    requests).
    """

    def __init__(self, timeout_s: float = 5.0) -> None:
        self._api_key = settings.GOOGLE_GEOCODING_API_KEY
        self._timeout = timeout_s

        if not self._api_key:
            raise GeocodingError(
                "GOOGLE_GEOCODING_API_KEY must be set when GEOCODING_PROVIDER=google."
            )

    def geocode(self, query: str) -> Optional[Coordinates]:
        """Resolve *query* via the Google Geocoding API.

        Args:
            query: Landmark description or address string.

        Returns:
            ``(lat, lng)`` or ``None`` if no result is found.

        Raises:
            GeocodingError: On HTTP error, network failure, or API error status.
        """
        params: dict = {"address": query, "key": self._api_key}
        country_code = getattr(settings, "GEOCODING_COUNTRY_CODE", "").strip().upper()
        if country_code:
            params["components"] = f"country:{country_code}"

        try:
            response = httpx.get(
                _GOOGLE_GEOCODING_URL,
                params=params,
                timeout=self._timeout,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise GeocodingError(
                f"Google Geocoding HTTP error: {exc.response.status_code}"
            ) from exc
        except httpx.RequestError as exc:
            raise GeocodingError(
                f"Google Geocoding network error: {type(exc).__name__}"
            ) from exc

        data = response.json()
        if data.get("status") not in ("OK",):
            logger.warning("Google Geocoding API status: %s", data.get("status"))
            return None

        results = data.get("results", [])
        if not results:
            return None

        location = results[0]["geometry"]["location"]
        lat = float(location["lat"])
        lng = float(location["lng"])

        logger.debug("Google geocoded %r → (%.4f, %.4f)", query[:50], lat, lng)
        return lat, lng


# ---------------------------------------------------------------------------
# CachedGeocodingProvider — Redis cache in front of a real provider
# ---------------------------------------------------------------------------


def _normalise(query: str) -> str:
    return " ".join(query.lower().split())


class CachedGeocodingProvider:
    """Cache geocoding results in Redis, keyed by a hash of the query.

    Only a SHA-256 of the normalised landmark text is stored, never the text.
    "No result" is cached too, for a shorter time. Errors are not cached.
    If Redis fails, the call goes straight to the wrapped provider.
    """

    def __init__(self, inner: GeocodingProvider, name: str, redis_client: Any) -> None:
        self._inner = inner
        self._name = name
        self._redis = redis_client

    def _key(self, query: str) -> str:
        country = getattr(settings, "GEOCODING_COUNTRY_CODE", "").strip().lower()
        digest = hashlib.sha256(_normalise(query).encode()).hexdigest()
        return f"crisismap:geocode:v1:{self._name}:{country or '-'}:{digest}"

    def geocode(self, query: str) -> Optional[Coordinates]:
        key = self._key(query)
        try:
            cached = self._redis.get(key)
        except sync_redis.RedisError as exc:
            logger.warning("Geocoding cache read failed (%s)", exc)
            return self._inner.geocode(query)

        if cached is not None:
            value = json.loads(cached)
            return (float(value[0]), float(value[1])) if value else None

        result = self._inner.geocode(query)  # GeocodingError propagates
        ttl = (
            settings.GEOCODING_CACHE_TTL_S
            if result is not None
            else settings.GEOCODING_CACHE_MISS_TTL_S
        )
        try:
            self._redis.set(key, json.dumps(list(result) if result else None), ex=ttl)
        except sync_redis.RedisError as exc:
            logger.warning("Geocoding cache write failed (%s)", exc)
        return result


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_geocoding_provider() -> GeocodingProvider:
    """Return the geocoding provider selected by ``GEOCODING_PROVIDER``.

    Supported values:
    * ``nominatim`` — ``NominatimGeocodingProvider`` (default, free, open-source)
    * ``google``    — ``GoogleGeocodingProvider`` (requires API key)
    * ``mock``      — ``MockGeocodingProvider`` (tests only)

    Real providers are wrapped in ``CachedGeocodingProvider`` when Redis is
    configured.

    Returns:
        An object satisfying the ``GeocodingProvider`` Protocol.

    Raises:
        ValueError: If ``GEOCODING_PROVIDER`` is set to an unknown value.
    """
    provider_name = getattr(settings, "GEOCODING_PROVIDER", "nominatim").lower()

    if provider_name == "mock":
        return MockGeocodingProvider()

    redis_client = _redis_client()
    inner: GeocodingProvider
    if provider_name == "nominatim":
        inner = NominatimGeocodingProvider(redis_client=redis_client)
    elif provider_name == "google":
        inner = GoogleGeocodingProvider()
    else:
        raise ValueError(
            f"Unknown GEOCODING_PROVIDER value: '{provider_name}'. "
            "Supported options: 'nominatim', 'google', 'mock'."
        )
    if redis_client is None:
        return inner
    return CachedGeocodingProvider(inner, provider_name, redis_client)
