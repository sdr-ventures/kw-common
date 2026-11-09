"""Check the caller's bearer, with a credential that can rotate without an outage.

An engine has exactly one caller, which presents one managed bearer in the `Authorization`
header. The engine holds the value (or, during a rotation, two) and answers every request with
one call:

    ACCEPTED = accepted_bearers(current_value, next_value_or_none)   # once, at boot
    ...
    if not check_bearer(request_header, ACCEPTED):                   # on every request
        refuse with 401 before reading the body

Standard library only, no logging, no environment reads: the caller passes what it holds.

⭐ HOLD A VERIFIER, NOT THE BEARER. `bearer_verifier(token)` is the hex SHA-256 of a bearer;
`accepted_verifiers` and `check_bearer_verifier` are the same pair as above for a service that
holds only that digest, so its configuration cannot be replayed as the credential. Holding the
plain bearer with `accepted_bearers`/`check_bearer` keeps working.

⭐ A ROTATION IS TWO VALUES, NOT A WINDOW OF DOWNTIME. The engine is given the current value and
the next one and accepts both. The caller then switches to the next value, and the engine is
restarted with only the next one. At no point does a request carrying a value the caller still
legitimately holds fail.

⚠️ WHAT IS CONSTANT-TIME AND WHAT IS NOT. Every accepted value is compared on every call, with
no early exit, using `hmac.compare_digest`, so which value matched (or whether one did) does not
show in the time taken. `compare_digest` does reveal the LENGTH of a mismatching value, which is
not a secret worth protecting here: a managed bearer is a long random string of a known length.

⛔ NOTHING HERE LOGS, AND NO ERROR TEXT CARRIES A VALUE. A refusal names the argument that was
wrong, never what it held, so a mistyped credential does not end up in a log.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable

__all__ = [
    "BearerConfigError",
    "accepted_bearers",
    "accepted_verifiers",
    "bearer_verifier",
    "check_bearer",
    "check_bearer_verifier",
]

_DIGEST_LEN = 64  # hex characters of a SHA-256 digest
_HEX = frozenset("0123456789abcdef")

# A header longer than this cannot be a managed bearer, and refusing it up front keeps a hostile
# caller from making every request do work proportional to what it sent.
_MAX_TOKEN = 4096


class BearerConfigError(ValueError):
    """The value(s) the service was configured with cannot be used as a bearer.

    Raised at boot, so a service refuses to start rather than start accepting nothing, or
    accepting something it should not. The message names the argument, never its value.
    """


def _token_ok(value: str) -> bool:
    """Printable ASCII with no whitespace: what can actually travel in a header after `Bearer `."""
    return value.isascii() and value.isprintable() and not any(c.isspace() for c in value)


def accepted_bearers(current: str | None, upcoming: str | None = None) -> tuple[str, ...]:
    """Build the accepted set once, at boot: the current value plus, during a rotation, the next.

    `current` is required: a service with no bearer configured must refuse to start, not start
    open (or start shut with nobody told). `upcoming` is optional, and None or blank means no
    rotation is under way. Leading and trailing whitespace is stripped from both, because a value
    pasted from a secret store often carries a trailing newline.

    Raises `BearerConfigError` for a missing `current`, for either value containing whitespace or
    non-printable or non-ASCII characters, or for a value longer than a header can carry.
    """
    out: list[str] = []
    for name, raw in (("current", current), ("upcoming", upcoming)):
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if name == "current":
                raise BearerConfigError("the current bearer is missing or blank")
            continue
        if not isinstance(raw, str):
            raise BearerConfigError(f"the {name} bearer is not a string")
        value = raw.strip()
        if not _token_ok(value) or len(value) > _MAX_TOKEN:
            raise BearerConfigError(
                f"the {name} bearer has a character or a length that cannot travel in a header"
            )
        out.append(value)
    # Two identical values are one value; keeping both would only hide a rotation that never moved.
    return tuple(dict.fromkeys(out))


def check_bearer(authorization: str | None, accepted: Iterable[str] | str | None) -> bool:
    """True only when `authorization` is `Bearer <token>` and the token equals an accepted value.

    `authorization` is the raw header value, or None when the header is absent. Anything else
    returns False and never raises: a missing header, a non-string, another scheme (`Basic …`),
    no token, extra parts, a token with whitespace or control or non-ASCII characters. The scheme
    word is case-insensitive and is followed by exactly one space, as clients send it.

    `accepted` is what `accepted_bearers` returned (a bare string is treated as one value).
    An empty set accepts nothing: this fails closed, it never fails open. Blank members are
    ignored, so a blank "next" value can never match an empty token.
    """
    presented = _presented(authorization)
    return presented is not None and _matches_any(presented, accepted)


def _presented(authorization: object) -> bytes | None:
    """The token of a well-formed `Bearer <token>` header as ASCII bytes, else None."""
    if not isinstance(authorization, str):
        return None
    scheme, sep, token = authorization.partition(" ")
    if sep != " " or scheme.lower() != "bearer":
        return None
    if not token or len(token) > _MAX_TOKEN or not _token_ok(token):
        return None
    return token.encode("ascii")


def _matches_any(presented: bytes, accepted: Iterable[str] | str | None) -> bool:
    if isinstance(accepted, str):
        accepted = (accepted,)
    if accepted is None:
        return False
    ok = False
    for value in accepted:
        if not isinstance(value, str) or not value:
            continue
        # `|=` and no break: every value is compared whatever matched first.
        ok |= hmac.compare_digest(presented, value.encode("utf-8"))
    return ok


def bearer_verifier(token: str) -> str:
    """The verifier of a bearer: the lowercase hex SHA-256 of its ASCII bytes (64 characters).

    A service holds this instead of the bearer, so its configuration cannot be replayed as the
    credential. Unsalted and fast on purpose: a managed bearer is a long random string, so there is
    nothing to guess, and the check must be cheap on every request. Raises `BearerConfigError` for
    a token that cannot travel in a header; the message never carries it.
    """
    if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN or not _token_ok(token):
        raise BearerConfigError("the bearer is missing or cannot travel in a header")
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def accepted_verifiers(current: str | None, upcoming: str | None = None) -> tuple[str, ...]:
    """Build the held verifier set once, at boot: the current one plus, in a rotation, the next.

    Each is the hex SHA-256 of a bearer, as `bearer_verifier` returns it. Whitespace around a value
    is stripped and case is folded, so a verifier pasted in upper case still matches. `current` is
    required; `upcoming` is optional and None or blank means no rotation is under way.

    Raises `BearerConfigError` for a missing `current`, or for a value that is not exactly 64 hex
    characters (a plain bearer pasted by mistake lands here). The message names the argument and
    never what it held.
    """
    out: list[str] = []
    for name, raw in (("current", current), ("upcoming", upcoming)):
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            if name == "current":
                raise BearerConfigError("the current verifier is missing or blank")
            continue
        if not isinstance(raw, str):
            raise BearerConfigError(f"the {name} verifier is not a string")
        value = raw.strip().lower()
        if len(value) != _DIGEST_LEN or any(c not in _HEX for c in value):
            raise BearerConfigError(
                f"the {name} verifier is not a {_DIGEST_LEN}-character hex SHA-256 digest"
            )
        out.append(value)
    return tuple(dict.fromkeys(out))


def check_bearer_verifier(authorization: str | None, verifiers: Iterable[str] | str | None) -> bool:
    """True only when `authorization` is `Bearer <token>` and the token's verifier is held.

    The presented token is hashed (`bearer_verifier`) and the digest compared in constant time
    with every held verifier, with no early exit. Header handling, fail-closed behaviour and
    "never raises on header content" are exactly those of `check_bearer`. `verifiers` is what
    `accepted_verifiers` returned.
    """
    presented = _presented(authorization)
    if presented is None:
        return False
    return _matches_any(hashlib.sha256(presented).hexdigest().encode("ascii"), verifiers)
