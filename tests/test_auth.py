import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

from launchkit import auth, config

KID = "test-key"
_private = ec.generate_private_key(ec.SECP256R1())


class FakeJWKClient:
    def get_signing_key_from_jwt(self, token):
        kid = jwt.get_unverified_header(token).get("kid")
        if kid != KID:
            raise jwt.PyJWKClientError("unknown kid")
        jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(_private.public_key()))
        return jwt.PyJWK({**jwk, "kid": KID, "alg": "ES256"})


@pytest.fixture(autouse=True)
def fake_jwks(monkeypatch):
    monkeypatch.setattr(auth, "_jwks_client", lambda: FakeJWKClient())
    config._overrides.pop("SUPABASE_JWT_SECRET", None)
    yield
    config._overrides.pop("SUPABASE_JWT_SECRET", None)


def token(aud="authenticated", exp_in=3600, key=_private, kid=KID, alg="ES256"):
    claims = {"sub": "user-1", "email": "a@b.co", "aud": aud, "exp": int(time.time()) + exp_in}
    return jwt.encode(claims, key, algorithm=alg, headers={"kid": kid})


def test_valid_es256():
    assert auth.verify(token())["sub"] == "user-1"


def test_wrong_audience():
    with pytest.raises(auth.AuthError):
        auth.verify(token(aud="anon"))


def test_expired():
    with pytest.raises(auth.AuthError):
        auth.verify(token(exp_in=-10))


def test_wrong_key():
    with pytest.raises(auth.AuthError):
        auth.verify(token(key=ec.generate_private_key(ec.SECP256R1())))


def test_unknown_kid():
    with pytest.raises(auth.AuthError):
        auth.verify(token(kid="other"))


def test_missing():
    with pytest.raises(auth.AuthError):
        auth.verify(None)


def test_hs256_requires_secret():
    t = token(key="shh-secret-that-is-long-enough-for-hs256", alg="HS256")
    with pytest.raises(auth.AuthError):
        auth.verify(t)
    config.configure(SUPABASE_JWT_SECRET="shh-secret-that-is-long-enough-for-hs256")
    assert auth.verify(t)["sub"] == "user-1"


def test_token_from():
    assert auth.token_from("Bearer abc", {}) == "abc"
    assert auth.token_from(None, {"sb-access-token": "xyz"}) == "xyz"
    assert auth.token_from(None, {}) is None


def test_fastapi_dependency():
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()

    @app.get("/me")
    def me(user=Depends(auth.current_user)):
        return {"sub": user["sub"]}

    c = TestClient(app)
    assert c.get("/me").status_code == 401
    assert c.get("/me", headers={"Authorization": "Bearer " + token()}).json() == {"sub": "user-1"}
    c.cookies.set("sb-access-token", token())
    assert c.get("/me").json() == {"sub": "user-1"}
