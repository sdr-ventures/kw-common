"""`kw_common.bearer` verifiers: a service holds the digest of its caller's bearer, not the bearer.

Every value here is a made-up placeholder; none resembles a real credential.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

import pytest

from kw_common import bearer
from kw_common.bearer import (
    BearerConfigError,
    accepted_verifiers,
    bearer_verifier,
    check_bearer_verifier,
)

OLD = "old-value-aaaaaaaaaaaaaaaa"
NEW = "new-value-bbbbbbbbbbbbbbbb"


def _h(token: str) -> str:
    return f"Bearer {token}"


def test_a_verifier_is_the_lowercase_hex_sha256_of_the_token() -> None:
    assert bearer_verifier(OLD) == hashlib.sha256(OLD.encode()).hexdigest()
    assert len(bearer_verifier(OLD)) == 64
    assert bearer_verifier(OLD) != bearer_verifier(NEW)


def test_the_current_verifier_passes_its_bearer_and_nothing_else() -> None:
    held = accepted_verifiers(bearer_verifier(OLD))
    assert check_bearer_verifier(_h(OLD), held) is True
    assert check_bearer_verifier(_h(NEW), held) is False
    assert check_bearer_verifier(_h(OLD + "x"), held) is False
    assert check_bearer_verifier(_h(OLD[:-1]), held) is False


def test_the_digest_itself_is_not_the_credential() -> None:
    # Holding only the verifier: replaying the verifier as the bearer must fail.
    digest = bearer_verifier(OLD)
    assert check_bearer_verifier(_h(digest), accepted_verifiers(digest)) is False


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer",
        "Bearer ",
        "Bearer  " + OLD,
        "Bearer\t" + OLD,
        "Basic " + OLD,
        OLD,
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
    held = accepted_verifiers(bearer_verifier(OLD))
    assert check_bearer_verifier(header, held) is False  # type: ignore[arg-type]


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
def test_the_scheme_word_is_case_insensitive_the_token_is_not(scheme: str) -> None:
    held = accepted_verifiers(bearer_verifier(OLD))
    assert check_bearer_verifier(f"{scheme} {OLD}", held) is True
    assert check_bearer_verifier(f"{scheme} {OLD.upper()}", held) is False


def test_an_oversized_header_is_refused() -> None:
    held = accepted_verifiers(bearer_verifier(OLD))
    assert check_bearer_verifier("Bearer " + "a" * 100_000, held) is False


def test_a_rotation_both_pass_then_the_old_one_is_withdrawn() -> None:
    during = accepted_verifiers(bearer_verifier(OLD), bearer_verifier(NEW))
    assert check_bearer_verifier(_h(OLD), during) is True
    assert check_bearer_verifier(_h(NEW), during) is True
    after = accepted_verifiers(bearer_verifier(NEW))
    assert check_bearer_verifier(_h(NEW), after) is True
    assert check_bearer_verifier(_h(OLD), after) is False


def test_every_held_verifier_is_compared_on_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bytes] = []
    real = hmac.compare_digest

    def spy(a: bytes, b: bytes) -> bool:
        seen.append(b)
        return real(a, b)

    monkeypatch.setattr(bearer.hmac, "compare_digest", spy)
    held = accepted_verifiers(bearer_verifier(OLD), bearer_verifier(NEW))
    assert check_bearer_verifier(_h(OLD), held) is True
    # The first matched and the second was still compared: no early exit.
    assert seen == [v.encode() for v in held]


def test_no_held_verifiers_fails_closed() -> None:
    for empty in ((), [], None, "", ("",), (None,)):
        assert check_bearer_verifier(_h(OLD), empty) is False  # type: ignore[arg-type]


def test_a_bare_string_is_one_held_verifier() -> None:
    assert check_bearer_verifier(_h(OLD), bearer_verifier(OLD)) is True
    assert check_bearer_verifier(_h(NEW), bearer_verifier(OLD)) is False


def test_the_plain_bearer_path_is_unchanged_beside_it() -> None:
    # A held plain bearer keeps working exactly as in 1.18.0.
    assert bearer.check_bearer(_h(OLD), bearer.accepted_bearers(OLD)) is True
    assert bearer.check_bearer(_h(NEW), bearer.accepted_bearers(OLD)) is False


def test_accepted_verifiers_requires_a_current_value() -> None:
    for missing in (None, "", "   ", "\n"):
        with pytest.raises(BearerConfigError):
            accepted_verifiers(missing)


def test_accepted_verifiers_ignores_a_blank_or_absent_upcoming_value() -> None:
    cur = bearer_verifier(OLD)
    for none in (None, "", "  \n"):
        assert accepted_verifiers(cur, none) == (cur,)


def test_accepted_verifiers_strips_whitespace_and_folds_case() -> None:
    cur, nxt = bearer_verifier(OLD), bearer_verifier(NEW)
    assert accepted_verifiers(cur.upper() + "\n", "  " + nxt + "\r\n") == (cur, nxt)


def test_identical_verifiers_collapse_to_one() -> None:
    cur = bearer_verifier(OLD)
    assert accepted_verifiers(cur, cur.upper()) == (cur,)


@pytest.mark.parametrize(
    "bad",
    [
        OLD,  # a plain bearer pasted where a verifier belongs
        "a" * 63,
        "a" * 65,
        "g" * 64,
        "sha256:" + "a" * 64,
        "a" * 32 + " " + "a" * 31,
        "é" * 64,
    ],
)
def test_a_malformed_verifier_is_refused_at_boot(bad: str) -> None:
    good = bearer_verifier(NEW)
    with pytest.raises(BearerConfigError):
        accepted_verifiers(bad)
    with pytest.raises(BearerConfigError):
        accepted_verifiers(good, bad)


def test_a_non_string_verifier_is_refused_at_boot() -> None:
    with pytest.raises(BearerConfigError):
        accepted_verifiers(bearer_verifier(OLD), 5)  # type: ignore[arg-type]


@pytest.mark.parametrize("bad", [OLD, "a" * 63, "sha256:" + "a" * 64])
def test_a_refusal_never_carries_the_value(bad: str) -> None:
    with pytest.raises(BearerConfigError) as exc:
        accepted_verifiers(bearer_verifier(NEW), bad)
    text = str(exc.value)
    assert bad not in text
    assert "upcoming" in text
    with pytest.raises(BearerConfigError) as exc2:
        accepted_verifiers(bad)
    assert bad not in str(exc2.value)
    assert "current" in str(exc2.value)


@pytest.mark.parametrize("bad", [None, "", "has space", "café", "a" * 5000, 5])
def test_bearer_verifier_refuses_a_token_that_cannot_travel(bad: object) -> None:
    with pytest.raises(BearerConfigError) as exc:
        bearer_verifier(bad)  # type: ignore[arg-type]
    if isinstance(bad, str) and bad:
        assert bad not in str(exc.value)


def test_nothing_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        held = accepted_verifiers(bearer_verifier(OLD), bearer_verifier(NEW))
        check_bearer_verifier(_h(OLD), held)
        check_bearer_verifier(_h("wrong-wrong-wrong-wrong"), held)
        check_bearer_verifier("garbage", held)
    assert caplog.records == []
