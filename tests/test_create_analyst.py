"""Tests for app/cli/create_analyst.py.

All tests are pure unit tests — no real Redis connection or SMS gateway is
needed.  Redis and auth_service are mocked at the boundary so the test suite
is fast and hermetic.

Coverage targets
----------------
* Argument parsing (valid and invalid inputs)
* E-mail format validation
* Role validation
* Successful token issuance (analyst / responder / admin)
* Redis connection and teardown
* Error paths: bad e-mail, bad role, issue_analyst_token raises
* Output format: access token and refresh token printed; e-mail NOT printed
* Exit codes: 0 on success, 1 on failure
* No plaintext e-mail in any stdout / stderr / log output
"""

from __future__ import annotations

import re
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# E.164 pattern used to assert that phone-like PII is not printed.
# For e-mail we assert the literal address does not appear in output.
_TEST_EMAIL = "analyst@example.com"
_TEST_ROLE = "analyst"
_FAKE_ACCESS = "eyJhbGciOiJIUzI1NiJ9.access"
_FAKE_REFRESH = "deadbeefcafebabe" * 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_redis_mock():
    """Return a mock Redis client whose async methods succeed by default."""
    mock = AsyncMock()
    mock.aclose = AsyncMock()
    return mock


def _patch_redis(mock=None):
    if mock is None:
        mock = _make_redis_mock()
    return patch("redis.asyncio.Redis.from_url", return_value=mock)


def _patch_issue_token(access=_FAKE_ACCESS, refresh=_FAKE_REFRESH, raises=None):
    """Context manager that patches issue_analyst_token."""
    if raises:
        side_effect = raises
        return_value = None
    else:
        side_effect = None
        return_value = (access, refresh)

    return patch(
        "app.cli.create_analyst.issue_analyst_token",
        new=AsyncMock(return_value=return_value, side_effect=side_effect),
    )


# ---------------------------------------------------------------------------
# _parse_args
# ---------------------------------------------------------------------------


class TestParseArgs:
    def test_defaults(self):
        from app.cli.create_analyst import _parse_args

        args = _parse_args(["--email", _TEST_EMAIL])
        assert args.email == _TEST_EMAIL
        assert args.role == "analyst"

    def test_explicit_role_admin(self):
        from app.cli.create_analyst import _parse_args

        args = _parse_args(["--email", _TEST_EMAIL, "--role", "admin"])
        assert args.role == "admin"

    def test_explicit_role_responder(self):
        from app.cli.create_analyst import _parse_args

        args = _parse_args(["--email", _TEST_EMAIL, "--role", "responder"])
        assert args.role == "responder"

    def test_invalid_role_raises_system_exit(self):
        from app.cli.create_analyst import _parse_args

        with pytest.raises(SystemExit):
            _parse_args(["--email", _TEST_EMAIL, "--role", "anonymous_reporter"])

    def test_missing_email_raises_system_exit(self):
        from app.cli.create_analyst import _parse_args

        with pytest.raises(SystemExit):
            _parse_args([])


# ---------------------------------------------------------------------------
# _run — success paths
# ---------------------------------------------------------------------------


class TestRunSuccess:
    @pytest.mark.asyncio
    async def test_analyst_role_returns_zero(self, capsys):
        from app.cli.create_analyst import _run

        with _patch_redis(), _patch_issue_token():
            code = await _run(_TEST_EMAIL, "analyst")

        assert code == 0

    @pytest.mark.asyncio
    async def test_admin_role_returns_zero(self, capsys):
        from app.cli.create_analyst import _run

        with _patch_redis(), _patch_issue_token():
            code = await _run(_TEST_EMAIL, "admin")

        assert code == 0

    @pytest.mark.asyncio
    async def test_responder_role_returns_zero(self, capsys):
        from app.cli.create_analyst import _run

        with _patch_redis(), _patch_issue_token():
            code = await _run(_TEST_EMAIL, "responder")

        assert code == 0

    @pytest.mark.asyncio
    async def test_access_token_printed_to_stdout(self, capsys):
        from app.cli.create_analyst import _run

        with _patch_redis(), _patch_issue_token():
            await _run(_TEST_EMAIL, "analyst")

        out = capsys.readouterr().out
        assert _FAKE_ACCESS in out

    @pytest.mark.asyncio
    async def test_refresh_token_printed_to_stdout(self, capsys):
        from app.cli.create_analyst import _run

        with _patch_redis(), _patch_issue_token():
            await _run(_TEST_EMAIL, "analyst")

        out = capsys.readouterr().out
        assert _FAKE_REFRESH in out

    @pytest.mark.asyncio
    async def test_plaintext_email_not_in_stdout(self, capsys):
        """The e-mail address must never appear in stdout."""
        from app.cli.create_analyst import _run

        with _patch_redis(), _patch_issue_token():
            await _run(_TEST_EMAIL, "analyst")

        out = capsys.readouterr().out
        assert _TEST_EMAIL not in out

    @pytest.mark.asyncio
    async def test_plaintext_email_not_in_stderr(self, capsys):
        """The e-mail address must never appear in stderr."""
        from app.cli.create_analyst import _run

        with _patch_redis(), _patch_issue_token():
            await _run(_TEST_EMAIL, "analyst")

        err = capsys.readouterr().err
        assert _TEST_EMAIL not in err

    @pytest.mark.asyncio
    async def test_role_label_present_in_stdout(self, capsys):
        from app.cli.create_analyst import _run

        with _patch_redis(), _patch_issue_token():
            await _run(_TEST_EMAIL, "admin")

        out = capsys.readouterr().out
        assert "admin" in out

    @pytest.mark.asyncio
    async def test_redis_aclose_called_on_success(self):
        """Redis client must be closed even on the happy path."""
        from app.cli.create_analyst import _run

        redis_mock = _make_redis_mock()
        with _patch_redis(redis_mock), _patch_issue_token():
            await _run(_TEST_EMAIL, "analyst")

        redis_mock.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_issue_analyst_token_called_with_correct_role(self):
        from app.cli.create_analyst import _run
        from app.services.auth_service import Role

        with _patch_redis() as _r, _patch_issue_token() as mock_issue:
            await _run(_TEST_EMAIL, "analyst")

        mock_issue.assert_awaited_once()
        _, kwargs = mock_issue.call_args
        assert (
            kwargs.get("role") == Role.analyst
            or mock_issue.call_args[0][1] == Role.analyst
        )

    @pytest.mark.asyncio
    async def test_issue_analyst_token_email_arg_is_plaintext(self):
        """The email passed to issue_analyst_token is the raw address; the
        service is responsible for hashing it (tested separately in auth tests).
        """
        from app.cli.create_analyst import _run

        with _patch_redis(), _patch_issue_token() as mock_issue:
            await _run(_TEST_EMAIL, "analyst")

        call_args = mock_issue.call_args
        # email could be positional or keyword
        email_arg = call_args.kwargs.get("email") or (
            call_args.args[0] if call_args.args else None
        )
        assert email_arg == _TEST_EMAIL


# ---------------------------------------------------------------------------
# _run — validation failure paths
# ---------------------------------------------------------------------------


class TestRunValidationFailures:
    @pytest.mark.asyncio
    async def test_invalid_email_returns_one(self):
        from app.cli.create_analyst import _run

        code = await _run("not-an-email", "analyst")
        assert code == 1

    @pytest.mark.asyncio
    async def test_invalid_email_error_to_stderr(self, capsys):
        from app.cli.create_analyst import _run

        await _run("bad@@bad", "analyst")
        err = capsys.readouterr().err
        assert "ERROR" in err or "valid" in err.lower()

    @pytest.mark.asyncio
    async def test_invalid_role_returns_one(self):
        from app.cli.create_analyst import _run

        # _run receives a role string; 'reporter' is not elevated.
        code = await _run(_TEST_EMAIL, "reporter")
        assert code == 1

    @pytest.mark.asyncio
    async def test_invalid_role_error_to_stderr(self, capsys):
        from app.cli.create_analyst import _run

        await _run(_TEST_EMAIL, "anonymous_reporter")
        err = capsys.readouterr().err
        assert "ERROR" in err or "role" in err.lower()

    @pytest.mark.asyncio
    async def test_no_redis_call_on_validation_failure(self):
        """Redis must not be touched if input validation fails."""
        from app.cli.create_analyst import _run

        with _patch_redis() as mock_from_url:
            await _run("bad", "analyst")

        mock_from_url.assert_not_called()


# ---------------------------------------------------------------------------
# _run — issue_analyst_token raises
# ---------------------------------------------------------------------------


class TestRunTokenIssuanceFailure:
    @pytest.mark.asyncio
    async def test_returns_one_on_exception(self):
        from app.cli.create_analyst import _run

        with _patch_redis(), _patch_issue_token(raises=RuntimeError("Redis down")):
            code = await _run(_TEST_EMAIL, "analyst")

        assert code == 1

    @pytest.mark.asyncio
    async def test_error_written_to_stderr(self, capsys):
        from app.cli.create_analyst import _run

        with _patch_redis(), _patch_issue_token(raises=RuntimeError("Redis down")):
            await _run(_TEST_EMAIL, "analyst")

        err = capsys.readouterr().err
        assert "ERROR" in err

    @pytest.mark.asyncio
    async def test_redis_aclose_called_on_exception(self):
        """Redis client must be closed even when token issuance raises."""
        from app.cli.create_analyst import _run

        redis_mock = _make_redis_mock()
        with _patch_redis(redis_mock), _patch_issue_token(raises=RuntimeError("boom")):
            await _run(_TEST_EMAIL, "analyst")

        redis_mock.aclose.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_plaintext_email_not_in_stderr_on_failure(self, capsys):
        from app.cli.create_analyst import _run

        with _patch_redis(), _patch_issue_token(raises=RuntimeError("boom")):
            await _run(_TEST_EMAIL, "analyst")

        err = capsys.readouterr().err
        assert _TEST_EMAIL not in err


# ---------------------------------------------------------------------------
# main() — sys.exit integration
# ---------------------------------------------------------------------------


class TestMain:
    def test_main_exits_zero_on_success(self):
        from app.cli.create_analyst import main

        with (
            _patch_redis(),
            _patch_issue_token(),
            pytest.raises(SystemExit) as exc_info,
        ):
            main(["--email", _TEST_EMAIL, "--role", "analyst"])

        assert exc_info.value.code == 0

    def test_main_exits_one_on_bad_email(self):
        from app.cli.create_analyst import main

        with pytest.raises(SystemExit) as exc_info:
            main(["--email", "notanemail", "--role", "analyst"])

        assert exc_info.value.code == 1

    def test_main_exits_nonzero_on_bad_role_arg(self):
        """argparse itself rejects unknown choices and calls sys.exit(2)."""
        from app.cli.create_analyst import main

        with pytest.raises(SystemExit) as exc_info:
            main(["--email", _TEST_EMAIL, "--role", "superuser"])

        assert exc_info.value.code != 0


# ---------------------------------------------------------------------------
# PII guard — log output must not contain plaintext e-mail
# ---------------------------------------------------------------------------


class TestNoPIIInLogs:
    @pytest.mark.asyncio
    async def test_no_email_in_log_records(self, caplog):
        """No log record at any level may contain the plaintext e-mail."""
        import logging

        from app.cli.create_analyst import _run

        with caplog.at_level(logging.DEBUG, logger="app"):
            with _patch_redis(), _patch_issue_token():
                await _run(_TEST_EMAIL, "analyst")

        for record in caplog.records:
            assert (
                _TEST_EMAIL not in record.getMessage()
            ), f"Plaintext e-mail found in log record: {record.getMessage()}"
