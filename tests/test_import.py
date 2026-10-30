"""What happens when someone else imports the server: wrong values, missing settings, restarts. Offline."""
from __future__ import annotations

import gzip
import json
import os
import time
from pathlib import Path

os.environ.setdefault("AUTH_MODE", "none")
os.environ.setdefault("CATALOG_AUTO_REFRESH", "false")

import jwt  # noqa: E402
import pytest  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from megamcp import __version__  # noqa: E402
from megamcp.app import app  # noqa: E402
from megamcp.auth import entra  # noqa: E402
from megamcp.auth.entra import AuthError, principal_from_request_headers, validate_bearer  # noqa: E402
from megamcp.catalog.store import CatalogStore, longpath  # noqa: E402
from megamcp.config import Settings, settings  # noqa: E402
from megamcp.diagnostics import connections  # noqa: E402
from megamcp.runbook.policy import AuditLog  # noqa: E402

TENANT = "11111111-1111-1111-1111-111111111111"
CLIENT = "22222222-2222-2222-2222-222222222222"
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
MCP_HEADERS = {"accept": "application/json, text/event-stream"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def delegated(monkeypatch):
    monkeypatch.setattr(settings, "auth_mode", "delegated")
    monkeypatch.setattr(settings, "tenant_id", TENANT)
    monkeypatch.setattr(settings, "client_id", CLIENT)
    connections.reset()
    yield settings
    connections.reset()


class _Keys:
    """Stands in for Entra's signing keys, so tokens here go through the real validation."""

    def __init__(self) -> None:
        self.private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def client(self, _tenant: str):
        public = self.private.public_key()

        class _Client:
            def get_signing_key_from_jwt(self, _token: str):
                return type("Key", (), {"key": public})()

        return _Client()

    def token(self, **claims) -> str:
        now = int(time.time())
        body = {"tid": TENANT, "aud": CLIENT, "iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "oid": "o1", "iat": now, "exp": now + 3600, **claims}
        return jwt.encode(body, self.private, algorithm="RS256")


@pytest.fixture
def keys(monkeypatch):
    k = _Keys()
    monkeypatch.setattr(entra, "_jwks", k)
    return k


def env_settings(monkeypatch, **values) -> Settings:
    for name in ("AUTH_MODE", "TENANT_ID", "CLIENT_ID", "CLIENT_SECRET", "USE_MANAGED_IDENTITY_FIC", "WEBSITE_SITE_NAME", "CONTAINER_APP_NAME",
                 "KUBERNETES_SERVICE_HOST", "IDENTITY_ENDPOINT", "MSI_ENDPOINT", "AZURE_FEDERATED_TOKEN_FILE", "HOST", "ALLOW_NO_AUTH", "PUBLIC_BASE_URL", "MCP_PATH"):
        monkeypatch.delenv(name, raising=False)
    for k, v in values.items():
        monkeypatch.setenv(k, v)
    return Settings()


# ------------------------------------------------------------ no open doors
def test_delegated_without_ids_refuses_every_token(monkeypatch, keys):
    """The bug this file exists for: with no tenant or client id, any Microsoft token used to be accepted."""
    monkeypatch.setattr(settings, "auth_mode", "delegated")
    monkeypatch.setattr(settings, "tenant_id", "")
    monkeypatch.setattr(settings, "client_id", "")
    with pytest.raises(AuthError) as e:
        validate_bearer(keys.token(tid="33333333-3333-3333-3333-333333333333"))
    assert e.value.status == 503 and e.value.kind == "not_configured"


def test_no_auth_defaults_and_refusal_on_a_host(monkeypatch):
    assert env_settings(monkeypatch).auth_mode == "none"
    assert env_settings(monkeypatch).host == "127.0.0.1", "a laptop with no sign-in must not listen to the network"
    hosted = env_settings(monkeypatch, WEBSITE_SITE_NAME="contoso")
    assert hosted.auth_mode == "delegated" and hosted.host == "0.0.0.0"
    assert any("TENANT_ID" in p for p in hosted.validate())
    forced = env_settings(monkeypatch, WEBSITE_SITE_NAME="contoso", AUTH_MODE="none")
    assert forced.no_auth_refused and any("AUTH_MODE=none" in p for p in forced.validate())
    assert not env_settings(monkeypatch, WEBSITE_SITE_NAME="contoso", AUTH_MODE="none", ALLOW_NO_AUTH="true").no_auth_refused


def test_no_auth_on_a_host_is_refused_at_the_door(monkeypatch):
    monkeypatch.setenv("WEBSITE_SITE_NAME", "contoso")
    monkeypatch.setattr(settings, "auth_mode", "none")
    monkeypatch.setattr(settings, "allow_no_auth", False)
    with pytest.raises(AuthError) as e:
        principal_from_request_headers({})
    assert e.value.status == 503


# ------------------------------------------------------------ wrong values
def test_validate_explains_values_pasted_into_the_wrong_box(monkeypatch):
    problems = " ".join(env_settings(monkeypatch, AUTH_MODE="delegated", TENANT_ID="contoso.onmicrosoft.com", CLIENT_ID="api://x",
                                     CLIENT_SECRET="44444444-4444-4444-4444-444444444444").validate())
    assert "TENANT_ID must be" in problems
    assert "CLIENT_ID must be" in problems
    assert "secret's ID" in problems
    fine = env_settings(monkeypatch, AUTH_MODE="delegated", TENANT_ID=TENANT, CLIENT_ID=CLIENT, CLIENT_SECRET="abc~notaguid")
    assert fine.validate() == []
    no_identity = " ".join(env_settings(monkeypatch, AUTH_MODE="delegated", TENANT_ID=TENANT, CLIENT_ID=CLIENT, USE_MANAGED_IDENTITY_FIC="true").validate())
    assert "no managed identity" in no_identity


def test_pasted_whitespace_and_urls_are_forgiven(monkeypatch):
    s = env_settings(monkeypatch, AUTH_MODE="delegated", TENANT_ID=f" {TENANT}\n", CLIENT_ID=f"{CLIENT} ", CLIENT_SECRET="abc~x",
                     PUBLIC_BASE_URL="https://x.azurewebsites.net/mcp/", MCP_PATH="mcp/")
    assert s.tenant_id == TENANT and s.client_id == CLIENT and s.validate() == []
    assert s.public_base_url == "https://x.azurewebsites.net" and s.mcp_path == "/mcp"


@pytest.mark.parametrize("claims,kind,hint", [
    ({"aud": "https://graph.microsoft.com"}, "wrong_audience", f"{CLIENT}/.default offline_access"),
    ({"tid": "33333333-3333-3333-3333-333333333333"}, "wrong_tenant", TENANT),
    ({"exp": 1000, "iat": 900}, "token_expired", "offline_access"),
    ({"iss": "https://evil.example.com/"}, "wrong_issuer", "URLs"),
])
def test_refused_tokens_say_what_to_change(delegated, keys, client, claims, kind, hint):
    r = client.post("/mcp", json=PING, headers={**MCP_HEADERS, "authorization": f"Bearer {keys.token(**claims)}"})
    assert r.status_code == 401 and r.json()["fix"]
    row = next(x for x in connections.summary()["refused"] if x["kind"] == kind)
    assert hint in row["fix"]
    assert "www-authenticate" in r.headers


def test_good_token_connects_and_is_recorded(delegated, keys, client):
    r = client.post("/mcp", json=PING, headers={**MCP_HEADERS, "authorization": f"Bearer {keys.token()}"})
    assert r.status_code == 200, r.text
    assert connections.summary()["requests_accepted"] == 1 and connections.summary()["last_accepted_utc"]


def test_signing_keys_unreachable_is_not_a_sign_in_problem(delegated, monkeypatch, keys, client):
    class Down:
        def client(self, _tenant):
            class C:
                def get_signing_key_from_jwt(self, _t):
                    raise entra.PyJWKClientConnectionError("no route")
            return C()

    token = keys.token()
    monkeypatch.setattr(entra, "_jwks", Down())
    r = client.post("/mcp", json=PING, headers={**MCP_HEADERS, "authorization": f"Bearer {token}"})
    assert r.status_code == 503 and "login.microsoftonline.com" in r.json()["fix"]


@pytest.mark.parametrize("path", ["/", "/mcp/"])
def test_a_slightly_wrong_server_url_still_works(client, path):
    connections.reset()
    r = client.post(path, json=PING, headers=MCP_HEADERS)
    assert r.status_code == 200, r.text
    assert bool(connections.summary()["server_url_without_mcp_seen_utc"]) == (path == "/")


# ------------------------------------------------------------ the setup page
def test_setup_page_has_the_exact_wizard_values(delegated, monkeypatch, client):
    monkeypatch.setattr(settings, "use_managed_identity_fic", False)
    monkeypatch.setattr(settings, "client_secret", "")
    page = client.get("/setup").text
    assert f"{CLIENT}/.default offline_access" in page
    assert f"https://login.microsoftonline.com/{TENANT}/oauth2/v2.0/authorize" in page
    assert "End user credentials" in page and "delegated mode needs" in page
    assert "/mcp" in page


def test_setup_page_escapes_what_a_token_or_link_can_put_there(delegated, keys, client):
    client.post("/mcp", json=PING, headers={**MCP_HEADERS, "authorization": f"Bearer {keys.token(aud='<script>alert(1)</script>')}"})
    r = client.get("/setup", params={"error": "<img src=x onerror=alert(2)>", "error_description": "<b>x</b>"})
    assert "<script>alert(1)" not in r.text and "<img src=x" not in r.text and "<b>x</b>" not in r.text
    assert "&lt;script&gt;" in r.text
    assert "default-src 'none'" in r.headers["content-security-policy"]


def test_setup_page_contains_no_secret(delegated, monkeypatch, client):
    monkeypatch.setattr(settings, "client_secret", "abc~SECRET-VALUE")
    assert "SECRET-VALUE" not in client.get("/setup").text
    assert "SECRET-VALUE" not in client.get("/ready").text


def test_browser_at_the_root_lands_on_setup(client):
    r = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "/setup"
    assert client.get("/", headers={"accept": "application/json"}).json()["setup"] == "/setup"


# ------------------------------------------------------------ surviving restarts and damage
def test_audit_log_survives_a_restart(tmp_path):
    first = AuditLog(100)
    first.attach(str(tmp_path / "state"))
    first.record({"user": "a@contoso.com", "method": "GET", "status": 200})
    with open(first._path, "a", encoding="utf-8") as f:
        f.write('{"cut short by a cra')
    second = AuditLog(100)
    second.attach(str(tmp_path / "state"))
    assert [e["user"] for e in second.recent()] == ["a@contoso.com"]
    assert second.stats()["stored_in"].endswith("audit.jsonl")


def test_audit_log_rotates_rather_than_filling_the_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(AuditLog, "MAX_FILE_BYTES", 200)
    log = AuditLog(10)
    log.attach(str(tmp_path))
    for i in range(50):
        log.record({"user": "a", "i": i})
    files = sorted(p.name for p in Path(tmp_path).iterdir())
    assert "audit.jsonl.1" in files and len(files) <= AuditLog.KEEP_FILES + 1


def test_unwritable_state_dir_never_stops_the_server(tmp_path):
    blocker = tmp_path / "a-file"
    blocker.write_text("x")
    log = AuditLog(10)
    log.attach(str(blocker / "state"))
    log.record({"user": "a"})
    assert log.recent() and log.persistence.startswith("memory only")


def test_damaged_catalog_never_stops_the_server(tmp_path, monkeypatch):
    bad = tmp_path / "catalog.json.gz"
    bad.write_bytes(b"this is not gzip")
    monkeypatch.setattr(settings, "catalog_path", str(bad))
    store = CatalogStore()
    store.load_bundled()
    assert store.source == "curated-only"
    truncated = gzip.compress(json.dumps({"operations": []}).encode())[:10]
    bad.write_bytes(truncated)
    store.load_bundled()
    assert store.source == "curated-only"


def test_server_says_when_newer_code_exists():
    store = CatalogStore()
    assert store.update_available() == ""
    store.meta = {"server_version": "99.0.0"}
    assert store.update_available() == "99.0.0"
    store.meta = {"server_version": __version__}
    assert store.update_available() == ""
    store.meta = {"server_version": "not a version"}
    assert store.update_available() == ""


# ------------------------------------------------------------ what gets deployed
ROOT = Path(__file__).resolve().parent.parent


def _text(*parts: str) -> str:
    with open(longpath(ROOT.joinpath(*parts)), encoding="utf-8") as f:
        return f.read()


def test_compiled_template_matches_what_the_installer_passes():
    template = json.loads(_text("deploy", "azuredeploy.json"))
    params = template["parameters"]
    for needed in ("appName", "location", "sku", "tenantId", "clientId", "useManagedIdentityFederatedCredential", "containerImage", "defaultSubscriptionId"):
        assert needed in params, needed
    assert "none" not in params["authMode"]["allowedValues"]
    assert params["containerImage"]["defaultValue"].startswith("ghcr.io/crowdedleopard/")


def test_version_is_the_same_everywhere():
    assert f'version = "{__version__}"' in _text("pyproject.toml")


def test_every_dependency_is_pinned_for_deployment():
    lines = [x for x in _text("requirements.txt").splitlines() if x and not x.startswith("#")]
    assert lines and all("==" in x for x in lines)
    for needed in ("mcp==", "starlette==", "pydantic==", "msal==", "PyJWT=="):
        assert any(x.startswith(needed) for x in lines), needed
