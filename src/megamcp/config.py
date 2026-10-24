"""Runtime configuration for Mega Admin MCP.

Everything is driven by environment variables so the same image runs locally,
in App Service, in Container Apps or anywhere else. No .env magic: set the
variables in the host and restart.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return default if v is None or v == "" else v


def _bool(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    if v is None or v == "":
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _list(name: str) -> list[str]:
    raw = _env(name, "")
    if not raw:
        return []
    parts = re.split(r"[\n;,]+", raw)
    return [p.strip() for p in parts if p.strip()]


DATA_DIR = Path(__file__).parent / "data"


@dataclass
class Settings:
    # --- who may call the MCP endpoint -------------------------------------
    # delegated : Copilot Studio sends an Entra bearer token for THIS app; we
    #             exchange it on-behalf-of the signed-in admin for every
    #             downstream Microsoft API. Recommended for production.
    # apikey    : header secret protects the endpoint; downstream calls use
    #             the server's own identity (managed identity or client
    #             secret). Good for scheduled FinOps jobs.
    # none      : local development only. No inbound auth; downstream calls
    #             use the developer's Azure CLI / DefaultAzureCredential.
    auth_mode: str = field(default_factory=lambda: _env("AUTH_MODE", "none").lower())

    tenant_id: str = field(default_factory=lambda: _env("TENANT_ID"))
    client_id: str = field(default_factory=lambda: _env("CLIENT_ID"))
    client_secret: str = field(default_factory=lambda: _env("CLIENT_SECRET"))
    client_certificate_path: str = field(default_factory=lambda: _env("CLIENT_CERTIFICATE_PATH"))
    client_certificate_thumbprint: str = field(default_factory=lambda: _env("CLIENT_CERTIFICATE_THUMBPRINT"))
    # Use the App Service / Container App managed identity as the federated
    # credential of the app registration (no secret to rotate).
    use_managed_identity_fic: bool = field(default_factory=lambda: _bool("USE_MANAGED_IDENTITY_FIC", False))
    # In apikey mode: use managed identity for downstream calls.
    use_managed_identity: bool = field(default_factory=lambda: _bool("USE_MANAGED_IDENTITY", False))
    managed_identity_client_id: str = field(default_factory=lambda: _env("MANAGED_IDENTITY_CLIENT_ID"))
    api_key: str = field(default_factory=lambda: _env("MCP_API_KEY"))
    api_key_header: str = field(default_factory=lambda: _env("MCP_API_KEY_HEADER", "x-api-key").lower())
    extra_audiences: list[str] = field(default_factory=lambda: _list("ACCEPTED_AUDIENCES"))
    public_base_url: str = field(default_factory=lambda: _env("PUBLIC_BASE_URL").rstrip("/"))

    # --- safety -----------------------------------------------------------
    read_only: bool = field(default_factory=lambda: _bool("READ_ONLY", False))
    allow_patterns: list[str] = field(default_factory=lambda: _list("ALLOW_PATTERNS"))
    deny_patterns: list[str] = field(default_factory=lambda: _list("DENY_PATTERNS"))
    require_confirm_for_writes: bool = field(default_factory=lambda: _bool("REQUIRE_CONFIRM_FOR_WRITES", True))
    max_response_chars: int = field(default_factory=lambda: _int("MAX_RESPONSE_CHARS", 60000))
    audit_log_size: int = field(default_factory=lambda: _int("AUDIT_LOG_SIZE", 500))

    # --- catalog (self-updating) ------------------------------------------
    catalog_path: str = field(default_factory=lambda: _env("CATALOG_PATH", str(DATA_DIR / "catalog.json.gz")))
    catalog_url: str = field(default_factory=lambda: _env(
        "CATALOG_URL",
        "https://github.com/crowdedLeopard/mega-admin-mcp/releases/latest/download/catalog.json.gz"))
    changelog_url: str = field(default_factory=lambda: _env(
        "CHANGELOG_URL",
        "https://github.com/crowdedLeopard/mega-admin-mcp/releases/latest/download/changelog.json"))
    catalog_refresh_hours: int = field(default_factory=lambda: _int("CATALOG_REFRESH_HOURS", 24))
    catalog_auto_refresh: bool = field(default_factory=lambda: _bool("CATALOG_AUTO_REFRESH", True))

    # --- behaviour --------------------------------------------------------
    request_timeout_seconds: int = field(default_factory=lambda: _int("REQUEST_TIMEOUT_SECONDS", 90))
    max_retries: int = field(default_factory=lambda: _int("MAX_RETRIES", 3))
    # Copilot Studio cancels a tool call after 120 s; every MCP request gets this
    # much time and the runbook caps retry waits and HTTP timeouts to what is left.
    tool_time_budget_seconds: int = field(default_factory=lambda: _int("TOOL_TIME_BUDGET_SECONDS", 100))
    graph_auto_beta_fallback: bool = field(default_factory=lambda: _bool("GRAPH_AUTO_BETA_FALLBACK", True))
    arm_auto_api_version: bool = field(default_factory=lambda: _bool("ARM_AUTO_API_VERSION", True))
    default_subscription_id: str = field(default_factory=lambda: _env("DEFAULT_SUBSCRIPTION_ID"))
    default_dataverse_url: str = field(default_factory=lambda: _env("DEFAULT_DATAVERSE_URL").rstrip("/"))
    powerplatform_api_version: str = field(default_factory=lambda: _env("POWERPLATFORM_API_VERSION", "2024-10-01"))
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO").upper())
    host: str = field(default_factory=lambda: _env("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: _int("PORT", 8000))
    mcp_path: str = field(default_factory=lambda: _env("MCP_PATH", "/mcp"))

    @property
    def authority(self) -> str:
        return f"https://login.microsoftonline.com/{self.tenant_id or 'organizations'}"

    @property
    def accepted_audiences(self) -> list[str]:
        auds = []
        if self.client_id:
            auds += [self.client_id, f"api://{self.client_id}"]
        if self.public_base_url:
            auds.append(self.public_base_url)
        auds += self.extra_audiences
        return auds

    def validate(self) -> list[str]:
        """Return a list of configuration problems (empty when healthy)."""
        problems: list[str] = []
        if self.auth_mode not in ("delegated", "apikey", "none"):
            problems.append(f"AUTH_MODE must be delegated, apikey or none (got {self.auth_mode!r})")
        if self.auth_mode == "delegated":
            if not self.tenant_id:
                problems.append("TENANT_ID is required in delegated mode")
            if not self.client_id:
                problems.append("CLIENT_ID is required in delegated mode")
            if not (self.client_secret or self.client_certificate_path or self.use_managed_identity_fic):
                problems.append("delegated mode needs CLIENT_SECRET, CLIENT_CERTIFICATE_PATH or USE_MANAGED_IDENTITY_FIC=true")
        if self.auth_mode == "apikey":
            if not self.api_key:
                problems.append("MCP_API_KEY is required in apikey mode")
            if not self.use_managed_identity and not (self.client_id and self.tenant_id and (self.client_secret or self.client_certificate_path)):
                problems.append("apikey mode needs USE_MANAGED_IDENTITY=true or CLIENT_ID + TENANT_ID + CLIENT_SECRET")
        return problems


settings = Settings()
