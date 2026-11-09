"""`kw_common.bearer`: an engine checks its caller's bearer; a credential rotates with no outage.

Every value here is a made-up placeholder; none resembles a real credential.
"""

from __future__ import annotations

import hmac
import logging

import pytest

from kw_common import bearer
from kw_common.bearer import BearerConfigError, accepted_bearers, check_bearer

OLD = "old-value-aaaaaaaaaaaaaaaa"
NEW = "new-value-bbbbbbbbbbbbbbbb"


def _h(token: str) -> str:
    return f"Bearer {token}"


def test_the_current_value_passes_and_a_wrong_one_does_not() -> None:
    accepted = accepted_bearers(OLD)
    assert check_bearer(_h(OLD), accepted) is True
    assert check_bearer(_h(NEW), accepted) is False
    assert check_bearer(_h(OLD + "x"), accepted) is False
    assert check_bearer(_h(OLD[:-1]), accepted) is False


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer",
        "Bearer ",
        "Bearer  " + OLD,  # two spaces: the token would start with a space
        "Bearer\t" + OLD,
        "Basic " + OLD,
        "Token " + OLD,
        OLD,  # no scheme
        "Bearer " + OLD + " extra",
        "Bearer " + OLD + "\r\n",
        "Bearer " + OLD + "\x00",
        "Bearer é" + OLD,
        "BearerX " + OLD,
        b"Bearer " + OLD.encode(),
        123,
    ],
)
def test_a_missing_or_malformed_header_is_refused_and_never_raises(header: object) -> None:
    assert check_bearer(header, accepted_bearers(OLD)) is False  # type: ignore[arg-type]


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER", "bEaReR"])
def test_the_scheme_word_is_case_insensitive_the_token_is_not(scheme: str) -> None:
    accepted = accepted_bearers(OLD)
    assert check_bearer(f"{scheme} {OLD}", accepted) is True
    assert check_bearer(f"{scheme} {OLD.upper()}", accepted) is False


def test_an_oversized_header_is_refused() -> None:
    assert check_bearer("Bearer " + "a" * 100_000, accepted_bearers(OLD)) is False


def test_a_rotation_both_pass_then_the_old_one_is_withdrawn() -> None:
    # 1. The engine is told the next value while the caller still sends the old one.
    during = accepted_bearers(OLD, NEW)
    assert check_bearer(_h(OLD), during) is True
    # 2. The caller switches to the new one: it passes, with no restart in between.
    assert check_bearer(_h(NEW), during) is True
    # 3. The engine drops the old value and keeps only the new one.
    after = accepted_bearers(NEW)
    assert check_bearer(_h(NEW), after) is True
    assert check_bearer(_h(OLD), after) is False


def test_every_accepted_value_is_compared_on_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        seen.append((a, b))
        return real(a, b)

    monkeypatch.setattr(bearer.hmac, "compare_digest", spy)
    assert check_bearer(_h(OLD), accepted_bearers(OLD, NEW)) is True
    # The first value matched, and the second was still compared: no early exit.
    assert [b for _, b in seen] == [OLD.encode(), NEW.encode()]


def test_no_accepted_values_fails_closed() -> None:
    for empty in ((), [], None, "", ("",), (None,)):
        assert check_bearer(_h(OLD), empty) is False  # type: ignore[arg-type]
    # A blank member never matches an empty token either.
    assert check_bearer("Bearer ", ("",)) is False


def test_a_bare_string_is_one_accepted_value() -> None:
    assert check_bearer(_h(OLD), OLD) is True
    assert check_bearer(_h(NEW), OLD) is False


def test_accepted_bearers_requires_a_current_value() -> None:
    for missing in (None, "", "   ", "\n"):
        with pytest.raises(BearerConfigError):
            accepted_bearers(missing)


def test_accepted_bearers_ignores_a_blank_or_absent_upcoming_value() -> None:
    for none in (None, "", "  \n"):
        assert accepted_bearers(OLD, none) == (OLD,)


def test_accepted_bearers_strips_the_whitespace_a_secret_store_adds() -> None:
    assert accepted_bearers(OLD + "\n", "  " + NEW + "\r\n") == (OLD, NEW)


def test_identical_values_collapse_to_one() -> None:
    assert accepted_bearers(OLD, OLD) == (OLD,)


@pytest.mark.parametrize(
    "bad", ["has space", "tab\there", "ctl\x01char", "café", "a" * 5000, "in\nside"]
)
def test_a_value_that_cannot_travel_in_a_header_is_refused_at_boot(bad: str) -> None:
    with pytest.raises(BearerConfigError):
        accepted_bearers(bad)
    with pytest.raises(BearerConfigError):
        accepted_bearers(OLD, bad)


def test_a_non_string_value_is_refused_at_boot() -> None:
    with pytest.raises(BearerConfigError):
        accepted_bearers(OLD, 5)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", ["has space", "café", "a" * 5000, ""])
def test_a_refusal_never_carries_the_value(bad: str) -> None:
    with pytest.raises(BearerConfigError) as exc:
        accepted_bearers(bad if bad else None, None)
    text = str(exc.value)
    assert bad not in text or bad == ""
    assert "current" in text


def test_nothing_is_logged_and_no_value_is_in_a_message(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        accepted = accepted_bearers(OLD, NEW)
        check_bearer(_h(OLD), accepted)
        check_bearer(_h("wrong-wrong-wrong-wrong"), accepted)
        check_bearer("garbage", accepted)
    assert caplog.records == []


def test_the_public_names_are_exactly_the_documented_ones() -> None:
    assert sorted(bearer.__all__) == [
        "BearerConfigError",
        "accepted_bearers",
        "accepted_verifiers",
        "bearer_verifier",
        "check_bearer",
        "check_bearer_verifier",
    ]
