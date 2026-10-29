"""Offline tests: catalog search, safety policy, tool schemas (Copilot Studio constraints), URL building."""
from __future__ import annotations

import asyncio
import json
import os
import re

os.environ.setdefault("AUTH_MODE", "none")
os.environ.setdefault("CATALOG_AUTO_REFRESH", "false")

from megamcp.catalog.services import resolve_service, service_for_url  # noqa: E402
from megamcp.catalog.store import catalog, tokenize  # noqa: E402
from megamcp.runbook.invoke import runbook  # noqa: E402
from megamcp.runbook.policy import Policy  # noqa: E402
from megamcp.server import create_server  # noqa: E402


def setup_module(_):
    catalog.load_bundled()


def test_catalog_loaded():
    assert len(catalog.ops) > 50
    svcs = {o["svc"] for o in catalog.ops}
    assert "dataverse" in svcs
    assert "powerbi" in svcs


def test_tokenize_splits_camel_case_and_stems():
    toks = tokenize("/licensing/billingPolicies")
    assert tokenize("billing")[0] in toks
    assert tokenize("policies")[0] in toks
    assert tokenize("policy")[0] == tokenize("policies")[0]
    assert tokenize("allocation")[0] == tokenize("allocations")[0] == tokenize("allocated")[0]


def test_search_finds_bots():
    res = catalog.search("copilot studio agents bots", service="dataverse", limit=5)
    assert res and any("/bots" in r["path"] for r in res)


def test_describe_round_trip():
    res = catalog.search("WhoAmI", service="dataverse", limit=1)
    d = catalog.describe(res[0]["id"])
    assert d and d["how_to_call"]["service"] == "dataverse"


def test_policy_classifies_writes():
    p = Policy()
    assert p.is_write("DELETE", "https://graph.microsoft.com/v1.0/users/x")
    assert not p.is_write("GET", "https://graph.microsoft.com/v1.0/users")
    # read-like POSTs do not need confirmation
    assert not p.is_write("POST", "https://management.azure.com/subscriptions/x/providers/Microsoft.CostManagement/query?api-version=1")
    assert not p.is_write("POST", "https://management.azure.com/providers/Microsoft.ResourceGraph/resources?api-version=1")
    d = p.evaluate("PATCH", "https://graph.microsoft.com/v1.0/users/x", confirm=False)
    assert not d.allowed and d.get("needs_confirm")
    assert p.evaluate("PATCH", "https://graph.microsoft.com/v1.0/users/x", confirm=True).allowed


def test_service_routing():
    assert resolve_service("pp").name == "powerplatform"
    assert service_for_url("https://graph.microsoft.com/beta/me").name == "graph"
    assert service_for_url("https://contoso.crm.dynamics.com/api/data/v9.2/bots").name == "dataverse"
    assert service_for_url("https://evil.example.com/") is None


def test_url_building():
    g = resolve_service("graph")
    url, ver = runbook.build_url(g, "/users", {"$top": 5}, "", "")
    assert url.startswith("https://graph.microsoft.com/v1.0/users?") and ("$top=5" in url or "%24top=5" in url) and ver == "v1.0"
    pp = resolve_service("powerplatform")
    url, ver = runbook.build_url(pp, "/licensing/billingPolicies", None, "", "")
    assert "api-version=" in url
    dv = resolve_service("dataverse")
    url, _ = runbook.build_url(dv, "/bots", None, "", "https://org.crm.dynamics.com")
    assert url == "https://org.crm.dynamics.com/api/data/v9.2/bots"


def test_tool_schemas_are_copilot_studio_safe():
    mcp = create_server()
    tools = asyncio.run(mcp.list_tools())
    assert len(tools) >= 25
    for t in tools:
        schema = getattr(t, "inputSchema", None) or getattr(t, "input_schema", None)
        s = json.dumps(schema)
        assert "$ref" not in s, t.name
        assert "anyOf" not in s and "oneOf" not in s, t.name
        assert re.match(r"^[A-Za-z0-9_-]{1,64}$", t.name)
        for name, prop in schema.get("properties", {}).items():
            assert isinstance(prop.get("type"), str), f"{t.name}.{name} has non-primitive type"
            assert prop["type"] in ("string", "integer", "number", "boolean"), f"{t.name}.{name} is {prop['type']}"
        assert t.description and len(t.description) > 40, t.name


def test_time_budget_caps_waits():
    from megamcp.runbook import budget

    assert budget.remaining() is None  # no budget outside a request
    assert budget.can_wait(60)
    tok = budget.start(20)
    try:
        rem = budget.remaining()
        assert rem is not None and 19 <= rem <= 20
        assert budget.can_wait(5)
        assert not budget.can_wait(15)  # 15 + 8 s reserve > 20
        assert 5 <= budget.http_timeout(90) <= 17
        assert 5 <= budget.fanout_timeout(80) <= 8
    finally:
        budget.clear(tok)
    assert budget.remaining() is None


def test_retail_price_cache_and_filter():
    from megamcp.tools import finops

    assert finops._price_filter("gpt-4o", "swedencentral") == "contains(productName,'Azure OpenAI') and contains(skuName,'4o') and armRegionName eq 'swedencentral'"
    item = finops._price_item({"skuName": "gpt-4o-0806-Inp-glbl", "meterName": "gpt 4o 0806 Inp glbl Tokens", "unitOfMeasure": "1K", "retailPrice": 0.0025, "currencyCode": "USD"}, "gpt-4o")
    assert item["price_per_1M"] == 2.5 and item["category"] == "input" and item["deployment_type"] == "global"
    finops._price_cache[("x", "USD")] = (finops.time.time(), [{"skuName": "cached"}])
    items, error = asyncio.run(finops._retail_prices("x", "USD"))
    assert items == [{"skuName": "cached"}] and error == ""


def test_tokenomics_schema_has_subscription_mode():
    server = create_server()
    tools = asyncio.run(server.list_tools())
    tok = next(t for t in tools if t.name == "finops_tokenomics")
    schema = getattr(tok, "inputSchema", None) or getattr(tok, "input_schema", None)
    props = schema["properties"]
    assert "subscription_id" in props and "top_accounts" in props
    assert "resource_id" not in schema.get("required", [])


def test_inventory_query_renders_kql_and_clauses():
    from megamcp.tools.inventory import Query, kql_str

    q = (Query().where("type == 'microsoft.copilotstudio/agents'").where("todatetime(properties.createdAt) >= ago(7d)")
         .join_environments().join_usage().project("agent_id = name", "environment = environmentName", "last_used = lastUsed")
         .order("agent_id", "desc").take(10))
    k = q.kql()
    assert k.startswith("PowerPlatformResources\n| where type == 'microsoft.copilotstudio/agents'")
    assert "join kind=leftouter" in k and "usagerecords" in k and k.rstrip().endswith("| take 10")
    clauses = q.clauses()
    kinds = [c["$type"] for c in clauses]
    assert kinds.count("join") == 2 and kinds[-1] == "take" and "orderby" in kinds
    # every where becomes a computed flag then an equality, so any boolean expression survives the fallback route
    assert clauses[0] == {"$type": "extend", "FieldName": "_m1", "Expression": "(type == 'microsoft.copilotstudio/agents')"}
    assert clauses[1] == {"$type": "where", "FieldName": "_m1", "Operator": "==", "Values": ["true"]}
    assert kql_str("O'Brien \\ x\n") == "'O\\'Brien \\\\ x '"
    raw = Query().where("type == 'x'").raw("| summarize dcount(name)")
    assert raw.kql_only
    try:
        raw.clauses()
        raise AssertionError("raw KQL must not render to clauses")
    except ValueError:
        pass


def test_inventory_date_and_tri_state_validation():
    from megamcp.tools.inventory import _tri, kql_date

    assert kql_date("2026-10-22", "x") == "datetime(2026-10-22)"
    assert kql_date("2026-10-22T10:45:00Z", "x") == "datetime(2026-10-22T10:45:00Z)"
    for bad in ("yesterday", "2026-10-22; drop", "22/10/2026"):
        try:
            kql_date(bad, "x")
            raise AssertionError(bad)
        except ValueError:
            pass
    assert _tri("Yes", "p") == "yes" and _tri("", "p") == "any" and _tri("false", "p") == "no"


def test_job_store_runs_and_isolates_users():
    from megamcp.auth.entra import Principal
    from megamcp.runbook.jobs import JobStore

    async def scenario():
        store = JobStore()
        alice, bob = Principal(mode="none", oid="alice"), Principal(mode="none", oid="bob")

        async def work(job):
            job.progress["done"] = 1
            return {"ok": True, "answer": 42}

        job = store.start(alice, "test", "t", work, total=1)
        assert await store.wait(job, 5)
        assert job.status == "succeeded" and job.result == {"ok": True, "answer": 42}
        assert store.get(job.id, "alice") is job and store.get(job.id, "bob") is None

        async def slow(job):
            await asyncio.sleep(30)
            return {}

        j2 = store.start(bob, "test", "slow", slow)
        store.cancel(j2)
        await store.wait(j2, 5)
        assert j2.status == "cancelled"

    asyncio.run(scenario())


def _offline_access(monkey_roles):
    """Patch the access module's network lookups with fixed answers."""
    from megamcp import access

    async def facts(principal=None, force=False):
        return {"roles": monkey_roles, "object_id": "11111111-2222-3333-4444-555555555555", "upn": "admin@contoso.com", "error": ""}

    async def scopes(service_name, principal=None):
        return ["User.Read", "User.ReadBasic.All"] if service_name == "graph" else []

    async def az_roles(subscription_id, object_id, principal=None):
        return ["Reader"]

    access.caller_facts, access.granted_scopes, access.azure_roles_at = facts, scopes, az_roles
    return access


def test_access_explains_missing_consent_role_and_azure_rbac():
    access = _offline_access(["AI Administrator", "Global Reader"])

    async def scenario():
        graph = {"status": 403, "ok": False, "service": "graph", "method": "GET",
                 "url": "https://graph.microsoft.com/v1.0/reports/getMicrosoft365CopilotUsageUserDetail(period='D30')",
                 "body": {"error": {"code": "S2SUnauthorized", "message": "Invalid permission."}}}
        a = await access.classify(graph, "finops_m365_copilot_licences")
        assert a["kind"] == "app_permission_missing" and "consent" in a["who_can_fix"].lower()
        assert a["user_message"].startswith("Sorry, you don't have access to Microsoft 365 Copilot licence")

        pp = {"status": 403, "ok": False, "service": "powerplatform", "method": "PUT",
              "url": "https://api.powerplatform.com/licensing/allocationsV2?api-version=2024-10-01", "body": {"error": {"code": "Forbidden", "message": "Forbidden"}}}
        b = await access.classify(pp, "finops_allocate_copilot_credits")
        assert b["kind"] == "role_missing" and b["confirmed"] is True
        assert b["your_roles"] == ["AI Administrator", "Global Reader"] and "Power Platform Administrator" in b["missing"]

        arm = {"status": 403, "ok": False, "service": "arm", "method": "PUT",
               "url": "https://management.azure.com/subscriptions/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/providers/Microsoft.Consumption/budgets/b1",
               "body": {"error": {"code": "AuthorizationFailed", "message": "The client 'admin@contoso.com' with object id 'x' does not have authorization to perform action "
                                  "'Microsoft.Consumption/budgets/write' over scope '/subscriptions/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee' or the scope is invalid."}}}
        c = await access.classify(arm, "finops_create_budget")
        assert c["kind"] == "azure_rbac_missing" and "Cost Management Contributor" in c["missing"]
        assert c["your_azure_roles_on_subscription"] == ["Reader"]

        blocked = {"status": "blocked", "reason": "server is in READ_ONLY mode; write operations are disabled"}
        d = await access.classify(blocked, "invoke")
        assert d["kind"] == "server_policy" and "switched off on this server" in d["user_message"]

        dv = {"status": 401, "url": "https://org1.crm.dynamics.com/api/data/v9.2/bots", "message": "The user is not a member of the organization."}
        e = await access.classify(dv, "pp_query_all_environments")
        assert e["kind"] == "environment_membership" and "org1.crm.dynamics.com" in e["reason"]

    asyncio.run(scenario())


def test_access_enrich_marks_denied_and_partial():
    access = _offline_access(["Global Reader"])

    async def scenario():
        denied = await access.enrich("pp_agents_tenant", {"ok": False, "error": "inventory_query_failed",
                                                          "attempts": [{"route": "azure_resource_graph", "service": "arm", "status": 403, "message": "Forbidden"}]})
        assert denied["access_denied"] is True and denied["access"]["kind"] == "role_missing"
        partial = await access.enrich("finops_billing_plans", {"ok": True, "plans": [{"name": "p1", "azure_cost": {"status": 403, "service": "arm", "url": "https://management.azure.com/subscriptions/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/providers/Microsoft.CostManagement/query", "message": "denied"}}]})
        assert partial["access_partial"] is True and partial["access"]["user_message"].startswith("Part of this answer is missing")
        fine = await access.enrich("pp_environments", {"ok": True, "environments": []})
        assert "access_denied" not in fine and "access_partial" not in fine
        assert await access.enrich("whoami", {"status": 403}) == {"status": 403}

    asyncio.run(scenario())


def test_every_tool_is_wrapped_for_access_explanations():
    server = create_server()
    tools = server._tool_manager.list_tools()
    assert all(getattr(t.fn, "_megamcp_wrapped", False) for t in tools)
    assert any(t.name == "my_access" for t in tools)
