"""Token verification. No database needed."""
import uuid

import jwt
import pytest
from jwtutil import make_token, new_key

from app.auth import jwt as auth_jwt


@pytest.fixture
def key(monkeypatch):
    k = new_key()
    monkeypatch.setenv("SUPABASE_URL", "https://test.supabase.co")
    monkeypatch.setattr(auth_jwt, "get_signing_key", lambda token: k.public_key())
    return k


def test_valid_token_returns_user(key):
    uid = uuid.uuid4()
    user = auth_jwt.verify_token(make_token(key, sub=uid, email="Ana@Test.dev"))
    assert user.id == uid
    assert user.email == "ana@test.dev"


def test_expired_token_rejected(key):
    with pytest.raises(auth_jwt.AuthError):
        auth_jwt.verify_token(make_token(key, exp_in=-10))


@pytest.mark.parametrize("override", [
    {"aud": "someone-else"},
    {"iss": "https://evil.example/auth/v1"},
    {"role": "service_role"},
    {"role": "anon"},
    {"sub": "not-a-uuid"},
    {"is_anonymous": True},
])
def test_bad_claims_rejected(key, override):
    with pytest.raises(auth_jwt.AuthError):
        auth_jwt.verify_token(make_token(key, **override))


@pytest.mark.parametrize("missing", ["sub", "exp", "aud", "iss"])
def test_required_claims_must_be_present(key, missing):
    with pytest.raises(auth_jwt.AuthError):
        auth_jwt.verify_token(make_token(key, omit=(missing,)))


def test_token_signed_by_another_key_rejected(key):
    with pytest.raises(auth_jwt.AuthError):
        auth_jwt.verify_token(make_token(new_key()))


def test_tampered_payload_rejected(key):
    header, payload, sig = make_token(key).split(".")
    forged = jwt.encode({"sub": str(uuid.uuid4())}, new_key(), algorithm="ES256").split(".")[1]
    with pytest.raises(auth_jwt.AuthError):
        auth_jwt.verify_token(f"{header}.{forged}.{sig}")


def test_hs256_algorithm_confusion_rejected(key):
    """An attacker signs with HS256 using the (public) key bytes as the HMAC secret. Must never verify.
    PyJWT refuses to build this token itself, so it is forged by hand here."""
    import base64
    import hashlib
    import hmac
    import json

    from cryptography.hazmat.primitives import serialization

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    signing_input = (
        b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()) + "."
        + b64(json.dumps({"iss": "https://test.supabase.co/auth/v1", "aud": "authenticated",
                          "role": "authenticated", "sub": str(uuid.uuid4()), "exp": 4102444800}).encode())
    )
    sig = hmac.new(public_pem, signing_input.encode(), hashlib.sha256).digest()
    with pytest.raises(auth_jwt.AuthError):
        auth_jwt.verify_token(f"{signing_input}.{b64(sig)}")


def test_alg_none_rejected(key):
    import base64
    import json

    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    forged = f"{b64({'alg': 'none', 'typ': 'JWT'})}.{b64({'sub': str(uuid.uuid4()), 'aud': 'authenticated', 'iss': 'https://test.supabase.co/auth/v1', 'role': 'authenticated', 'exp': 4102444800})}."
    with pytest.raises(auth_jwt.AuthError):
        auth_jwt.verify_token(forged)


def test_garbage_rejected(key):
    for bad in ["", "abc", "a.b.c", "Bearer x"]:
        with pytest.raises(auth_jwt.AuthError):
            auth_jwt.verify_token(bad)
