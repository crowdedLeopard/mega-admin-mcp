"""Inbound authentication: validate the Entra bearer token Copilot Studio sends.

Copilot Studio (Manual OAuth 2.0) signs the admin in with OUR app registration
and sends us an access token whose audience is this API (api://<client-id>).
We validate signature, issuer, audience and expiry against the tenant's JWKS
and keep the raw token so the token broker can exchange it on-behalf-of the
signed-in admin.
"""
from __future__ import annotations

import contextvars
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import jwt
from jwt import PyJWKClient
from jwt.exceptions import PyJWKClientConnectionError

from ..config import settings

log = logging.getLogger("megamcp.auth")


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401, detail: dict | None = None, kind: str = "rejected", fix: str = "", seen: str = ""):
        super().__init__(message)
        self.status = status
        self.detail = detail or {}
        self.kind = kind  # one row in the connection log per kind
        self.fix = fix  # what the person setting this up should change
        self.seen = seen  # the unverified value that gave it away, for the setup page


@dataclass
class Principal:
    """Who is calling us. In 'none' and 'apikey' modes this is synthetic."""

    mode: str
    oid: str = ""
    tid: str = ""
    upn: str = ""
    name: str = ""
    app_id: str = ""
    scopes: list[str] = field(default_factory=list)
    roles: list[str] = field(default_factory=list)
    raw_token: str = ""
    claims: dict[str, Any] = field(default_factory=dict)

    @property
    def cache_key(self) -> str:
        return self.oid or self.app_id or self.mode

    def summary(self) -> dict[str, Any]:
        return {
            "auth_mode": self.mode,
            "object_id": self.oid,
            "tenant_id": self.tid,
            "user_principal_name": self.upn,
            "display_name": self.name,
            "client_app_id": self.app_id,
            "scopes": self.scopes,
            "roles": self.roles,
        }


_current: contextvars.ContextVar[Principal | None] = contextvars.ContextVar("megamcp_principal", default=None)


def set_principal(p: Principal | None) -> contextvars.Token:
    return _current.set(p)


def current_principal() -> Principal:
    p = _current.get()
    if p is None:
        # Local/none mode without middleware (e.g. unit tests)
        return Principal(mode=settings.auth_mode or "none")
    return p


class _JwksCache:
    """One PyJWKClient per tenant, created lazily and refreshed by PyJWT itself."""

    def __init__(self) -> None:
        self._clients: dict[str, PyJWKClient] = {}
        self._lock = threading.Lock()

    def client(self, tenant: str) -> PyJWKClient:
        with self._lock:
            c = self._clients.get(tenant)
            if c is None:
                url = f"https://login.microsoftonline.com/{tenant}/discovery/v2.0/keys"
                c = PyJWKClient(url, cache_keys=True, lifespan=3600, timeout=15)
                self._clients[tenant] = c
            return c


_jwks = _JwksCache()


def _expected_issuers(tid: str) -> set[str]:
    return {
        f"https://login.microsoftonline.com/{tid}/v2.0",
        f"https://sts.windows.net/{tid}/",
    }


def _wrong_audience(aud: Any) -> AuthError:
    return AuthError(
        "Token audience is not this API. In Copilot Studio set Scopes to '<client-id>/.default offline_access' (GUID form)",
        detail={"aud": aud, "accepted": settings.accepted_audiences}, kind="wrong_audience",
        fix=f"The token was issued for another API. Set Scopes in the wizard to '{settings.client_id}/.default offline_access', then create a new connection.",
        seen=str(aud),
    )


def validate_bearer(token: str) -> Principal:
    """Validate an Entra access token issued for this API and return the principal."""
    # Never fall through to "any Microsoft token will do": with no tenant or client id there is nothing to check the token against.
    if not settings.tenant_id or not settings.client_id:
        raise AuthError("This server is not configured yet: TENANT_ID and CLIENT_ID are required.", status=503, kind="not_configured",
                        fix="Set TENANT_ID and CLIENT_ID on the host, or run scripts/install.ps1.")
    if not token:
        raise AuthError("Missing bearer token", kind="no_token",
                        fix="In the Copilot Studio wizard choose OAuth 2.0, then Manual, and create a connection. Isolated hits here with no accepted requests can just be internet scanners.")
    try:
        unverified = jwt.decode(token, options={"verify_signature": False, "verify_exp": False})
    except jwt.PyJWTError as e:
        raise AuthError(f"Token is not a valid JWT: {e}", kind="not_a_jwt",
                        fix="The caller sent something that is not an Entra access token. Check Authentication is OAuth 2.0 Manual with this server's values.") from e

    tid = unverified.get("tid") or settings.tenant_id
    if tid != settings.tenant_id:
        raise AuthError("Token was issued by another tenant", detail={"token_tid": tid, "expected": settings.tenant_id}, kind="wrong_tenant",
                        fix=f"Sign in with an account from tenant {settings.tenant_id}, and check the Authorization and Token URLs in the wizard contain that tenant id.", seen=str(tid))

    # Check this before the signature. A wrong scope usually means a Microsoft Graph token, and those don't verify
    # against the published keys, so the signature check would hide the real reason behind a vague one.
    aud = unverified.get("aud")
    if not set(aud if isinstance(aud, list) else [aud]) & set(settings.accepted_audiences):
        raise _wrong_audience(aud)

    try:
        signing_key = _jwks.client(tid).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=settings.accepted_audiences or None,
            options={"verify_aud": bool(settings.accepted_audiences), "require": ["exp", "iat"]},
            leeway=60,
        )
    except PyJWKClientConnectionError as e:
        # Our problem, not the caller's: a 401 here would send Copilot Studio round a sign-in loop that cannot help.
        raise AuthError("The server cannot reach Microsoft to check the token's signature", status=503, kind="signing_keys_unreachable",
                        fix="Allow outbound HTTPS from this host to login.microsoftonline.com.") from e
    except jwt.ExpiredSignatureError as e:
        raise AuthError("Token has expired", detail={"error": "token_expired"}, kind="token_expired",
                        fix=f"Isolated hits are normal. If it keeps happening the connection has no refresh token: Scopes must be '{settings.client_id}/.default offline_access'; then reconnect.") from e
    except jwt.InvalidAudienceError as e:
        raise _wrong_audience(aud) from e
    except jwt.PyJWTError as e:
        raise AuthError(f"Token validation failed: {e}", kind="invalid_token",
                        fix="The token's signature or claims did not check out. Create a new connection; if that fails, check the wizard values against this page.") from e

    iss = claims.get("iss", "")
    if iss not in _expected_issuers(tid):
        raise AuthError("Unexpected token issuer", detail={"iss": iss}, kind="wrong_issuer", fix="Check the Authorization and Token URLs in the wizard.", seen=str(iss))

    scopes = (claims.get("scp") or "").split() if isinstance(claims.get("scp"), str) else list(claims.get("scp") or [])
    return Principal(
        mode="delegated",
        oid=claims.get("oid", ""),
        tid=tid,
        upn=claims.get("preferred_username") or claims.get("upn") or claims.get("unique_name") or "",
        name=claims.get("name", ""),
        app_id=claims.get("azp") or claims.get("appid") or "",
        scopes=scopes,
        roles=list(claims.get("roles") or []),
        raw_token=token,
        claims={k: v for k, v in claims.items() if k not in ("aud", "iss", "iat", "nbf", "exp", "aio", "rh", "uti", "sub")},
    )


def principal_from_request_headers(headers: dict[str, str]) -> Principal:
    """Apply the configured AUTH_MODE to the inbound headers."""
    mode = settings.auth_mode
    h = {k.lower(): v for k, v in headers.items()}
    if settings.no_auth_refused:
        raise AuthError("AUTH_MODE=none is refused on a hosting platform: this server would be open to the internet.", status=503, kind="no_auth_refused",
                        fix="Set AUTH_MODE=delegated, or ALLOW_NO_AUTH=true if something else in front of it does the sign-in.")
    if mode == "none":
        return Principal(mode="none", name="local developer", oid="local", tid=settings.tenant_id)
    if mode == "apikey":
        if not settings.api_key:
            raise AuthError("This server is not configured yet: MCP_API_KEY is required in apikey mode.", status=503, kind="not_configured", fix="Set MCP_API_KEY on the host.")
        provided = h.get(settings.api_key_header, "")
        if not provided:
            auth = h.get("authorization", "")
            if auth.lower().startswith("bearer "):
                provided = auth[7:].strip()
        if not provided or not _constant_time_eq(provided, settings.api_key):
            raise AuthError(f"Missing or invalid API key (header {settings.api_key_header})", kind="bad_api_key",
                            fix=f"Send the key in the {settings.api_key_header} header.")
        return Principal(mode="apikey", name="api-key caller", oid="apikey", tid=settings.tenant_id, app_id=settings.client_id)
    # delegated
    auth = h.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return validate_bearer("")
    return validate_bearer(auth[7:].strip())


def _constant_time_eq(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a.encode(), b.encode())


def protected_resource_metadata() -> dict[str, Any]:
    """RFC 9728 document so OAuth-aware MCP clients can discover Entra."""
    base = settings.public_base_url or ""
    return {
        "resource": f"{base}{settings.mcp_path}" if base else settings.mcp_path,
        "authorization_servers": [settings.authority + "/v2.0"],
        "scopes_supported": [f"api://{settings.client_id}/access_as_user"] if settings.client_id else [],
        "bearer_methods_supported": ["header"],
        "resource_documentation": "https://github.com/crowdedLeopard/mega-admin-mcp",
        "generated_at": int(time.time()),
    }
