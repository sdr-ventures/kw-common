"""Check the caller's bearer, with a credential that can rotate without an outage.

An engine has exactly one caller, which presents one managed bearer in the `Authorization`
header. The engine holds the value (or, during a rotation, two) and answers every request with
one call:

    ACCEPTED = accepted_bearers(current_value, next_value_or_none)   # once, at boot
    ...
    if not check_bearer(request_header, ACCEPTED):                   # on every request
        refuse with 401 before reading the body

Standard library only, no logging, no environment reads: the caller passes what it holds.

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

import hmac
from collections.abc import Iterable

__all__ = ["BearerConfigError", "accepted_bearers", "check_bearer"]

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
    if isinstance(accepted, str):
        accepted = (accepted,)
    if not isinstance(authorization, str) or accepted is None:
        return False
    scheme, sep, token = authorization.partition(" ")
    if sep != " " or scheme.lower() != "bearer":
        return False
    if not token or len(token) > _MAX_TOKEN or not _token_ok(token):
        return False
    presented = token.encode("ascii")
    ok = False
    for value in accepted:
        if not isinstance(value, str) or not value:
            continue
        # `|=` and no break: every value is compared whatever matched first.
        ok |= hmac.compare_digest(presented, value.encode("utf-8"))
    return ok
