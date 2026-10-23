"""Token broker: get a token for any downstream Microsoft API.

Three strategies, chosen by AUTH_MODE:
  delegated -> MSAL on-behalf-of exchange of the caller's token (the admin's
               own permissions apply, every call is attributable to them)
  apikey    -> the server's own identity: managed identity or client secret
  none      -> local developer credentials (Azure CLI, then DefaultAzureCredential)

Tokens are cached per (caller, scope) until five minutes before expiry.
Self-healing: a 401 downstream clears the cache entry and retries once.
"""
from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from ..config import settings
from .entra import Principal

log = logging.getLogger("megamcp.tokens")


class TokenError(Exception):
    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail or {}

    def to_dict(self) -> dict[str, Any]:
        return {"error": "token_acquisition_failed", "message": str(self), **self.detail}


@dataclass
class _Cached:
    token: str
    expires_at: float


class TokenBroker:
    def __init__(self) -> None:
        self._cache: dict[tuple[str, str], _Cached] = {}
        self._lock = threading.Lock()
        self._msal_app = None
        self._msal_app_created = 0.0
        self._cli_cred = None
        self._app_cred = None

    # ------------------------------------------------------------------ public
    async def get_token(self, scope: str, principal: Principal, force_refresh: bool = False) -> str:
        key = (principal.cache_key, scope)
        if not force_refresh:
            with self._lock:
                c = self._cache.get(key)
            if c and c.expires_at - 300 > time.time():
                return c.token
        token, exp = await asyncio.to_thread(self._acquire, scope, principal)
        with self._lock:
            self._cache[key] = _Cached(token, exp)
        return token

    def invalidate(self, scope: str, principal: Principal) -> None:
        with self._lock:
            self._cache.pop((principal.cache_key, scope), None)

    def describe(self) -> dict[str, Any]:
        return {
            "auth_mode": settings.auth_mode,
            "cached_tokens": len(self._cache),
            "client_id": settings.client_id,
            "tenant_id": settings.tenant_id,
            "credential": self._credential_description(),
        }

    # ----------------------------------------------------------------- private
    def _credential_description(self) -> str:
        if settings.auth_mode == "delegated":
            if settings.use_managed_identity_fic:
                return "MSAL on-behalf-of with managed identity federated credential"
            if settings.client_certificate_path:
                return "MSAL on-behalf-of with certificate"
            return "MSAL on-behalf-of with client secret"
        if settings.auth_mode == "apikey":
            return "managed identity" if settings.use_managed_identity else "client credentials"
        return "Azure CLI / DefaultAzureCredential (local developer)"

    def _acquire(self, scope: str, principal: Principal) -> tuple[str, float]:
        mode = settings.auth_mode
        if mode == "delegated":
            if principal.mode != "delegated" or not principal.raw_token:
                raise TokenError("No delegated user token on this request")
            return self._obo(scope, principal.raw_token)
        if mode == "apikey":
            return self._app_identity(scope)
        return self._developer(scope)

    # --- delegated (OBO) ------------------------------------------------------
    def _msal(self):
        import msal

        # Rebuild hourly when using a managed-identity federated credential so
        # the client assertion stays fresh.
        if self._msal_app is not None and (not settings.use_managed_identity_fic or time.time() - self._msal_app_created < 3000):
            return self._msal_app
        if settings.use_managed_identity_fic:
            credential: Any = {"client_assertion": self._mi_client_assertion()}
        elif settings.client_certificate_path:
            with open(settings.client_certificate_path, "r", encoding="utf-8") as f:
                pem = f.read()
            credential = {"private_key": pem, "thumbprint": settings.client_certificate_thumbprint}
        else:
            credential = settings.client_secret
        self._msal_app = msal.ConfidentialClientApplication(
            settings.client_id,
            authority=settings.authority,
            client_credential=credential,
        )
        self._msal_app_created = time.time()
        return self._msal_app

    def _mi_client_assertion(self) -> str:
        from azure.identity import ManagedIdentityCredential

        kwargs = {"client_id": settings.managed_identity_client_id} if settings.managed_identity_client_id else {}
        cred = ManagedIdentityCredential(**kwargs)
        return cred.get_token("api://AzureADTokenExchange/.default").token

    def _obo(self, scope: str, user_assertion: str) -> tuple[str, float]:
        app = self._msal()
        result = app.acquire_token_on_behalf_of(user_assertion=user_assertion, scopes=[scope])
        if "access_token" not in result:
            err = result.get("error", "unknown_error")
            desc = result.get("error_description", "")
            detail: dict[str, Any] = {
                "aad_error": err,
                "description": desc[:600],
                "suberror": result.get("suberror"),
                "correlation_id": result.get("correlation_id"),
                "scope": scope,
            }
            if "AADSTS65001" in desc or err in ("invalid_grant", "interaction_required"):
                detail["fix"] = (
                    "The app registration has not been consented for this API. Grant admin consent "
                    "(Entra admin centre > App registrations > API permissions > Grant admin consent) or open the consent URL."
                )
                detail["admin_consent_url"] = (
                    f"https://login.microsoftonline.com/{settings.tenant_id}/adminconsent?client_id={settings.client_id}"
                )
            if "AADSTS50013" in desc or "AADSTS500131" in desc:
                detail["fix"] = "The incoming token audience is wrong. Scopes in Copilot Studio must be api://<client-id>/.default."
            if result.get("claims"):
                detail["claims_challenge"] = result["claims"]
                detail["fix"] = "Conditional Access requires the admin to re-authenticate. Ask them to sign in again in Copilot Studio."
            raise TokenError(f"On-behalf-of exchange failed for {scope}", detail)
        return result["access_token"], time.time() + int(result.get("expires_in", 3600))

    # --- app identity ---------------------------------------------------------
    def _app_identity(self, scope: str) -> tuple[str, float]:
        if self._app_cred is None:
            from azure.identity import CertificateCredential, ClientSecretCredential, ManagedIdentityCredential

            if settings.use_managed_identity:
                kwargs = {"client_id": settings.managed_identity_client_id} if settings.managed_identity_client_id else {}
                self._app_cred = ManagedIdentityCredential(**kwargs)
            elif settings.client_certificate_path:
                self._app_cred = CertificateCredential(settings.tenant_id, settings.client_id, settings.client_certificate_path)
            else:
                self._app_cred = ClientSecretCredential(settings.tenant_id, settings.client_id, settings.client_secret)
        try:
            t = self._app_cred.get_token(scope)
        except Exception as e:  # noqa: BLE001
            raise TokenError(f"App identity could not get a token for {scope}: {e}") from e
        return t.token, float(t.expires_on)

    # --- local developer ------------------------------------------------------
    def _developer(self, scope: str) -> tuple[str, float]:
        if self._cli_cred is None:
            from azure.identity import AzureCliCredential, DefaultAzureCredential

            class _Chain:
                def __init__(self) -> None:
                    self.cli = AzureCliCredential()
                    self.default = None

                def get_token(self, s: str):
                    try:
                        return self.cli.get_token(s)
                    except Exception:  # noqa: BLE001
                        if self.default is None:
                            self.default = DefaultAzureCredential(exclude_interactive_browser_credential=True)
                        return self.default.get_token(s)

            self._cli_cred = _Chain()
        try:
            t = self._cli_cred.get_token(scope)
        except Exception as e:  # noqa: BLE001
            raise TokenError(
                f"Local credentials could not get a token for {scope}. Run 'az login' (and 'az account set') first.",
                {"underlying": str(e)[:300]},
            ) from e
        return t.token, float(t.expires_on)


broker = TokenBroker()
