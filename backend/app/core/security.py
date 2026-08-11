"""Supabase JWT verification.

The backend never issues tokens — GoTrue does. This module's only job is to
decide whether an incoming bearer token is genuine, and to extract the claims
that are subsequently handed to Postgres as `request.jwt.claims`.

That handoff is the reason verification has to be strict: whatever comes out of
here is what `auth.uid()` returns inside every RLS policy for the rest of the
request. An unverified claims blob would be an authentication bypass with extra
steps.
"""

from typing import Any, Dict, Optional

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings, get_settings

bearer_scheme = HTTPBearer(auto_error=False)


class AuthError(HTTPException):
    def __init__(self, detail: str):
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
        )


_ASYMMETRIC = ("ES256", "RS256", "EdDSA")

# One client per JWKS URL. PyJWKClient caches the fetched key set internally,
# so this avoids an HTTP round trip to GoTrue on every single request while
# still picking up a rotated signing key.
_jwks_clients: Dict[str, "jwt.PyJWKClient"] = {}


def _jwks_client(settings: Settings) -> "jwt.PyJWKClient":
    url = f"{settings.supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json"
    client = _jwks_clients.get(url)
    if client is None:
        client = jwt.PyJWKClient(url, cache_keys=True, lifespan=600)
        _jwks_clients[url] = client
    return client


def decode_token(token: str, settings: Settings) -> Dict[str, Any]:
    """Verify a Supabase access token.

    Supabase issues ES256 tokens signed with a rotating key published at the
    JWKS endpoint, and older projects (plus some self-hosted setups) still use
    HS256 with a shared secret. Both are accepted, but the algorithm is taken
    from the token header and then *pinned* to the matching verification path —
    it is never used to choose whether verification happens.

    The mismatch to avoid here is the classic one: accepting an HS256 token
    signed with the public key of an asymmetric pair. Because each branch below
    passes exactly one algorithm to `jwt.decode`, a token claiming HS256 can
    only ever be checked against the shared secret.
    """
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise AuthError(f"Malformed authentication token: {exc}")

    alg = header.get("alg")

    common = {
        "audience": settings.jwt_audience,
        "options": {"require": ["exp", "sub"]},
    }

    try:
        if alg in _ASYMMETRIC:
            signing_key = _jwks_client(settings).get_signing_key_from_jwt(token)
            return jwt.decode(token, signing_key.key, algorithms=[alg], **common)

        if alg == "HS256":
            return jwt.decode(
                token, settings.supabase_jwt_secret, algorithms=["HS256"], **common
            )

        raise AuthError(f"Unsupported token algorithm: {alg}")

    except jwt.ExpiredSignatureError:
        raise AuthError("Session expired. Please sign in again.")
    except jwt.InvalidAudienceError:
        raise AuthError("Token was not issued for this application.")
    except jwt.PyJWKClientError as exc:
        raise AuthError(f"Could not verify token signing key: {exc}")
    except jwt.PyJWTError as exc:
        raise AuthError(f"Invalid authentication token: {exc}")


async def get_token_claims(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(bearer_scheme),
    settings: Settings = Depends(get_settings),
) -> Dict[str, Any]:
    if credentials is None or not credentials.credentials:
        raise AuthError("Not authenticated.")
    if credentials.scheme.lower() != "bearer":
        raise AuthError("Authorization scheme must be Bearer.")

    claims = decode_token(credentials.credentials, settings)

    if not claims.get("sub"):
        raise AuthError("Token has no subject.")

    return claims
