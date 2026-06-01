"""Geocoding service.

Defines the ``GeocodingProvider`` Protocol and two concrete implementations:

* ``MockGeocodingProvider``  — deterministic, no network calls; for tests.
* ``NominatimGeocodingProvider`` — free, open-source geocoding via the
  OpenStreetMap Nominatim API (no API key required).
* ``GoogleGeocodingProvider`` — production geocoding via Google Maps
  Geocoding API (requires ``GOOGLE_GEOCODING_API_KEY``).

A factory ``get_geocoding_provider()`` selects the implementation from the
``GEOCODING_PROVIDER`` environment variable.

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

import logging
from typing import Optional, Protocol, Tuple, runtime_checkable

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

# Geographic bounding box for Kenya (spec §7.4).
# Queries returning coordinates outside this box are silently rejected.
_KENYA_BBOX = {
    "min_lng": 33.9,
    "max_lng": 41.9,
    "min_lat": -4.7,
    "max_lat": 4.6,
}

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

# Nominatim usage policy: no more than 1 request/second and a descriptive
# User-Agent identifying the application.
_NOMINATIM_HEADERS = {
    "User-Agent": "CrisisMap/1.0 (Matata crisis-reporting platform; contact: crisismap@example.com)",
    "Accept-Language": "en",
}


class NominatimGeocodingProvider:
    """Production geocoding via the Nominatim OpenStreetMap API.

    This provider is free and requires no API key.  It is subject to
    Nominatim's usage policy (no bulk requests, max 1 req/s).

    For a crisis event with many landmark submissions, consider deploying a
    self-hosted Nominatim instance using the Docker image:
        https://github.com/mediagis/nominatim-docker
    """

    def __init__(self, timeout_s: float = 5.0) -> None:
        self._timeout = timeout_s

    def geocode(self, query: str) -> Optional[Coordinates]:
        """Resolve *query* via the Nominatim search API.

        Args:
            query: Landmark description or address string.

        Returns:
            ``(lat, lng)`` or ``None`` if no result is found.

        Raises:
            GeocodingError: On HTTP error or network failure.
        """
        try:
            response = httpx.get(
                _NOMINATIM_URL,
                params={
                    "q": query,
                    "format": "json",
                    "limit": 1,
                    "countrycodes": "ke",  # Bias toward Kenya
                },
                headers=_NOMINATIM_HEADERS,
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

        # Reject coordinates outside Kenya bounding box.
        if not _within_kenya(lat, lng):
            logger.warning(
                "Nominatim result outside Kenya bounds (lat=%.4f lng=%.4f) — discarded",
                lat,
                lng,
            )
            return None

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
        try:
            response = httpx.get(
                _GOOGLE_GEOCODING_URL,
                params={
                    "address": query,
                    "key": self._api_key,
                    "components": "country:KE",
                },
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

        if not _within_kenya(lat, lng):
            logger.warning(
                "Google result outside Kenya bounds (lat=%.4f lng=%.4f) — discarded",
                lat,
                lng,
            )
            return None

        logger.debug("Google geocoded %r → (%.4f, %.4f)", query[:50], lat, lng)
        return lat, lng


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _within_kenya(lat: float, lng: float) -> bool:
    """Return ``True`` if coordinates fall within the Kenya bounding box."""
    return (
        _KENYA_BBOX["min_lat"] <= lat <= _KENYA_BBOX["max_lat"]
        and _KENYA_BBOX["min_lng"] <= lng <= _KENYA_BBOX["max_lng"]
    )


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_geocoding_provider() -> GeocodingProvider:
    """Return the geocoding provider selected by ``GEOCODING_PROVIDER``.

    Supported values:
    * ``nominatim`` — ``NominatimGeocodingProvider`` (default, free, open-source)
    * ``google``    — ``GoogleGeocodingProvider`` (requires API key)
    * ``mock``      — ``MockGeocodingProvider`` (tests only)

    Returns:
        An object satisfying the ``GeocodingProvider`` Protocol.

    Raises:
        ValueError: If ``GEOCODING_PROVIDER`` is set to an unknown value.
    """
    provider_name = getattr(settings, "GEOCODING_PROVIDER", "nominatim").lower()

    if provider_name == "nominatim":
        return NominatimGeocodingProvider()
    if provider_name == "google":
        return GoogleGeocodingProvider()
    if provider_name == "mock":
        return MockGeocodingProvider()

    raise ValueError(
        f"Unknown GEOCODING_PROVIDER value: '{provider_name}'. "
        "Supported options: 'nominatim', 'google', 'mock'."
    )