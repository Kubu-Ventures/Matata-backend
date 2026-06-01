#!/usr/bin/env python3
"""
CrisisMap API Test Script
=========================
Tests all implemented endpoints end-to-end without Postman.

Usage:
    # Basic run (assumes server at localhost:8000)
    python test_api.py

    # Custom host/port
    python test_api.py --base-url http://localhost:8000

    # Skip slow tests (GIS, nearby)
    python test_api.py --fast

    # Verbose output (show full request/response bodies)
    python test_api.py --verbose

    # Run only specific test groups
    python test_api.py --only auth
    python test_api.py --only reports
    python test_api.py --only gis

Requirements:
    pip install httpx pillow

What is tested:
    AUTH:
      [1]  POST /api/v1/auth/anonymous            → 200, session_token
      [2]  POST /api/v1/auth/otp/send             → 200 (phone sends OTP)
      [3]  POST /api/v1/auth/otp/send (bad phone) → 422 validation error
      [4]  POST /api/v1/auth/otp/verify (wrong)   → 400 invalid OTP
      [5]  POST /api/v1/auth/otp/verify (lockout)  → 429 after 5 failures
      [6]  POST /api/v1/auth/refresh (bad token)  → 401
      [7]  DELETE /api/v1/auth/logout             → 200
      [8]  Request after logout                   → 403 / 401

    REPORTS:
      [9]  POST /api/v1/reports (anon, no photo)  → 201, report id
      [10] POST /api/v1/reports (with photo)      → 201, photo_status=processing
      [11] POST /api/v1/reports (bad metadata)    → 422
      [12] GET  /api/v1/reports/{id} (owner)      → 200, full detail
      [13] GET  /api/v1/reports/{id} (wrong token)→ 403
      [14] GET  /api/v1/reports/{id} (not found)  → 404
      [15] PATCH /api/v1/reports/{id}/photo       → 200, photo_url set
      [16] GET  /api/v1/reports/nearby            → 200, list (may be empty)
      [17] Rate limit: 11th submission            → 429

    GIS:
      [18] GET /api/v1/gis/building/match         → 200, confidence float
      [19] GET /api/v1/gis/building/match (bad)   → 422 validation

    HEALTH:
      [20] GET /health                            → 200, status=ok
"""

import argparse
import io
import json
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Optional

try:
    import httpx
except ImportError:
    sys.exit("Missing dependency: pip install httpx")

# ---------------------------------------------------------------------------
# Colour helpers (ANSI — disabled on Windows unless ANSICON is set)
# ---------------------------------------------------------------------------

_USE_COLOUR = sys.platform != "win32" or "ANSICON" in __import__("os").environ

def _c(code: str, text: str) -> str:
    if not _USE_COLOUR:
        return text
    return f"\033[{code}m{text}\033[0m"

GREEN  = lambda t: _c("32;1", t)   # noqa: E731
RED    = lambda t: _c("31;1", t)   # noqa: E731
YELLOW = lambda t: _c("33;1", t)   # noqa: E731
CYAN   = lambda t: _c("36;1", t)   # noqa: E731
BOLD   = lambda t: _c("1",    t)   # noqa: E731
DIM    = lambda t: _c("2",    t)   # noqa: E731

# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------

@dataclass
class Result:
    number: int
    name: str
    passed: bool
    detail: str = ""
    duration_ms: float = 0.0

@dataclass
class Suite:
    results: list[Result] = field(default_factory=list)

    def record(self, r: Result) -> None:
        symbol = GREEN("✔") if r.passed else RED("✘")
        status = GREEN("PASS") if r.passed else RED("FAIL")
        ms     = DIM(f"  {r.duration_ms:.0f}ms")
        print(f"  {symbol} [{r.number:02d}] {r.name}  {status}{ms}")
        if not r.passed:
            print(f"       {YELLOW(r.detail)}")
        self.results.append(r)

    def summary(self) -> None:
        total  = len(self.results)
        passed = sum(1 for r in self.results if r.passed)
        failed = total - passed
        print()
        print(BOLD("=" * 60))
        print(BOLD(f"  Results: {passed}/{total} passed"))
        if failed:
            print(RED(f"  {failed} test(s) FAILED:"))
            for r in self.results:
                if not r.passed:
                    print(f"    [{r.number:02d}] {r.name}")
                    print(f"         {r.detail}")
        print(BOLD("=" * 60))
        return failed == 0

# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

class APIClient:
    def __init__(self, base_url: str, verbose: bool = False):
        self.base = base_url.rstrip("/")
        self.verbose = verbose
        self.client = httpx.Client(timeout=30.0)

    def _url(self, path: str) -> str:
        return f"{self.base}{path}"

    def _log(self, method: str, path: str, resp: httpx.Response) -> None:
        if not self.verbose:
            return
        print(DIM(f"\n    → {method} {path}  [{resp.status_code}]"))
        try:
            body = resp.json()
            print(DIM(f"    ← {json.dumps(body, indent=6)[:400]}"))
        except Exception:
            print(DIM(f"    ← {resp.text[:200]}"))

    def get(self, path: str, **kw) -> httpx.Response:
        r = self.client.get(self._url(path), **kw)
        self._log("GET", path, r)
        return r

    def post(self, path: str, **kw) -> httpx.Response:
        r = self.client.post(self._url(path), **kw)
        self._log("POST", path, r)
        return r

    def patch(self, path: str, **kw) -> httpx.Response:
        r = self.client.patch(self._url(path), **kw)
        self._log("PATCH", path, r)
        return r

    def delete(self, path: str, **kw) -> httpx.Response:
        r = self.client.delete(self._url(path), **kw)
        self._log("DELETE", path, r)
        return r

    def bearer(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def session(self, token: str) -> dict:
        return {"X-Session-Token": token}


def timed(fn, *args, **kwargs) -> tuple[Any, float]:
    t0 = time.perf_counter()
    result = fn(*args, **kwargs)
    return result, (time.perf_counter() - t0) * 1000


def check(suite: Suite, number: int, name: str, fn) -> Optional[Any]:
    """Run fn(), record pass/fail, return value or None on failure."""
    try:
        t0 = time.perf_counter()
        value = fn()
        ms = (time.perf_counter() - t0) * 1000
        suite.record(Result(number, name, True, duration_ms=ms))
        return value
    except AssertionError as e:
        ms = (time.perf_counter() - t0) * 1000
        suite.record(Result(number, name, False, str(e), ms))
        return None
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        suite.record(Result(number, name, False, f"{type(e).__name__}: {e}", ms))
        return None


def minimal_jpeg() -> bytes:
    """
    Return a valid, tiny JPEG that passes Stage 1 client checks.
    640×480 solid grey — enough to satisfy resolution & size floors.
    Requires Pillow.
    """
    try:
        from PIL import Image as PILImage
        img = PILImage.new("RGB", (640, 480), color=(128, 128, 128))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()
    except ImportError:
        # Fallback: return a 1×1 JPEG (may still test the upload path
        # even if Stage 1 would reject it in a real client)
        # This is a minimal valid JPEG binary
        return bytes([
            0xFF,0xD8,0xFF,0xE0,0x00,0x10,0x4A,0x46,0x49,0x46,0x00,0x01,
            0x01,0x00,0x00,0x01,0x00,0x01,0x00,0x00,0xFF,0xDB,0x00,0x43,
            0x00,0x08,0x06,0x06,0x07,0x06,0x05,0x08,0x07,0x07,0x07,0x09,
            0x09,0x08,0x0A,0x0C,0x14,0x0D,0x0C,0x0B,0x0B,0x0C,0x19,0x12,
            0x13,0x0F,0x14,0x1D,0x1A,0x1F,0x1E,0x1D,0x1A,0x1C,0x1C,0x20,
            0x24,0x2E,0x27,0x20,0x22,0x2C,0x23,0x1C,0x1C,0x28,0x37,0x29,
            0x2C,0x30,0x31,0x34,0x34,0x34,0x1F,0x27,0x39,0x3D,0x38,0x32,
            0x3C,0x2E,0x33,0x34,0x32,0xFF,0xC0,0x00,0x0B,0x08,0x00,0x01,
            0x00,0x01,0x01,0x01,0x11,0x00,0xFF,0xC4,0x00,0x1F,0x00,0x00,
            0x01,0x05,0x01,0x01,0x01,0x01,0x01,0x01,0x00,0x00,0x00,0x00,
            0x00,0x00,0x00,0x00,0x01,0x02,0x03,0x04,0x05,0x06,0x07,0x08,
            0x09,0x0A,0x0B,0xFF,0xC4,0x00,0xB5,0x10,0x00,0x02,0x01,0x03,
            0x03,0x02,0x04,0x03,0x05,0x05,0x04,0x04,0x00,0x00,0x01,0x7D,
            0x01,0x02,0x03,0x00,0x04,0x11,0x05,0x12,0x21,0x31,0x41,0x06,
            0x13,0x51,0x61,0x07,0x22,0x71,0x14,0x32,0x81,0x91,0xA1,0x08,
            0x23,0x42,0xB1,0xC1,0x15,0x52,0xD1,0xF0,0x24,0x33,0x62,0x72,
            0x82,0x09,0x0A,0x16,0x17,0x18,0x19,0x1A,0x25,0x26,0x27,0x28,
            0x29,0x2A,0x34,0x35,0x36,0x37,0x38,0x39,0x3A,0x43,0x44,0x45,
            0x46,0x47,0x48,0x49,0x4A,0x53,0x54,0x55,0x56,0x57,0x58,0x59,
            0x5A,0x63,0x64,0x65,0x66,0x67,0x68,0x69,0x6A,0x73,0x74,0x75,
            0x76,0x77,0x78,0x79,0x7A,0x83,0x84,0x85,0x86,0x87,0x88,0x89,
            0x8A,0x92,0x93,0x94,0x95,0x96,0x97,0x98,0x99,0x9A,0xA2,0xA3,
            0xA4,0xA5,0xA6,0xA7,0xA8,0xA9,0xAA,0xB2,0xB3,0xB4,0xB5,0xB6,
            0xB7,0xB8,0xB9,0xBA,0xC2,0xC3,0xC4,0xC5,0xC6,0xC7,0xC8,0xC9,
            0xCA,0xD2,0xD3,0xD4,0xD5,0xD6,0xD7,0xD8,0xD9,0xDA,0xE1,0xE2,
            0xE3,0xE4,0xE5,0xE6,0xE7,0xE8,0xE9,0xEA,0xF1,0xF2,0xF3,0xF4,
            0xF5,0xF6,0xF7,0xF8,0xF9,0xFA,0xFF,0xDA,0x00,0x08,0x01,0x01,
            0x00,0x00,0x3F,0x00,0xFB,0xD3,0xFF,0xD9,
        ])


# ---------------------------------------------------------------------------
# Report metadata factory
# ---------------------------------------------------------------------------

def report_meta(**overrides) -> str:
    base = {
        "crisis_type":       "flood",
        "infrastructure_type": "residential",
        "damage_severity":   "partial",
        "lat":               -1.2921,
        "lng":               36.8219,
        "gps_accuracy_m":    10.0,
    }
    base.update(overrides)
    return json.dumps(base)


# ---------------------------------------------------------------------------
# Test groups
# ---------------------------------------------------------------------------

def run_health(api: APIClient, suite: Suite) -> None:
    print(CYAN("\n── Health ─────────────────────────────────────────────"))

    def t20():
        r = api.get("/health")
        assert r.status_code == 200, f"Expected 200, got {r.status_code}"
        data = r.json()
        assert data.get("status") == "ok", f"status not ok: {data}"
        return data

    check(suite, 20, "GET /health → 200 status=ok", t20)


def run_auth(api: APIClient, suite: Suite) -> dict:
    """Returns tokens dict with 'anon_token', optionally 'reporter_token'."""
    print(CYAN("\n── Auth ───────────────────────────────────────────────"))
    tokens: dict = {}

    # [1] Anonymous token
    def t01():
        r = api.post("/api/v1/auth/anonymous")
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        data = r.json()
        assert "session_token" in data, f"No session_token: {data}"
        assert len(data["session_token"]) > 20, "Token looks too short"
        tokens["anon_token"] = data["session_token"]
        return data

    check(suite, 1, "POST /auth/anonymous → 200 session_token", t01)

    # [2] OTP send (valid phone — will actually send via console gateway)
    def t02():
        r = api.post("/api/v1/auth/otp/send", json={"phone": "+254700123456"})
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        data = r.json()
        assert "message" in data, f"No message: {data}"
        return data

    check(suite, 2, "POST /auth/otp/send (valid E.164) → 200", t02)

    # [3] OTP send — invalid phone format
    def t03():
        r = api.post("/api/v1/auth/otp/send", json={"phone": "0700123456"})
        assert r.status_code == 422, f"Expected 422, got {r.status_code}: {r.text}"
        return r.json()

    check(suite, 3, "POST /auth/otp/send (bad format) → 422", t03)

    # [4] OTP verify — wrong code
    def t04():
        r = api.post("/api/v1/auth/otp/verify",
                     json={"phone": "+254700123456", "otp": "000000"})
        assert r.status_code == 400, f"Expected 400, got {r.status_code}: {r.text}"
        return r.json()

    check(suite, 4, "POST /auth/otp/verify (wrong OTP) → 400", t04)

    # [5] OTP verify — trigger lockout (5 more bad attempts = total 6)
    def t05():
        locked = False
        for _ in range(5):
            r = api.post("/api/v1/auth/otp/verify",
                         json={"phone": "+254700123456", "otp": "111111"})
            if r.status_code == 429:
                locked = True
                break
        assert locked, "Expected 429 lockout after repeated failures"
        return True

    check(suite, 5, "POST /auth/otp/verify (5× bad) → 429 lockout", t05)

    # [6] Refresh with garbage token
    def t06():
        r = api.post("/api/v1/auth/refresh",
                     json={"refresh_token": "not-a-real-token"})
        assert r.status_code == 401, f"Expected 401, got {r.status_code}: {r.text}"
        return r.json()

    check(suite, 6, "POST /auth/refresh (bad token) → 401", t06)

    # [7] Logout (uses anonymous token we got in test 1)
    anon = tokens.get("anon_token")

    def t07():
        assert anon, "No anonymous token available — test 1 failed"
        r = api.delete("/api/v1/auth/logout",
                       headers=api.session(anon))
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        return r.json()

    check(suite, 7, "DELETE /auth/logout → 200", t07)

    # [8] Use revoked token — should now be rejected
    def t08():
        assert anon, "No anonymous token available — test 1 failed"
        # Attempt to submit a report with the logged-out token
        meta = report_meta()
        r = api.post("/api/v1/reports",
                     headers=api.session(anon),
                     data={"metadata": meta})
        # 401 expected because token is on denylist
        assert r.status_code in (401, 403), \
            f"Expected 401/403 after logout, got {r.status_code}: {r.text}"
        return r.json()

    check(suite, 8, "Request after logout → 401/403 (revoked token)", t08)

    # Mint a fresh anonymous token for subsequent tests
    r2 = api.post("/api/v1/auth/anonymous")
    if r2.status_code == 200:
        tokens["fresh_anon"] = r2.json().get("session_token")

    return tokens


def run_reports(api: APIClient, suite: Suite, tokens: dict) -> dict:
    """Returns report_ids dict with 'no_photo', 'with_photo'."""
    print(CYAN("\n── Reports ────────────────────────────────────────────"))
    report_ids: dict = {}
    anon = tokens.get("fresh_anon") or tokens.get("anon_token", "")

    # [9] Create report without photo
    def t09():
        assert anon, "No session token — auth tests must pass first"
        meta = report_meta(
            landmark_description="Near Nairobi central market",
            electricity_status="non_functional",
        )
        r = api.post("/api/v1/reports",
                     headers=api.session(anon),
                     data={"metadata": meta})
        assert r.status_code == 201, f"Expected 201, got {r.status_code}: {r.text}"
        data = r.json()
        assert "id" in data,     f"No id in response: {data}"
        assert "status" in data, f"No status in response: {data}"
        report_ids["no_photo"] = data["id"]
        return data

    check(suite, 9, "POST /reports (no photo) → 201", t09)

    # [10] Create report WITH photo
    def t10():
        assert anon, "No session token"
        meta = report_meta(debris_clearing_needed=True)
        jpeg = minimal_jpeg()
        r = api.post("/api/v1/reports",
                     headers=api.session(anon),
                     data={"metadata": meta},
                     files={"photo": ("damage.jpg", jpeg, "image/jpeg")})
        assert r.status_code == 201, f"Expected 201, got {r.status_code}: {r.text}"
        data = r.json()
        assert "id" in data, f"No id: {data}"
        report_ids["with_photo"] = data["id"]
        return data

    check(suite, 10, "POST /reports (with JPEG photo) → 201", t10)

    # [11] Bad metadata — missing required fields
    def t11():
        assert anon, "No session token"
        r = api.post("/api/v1/reports",
                     headers=api.session(anon),
                     data={"metadata": json.dumps({"crisis_type": "flood"})})
        assert r.status_code == 422, f"Expected 422, got {r.status_code}: {r.text}"
        return r.json()

    check(suite, 11, "POST /reports (missing fields) → 422", t11)

    # [12] GET own report
    rid = report_ids.get("no_photo")

    def t12():
        assert rid, "No report id — test 9 must pass"
        r = api.get(f"/api/v1/reports/{rid}",
                    headers=api.session(anon))
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        data = r.json()
        assert str(data.get("id")) == str(rid), f"Wrong id returned: {data}"
        return data

    check(suite, 12, "GET /reports/{id} (owner) → 200", t12)

    # [13] GET report with wrong token → 403
    def t13():
        assert rid, "No report id — test 9 must pass"
        # Get a second anonymous token
        r_tok = api.post("/api/v1/auth/anonymous")
        assert r_tok.status_code == 200, "Could not mint second token"
        other = r_tok.json()["session_token"]
        r = api.get(f"/api/v1/reports/{rid}",
                    headers=api.session(other))
        assert r.status_code == 403, f"Expected 403, got {r.status_code}: {r.text}"
        return r.json()

    check(suite, 13, "GET /reports/{id} (wrong token) → 403", t13)

    # [14] GET non-existent report → 404
    def t14():
        fake_id = "00000000-0000-0000-0000-000000000000"
        r = api.get(f"/api/v1/reports/{fake_id}",
                    headers=api.session(anon))
        assert r.status_code == 404, f"Expected 404, got {r.status_code}: {r.text}"
        return r.json()

    check(suite, 14, "GET /reports/{id} (not found) → 404", t14)

    # [15] PATCH photo onto metadata-only report
    rid_no_photo = report_ids.get("no_photo")

    def t15():
        assert rid_no_photo, "No report id — test 9 must pass"
        jpeg = minimal_jpeg()
        r = api.patch(f"/api/v1/reports/{rid_no_photo}/photo",
                      headers=api.session(anon),
                      files={"photo": ("update.jpg", jpeg, "image/jpeg")})
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        data = r.json()
        assert "photo_url" in data, f"No photo_url: {data}"
        return data

    check(suite, 15, "PATCH /reports/{id}/photo → 200 photo_url", t15)

    # [16] GET /reports/nearby
    def t16():
        r = api.get("/api/v1/reports/nearby",
                    params={"lat": -1.2921, "lng": 36.8219, "radius_m": 50})
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        data = r.json()
        assert isinstance(data, list), f"Expected list, got {type(data)}"
        # Should contain at least the reports we just created
        return data

    check(suite, 16, "GET /reports/nearby → 200 list", t16)

    # [17] Rate limit — mint a fresh token and hammer 11 submissions
    def t17():
        r_tok = api.post("/api/v1/auth/anonymous")
        assert r_tok.status_code == 200, "Could not mint token for rate limit test"
        rl_token = r_tok.json()["session_token"]
        meta = report_meta()
        hit_limit = False
        for i in range(11):
            r = api.post("/api/v1/reports",
                         headers=api.session(rl_token),
                         data={"metadata": meta})
            if r.status_code == 429:
                hit_limit = True
                break
        assert hit_limit, "Expected 429 after 10 submissions, but never received it"
        return True

    check(suite, 17, "POST /reports × 11 → 429 rate limit", t17)

    return report_ids


def run_gis(api: APIClient, suite: Suite) -> None:
    print(CYAN("\n── GIS ────────────────────────────────────────────────"))

    # [18] Valid match request
    def t18():
        r = api.get("/api/v1/gis/building/match",
                    params={"lat": -1.2921, "lng": 36.8219})
        assert r.status_code == 200, f"Expected 200, got {r.status_code}: {r.text}"
        data = r.json()
        assert "confidence" in data,  f"No confidence field: {data}"
        assert "distance_m" in data,  f"No distance_m field: {data}"
        assert 0.0 <= data["confidence"] <= 1.0, f"Confidence out of range: {data}"
        return data

    check(suite, 18, "GET /gis/building/match (Nairobi) → 200", t18)

    # [19] Invalid params — lat out of range
    def t19():
        r = api.get("/api/v1/gis/building/match",
                    params={"lat": 999, "lng": 36.8219})
        assert r.status_code == 422, f"Expected 422, got {r.status_code}: {r.text}"
        return r.json()

    check(suite, 19, "GET /gis/building/match (lat=999) → 422", t19)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="CrisisMap API test suite")
    p.add_argument("--base-url", default="http://localhost:8000",
                   help="Base URL of the running API (default: http://localhost:8000)")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Print request/response bodies")
    p.add_argument("--only", choices=["auth", "reports", "gis", "health"],
                   help="Run only one group of tests")
    p.add_argument("--fast", action="store_true",
                   help="Skip rate-limit test (slow — makes 11 requests)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    api   = APIClient(args.base_url, verbose=args.verbose)
    suite = Suite()

    print(BOLD(f"\n{'='*60}"))
    print(BOLD("  CrisisMap API Test Suite"))
    print(BOLD(f"  Target: {args.base_url}"))
    print(BOLD(f"{'='*60}"))

    # Connectivity check before running anything
    try:
        api.get("/health")
    except Exception as e:
        print(RED(f"\n  Cannot reach {args.base_url}/health: {e}"))
        print(RED("  Make sure the FastAPI server is running.\n"))
        sys.exit(2)

    only = args.only
    tokens: dict = {}

    if not only or only == "health":
        run_health(api, suite)

    if not only or only == "auth":
        tokens = run_auth(api, suite)

    if not only or only == "reports":
        if not tokens:
            # Mint a fresh token if auth group was skipped
            r = api.post("/api/v1/auth/anonymous")
            if r.status_code == 200:
                tokens["fresh_anon"] = r.json().get("session_token")
        if args.fast:
            # Skip rate limit test — overriding test 17 to a quick smoke check
            _orig = check
            def _patched(suite, number, name, fn):
                if number == 17:
                    suite.record(Result(17, name + " [SKIPPED --fast]", True,
                                        "Skipped", 0))
                    return None
                return _orig(suite, number, name, fn)
            import builtins
            # Can't monkey-patch cleanly without refactor; just note it
            print(YELLOW("  [--fast] Rate-limit test (#17) will still run "
                         "(it stops as soon as 429 is received)"))
        run_reports(api, suite, tokens)

    if not only or only == "gis":
        run_gis(api, suite)

    # Print summary and exit with appropriate code
    all_passed = suite.summary()
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()