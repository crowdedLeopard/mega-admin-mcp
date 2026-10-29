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


# Entra errors that mean the server's own credential was refused (not the caller's access).
_SWITCH_TO_MI = ("Or stop using a secret: run scripts/use-managed-identity.ps1, which makes the App Service's managed identity "
                 "the app's credential and sets USE_MANAGED_IDENTITY_FIC=true.")
_CREDENTIAL_FIXES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("AADSTS7000215",), "Entra rejected the server's client secret. The CLIENT_SECRET App Service setting does not hold the Value of a "
                         "current secret on the app registration: it is often the Secret ID pasted instead of the Value, or a secret that "
                         "has since been deleted. Create a secret, paste its Value into CLIENT_SECRET, and the app restarts. " + _SWITCH_TO_MI),
    (("AADSTS7000222",), "The server's client secret has expired. Create a new secret on the app registration and paste its Value into "
                         "CLIENT_SECRET. " + _SWITCH_TO_MI),
    (("AADSTS7000216",), "The server sent no credential. Set CLIENT_SECRET, a certificate, or USE_MANAGED_IDENTITY_FIC=true."),
    (("AADSTS70021", "AADSTS700211", "AADSTS700212", "AADSTS700213"),
     "Entra found no federated credential on the app registration matching this App Service's managed identity. Add one (Certificates "
     "and secrets > Federated credentials, scenario Managed identity, pick the web app's identity) or run "
     "scripts/use-managed-identity.ps1. If the web app was recreated, its identity changed and the credential must be added again."),
    (("AADSTS700027", "AADSTS700024"), "Entra rejected the server's certificate or client assertion (wrong certificate, thumbprint, or "
                                       "the clock is off). Check CLIENT_CERTIFICATE_PATH and CLIENT_CERTIFICATE_THUMBPRINT."),
    (("AADSTS700016",), "Entra cannot find the app. CLIENT_ID or TENANT_ID is wrong, or the app registration was deleted."),
    (("AADSTS7000112",), "The app registration is disabled in this tenant. An Application Administrator can re-enable it."),
    (("ManagedIdentityCredential", "managed identity", "IDENTITY_ENDPOINT"),
     "USE_MANAGED_IDENTITY_FIC=true, but the App Service has no managed identity the server can reach. Turn on the system-assigned "
     "identity (App Service > Identity) and add it as a federated credential on the app registration, or set "
     "USE_MANAGED_IDENTITY_FIC=false and use CLIENT_SECRET."),
)


def is_credential_error(text: str) -> bool:
    return any(code in text for codes, _ in _CREDENTIAL_FIXES[:-1] for code in codes)


def credential_fix(text: str) -> str:
    for codes, fix in _CREDENTIAL_FIXES:
        if any(code.lower() in text.lower() for code in codes):
            return fix
    return "Run doctor and read server_credential. Check TENANT_ID, CLIENT_ID and the credential settings on the App Service."


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
        self._mi_cred = None
        self._cred_check: dict[str, Any] | None = None
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

    # --- the server's own credential --------------------------------------------
    def credential_check(self, max_age: float | None = None) -> dict[str, Any]:
        """Prove the server's own credential (secret, certificate or managed identity) is accepted by Entra.

        Asks for an app-only Graph token. That needs no permissions and no user, so a failure here
        means the server cannot authenticate itself, whatever the signed-in admin's roles are.
        Cached: ten minutes when it works, one minute when it does not.
        """
        if settings.auth_mode != "delegated":
            return {"ok": True, "checked": False, "credential": self._credential_description(),
                    "note": "Only checked in delegated mode."}
        now = time.time()
        c = self._cred_check
        if c and now - c["_at"] < (max_age if max_age is not None else (600 if c["ok"] else 60)):
            return {k: v for k, v in c.items() if k != "_at"}
        out: dict[str, Any] = {"ok": False, "checked": True, "credential": self._credential_description()}
        try:
            result = self._msal().acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
            if "access_token" in result:
                out["ok"] = True
            else:
                desc = str(result.get("error_description", ""))
                out.update(aad_error=result.get("error"), description=desc[:400], fix=credential_fix(desc))
        except Exception as e:  # noqa: BLE001  (managed identity endpoint missing, unreadable certificate, ...)
            msg = str(e)
            out.update(aad_error=type(e).__name__, description=msg[:400], fix=credential_fix(msg))
        self._cred_check = {**out, "_at": now}
        return out

    # --- delegated (OBO) ------------------------------------------------------
    def _msal(self):
        import msal

        if self._msal_app is not None:
            return self._msal_app
        if settings.use_managed_identity_fic:
            # A callable: MSAL asks for a fresh assertion whenever it goes to Entra, and the
            # managed identity credential caches and renews its own token.
            credential: Any = {"client_assertion": self._mi_client_assertion}
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
        if self._mi_cred is None:
            from azure.identity import ManagedIdentityCredential

            kwargs = {"client_id": settings.managed_identity_client_id} if settings.managed_identity_client_id else {}
            self._mi_cred = ManagedIdentityCredential(**kwargs)
        return self._mi_cred.get_token("api://AzureADTokenExchange/.default").token

    def _obo(self, scope: str, user_assertion: str) -> tuple[str, float]:
        try:
            app = self._msal()
            result = app.acquire_token_on_behalf_of(user_assertion=user_assertion, scopes=[scope])
        except Exception as e:  # noqa: BLE001  (managed identity endpoint, certificate file, network)
            msg = str(e)
            raise TokenError(f"The server could not use its own credential for {scope}", {
                "aad_error": type(e).__name__, "description": msg[:600], "scope": scope,
                "server_credential_problem": True, "fix": credential_fix(msg)}) from e
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
            consent_problem = "AADSTS65001" in desc or result.get("suberror") == "consent_required"
            server_fix = credential_fix(desc) if is_credential_error(desc) else ""
            if server_fix:
                # The server could not prove its own identity. Nothing to do with the caller's access.
                detail["server_credential_problem"] = True
                detail["fix"] = server_fix
                self._cred_check = None
            elif consent_problem:
                detail["fix"] = (
                    "The signed-in admin (or the tenant) has not consented to this app for this API yet. Consent happens when the admin "
                    "signs in to the Copilot Studio connection with scopes '<client-id>/.default offline_access'. If the tier includes admin-only scopes, "
                    "a Global Administrator must grant admin consent once (Entra admin centre > App registrations > API permissions, or the URL below)."
                )
                detail["admin_consent_url"] = (
                    f"https://login.microsoftonline.com/{settings.tenant_id}/adminconsent?client_id={settings.client_id}"
                )
            elif "AADSTS50013" in desc or "AADSTS500131" in desc:
                detail["fix"] = "The incoming token audience is wrong. Scopes in Copilot Studio must be '<client-id>/.default offline_access' (GUID form)."
            elif result.get("claims") or err == "interaction_required":
                detail["claims_challenge"] = result.get("claims")
                detail["fix"] = "Conditional Access requires the admin to re-authenticate. Ask them to sign in again in Copilot Studio."
            elif err == "invalid_grant":
                detail["fix"] = "The incoming token could not be exchanged. Ask the admin to sign out of the Copilot Studio connection and sign in again."
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
