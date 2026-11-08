"""Bearer token -> caller role.

The token is an HS256 JWT. It must be signed with the gateway secret, must carry an `exp`
claim that is still in the future, and must carry a `role` claim that names a known role.
"""

from enum import StrEnum

import jwt

JWT_ALGORITHM = "HS256"
BEARER_SCHEME = "bearer"
REQUIRED_CLAIMS = ("exp", "role")


class Role(StrEnum):
    ADMIN = "admin"
    VIEWER = "viewer"


class AuthenticationError(Exception):
    """The request carries no usable Bearer token. The reason is for logs only."""


def role_from_authorization(header: str | None, secret: str) -> Role:
    scheme, _, token = (header or "").strip().partition(" ")
    token = token.strip()
    if scheme.lower() != BEARER_SCHEME or not token:
        raise AuthenticationError("missing or non-Bearer Authorization header")
    try:
        # `algorithms` is pinned, so "none" and algorithm-confusion tokens fail here.
        claims = jwt.decode(
            token, secret, algorithms=[JWT_ALGORITHM], options={"require": list(REQUIRED_CLAIMS)}
        )
    except jwt.InvalidTokenError as exc:
        raise AuthenticationError(f"invalid token: {type(exc).__name__}") from exc
    try:
        return Role(claims["role"])
    except ValueError as exc:
        raise AuthenticationError("unknown role claim") from exc
