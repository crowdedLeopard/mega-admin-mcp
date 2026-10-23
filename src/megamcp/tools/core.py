"""Core tools: identity, diagnostics, catalog search and the universal runbook."""
from __future__ import annotations

import asyncio
import time
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from .. import __version__
from ..auth.entra import current_principal
from ..auth.tokens import TokenError, broker
from ..catalog.services import SERVICES, resolve_service
from ..catalog.store import catalog
from ..config import settings
from ..runbook.invoke import runbook
from ..runbook.policy import audit, policy
from .common import READ, WRITE, err, parse_json_arg

STARTED = time.time()


def register(mcp: MCPServer) -> None:
    @mcp.tool(structured_output=False, annotations=READ, description=(
        "Who am I? Returns the signed-in admin (or the server identity), the authentication mode, the Microsoft tenant, "
        "the safety policy in force and the services this server can reach. Call this first in a new conversation."))
    async def whoami() -> dict[str, Any]:
        p = current_principal()
        services = {k: {"title": v.title, "base_url": v.base_url or "per environment", "notes": v.notes} for k, v in SERVICES.items()}
        return {
            "ok": True,
            "server": {"name": "Mega Admin MCP", "version": __version__, "uptime_seconds": int(time.time() - STARTED)},
            "caller": p.summary(),
            "policy": policy.describe(),
            "catalog_operations": len(catalog.ops),
            "services": services,
            "how_to_work": [
                "1. search_operations to find the REST operation you need (any Microsoft admin API).",
                "2. describe_operation to see parameters, permissions and the exact invoke call.",
                "3. invoke to run it. Writes need confirm=true after the admin approves the preview.",
                "4. finops_* tools give ready-made Copilot Credits, Azure OpenAI token and cost answers.",
            ],
        }

    @mcp.tool(structured_output=False, annotations=READ, description=(
        "Self-diagnosis. Checks configuration, catalog freshness, token acquisition for every downstream Microsoft API "
        "(Graph, Azure, Power Platform API, Power BI, Dataverse) and reports exactly what to fix, including admin consent URLs."))
    async def doctor(
        dataverse_org_url: Annotated[str, Field(description="Optional Dataverse environment URL to test, e.g. https://org.crm.dynamics.com")] = "",
    ) -> dict[str, Any]:
        p = current_principal()
        problems = settings.validate()
        checks: list[dict[str, Any]] = []

        async def check(name: str, scope: str) -> dict[str, Any]:
            t0 = time.time()
            try:
                await broker.get_token(scope, p)
                return {"service": name, "scope": scope, "status": "ok", "ms": int((time.time() - t0) * 1000)}
            except TokenError as e:
                return {"service": name, "scope": scope, "status": "failed", **e.to_dict()}
            except Exception as e:  # noqa: BLE001
                return {"service": name, "scope": scope, "status": "failed", "message": str(e)[:300]}

        targets = [(k, v.scope) for k, v in SERVICES.items() if v.scope]
        if dataverse_org_url:
            targets.append(("dataverse", dataverse_org_url.rstrip("/") + "/.default"))
        checks = await asyncio.gather(*(check(n, s) for n, s in targets))
        ok_count = sum(1 for c in checks if c["status"] == "ok")

        live: list[dict[str, Any]] = []
        for svc, path in (("graph", "/v1.0/me" if p.mode == "delegated" or settings.auth_mode == "none" else "/v1.0/organization"),
                          ("arm", "/subscriptions"), ("powerplatform", "/licensing/billingPolicies")):
            r = await runbook.call(svc, "GET", path, query={"api-version": "2024-10-01"} if svc == "powerplatform" else None, max_chars=2000)
            live.append({"service": svc, "path": path, "status": r.get("status"), "ok": r.get("ok"), "message": r.get("message", ""), "hints": r.get("hints", [])})

        return {
            "ok": not problems and ok_count == len(checks),
            "configuration_problems": problems,
            "auth": broker.describe(),
            "caller": p.summary(),
            "token_checks": list(checks),
            "live_checks": live,
            "catalog": catalog.status(),
            "circuit_breakers_open": runbook.breaker_status(),
            "audit": audit.stats(),
            "advice": _advice(problems, list(checks), live),
        }

    @mcp.tool(structured_output=False, annotations=READ, description=(
        "Search the catalog of every Microsoft admin REST operation this server knows (Microsoft Graph v1.0 and beta, "
        "Azure Resource Manager including Cost Management and Azure OpenAI, Power Platform API, Dataverse, Power BI, Fabric, "
        "Office 365 Management). Use plain words, e.g. 'copilot credits allocation', 'openai deployments', 'billing policy', "
        "'conditional access policy', 'budget'. Returns operation ids to pass to describe_operation or invoke."))
    async def search_operations(
        query: Annotated[str, Field(description="What you want to do, in plain words")],
        service: Annotated[str, Field(description="Optional filter: graph, arm, powerplatform, dataverse, powerbi, fabric, bap, powerapps, flow, o365mgmt, devops, retailprices")] = "",
        method: Annotated[str, Field(description="Optional HTTP method filter: GET, POST, PUT, PATCH, DELETE")] = "",
        limit: Annotated[int, Field(description="Maximum results (1-50)")] = 15,
    ) -> dict[str, Any]:
        if service:
            try:
                service = resolve_service(service).name
            except KeyError as e:
                return err(str(e))
        results = catalog.search(query, service=service, method=method, limit=max(1, min(int(limit), 50)))
        return {"ok": True, "query": query, "count": len(results), "results": results,
                "next_step": "Call describe_operation with an id for parameters and permissions, then invoke."}

    @mcp.tool(structured_output=False, annotations=READ, description=(
        "Describe one catalog operation: parameters, request body, required Graph permissions, api-version, documentation link "
        "and a ready-to-use invoke call. Pass the id returned by search_operations."))
    async def describe_operation(
        operation_id: Annotated[str, Field(description="Operation id from search_operations, e.g. graph.v1.0:GET:/users")],
    ) -> dict[str, Any]:
        d = catalog.describe(operation_id)
        if not d:
            close = catalog.search(operation_id.replace(":", " ").replace("/", " "), limit=5)
            return err(f"No operation with id {operation_id}", did_you_mean=close)
        return {"ok": True, **d}

    @mcp.tool(structured_output=False, annotations=WRITE, description=(
        "THE RUNBOOK. Execute any REST call against a Microsoft admin API as the signed-in admin. "
        "service: graph | arm | powerplatform | dataverse | powerbi | fabric | bap | powerapps | flow | o365mgmt | devops | retailprices. "
        "path: relative path (e.g. /v1.0/users, /subscriptions, /licensing/billingPolicies, /bots) or a full URL. "
        "query_json / body_json / headers_json are JSON strings. api_version is added automatically for Azure and Power Platform when omitted. "
        "Write operations (POST/PUT/PATCH/DELETE) return a preview unless confirm=true: show the preview to the admin, get approval, then call again with confirm=true. "
        "Responses include status, body, next_link, self-healing actions taken and hints when something fails."))
    async def invoke(
        service: Annotated[str, Field(description="Target service name")],
        method: Annotated[str, Field(description="HTTP method: GET, POST, PUT, PATCH, DELETE")],
        path: Annotated[str, Field(description="Relative path or absolute URL")],
        query_json: Annotated[str, Field(description="Query parameters as a JSON object string, e.g. {\"$top\": 10, \"$select\": \"displayName\"}")] = "",
        body_json: Annotated[str, Field(description="Request body as a JSON string")] = "",
        headers_json: Annotated[str, Field(description="Extra headers as a JSON object string")] = "",
        api_version: Annotated[str, Field(description="api-version for Azure / Power Platform calls (optional, auto-selected when omitted)")] = "",
        confirm: Annotated[bool, Field(description="Set true to execute a write after the admin approved the preview")] = False,
        dataverse_org_url: Annotated[str, Field(description="For service=dataverse: the environment URL, e.g. https://org.crm.dynamics.com")] = "",
    ) -> dict[str, Any]:
        try:
            query = parse_json_arg(query_json, "query_json")
            body = parse_json_arg(body_json, "body_json")
            headers = parse_json_arg(headers_json, "headers_json")
            if query is not None and not isinstance(query, dict):
                return err("query_json must be a JSON object")
            if headers is not None and not isinstance(headers, dict):
                return err("headers_json must be a JSON object")
        except ValueError as e:
            return err(str(e))
        try:
            return await runbook.call(service, method, path, query=query, body=body, headers=headers, api_version=api_version,
                                      confirm=confirm, dataverse_org_url=dataverse_org_url)
        except KeyError as e:
            return err(str(e).strip("'"))

    @mcp.tool(structured_output=False, annotations=WRITE, description=(
        "Call an absolute Microsoft API URL, typically a next_link, @odata.nextLink or a download link returned by a previous call. "
        "The service and token are inferred from the host. Writes need confirm=true."))
    async def invoke_url(
        url: Annotated[str, Field(description="Absolute https URL on a known Microsoft API host")],
        method: Annotated[str, Field(description="HTTP method")] = "GET",
        body_json: Annotated[str, Field(description="Request body as JSON string (for POST/PUT/PATCH)")] = "",
        confirm: Annotated[bool, Field(description="Set true to execute a write")] = False,
    ) -> dict[str, Any]:
        try:
            body = parse_json_arg(body_json, "body_json")
        except ValueError as e:
            return err(str(e))
        return await runbook.call("", method, url, body=body, confirm=confirm)

    @mcp.tool(structured_output=False, annotations=READ, description=(
        "Poll a long-running operation (Azure-AsyncOperation / Location / Operation-Location URL returned by a previous call) "
        "until it finishes or the wait budget is used. Returns the final status and body."))
    async def poll_operation(
        poll_url: Annotated[str, Field(description="The poll URL from async_operation.poll_url")],
        max_wait_seconds: Annotated[int, Field(description="How long to wait in total (5-120)")] = 60,
    ) -> dict[str, Any]:
        deadline = time.time() + max(5, min(int(max_wait_seconds), 120))
        last: dict[str, Any] = {}
        while True:
            last = await runbook.call("", "GET", poll_url)
            body = last.get("body")
            status = ""
            if isinstance(body, dict):
                status = str(body.get("status") or body.get("Status") or (body.get("properties") or {}).get("provisioningState") or "")
            if last.get("status") in (200, 201) and status.lower() in ("succeeded", "completed", "failed", "canceled", "cancelled", "success"):
                break
            if last.get("status") in (200, 201) and not status and last.get("status") != 202:
                break
            if not last.get("ok") and last.get("status") not in (202, 200):
                break
            if time.time() >= deadline:
                last["note"] = "Still running. Call poll_operation again later."
                break
            await asyncio.sleep(min(5, max(1, deadline - time.time())))
        last["operation_status"] = status
        return last

    @mcp.tool(structured_output=False, annotations=READ, description=(
        "Catalog health: how many operations per service, when the catalog was generated, where it was loaded from, "
        "and the result of the last automatic refresh from GitHub. Set refresh_now=true to pull the latest catalog immediately."))
    async def catalog_status(
        refresh_now: Annotated[bool, Field(description="Download the latest published catalog now")] = False,
    ) -> dict[str, Any]:
        result = ""
        if refresh_now:
            result = await catalog.refresh_from_remote(force=True)
        return {"ok": True, "refresh_result": result, **catalog.status()}

    @mcp.tool(structured_output=False, annotations=READ, description=(
        "What did Microsoft add or remove recently? Lists REST operations that appeared or disappeared in Microsoft's published API "
        "specifications over the last N days, per service. Useful for 'what is new in Copilot Studio admin APIs this week'."))
    async def catalog_changes(
        days: Annotated[int, Field(description="Look back this many days (1-180)")] = 7,
        service: Annotated[str, Field(description="Optional service filter")] = "",
    ) -> dict[str, Any]:
        return {"ok": True, **catalog.changes(days=max(1, min(int(days), 180)), service=service)}

    @mcp.tool(structured_output=False, annotations=READ, description=(
        "Recent calls made through this server (who, what, status, self-healing applied). Useful for audit and troubleshooting."))
    async def audit_recent(
        limit: Annotated[int, Field(description="Number of entries (1-200)")] = 50,
        user: Annotated[str, Field(description="Optional filter on user principal name")] = "",
    ) -> dict[str, Any]:
        return {"ok": True, "stats": audit.stats(), "entries": audit.recent(max(1, min(int(limit), 200)), user)}


def _advice(problems: list[str], checks: list[dict[str, Any]], live: list[dict[str, Any]]) -> list[str]:
    advice: list[str] = []
    if problems:
        advice.append("Fix the configuration problems first (environment variables on the host).")
    failed = [c for c in checks if c["status"] != "ok"]
    for c in failed:
        fix = c.get("fix")
        advice.append(f"{c['service']}: {c.get('message', 'token failed')}" + (f" Fix: {fix}" if fix else ""))
    for l in live:
        if not l.get("ok"):
            advice.append(f"{l['service']} {l['path']} returned {l['status']}: {l.get('message', '')} {' '.join(l.get('hints', []))}".strip())
    if not advice:
        advice.append("Everything looks healthy.")
    return advice
