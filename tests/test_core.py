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
