"""Supabase access-token verification. New projects sign with asymmetric keys published at the project's JWKS URL;
legacy projects use the shared HS256 secret (set SUPABASE_JWT_SECRET only for those). Audience is always enforced."""
import jwt

from . import config

try:
    from starlette.requests import Request
except ImportError:  # Flask apps call verify()/token_from() directly
    Request = object

AUDIENCE = "authenticated"
COOKIE = "sb-access-token"
_jwks = {}


class AuthError(Exception):
    pass


def _jwks_client():
    url = config.get("SUPABASE_URL").rstrip("/") + "/auth/v1/.well-known/jwks.json"
    if url not in _jwks:
        _jwks[url] = jwt.PyJWKClient(url, cache_keys=True, lifespan=600)
    return _jwks[url]


def verify(token):
    """Return the token's claims (sub = user id, email, ...) or raise AuthError."""
    if not token:
        raise AuthError("missing token")
    try:
        alg = jwt.get_unverified_header(token).get("alg")
        if alg == "HS256":
            secret = config.get("SUPABASE_JWT_SECRET", required=False)
            if not secret:
                raise AuthError("HS256 token but SUPABASE_JWT_SECRET is not set")
            return jwt.decode(token, secret, algorithms=["HS256"], audience=AUDIENCE)
        key = _jwks_client().get_signing_key_from_jwt(token).key
        return jwt.decode(token, key, algorithms=["ES256", "RS256", "EdDSA"], audience=AUDIENCE)
    except AuthError:
        raise
    except (jwt.PyJWTError, jwt.PyJWKClientError) as e:
        raise AuthError(str(e)) from e


def token_from(authorization_header=None, cookies=None):
    if authorization_header and authorization_header.lower().startswith("bearer "):
        return authorization_header[7:].strip()
    return (cookies or {}).get(COOKIE)


def current_user(request: Request):
    """FastAPI dependency: `user = Depends(auth.current_user)`. Raises 401 when not signed in."""
    from fastapi import HTTPException
    try:
        return verify(token_from(request.headers.get("authorization"), request.cookies))
    except AuthError:
        raise HTTPException(401, "Not signed in")


def optional_user(request: Request):
    try:
        return verify(token_from(request.headers.get("authorization"), request.cookies))
    except AuthError:
        return None
