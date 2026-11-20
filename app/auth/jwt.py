"""Verify Supabase Auth access tokens.

Supabase signs tokens with an asymmetric key (ES256 for this project) and publishes the public
keys at /auth/v1/.well-known/jwks.json, so no secret is needed here. We accept only asymmetric
algorithms (never HS256, which would allow an algorithm-confusion attack with the public key),
and require signature, expiry, audience, issuer and subject.
"""
from dataclasses import dataclass, field
from uuid import UUID

import jwt
from jwt import PyJWKClient

from app.config import supabase_url

ALLOWED_ALGORITHMS = ["ES256", "RS256"]
AUDIENCE = "authenticated"


class AuthError(Exception):
    """The token is missing, malformed, expired, or otherwise not acceptable."""


@dataclass(frozen=True)
class AuthUser:
    id: UUID
    email: str | None
    claims: dict = field(default_factory=dict, repr=False)


_clients: dict[str, PyJWKClient] = {}


def _issuer() -> str:
    return f"{supabase_url()}/auth/v1"


def get_signing_key(token: str):
    """Fetch (and cache) the public key that signed `token`. Replaced in tests."""
    jwks_url = f"{_issuer()}/.well-known/jwks.json"
    client = _clients.get(jwks_url)
    if client is None:
        client = _clients[jwks_url] = PyJWKClient(jwks_url, cache_keys=True, lifespan=3600)
    return client.get_signing_key_from_jwt(token).key


def verify_token(token: str) -> AuthUser:
    try:
        key = get_signing_key(token)
        claims = jwt.decode(
            token,
            key,
            algorithms=ALLOWED_ALGORITHMS,
            audience=AUDIENCE,
            issuer=_issuer(),
            options={"require": ["exp", "sub", "aud", "iss"]},
        )
        if claims.get("role") != "authenticated":
            raise AuthError("not an authenticated user token")
        if claims.get("is_anonymous"):
            raise AuthError("anonymous sessions are not allowed")
        user_id = UUID(str(claims["sub"]))
    except AuthError:
        raise
    except (jwt.PyJWTError, ValueError, KeyError) as e:
        raise AuthError(str(e)) from e
    email = claims.get("email")
    return AuthUser(id=user_id, email=email.lower() if isinstance(email, str) else None, claims=claims)
