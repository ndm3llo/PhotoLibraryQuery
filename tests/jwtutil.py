"""Helpers to mint Supabase-style tokens signed with a throwaway ES256 key."""
import time
import uuid

import jwt
from cryptography.hazmat.primitives.asymmetric import ec

ISSUER = "https://test.supabase.co/auth/v1"


def new_key():
    return ec.generate_private_key(ec.SECP256R1())


def make_token(key, sub=None, email="user@test.dev", *, alg="ES256", exp_in=3600, omit=(), **overrides) -> str:
    payload = {
        "iss": ISSUER,
        "aud": "authenticated",
        "sub": str(sub or uuid.uuid4()),
        "email": email,
        "role": "authenticated",
        "iat": int(time.time()),
        "exp": int(time.time()) + exp_in,
    }
    payload.update(overrides)
    payload = {k: v for k, v in payload.items() if v is not None and k not in omit}
    return jwt.encode(payload, key, algorithm=alg)
