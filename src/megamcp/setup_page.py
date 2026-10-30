"""The page an admin opens while importing the server: /setup.

Everything the Copilot Studio wizard asks for, filled in for this server, next to what
the server can see going wrong. It holds no secrets. Tenant and client ids are already
public through /connector.yaml, and every value from a request is escaped because it
may come from an unverified token.
"""
from __future__ import annotations

from html import escape
from typing import Any

_STYLE = """
:root { color-scheme: light dark; --ink:#1b1f24; --muted:#59636e; --line:#d1d9e0; --bg:#ffffff; --panel:#f6f8fa; --ok:#1a7f37; --warn:#9a6700; --bad:#cf222e; --accent:#0969da; }
@media (prefers-color-scheme: dark) { :root { --ink:#e6edf3; --muted:#9198a1; --line:#3d444d; --bg:#0d1117; --panel:#151b23; --ok:#3fb950; --warn:#d29922; --bad:#f85149; --accent:#4493f8; } }
* { box-sizing:border-box; }
body { margin:0 auto; max-width:820px; padding:32px 16px 64px; background:var(--bg); color:var(--ink); font:16px/1.5 "Segoe UI",system-ui,sans-serif; }
h1 { font-size:26px; margin:0 0 4px; } h2 { font-size:18px; margin:32px 0 8px; }
p { margin:8px 0; } .muted { color:var(--muted); font-size:14px; }
.status { border:1px solid var(--line); border-left:4px solid var(--c); border-radius:6px; padding:12px 14px; margin:16px 0; background:var(--panel); }
.ok { --c:var(--ok); } .warn { --c:var(--warn); } .bad { --c:var(--bad); }
.status b { color:var(--c); }
ul { margin:8px 0; padding-left:20px; }
table { width:100%; border-collapse:collapse; margin:8px 0; }
td { border-top:1px solid var(--line); padding:8px 8px 8px 0; vertical-align:top; }
td:first-child { width:170px; color:var(--muted); font-size:14px; }
.value { display:flex; gap:8px; align-items:flex-start; }
code { flex:1; min-width:0; overflow-wrap:anywhere; font:13px/1.5 Consolas,ui-monospace,monospace; }
pre { margin:8px 0; padding:10px 12px; background:var(--panel); border:1px solid var(--line); border-radius:6px; white-space:pre-wrap; overflow-wrap:anywhere; font:13px/1.5 Consolas,ui-monospace,monospace; }
button { flex:none; font:13px "Segoe UI",system-ui,sans-serif; padding:3px 10px; border:1px solid var(--line); border-radius:6px; background:var(--panel); color:var(--ink); cursor:pointer; }
a { color:var(--accent); }
"""

_SCRIPT = """
document.querySelectorAll('button[data-copy]').forEach(function (b) {
  b.addEventListener('click', function () {
    navigator.clipboard.writeText(document.getElementById(b.dataset.copy).textContent).then(function () {
      b.textContent = 'Copied';
      setTimeout(function () { b.textContent = 'Copy'; }, 1200);
    });
  });
});
"""

DESCRIPTION = ("Runs any Microsoft admin REST call as the signed-in administrator and answers FinOps questions about "
               "Copilot Credits, Microsoft 365 Copilot licences and Azure OpenAI tokens and cost.")
AGENT_INSTRUCTIONS = ("Use Mega Admin MCP for any question about Copilot Credits, licences, Azure OpenAI tokens or cost, Power Platform "
                      "environments and agents, Entra, Azure or Microsoft 365 administration. Prefer the finops tools for cost questions. "
                      "Always show write previews and ask before confirming.")


def _row(label: str, value: str, key: str) -> str:
    return (f'<tr><td>{escape(label)}</td><td><div class="value"><code id="{key}">{escape(value)}</code>'
            f'<button type="button" data-copy="{key}">Copy</button></div></td></tr>')


def _status(state: dict[str, Any]) -> str:
    problems = state["problems"]
    cred = state["credential"]
    if problems:
        items = "".join(f"<li>{escape(p)}</li>" for p in problems)
        return f'<div class="status bad"><b>Not ready. Fix this on the host, then restart.</b><ul>{items}</ul></div>'
    if not cred.get("ok"):
        return (f'<div class="status bad"><b>Entra refuses the server\'s own credential.</b> Every tool will fail until this is fixed.'
                f'<p>{escape(str(cred.get("fix") or cred.get("message") or ""))}</p></div>')
    if not state["operations"]:
        return '<div class="status warn"><b>Running, but the operation catalog is empty.</b> It reloads at the next refresh.</div>'
    return (f'<div class="status ok"><b>The server is ready.</b> Version {escape(state["version"])}, '
            f'{state["operations"]:,} operations, credential: {escape(str(cred.get("credential") or "not needed"))}.</div>')


def _consent_notice(params: dict[str, str]) -> str:
    if params.get("admin_consent", "").lower() == "true":
        return '<div class="status ok"><b>Admin consent granted.</b> Everyone in the tenant can now sign in without a consent prompt.</div>'
    if params.get("error"):
        return (f'<div class="status bad"><b>Consent did not complete: {escape(params["error"][:100])}</b>'
                f'<p>{escape(params.get("error_description", "")[:400])}</p></div>')
    return ""


def _connections(state: dict[str, Any]) -> str:
    c = state["connections"]
    parts: list[str] = []
    if c["last_accepted_utc"]:
        parts.append(f'<div class="status ok"><b>Connected.</b> {c["requests_accepted"]:,} requests accepted since the last restart, the latest at {escape(c["last_accepted_utc"])}.</div>')
    else:
        parts.append('<div class="status warn"><b>Nothing has connected yet.</b> When you create the connection in Copilot Studio, this section shows whether it reached the server.</div>')
    if c["server_url_without_mcp_seen_utc"]:
        parts.append(f'<div class="status warn"><b>A client is using the wrong Server URL.</b> It works, because this server forgives it, but the Server URL should end in <code>/mcp</code>. Last seen {escape(c["server_url_without_mcp_seen_utc"])}.</div>')
    for r in c["refused"]:
        seen = f'<p class="muted">Token said: {escape(r["seen"])}</p>' if r.get("seen") else ""
        parts.append(f'<div class="status warn"><b>{escape(r["message"])}</b> {r["count"]:,} times, last at {escape(r["last"])}.<p>{escape(r["fix"])}</p>{seen}</div>')
    return "".join(parts)


def render(state: dict[str, Any], params: dict[str, str]) -> str:
    base, tenant, client = state["base_url"], state["tenant_id"], state["client_id"]
    delegated = state["auth_mode"] == "delegated"
    login = f"https://login.microsoftonline.com/{tenant or '<tenant id>'}/oauth2/v2.0"
    client_shown = client or "<client id>"
    update = ""
    if state["update_available"]:
        update = (f'<div class="status warn"><b>Version {escape(state["update_available"])} is available.</b> '
                  'The operation catalog updates itself, server code does not. '
                  'Container deployments pick it up with <code>git pull</code> and <code>./scripts/install.ps1</code>.</div>')

    wizard = ""
    if delegated:
        consent = f"{login.rsplit('/oauth2', 1)[0]}/adminconsent?client_id={client}&redirect_uri={base}/setup"
        wizard = f"""
<h2>1. Add it to your agent</h2>
<p>In Copilot Studio open your agent, then <b>Tools</b>, <b>Add a tool</b>, <b>New tool</b>, <b>Model Context Protocol</b>.</p>
<table>
{_row("Server name", "Mega Admin MCP", "name")}
{_row("Server description", DESCRIPTION, "desc")}
{_row("Server URL", f"{base}/mcp", "url")}
</table>
<p>For <b>Authentication</b> choose <b>OAuth 2.0</b>, then <b>Manual</b>. Dynamic discovery will not work, because Entra has no dynamic client registration.</p>
<table>
{_row("Client ID", client_shown, "client")}
{_row("Authorization URL", f"{login}/authorize", "authz")}
{_row("Token URL template", f"{login}/token", "token")}
{_row("Refresh URL", f"{login}/token", "refresh")}
{_row("Scopes", f"{client_shown}/.default offline_access", "scopes")}
</table>
<p><b>Client secret</b> is the one the installer printed. It isn't shown here. If you've lost it, run <code>./scripts/setup-entra.ps1 -AppId {escape(client_shown)} -NewSecret</code>.</p>

<h2>2. Register the redirect URL</h2>
<p>Select <b>Create</b>. Copilot Studio shows a redirect URL, different for every connector. Add it to the app registration, or sign-in fails with AADSTS50011:</p>
<pre>./scripts/setup-entra.ps1 -AppId {escape(client_shown)} -AddRedirectUri "&lt;the redirect URL&gt;"</pre>

<h2>3. Create the connection</h2>
<p>Select <b>Next</b>, <b>Create a new connection</b>, sign in and accept the prompt, then <b>Add to agent</b>.</p>
<p>If the prompt says it needs admin approval, your tenant doesn't let users consent. Ask a Global Administrator or Privileged Role Administrator to open
<a href="{escape(consent, quote=True)}">the admin consent link</a>. It brings them back to this page.</p>

<h2>4. Keep it signed in as each user</h2>
<div class="status warn"><b>Leave Credentials to use on End user credentials.</b> It's on the tool's page under Additional details.
With Maker-provided credentials, everyone who can talk to the agent acts with your admin roles.</div>
<p>Add this to the agent's instructions:</p>
<div class="value"><pre id="instr" style="flex:1">{escape(AGENT_INSTRUCTIONS)}</pre><button type="button" data-copy="instr">Copy</button></div>
<p>Then try <i>Run doctor</i> in the test pane. It checks every Microsoft API with your account and says what to fix.</p>
"""
    else:
        wizard = f'<p>This server runs in <code>{escape(state["auth_mode"])}</code> mode, so there is no Copilot Studio sign-in to set up. The endpoint is <code>{escape(base)}/mcp</code>.</p>'

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Mega Admin MCP setup</title><style>{_STYLE}</style></head>
<body>
<h1>Mega Admin MCP setup</h1>
<p class="muted">Everything Copilot Studio asks for, filled in for this server, and what the server can see going wrong.</p>
{_consent_notice(params)}{_status(state)}{update}
{wizard}
<h2>What the server has seen</h2>
{_connections(state)}
<p class="muted">Counts reset when the server restarts. <a href="/ready">/ready</a> has the same facts as JSON. <a href="https://github.com/crowdedLeopard/mega-admin-mcp/blob/main/docs/08-troubleshooting.md">Troubleshooting guide</a></p>
<script>{_SCRIPT}</script>
</body></html>"""
