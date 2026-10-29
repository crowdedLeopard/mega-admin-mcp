"""FinOps and AI tokenomics tools.

Everything an admin needs to answer "what are we spending on AI, where, and
what should we do about it" across Copilot Studio (Copilot Credits), Microsoft
365 Copilot licences, Azure OpenAI / Foundry tokens and Azure cost.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import re
import time
from collections import defaultdict
from typing import Annotated, Any
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..config import settings
from ..runbook import budget
from ..runbook.invoke import RunbookError, runbook
from .admin import resolve_environment
from .common import READ, WRITE, err

PPV = settings.powerplatform_api_version
GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

CURRENCY_NAMES = {
    "MCSMessages": "Copilot Credits (Copilot Studio, Copilot Chat agents, agent flows)",
    "AI": "AI Builder credits",
    "MCSSessions": "Copilot Studio sessions (legacy per-session capacity)",
    "TenantM365Copilot": "Microsoft 365 Copilot (tenant entitlement count)",
    "AppPass": "Power Apps per app passes",
    "AppPassForTeams": "Power Apps per app passes (Teams)",
    "PerFlowPlan": "Power Automate per flow plans",
    "PowerAutomatePerProcess": "Power Automate per process (hosted process)",
    "PAHostedRPA": "Power Automate hosted RPA",
    "PAUnattendedRPA": "Power Automate unattended RPA",
    "PowerPagesAuthenticated": "Power Pages authenticated users",
    "PowerPagesAnonymous": "Power Pages anonymous users",
    "PortalLogins": "Portal logins (legacy)",
    "PortalViews": "Portal page views (legacy)",
    "ProcessMiningDataStorage": "Process mining data storage",
    "Invoice": "Invoice capacity",
    "W365APAYGO": "Windows 365 pay-as-you-go",
    "SCMessages": "Sales Copilot messages",
    "VAConversations": "Virtual Agents conversations (legacy)",
}

ARM_API_COST = "2025-03-01"
ARM_API_METRICS = "2024-02-01"
ARM_API_COGNITIVE = "2024-10-01"
ARM_API_BUDGETS = "2024-08-01"
ARM_API_ARG = "2022-10-01"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _pct(a: float, b: float) -> float | None:
    return round(a / b * 100, 2) if b else None


def _today() -> dt.date:
    return dt.datetime.now(dt.timezone.utc).date()


def _norm_scope(scope: str) -> str:
    s = (scope or "").strip()
    if not s:
        s = settings.default_subscription_id
    if not s:
        return ""
    if GUID.match(s):
        return f"/subscriptions/{s}"
    if not s.startswith("/"):
        s = "/" + s
    return s.rstrip("/")


async def _subscriptions() -> list[dict[str, Any]]:
    body = await runbook.get_json("arm", "/subscriptions", api_version="2022-12-01")
    return [{"id": s.get("subscriptionId"), "name": s.get("displayName"), "state": s.get("state")} for s in (body or {}).get("value", [])]


async def _scope_or_help(scope: str) -> tuple[str, dict[str, Any] | None]:
    s = _norm_scope(scope)
    if s:
        return s, None
    try:
        subs = await _subscriptions()
    except RunbookError as e:
        return "", e.result
    if len(subs) == 1:
        return f"/subscriptions/{subs[0]['id']}", None
    return "", err("scope is required (subscription id, /subscriptions/<id>, or a resource group / resource id). Set DEFAULT_SUBSCRIPTION_ID to skip this.", subscriptions=subs)


def _rows(query_result: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    props = (query_result or {}).get("properties") or {}
    cols = [c.get("name") for c in props.get("columns", [])]
    rows = [dict(zip(cols, r)) for r in props.get("rows", [])]
    currency = rows[0].get("Currency", "") if rows else ""
    return rows, currency


async def _cost_query(scope: str, timeframe: str, start: str, end: str, grouping: list[str], dim_filter: dict[str, Any] | None, granularity: str = "None") -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": "ActualCost",
        "timeframe": "Custom" if start and end else (timeframe or "MonthToDate"),
        "dataset": {
            "granularity": granularity,
            "aggregation": {"totalCost": {"name": "Cost", "function": "Sum"}},
            "grouping": [{"type": "Dimension", "name": g} for g in grouping],
        },
    }
    if start and end:
        body["timePeriod"] = {"from": f"{start}T00:00:00Z", "to": f"{end}T23:59:59Z"}
    if dim_filter:
        body["dataset"]["filter"] = {"dimensions": dim_filter}
    r = await runbook.call("arm", "POST", f"{scope}/providers/Microsoft.CostManagement/query", body=body, api_version=ARM_API_COST, raise_on_error=True, max_chars=2_000_000)
    return r.get("body") or {}


def _classify_meter(meter: str) -> str:
    m = f" {meter.lower()} "
    if "batch" in m:
        base = "batch_"
    else:
        base = ""
    if re.search(r"\bcd\b|cached|cache read", m) and re.search(r"\binp|input", m):
        return base + "cached_input"
    if re.search(r"\bwr\b|cache wr|cache write|cache creat", m):
        return base + "cache_write"
    if re.search(r"\binp\b|\binpt\b|input", m):
        return base + "input"
    if re.search(r"\boutp\b|\bopt\b|output", m):
        return base + "output"
    if "ptu" in m or "provisioned" in m:
        return "provisioned"
    if "image" in m or "dalle" in m or "gpt image" in m:
        return "images"
    if "audio" in m or "realtime" in m or "speech" in m or "whisper" in m or "tts" in m:
        return "audio"
    if "training" in m or "fine" in m:
        return "fine_tuning"
    return "other"


def _model_tokens(name: str) -> list[str]:
    toks = [t for t in re.split(r"[-_.\s/]+", (name or "").lower()) if t]
    return [t for t in toks if t not in ("gpt", "model", "deployment", "openai", "azure", "o", "preview", "global", "standard")]


def _meter_matches_model(meter: str, model_name: str) -> int:
    """Return a match score (number of model tokens found in the meter name)."""
    m = re.sub(r"[^a-z0-9. ]", " ", meter.lower())
    toks = _model_tokens(model_name)
    if not toks:
        return 0
    score = 0
    for t in toks:
        if re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", m):
            score += 1
    return score if score == len(toks) or (score >= 2 and score >= len(toks) - 1) else 0


TOKENOMICS_METHOD = ("Tokens from Azure Monitor (ProcessedPromptTokens, GeneratedTokens, TokenTransaction split by ModelDeploymentName). "
                     "Cost from Cost Management actual cost per meter, attributed to deployments by model name. Effective price = cost / tokens.")

# Retail prices change rarely and prices.azure.com throttles hard (Retry-After 60 s), so
# results are cached for twelve hours and at most two lookups run at a time.
_PRICE_TTL_SECONDS = 12 * 3600
_price_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_price_sem: asyncio.Semaphore | None = None


def _price_item(it: dict[str, Any], model_query: str = "") -> dict[str, Any]:
    unit = str(it.get("unitOfMeasure") or "")
    mult = 1000 if unit.strip().startswith("1K") else (1 if unit.strip().startswith("1M") else None)
    sku = str(it.get("skuName") or "")
    return {"model_query": model_query or None, "sku": it.get("skuName"), "meter": it.get("meterName"), "product": it.get("productName"), "region": it.get("armRegionName"),
            "unit": unit, "retail_price": it.get("retailPrice"), "currency": it.get("currencyCode"),
            "price_per_1M": round(float(it.get("retailPrice") or 0) * mult, 4) if mult else None, "category": _classify_meter(it.get("meterName") or ""),
            "deployment_type": "global" if re.search(r"glbl|global|\bgl\b", sku, re.I) else ("data zone" if re.search(r"data zone|\bdz\b", sku, re.I) else "regional"),
            "type": it.get("type"), "effective_from": it.get("effectiveStartDate")}


async def _retail_prices(flt: str, currency: str = "USD", retry_wait_budget: float = 8.0) -> tuple[list[dict[str, Any]], str]:
    """Items from the Azure Retail Prices API for a $filter, cached. Returns (items, error)."""
    global _price_sem
    key = (flt, currency or "USD")
    hit = _price_cache.get(key)
    if hit and time.time() - hit[0] < _PRICE_TTL_SECONDS:
        return hit[1], ""
    if _price_sem is None:
        _price_sem = asyncio.Semaphore(2)
    async with _price_sem:
        hit = _price_cache.get(key)  # another caller may have filled it while we waited
        if hit and time.time() - hit[0] < _PRICE_TTL_SECONDS:
            return hit[1], ""
        r = await runbook.call("retailprices", "GET", "/api/retail/prices", query={"$filter": flt, "currencyCode": currency or "USD", "api-version": "2023-01-01-preview"},
                               max_chars=3_000_000, retry_wait_budget=retry_wait_budget)
    if not r.get("ok"):
        if hit:  # stale is better than nothing
            return hit[1], f"using cached prices; refresh failed: {r.get('message')}"
        return [], str(r.get("message") or f"HTTP {r.get('status')}")
    items = list((r.get("body") or {}).get("Items", []))
    _price_cache[key] = (time.time(), items)
    return items, ""


def _price_filter(model_text: str, region: str = "", sep: str = " ") -> str:
    toks = _model_tokens(model_text)
    flt = f"contains(productName,'Azure OpenAI') and contains(skuName,'{sep.join(toks[:2])}')"
    if region:
        flt += f" and armRegionName eq '{region}'"
    return flt


async def _list_prices_for(pairs: list[tuple[str, str]]) -> tuple[list[dict[str, Any]], str]:
    """Best-effort list prices for (model, region) pairs; never raises, never waits long."""
    out: list[dict[str, Any]] = []
    notes: list[str] = []

    async def one(model: str, region: str) -> None:
        toks = _model_tokens(model)
        if not toks:
            return
        items, error = await _retail_prices(_price_filter(model, region))
        if not items and not error and len(toks) >= 2:  # sku names use spaces or hyphens inconsistently
            items, error = await _retail_prices(_price_filter(model, region, sep="-"))
        if error:
            notes.append(f"{model}: {error[:160]}")
        elif not items:
            notes.append(f"{model}: no retail price lines match (try finops_price_lookup with a shorter model name)")
        out.extend(_price_item(it, model) for it in items[:60])

    await asyncio.gather(*(one(m, r) for m, r in pairs), return_exceptions=True)
    return out, "; ".join(notes)


def _account_analysis(resource_id: str, region: str, days: int, tokens_r: Any, deps_r: Any, cost_rows: list[dict[str, Any]], currency: str) -> dict[str, Any]:
    """Join token metrics, deployments and cost rows for one account into the tokenomics view."""
    errors: list[str] = []
    deployments: dict[str, dict[str, Any]] = {}
    if isinstance(deps_r, dict) and deps_r.get("ok"):
        for dep in (deps_r.get("body") or {}).get("value", []):
            props = dep.get("properties") or {}
            deployments[dep.get("name")] = {"model": (props.get("model") or {}).get("name"), "version": (props.get("model") or {}).get("version"),
                                            "sku": (dep.get("sku") or {}).get("name"), "capacity": (dep.get("sku") or {}).get("capacity")}
    elif isinstance(deps_r, dict):
        errors.append(f"deployments: {deps_r.get('message')}")
    elif deps_r is not None:
        errors.append(f"deployments: {str(deps_r)[:200]}")

    per_dep: dict[str, dict[str, float]] = defaultdict(lambda: {"input": 0, "output": 0, "total": 0, "active_days": 0})
    label = {"ProcessedPromptTokens": "input", "GeneratedTokens": "output", "TokenTransaction": "total"}
    if isinstance(tokens_r, dict) and tokens_r.get("ok"):
        for metric in (tokens_r.get("body") or {}).get("value", []):
            key = label.get((metric.get("name") or {}).get("value"), "other")
            for ts in metric.get("timeseries", []):
                dep = next((mv.get("value") for mv in ts.get("metadatavalues", []) if (mv.get("name") or {}).get("value", "").lower() == "modeldeploymentname"), "(all)")
                for point in ts.get("data", []):
                    v = int(point.get("total") or 0)
                    per_dep[dep][key] += v
                    if key == "total" and v:
                        per_dep[dep]["active_days"] += 1
    elif isinstance(tokens_r, dict):
        errors.append(f"metrics: {tokens_r.get('message')}")
    elif tokens_r is not None:
        errors.append(f"metrics: {str(tokens_r)[:200]}")
    for dep_name in deployments:  # deployments with no traffic still appear (idle capacity)
        per_dep.setdefault(dep_name, {"input": 0, "output": 0, "total": 0, "active_days": 0})

    meter_rows: list[dict[str, Any]] = []
    dep_cost: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    unassigned: dict[str, float] = defaultdict(float)
    for r in cost_rows:
        meter = r.get("Meter") or ""
        cost = float(r.get("Cost") or 0)
        cat = _classify_meter(meter)
        best, best_score = None, 0
        for dep_name, info in deployments.items():
            sc = max(_meter_matches_model(meter, info.get("model") or ""), _meter_matches_model(meter, dep_name))
            if sc > best_score:
                best, best_score = dep_name, sc
        if best is None:
            for dep_name in per_dep:
                sc = _meter_matches_model(meter, dep_name)
                if sc > best_score:
                    best, best_score = dep_name, sc
        meter_rows.append({"meter": meter, "sub_category": r.get("MeterSubCategory"), "category": cat, "cost": round(cost, 6), "deployment": best})
        if best:
            dep_cost[best][cat] += cost
            dep_cost[best]["total"] += cost
        else:
            unassigned[meter] += cost

    results: list[dict[str, Any]] = []
    grand_cost = sum(float(r.get("Cost") or 0) for r in cost_rows)
    grand_tokens = sum(v["total"] for v in per_dep.values())
    for dep, t in sorted(per_dep.items(), key=lambda kv: -kv[1]["total"]):
        c = dep_cost.get(dep, {})
        inp_cost = c.get("input", 0) + c.get("batch_input", 0)
        out_cost = c.get("output", 0) + c.get("batch_output", 0)
        cached_cost = c.get("cached_input", 0) + c.get("cache_write", 0)
        total_cost = c.get("total", 0)
        all_input_cost = inp_cost + cached_cost  # ProcessedPromptTokens counts cached and uncached prompt tokens together
        eff_in = round(all_input_cost / (t["input"] / 1e6), 4) if t["input"] and all_input_cost else None
        eff_out = round(out_cost / (t["output"] / 1e6), 4) if t["output"] and out_cost else None
        eff_blended = round(total_cost / (t["total"] / 1e6), 4) if t["total"] and total_cost else None
        info = deployments.get(dep, {})
        results.append({
            "deployment": dep, "model": info.get("model"), "version": info.get("version"), "sku": info.get("sku"), "capacity": info.get("capacity"),
            "input_tokens": int(t["input"]), "output_tokens": int(t["output"]), "total_tokens": int(t["total"]), "active_days": int(t["active_days"]),
            "output_share_pct": _pct(t["output"], t["total"]), "share_of_account_tokens_pct": _pct(t["total"], grand_tokens),
            "cost": {"currency": currency, "input": round(inp_cost, 4), "output": round(out_cost, 4), "cached_and_cache_write": round(cached_cost, 4),
                     "other": round(total_cost - inp_cost - out_cost - cached_cost, 4), "total": round(total_cost, 4), "share_of_account_cost_pct": _pct(total_cost, grand_cost)},
            "effective_price_per_1M_tokens": {"input_all_prompt_tokens": eff_in, "output": eff_out, "blended": eff_blended},
            "cached_input_cost_share_pct": _pct(cached_cost, total_cost) if total_cost else None,
            "cache_meter_share_of_input_cost_pct": _pct(cached_cost, all_input_cost) if all_input_cost else None,
            "daily_run_rate": {"tokens": round(t["total"] / days), "cost": round(total_cost / days, 4)},
            "projected_30_day_cost": round(total_cost / days * 30, 2),
        })

    recommendations: list[str] = []
    for r_ in results:
        if r_["total_tokens"] == 0 and r_.get("capacity"):
            recommendations.append(f"{r_['deployment']}: no tokens in {days} days but {r_['capacity']} {('PTU' if 'provisioned' in str(r_.get('sku')).lower() else 'K TPM')} reserved. Delete or shrink the deployment; quota and PTU commitments cost money or block others.")
        if r_["cost"]["total"] and r_.get("cached_input_cost_share_pct") is not None and r_["cached_input_cost_share_pct"] < 5 and r_["input_tokens"] > 50_000_000:
            recommendations.append(f"{r_['deployment']}: cached input is under 5% of cost on a heavy input workload. Stable system prompts and tool definitions at the start of the prompt unlock prompt caching (up to 50-90% off cached input).")
        if r_["output_share_pct"] and r_["output_share_pct"] > 40:
            recommendations.append(f"{r_['deployment']}: output is {r_['output_share_pct']}% of tokens; output tokens cost several times input. Tighten max_tokens and response formats.")
        if r_["sku"] and "provisioned" not in str(r_["sku"]).lower() and r_["daily_run_rate"]["tokens"] > 50_000_000:
            recommendations.append(f"{r_['deployment']}: sustained {r_['daily_run_rate']['tokens']:,} tokens/day on pay-as-you-go. Evaluate PTU (provisioned) or a Foundry commitment; compare using the effective blended price per 1M tokens.")
    if unassigned:
        recommendations.append("Some meters could not be attributed to a deployment by model name; see unassigned_meters. They are still in the account total.")

    return {"totals": {"tokens": int(grand_tokens), "cost": round(grand_cost, 4), "currency": currency,
                       "effective_price_per_1M_tokens_blended": round(grand_cost / (grand_tokens / 1e6), 4) if grand_tokens and grand_cost else None,
                       "projected_30_day_cost": round(grand_cost / days * 30, 2)},
            "deployments": results, "meters": meter_rows, "unassigned": {k: round(v, 4) for k, v in unassigned.items()},
            "recommendations": recommendations, "errors": errors, "region": region, "resource_id": resource_id}


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------
def register(mcp: MCPServer) -> None:  # noqa: C901 (big but flat on purpose)

    # ---------------------------------------------------------------- Copilot Credits
    @mcp.tool(structured_output=False, annotations=READ, description=(
        "Copilot Credits and all Power Platform capacity currencies for the tenant: purchased, allocated, available to allocate, consumed "
        "(month to date), utilisation %, plus how many pay-as-you-go billing plans have the Copilot Studio meter switched on. "
        "The headline FinOps view for Copilot Studio."))
    async def finops_copilot_credits_overview() -> dict[str, Any]:
        try:
            report = await runbook.get_json("powerplatform", "/licensing/tenantCapacity/currencyReports",
                                            query={"includeAllocations": "true", "includeConsumptions": "true"}, api_version=PPV)
        except RunbookError as e:
            return e.result
        currencies = []
        for c in report if isinstance(report, list) else (report or {}).get("value", []):
            ctype = c.get("currencyType", "")
            purchased = c.get("purchased") or 0
            allocated = c.get("allocated") or 0
            consumed = (c.get("consumed") or {}).get("unitsConsumed")
            currencies.append({
                "currency": ctype, "name": CURRENCY_NAMES.get(ctype, ctype), "purchased": purchased, "allocated": allocated,
                "available_to_allocate": purchased - allocated, "consumed_month_to_date": consumed,
                "consumed_pct_of_purchased": _pct(consumed or 0, purchased) if consumed is not None else None,
                "allocated_pct_of_purchased": _pct(allocated, purchased),
                "consumption_last_updated": (c.get("consumed") or {}).get("lastUpdatedDay"),
            })
        currencies.sort(key=lambda x: -(x["purchased"] or 0))
        payg_plans: list[dict[str, Any]] = []
        payg_error = ""
        try:
            policies = await runbook.paged("powerplatform", "/licensing/billingPolicies", api_version=PPV, max_pages=5)
            for p in policies:
                mcs = [e for e in p.get("payGoEntitlements", []) if e.get("entitlementId") == "MCSMessages"]
                payg_plans.append({"id": p.get("id"), "name": p.get("name"), "status": p.get("status"), "location": p.get("location"),
                                   "environments": len(p.get("environmentIds") or []),
                                   "copilot_credits_payg_enabled": bool(mcs and mcs[0].get("payAsYouGoState")),
                                   "azure_subscription": (p.get("billingInstrument") or {}).get("subscriptionId"),
                                   "resource_group": (p.get("billingInstrument") or {}).get("resourceGroup")})
        except RunbookError as e:
            payg_error = e.result.get("message", "billing policies unavailable")
        mcs = next((c for c in currencies if c["currency"] == "MCSMessages"), None)
        insights: list[str] = []
        if mcs:
            if (mcs["purchased"] or 0) and (mcs["allocated"] or 0) / mcs["purchased"] < 0.05:
                insights.append("Less than 5% of purchased Copilot Credits are allocated to environments. Unallocated prepaid credits are still consumable by environments that draw from the tenant pool, but allocation is how you ring-fence spend per team.")
            if mcs.get("consumed_month_to_date") is not None and mcs["purchased"]:
                insights.append(f"Month-to-date Copilot Credit consumption is {mcs['consumed_pct_of_purchased']}% of purchased capacity.")
        if payg_plans and not any(p["copilot_credits_payg_enabled"] for p in payg_plans):
            insights.append("No billing plan has the Copilot Studio meter enabled, so environments cannot overflow to Azure pay-as-you-go. That is a hard stop when prepaid credits run out.")
        return {"ok": True, "tenant_currencies": currencies, "copilot_credits": mcs, "pay_as_you_go_plans": payg_plans, "pay_as_you_go_error": payg_error,
                "insights": insights,
                "next_steps": ["finops_environment_credits <environment> for one environment", "finops_agent_credits_history for spend per agent over the last 6 or 12 months",
                               "finops_billing_plans for the Azure side of pay-as-you-go"]}

    @mcp.tool(structured_output=False, annotations=READ, description=(
        "Copilot Credits position for ONE Power Platform environment: allocation and enforcement rules (draw from tenant pool), "
        "entitlement consumption, linked pay-as-you-go billing plan, and per-agent limits. environment = id, name or Dataverse URL."))
    async def finops_environment_credits(
        environment: Annotated[str, Field(description="Environment id, display name or https://org.crm.dynamics.com")],
    ) -> dict[str, Any]:
        try:
            env = await resolve_environment(environment)
        except RunbookError as e:
            return e.result
        eid = env["id"]

        async def safe(path: str, query: dict[str, Any] | None = None) -> Any:
            r = await runbook.call("powerplatform", "GET", path, query=query, api_version=PPV, max_chars=200_000)
            return r.get("body") if r.get("ok") else {"error": r.get("message"), "status": r.get("status")}

        alloc, alloc_v1, entitlements, billing, thresholds = await asyncio.gather(
            safe(f"/licensing/allocationsByEnvironment/{eid}"),
            safe(f"/licensing/environments/{eid}/allocations"),
            safe(f"/licensing/environments/{eid}/entitlements"),
            safe(f"/licensing/environments/{eid}/billingPolicy"),
            safe("/licensing/entitlements/MCSMessages/resourceThresholds"),
        )
        env_thresholds = thresholds
        if isinstance(thresholds, dict) and isinstance(thresholds.get("value"), list):
            env_thresholds = [t for t in thresholds["value"] if (t.get("environmentId") or "").lower() == eid.lower()]
        elif isinstance(thresholds, list):
            env_thresholds = [t for t in thresholds if (t.get("environmentId") or "").lower() == eid.lower()]
        # compact the (large) entitlement list: one row per entitlement with the numbers that matter
        ent_rows = []
        ent_list = entitlements if isinstance(entitlements, list) else (entitlements or {}).get("value", []) if isinstance(entitlements, dict) else []
        for e in ent_list:
            ent = e.get("entitlement") or {}
            cap = ent.get("capacity") or {}
            payg = ent.get("payGo") or {}
            allocated = (cap.get("allocated") or {}).get("value", 0) or 0
            consumed = (cap.get("consumed") or {}).get("value", 0) or 0
            payg_consumed = (payg.get("consumed") or {}).get("value", 0) or 0
            if not (allocated or consumed or payg_consumed or e.get("entitlementId") in ("MCSMessages", "AI", "MCSSessions")):
                continue
            ent_rows.append({"entitlement": e.get("entitlementId"), "name": CURRENCY_NAMES.get(e.get("entitlementId"), e.get("entitlementId")),
                             "unit": ent.get("unit"), "allocated": allocated, "auto_allocated": (cap.get("allocated") or {}).get("autoAllocated"),
                             "consumed": consumed, "available": cap.get("availableQuantity"), "status": cap.get("status"),
                             "tenant_pool_governed_by_policy": cap.get("isTenantPoolGovernedByPolicy"),
                             "enforcement_rules": cap.get("enforcementRules"), "payg_entitled": (payg.get("entitled") or {}).get("value"),
                             "payg_consumed": payg_consumed, "products": e.get("productCategories")})
        ent_rows.sort(key=lambda r: -(float(r["consumed"] or 0) + float(r["payg_consumed"] or 0)))
        return {"ok": True,
                "environment": {"id": eid, "displayName": env.get("displayName"), "type": env.get("type"), "url": env.get("url"), "region": env.get("azureRegion"),
                                "managed": (ent_list[0].get("isManagedEnvironment") if ent_list else None)},
                "allocations_and_enforcement": alloc, "currency_allocations": alloc_v1,
                "entitlements": ent_rows if ent_rows or isinstance(entitlements, list) else entitlements, "entitlements_total": len(ent_list),
                "billing_plan": billing, "agent_limits": env_thresholds,
                "glossary": {"MCSMessages": "Copilot Credits", "AI": "AI Builder credits", "TenantPool": "Draw from the available capacity in my tenant"}}

    @mcp.tool(structured_output=False, annotations=WRITE, description=(
        "Allocate Copilot Credits (or another entitlement) to an environment and set whether it may draw from the tenant pool when exhausted. "
        "Returns a preview with current availability unless confirm=true. Needs Power Platform Administrator."))
    async def finops_allocate_copilot_credits(
        environment: Annotated[str, Field(description="Environment id, display name or URL")],
        quantity: Annotated[int, Field(description="Credits to allocate to this environment (absolute, not incremental)")],
        draw_from_tenant_pool: Annotated[bool, Field(description="true = environment may continue consuming unallocated tenant capacity after its allocation is used")] = True,
        entitlement_id: Annotated[str, Field(description="MCSMessages (Copilot Credits) or AI (AI Builder credits)")] = "MCSMessages",
        confirm: Annotated[bool, Field(description="Set true to apply after the admin approved the preview")] = False,
    ) -> dict[str, Any]:
        try:
            env = await resolve_environment(environment)
        except RunbookError as e:
            return e.result
        eid = env["id"]
        availability = await runbook.call("powerplatform", "GET", "/licensing/allocationsV2/availability",
                                          query={"$filter": f"environmentId eq '{eid}' and EntitlementId in ('{entitlement_id}')"}, api_version=PPV, max_chars=20_000)
        body = {"scope": {"environmentId": eid},
                "allocatedEntitlements": [{"entitlementId": entitlement_id, "allocation": {"quantity": int(quantity)},
                                           "enforcementRules": [{"ruleType": "TenantPool", "enabled": bool(draw_from_tenant_pool)}]}]}
        result = await runbook.call("powerplatform", "PUT", "/licensing/allocationsV2", body=body, api_version=PPV, confirm=confirm)
        result["environment"] = {"id": eid, "displayName": env.get("displayName")}
        result["availability_before"] = availability.get("body") if availability.get("ok") else {"error": availability.get("message")}
        if not confirm:
            result["what_will_happen"] = (f"Set {entitlement_id} allocation for '{env.get('displayName')}' to {quantity} and "
                                          f"{'allow' if draw_from_tenant_pool else 'block'} drawing from the tenant pool.")
        return result

    @mcp.tool(structured_output=False, annotations=READ, description=(
        "List per-agent monthly Copilot Credit limits (resource thresholds) configured in the tenant: limit, notification threshold, "
        "hard stop flag, current consumption (month to date only), environment. For spend over longer periods use finops_agent_credits_history."))
    async def finops_agent_limits(
        entitlement_id: Annotated[str, Field(description="MCSMessages (Copilot Credits) or AI")] = "MCSMessages",
        environment_id: Annotated[str, Field(description="Optional environment id to filter")] = "",
        only_with_limit: Annotated[bool, Field(description="true = only agents that have a limit configured")] = False,
        top: Annotated[int, Field(description="Rows to return, ordered by consumption (1-500)")] = 100,
    ) -> dict[str, Any]:
        r = await runbook.call("powerplatform", "GET", f"/licensing/entitlements/{entitlement_id}/resourceThresholds", api_version=PPV, max_chars=4_000_000)
        if not r.get("ok"):
            return r
        body = r.get("body")
        items = body.get("value", body) if isinstance(body, dict) else body
        items = items if isinstance(items, list) else []
        if environment_id:
            items = [i for i in items if (i.get("environmentId") or "").lower() == environment_id.lower()]
        if only_with_limit:
            items = [i for i in items if (i.get("limit") or 0) > 0]
        items.sort(key=lambda i: -float(i.get("resourceConsumption") or 0))
        total_consumption = round(sum(float(i.get("resourceConsumption") or 0) for i in items), 2)
        with_limit = sum(1 for i in items if (i.get("limit") or 0) > 0)
        over = [i for i in items if (i.get("limit") or 0) > 0 and float(i.get("resourceConsumption") or 0) >= float(i.get("limit") or 0)]
        rows = [{"agent_bot_id": i.get("resourceId"), "environment_id": i.get("environmentId"), "consumption_month_to_date": i.get("resourceConsumption"),
                 "limit": i.get("limit") or None, "notification_threshold_pct": i.get("notificationThreshold") or None, "hard_stop": i.get("stopIfOverCapacity"),
                 "stopped": i.get("stopResource"), "created": i.get("createdOn")} for i in items[: max(1, min(int(top), 500))]]
        return {"ok": True, "entitlement": entitlement_id, "agents_total": len(items), "agents_with_limit": with_limit, "agents_over_limit": len(over),
                "consumption_month_to_date_total": total_consumption, "agents": rows,
                "note": "agent_bot_id is the Copilot Studio bot id (match it with pp_copilot_studio_agents). Use finops_set_agent_limit to add or change a limit."}

    @mcp.tool(structured_output=False, annotations=WRITE, description=(
        "Set or update a monthly Copilot Credit limit for ONE agent (Power Platform admin centre > Licensing > Copilot Studio > Manage agents). "
        "notification_threshold_percent triggers admin notifications; stop_when_exceeded turns the agent off at the limit. Preview unless confirm=true."))
    async def finops_set_agent_limit(
        environment: Annotated[str, Field(description="Environment id, display name or URL")],
        agent_bot_id: Annotated[str, Field(description="Agent (bot) id from pp_copilot_studio_agents")],
        monthly_limit: Annotated[int, Field(description="Monthly Copilot Credit limit for this agent")],
        notification_threshold_percent: Annotated[int, Field(description="Notify admins at this % of the limit (1-100)")] = 80,
        stop_when_exceeded: Annotated[bool, Field(description="true = hard stop: turn the agent off when the limit is reached")] = False,
        entitlement_id: Annotated[str, Field(description="MCSMessages (Copilot Credits)")] = "MCSMessages",
        confirm: Annotated[bool, Field(description="Set true to apply after the admin approved the preview")] = False,
    ) -> dict[str, Any]:
        try:
            env = await resolve_environment(environment)
        except RunbookError as e:
            return e.result
        body = {"limit": int(monthly_limit), "notificationThreshold": int(notification_threshold_percent), "notifyIfOverCapacity": True,
                "stopIfOverCapacity": bool(stop_when_exceeded)}
        r = await runbook.call("powerplatform", "PUT", f"/licensing/environments/{env['id']}/entitlements/{entitlement_id}/resources/{agent_bot_id}/threshold",
                               body=body, api_version=PPV, confirm=confirm)
        r["environment"] = {"id": env["id"], "displayName": env.get("displayName")}
        return r

    @mcp.tool(structured_output=False, annotations=READ, description=(
        "Pay-as-you-go billing plans (billing policies) with their linked environments, Azure subscription and resource group, which meters are "
        "enabled (Copilot Credits, Power Apps, Power Automate, Dataverse), and month-to-date Azure cost per plan from Cost Management. "
        "This joins the Power Platform admin centre view with the Azure bill."))
    async def finops_billing_plans(
        include_azure_cost: Annotated[bool, Field(description="Query Azure Cost Management for month-to-date cost per plan")] = True,
    ) -> dict[str, Any]:
        try:
            policies = await runbook.paged("powerplatform", "/licensing/billingPolicies", api_version=PPV, max_pages=10)
        except RunbookError as e:
            return e.result
        plans = []
        for p in policies:
            instr = p.get("billingInstrument") or {}
            meters = {e.get("entitlementId"): bool(e.get("payAsYouGoState")) for e in p.get("payGoEntitlements", [])}
            plans.append({"id": p.get("id"), "name": p.get("name"), "status": p.get("status"), "location": p.get("location"), "type": p.get("type"),
                          "azure_subscription_id": instr.get("subscriptionId"), "azure_resource_group": instr.get("resourceGroup"), "azure_resource_id": instr.get("id"),
                          "environment_ids": p.get("environmentIds") or [], "meters_enabled": {k: v for k, v in meters.items() if v},
                          "copilot_credits_payg_enabled": meters.get("MCSMessages", False), "created": p.get("createdOn"), "last_modified": p.get("lastModifiedOn")})
        cost_notes: list[str] = []
        if include_azure_cost:
            subs = sorted({p["azure_subscription_id"] for p in plans if p.get("azure_subscription_id")})

            async def one(sub: str) -> tuple[str, Any]:
                try:
                    return sub, await _cost_query(f"/subscriptions/{sub}", "MonthToDate", "", "", ["ResourceId", "Meter"],
                                                  {"name": "ResourceType", "operator": "In", "values": ["microsoft.powerplatform/accounts"]})
                except RunbookError as e:
                    return sub, e

            results_by_sub = await asyncio.gather(*(one(s) for s in subs[:12]))
            for sub, q in results_by_sub:
                try:
                    if isinstance(q, RunbookError):
                        raise q
                    rows, currency = _rows(q)
                    by_resource: dict[str, dict[str, Any]] = defaultdict(lambda: {"total": 0.0, "meters": {}})
                    for r in rows:
                        rid = (r.get("ResourceId") or "").lower()
                        by_resource[rid]["total"] += float(r.get("Cost") or 0)
                        by_resource[rid]["meters"][r.get("Meter")] = round(float(r.get("Cost") or 0), 4)
                    for p in plans:
                        if p.get("azure_subscription_id") != sub:
                            continue
                        key = (p.get("azure_resource_id") or "").lower()
                        match = by_resource.get(key) or next((v for k, v in by_resource.items() if k.endswith("/" + (p.get("name") or "").lower())), None)
                        p["azure_cost_month_to_date"] = {"currency": currency, "total": round(match["total"], 4), "by_meter": match["meters"]} if match else {"currency": currency, "total": 0.0, "by_meter": {}}
                except RunbookError as e:
                    cost_notes.append(f"subscription {sub}: {e.result.get('message', 'cost query failed')} {' '.join(e.result.get('hints', []))}")
        total = round(sum((p.get("azure_cost_month_to_date") or {}).get("total", 0) for p in plans), 4)
        return {"ok": True, "count": len(plans), "plans": plans, "azure_cost_month_to_date_total": total if include_azure_cost else None, "cost_notes": cost_notes,
                "note": "Each billing plan is a Microsoft.PowerPlatform/accounts resource in Azure; its meters (for example 'Pay As You Go Copilot Credit') show the Azure side of Copilot Credits."}

    @mcp.tool(structured_output=False, annotations=WRITE, description=(
        "Link (or unlink) an environment to a pay-as-you-go billing plan so its usage bills to Azure. Preview unless confirm=true."))
    async def finops_link_environment_billing_plan(
        billing_policy_id: Annotated[str, Field(description="Billing plan id from finops_billing_plans")],
        environment: Annotated[str, Field(description="Environment id, display name or URL")],
        remove: Annotated[bool, Field(description="true = unlink instead of link")] = False,
        confirm: Annotated[bool, Field(description="Set true to apply after the admin approved the preview")] = False,
    ) -> dict[str, Any]:
        try:
            env = await resolve_environment(environment)
        except RunbookError as e:
            return e.result
        action = "remove" if remove else "add"
        r = await runbook.call("powerplatform", "POST", f"/licensing/billingPolicies/{billing_policy_id}/environments/{action}",
                               body={"environmentIds": [env["id"]]}, api_version=PPV, confirm=confirm)
        r["environment"] = {"id": env["id"], "displayName": env.get("displayName")}
        return r

    @mcp.tool(structured_output=False, annotations=READ, description=(
        "Generate and download the Copilot Studio licensing (consumption) report for a date range via the Power Platform API, then "
        "aggregate billed and non-billed credits by agent, environment, product/feature, channel and model. This is the programmatic version of "
        "the Power Platform admin centre download. report_name is the report type; if unsure leave default and the tool lists what the API accepts."))
    async def finops_copilot_studio_consumption_report(
        start_date: Annotated[str, Field(description="Inclusive UTC start date YYYY-MM-DD")],
        end_date: Annotated[str, Field(description="Inclusive UTC end date YYYY-MM-DD")],
        report_name: Annotated[str, Field(description="Report type name accepted by the API (for example Environment, Agent or User). Default tries common names.")] = "",
        granularity: Annotated[str, Field(description="Summary (one row per entity for the range) or Daily")] = "Summary",
        wait_seconds: Annotated[int, Field(description="Maximum seconds to wait for generation (10-170)")] = 90,
        top: Annotated[int, Field(description="Rows to return per aggregation (1-200)")] = 50,
    ) -> dict[str, Any]:
        names = [report_name] if report_name else ["Environment", "Agent", "User", "Consumption", "Detailed"]
        body = {"startDate": start_date, "endDate": end_date, "granularity": granularity or "Summary"}
        start_resp: dict[str, Any] | None = None
        tried: list[dict[str, Any]] = []
        used = ""
        for n in names:
            r = await runbook.call("powerplatform", "POST", f"/copilotstudio/licensing/reports/{quote(n)}", body=body, api_version=PPV, confirm=True, max_chars=20_000)
            tried.append({"report_name": n, "status": r.get("status"), "message": r.get("message", "")})
            if r.get("ok"):
                start_resp, used = r, n
                break
        if start_resp is None:
            return err("The Copilot Studio licensing report endpoint (POST /copilotstudio/licensing/reports/{ReportName}) returned 404 for every report name tried. "
                       "As of October 2026 Microsoft has not published the accepted report names and the endpoint may not be enabled for this tenant yet. "
                       "Use finops_agent_credits_history instead: billed and non-billable credits per agent for any period up to 24 months. Other options: finops_agent_limits (per agent, month to date), "
                       "or download the consumption report in the Power Platform admin centre (Licensing > Copilot Studio > Download report).",
                       attempts=tried)
        poll_url = (start_resp.get("async_operation") or {}).get("poll_url") or start_resp.get("response_headers", {}).get("location")
        if not poll_url:
            return {"ok": True, "report_name": used, "started": start_resp, "note": "No operation URL returned; the report may have been returned inline."}
        deadline = asyncio.get_event_loop().time() + max(10, min(int(wait_seconds), 170))
        status_body: dict[str, Any] = {}
        while True:
            pr = await runbook.call("powerplatform", "GET", poll_url, max_chars=20_000)
            status_body = pr.get("body") if isinstance(pr.get("body"), dict) else {}
            st = str(status_body.get("status") or "").lower()
            if st in ("succeeded", "failed", "canceled", "cancelled"):
                break
            if asyncio.get_event_loop().time() >= deadline:
                return {"ok": False, "report_name": used, "status": "still_running", "poll_url": poll_url, "last": status_body,
                        "next_step": "Call poll_operation with poll_url, then invoke GET /copilotstudio/licensing/reports/<name>/files/<fileName>."}
            await asyncio.sleep(5)
        if str(status_body.get("status", "")).lower() != "succeeded":
            return err(f"Report generation {status_body.get('status')}: {status_body.get('error')}", report_name=used, operation=status_body)
        file_name = status_body.get("fileName") or status_body.get("file_name")
        dl = await runbook.call("powerplatform", "GET", f"/copilotstudio/licensing/reports/{quote(used)}/files/{quote(str(file_name))}", api_version=PPV, max_chars=5_000_000)
        if not dl.get("ok"):
            return dl
        data = dl.get("body") or {}
        rows = data.get("rows", []) if isinstance(data, dict) else []
        columns = data.get("columns", []) if isinstance(data, dict) else []
        credit_cols = [c for c in columns if re.search(r"credit|billed|units|consum", c, re.IGNORECASE) and not re.search(r"name|id|date", c, re.IGNORECASE)]
        dim_cols = [c for c in columns if re.search(r"agent|environment|product|feature|channel|model|user|knowledge|tool|harness", c, re.IGNORECASE) and c not in credit_cols]

        def numeric(v: Any) -> float:
            try:
                return float(str(v).replace(",", "") or 0)
            except ValueError:
                return 0.0

        aggregations: dict[str, list[dict[str, Any]]] = {}
        for dim in dim_cols[:8]:
            agg: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
            for r in rows:
                key = str(r.get(dim) or "(blank)")
                for cc in credit_cols:
                    agg[key][cc] += numeric(r.get(cc))
                agg[key]["rows"] += 1
            ordered = sorted(agg.items(), key=lambda kv: -max((kv[1].get(cc, 0) for cc in credit_cols), default=0))
            aggregations[dim] = [{dim: k, **{c: round(v, 2) for c, v in vals.items()}} for k, vals in ordered[: max(1, min(int(top), 200))]]
        totals = {cc: round(sum(numeric(r.get(cc)) for r in rows), 2) for cc in credit_cols}
        return {"ok": True, "report_name": used, "granularity": granularity, "period": {"start": start_date, "end": end_date}, "file_name": file_name,
                "row_count": data.get("row_count", len(rows)), "columns": columns, "credit_columns": credit_cols, "totals": totals,
                "aggregations": aggregations, "sample_rows": rows[:10],
                "note": "The report does not include token counts; credits are the billing unit. Use finops_azure_ai_tokens for Azure OpenAI tokens."}

    # ---------------------------------------------------------------- Azure AI
    @mcp.tool(structured_output=False, annotations=READ, description=(
        "Inventory of Azure OpenAI / Foundry (Cognitive Services) accounts and their model deployments across subscriptions: model, version, "
        "SKU (GlobalStandard, Standard, DataZoneStandard, ProvisionedManaged), capacity (TPM thousands or PTUs), rate limits, region, resource group. "
        "Uses Azure Resource Graph, so it is fast across many subscriptions."))
    async def finops_azure_ai_inventory(
        subscription_id: Annotated[str, Field(description="Optional subscription id to limit the search; default = all subscriptions you can see")] = "",
        include_deployments: Annotated[bool, Field(description="Also list model deployments per account (up to 40 accounts)")] = True,
    ) -> dict[str, Any]:
        query = ("resources | where type =~ 'microsoft.cognitiveservices/accounts' "
                 "| project id, name, kind, location, resourceGroup, subscriptionId, sku=tostring(sku.name), "
                 "endpoint=tostring(properties.endpoint), publicNetworkAccess=tostring(properties.publicNetworkAccess), tags "
                 "| order by kind asc, name asc")
        body: dict[str, Any] = {"query": query, "options": {"resultFormat": "objectArray", "$top": 1000}}
        if subscription_id:
            body["subscriptions"] = [subscription_id]
        r = await runbook.call("arm", "POST", "/providers/Microsoft.ResourceGraph/resources", body=body, api_version=ARM_API_ARG, max_chars=2_000_000)
        if not r.get("ok"):
            return r
        accounts = (r.get("body") or {}).get("data", [])
        ai_accounts = [a for a in accounts if (a.get("kind") or "").lower() in ("openai", "aiservices")]

        async def deployments(acc: dict[str, Any]) -> None:
            d = await runbook.call("arm", "GET", f"{acc['id']}/deployments", api_version=ARM_API_COGNITIVE, max_chars=400_000)
            if not d.get("ok"):
                acc["deployments_error"] = d.get("message")
                return
            acc["deployments"] = []
            for dep in (d.get("body") or {}).get("value", []):
                props = dep.get("properties") or {}
                model = props.get("model") or {}
                acc["deployments"].append({
                    "name": dep.get("name"), "model": model.get("name"), "version": model.get("version"), "format": model.get("format"),
                    "sku": (dep.get("sku") or {}).get("name"), "capacity": (dep.get("sku") or {}).get("capacity"),
                    "capacity_unit": "PTU" if "provisioned" in str((dep.get("sku") or {}).get("name", "")).lower() else "thousand tokens per minute",
                    "rate_limits": props.get("rateLimits"), "state": props.get("provisioningState"),
                    "version_upgrade": props.get("versionUpgradeOption"), "created": (dep.get("systemData") or {}).get("createdAt"),
                    "created_by": (dep.get("systemData") or {}).get("createdBy"),
                })

        if include_deployments and ai_accounts:
            await asyncio.gather(*(deployments(a) for a in ai_accounts[:40]))
        total_deps = sum(len(a.get("deployments", [])) for a in ai_accounts)
        return {"ok": True, "accounts_total": len(accounts), "ai_accounts": len(ai_accounts), "deployments_total": total_deps,
                "accounts": accounts, "next_steps": ["finops_tokenomics with subscription_id (resource_id empty) analyses every account in ONE call", "finops_azure_ai_tokens <account resource id> for daily token series of one account"]}

    @mcp.tool(structured_output=False, annotations=READ, description=(
        "Token consumption for an Azure OpenAI / Foundry account from Azure Monitor: prompt (input), completion (output) and total tokens per model "
        "deployment, per day, for the last N days. resource_id is the full ARM id of the account (from finops_azure_ai_inventory)."))
    async def finops_azure_ai_tokens(
        resource_id: Annotated[str, Field(description="ARM resource id: /subscriptions/.../providers/Microsoft.CognitiveServices/accounts/<name>")],
        days: Annotated[int, Field(description="Look back this many days (1-90)")] = 7,
        interval: Annotated[str, Field(description="ISO 8601 interval: P1D (daily) or PT1H (hourly, max 7 days)")] = "P1D",
        extra_metrics: Annotated[str, Field(description="Optional extra metric names, comma separated, e.g. AzureOpenAIProvisionedManagedUtilizationV2,TokensPerSecond")] = "",
    ) -> dict[str, Any]:
        days = max(1, min(int(days), 90))
        end = dt.datetime.now(dt.timezone.utc).replace(minute=0, second=0, microsecond=0)
        start = end - dt.timedelta(days=days)
        metrics = "ProcessedPromptTokens,GeneratedTokens,TokenTransaction"
        r = await runbook.call("arm", "GET", f"{resource_id}/providers/Microsoft.Insights/metrics", api_version=ARM_API_METRICS,
                               query={"metricnames": metrics, "interval": interval, "timespan": f"{start.isoformat().replace('+00:00', 'Z')}/{end.isoformat().replace('+00:00', 'Z')}",
                                      "aggregation": "Total", "$filter": "ModelDeploymentName eq '*'"}, max_chars=5_000_000)
        if not r.get("ok"):
            return r
        per_dep: dict[str, dict[str, Any]] = defaultdict(lambda: {"input": 0, "output": 0, "total": 0, "series": defaultdict(lambda: {"input": 0, "output": 0, "total": 0})})
        label = {"ProcessedPromptTokens": "input", "GeneratedTokens": "output", "TokenTransaction": "total"}
        for metric in (r.get("body") or {}).get("value", []):
            mname = (metric.get("name") or {}).get("value")
            key = label.get(mname, mname)
            for ts in metric.get("timeseries", []):
                dep = next((mv.get("value") for mv in ts.get("metadatavalues", []) if (mv.get("name") or {}).get("value", "").lower() == "modeldeploymentname"), "(all)")
                for point in ts.get("data", []):
                    v = int(point.get("total") or 0)
                    per_dep[dep][key] += v
                    if v:
                        per_dep[dep]["series"][point.get("timeStamp", "")[:13 if interval.startswith("PT") else 10]][key] += v
        extras: dict[str, Any] = {}
        if extra_metrics:
            r2 = await runbook.call("arm", "GET", f"{resource_id}/providers/Microsoft.Insights/metrics", api_version=ARM_API_METRICS,
                                    query={"metricnames": extra_metrics, "interval": interval, "timespan": f"{start.isoformat().replace('+00:00', 'Z')}/{end.isoformat().replace('+00:00', 'Z')}",
                                           "aggregation": "Average,Maximum,Total"}, max_chars=1_000_000)
            extras = r2.get("body") if r2.get("ok") else {"error": r2.get("message")}
        out = []
        grand = {"input": 0, "output": 0, "total": 0}
        for dep, v in per_dep.items():
            for k in grand:
                grand[k] += v[k]
            series = [{"period": p, **vals} for p, vals in sorted(v["series"].items())]
            out.append({"deployment": dep, "input_tokens": v["input"], "output_tokens": v["output"], "total_tokens": v["total"],
                        "output_share_pct": _pct(v["output"], v["total"]), "avg_tokens_per_day": round(v["total"] / days), "series": series})
        out.sort(key=lambda x: -x["total_tokens"])
        return {"ok": True, "resource_id": resource_id, "window": {"start": start.isoformat(), "end": end.isoformat(), "days": days, "interval": interval},
                "totals": grand, "deployments": out, "extra_metrics": extras,
                "notes": ["ProcessedPromptTokens = input, GeneratedTokens = output, TokenTransaction = total.", "Metrics are near real time (1-5 minutes lag); billing uses the same counters but applies cache discounts."]}

    @mcp.tool(structured_output=False, annotations=READ, description=(
        "Azure cost for AI services from Cost Management: cost per resource and per meter (input tokens, output tokens, cached input, PTU hours, "
        "images, audio) for a scope (subscription id, /subscriptions/<id>, resource group or resource id) and timeframe. Also works for any resource type."))
    async def finops_azure_ai_cost(
        scope: Annotated[str, Field(description="Subscription id, /subscriptions/<id>, /subscriptions/<id>/resourceGroups/<rg>, or a resource id. Default: DEFAULT_SUBSCRIPTION_ID")] = "",
        timeframe: Annotated[str, Field(description="MonthToDate | BillingMonthToDate | TheLastMonth | TheLastBillingMonth | WeekToDate | Custom (then set start_date and end_date)")] = "MonthToDate",
        start_date: Annotated[str, Field(description="YYYY-MM-DD when timeframe=Custom")] = "",
        end_date: Annotated[str, Field(description="YYYY-MM-DD when timeframe=Custom")] = "",
        resource_type: Annotated[str, Field(description="ARM resource type to filter, default microsoft.cognitiveservices/accounts; use * for all")] = "microsoft.cognitiveservices/accounts",
        group_by_day: Annotated[bool, Field(description="true = daily granularity")] = False,
    ) -> dict[str, Any]:
        s, help_ = await _scope_or_help(scope)
        if help_:
            return help_
        dim = None if resource_type.strip() in ("", "*") else {"name": "ResourceType", "operator": "In", "values": [resource_type.lower()]}
        try:
            q = await _cost_query(s, timeframe, start_date, end_date, ["ResourceId", "MeterSubCategory", "Meter"], dim, "Daily" if group_by_day else "None")
        except RunbookError as e:
            return e.result
        rows, currency = _rows(q)
        by_res: dict[str, dict[str, Any]] = defaultdict(lambda: {"total": 0.0, "meters": defaultdict(float), "categories": defaultdict(float)})
        total = 0.0
        for r in rows:
            c = float(r.get("Cost") or 0)
            rid = r.get("ResourceId") or "(none)"
            by_res[rid]["total"] += c
            by_res[rid]["meters"][r.get("Meter")] += c
            by_res[rid]["categories"][_classify_meter(r.get("Meter") or "")] += c
            total += c
        resources = [{"resource_id": rid, "name": rid.split("/")[-1], "total": round(v["total"], 4),
                      "by_category": {k: round(x, 4) for k, x in sorted(v["categories"].items(), key=lambda kv: -kv[1])},
                      "by_meter": {k: round(x, 4) for k, x in sorted(v["meters"].items(), key=lambda kv: -kv[1])}} for rid, v in by_res.items()]
        resources.sort(key=lambda x: -x["total"])
        daily = None
        if group_by_day:
            d: dict[str, float] = defaultdict(float)
            for r in rows:
                d[str(r.get("UsageDate") or r.get("BillingMonth") or "")] += float(r.get("Cost") or 0)
            daily = [{"date": k, "cost": round(v, 4)} for k, v in sorted(d.items())]
        return {"ok": True, "scope": s, "timeframe": timeframe if not (start_date and end_date) else f"{start_date}..{end_date}", "currency": currency,
                "total": round(total, 4), "resource_type": resource_type, "resources": resources, "daily": daily, "row_count": len(rows),
                "note": "Cost Management data lags usage by up to 24 hours and may be re-rated until the invoice closes."}

    # ---------------------------------------------------------------- tokenomics
    @mcp.tool(structured_output=False, annotations=READ, description=(
        "TOKENOMICS for Azure OpenAI / Foundry: joins Azure Monitor token counts with Cost Management actual cost per meter over the window, "
        "giving per deployment input/output/cached tokens, cost, effective price per million tokens, cache share, output ratio, daily run-rate, "
        "projected monthly spend, matching Azure retail list prices and plain-English recommendations. "
        "Leave resource_id EMPTY to analyse every account in a subscription in ONE call (preferred: one Cost Management query, accounts ranked by cost). "
        "Pass resource_id only for a single account. Never call this once per account in a loop."))
    async def finops_tokenomics(
        resource_id: Annotated[str, Field(description="Optional ARM id of one Azure OpenAI / AI Services account. Empty = all accounts in the subscription")] = "",
        subscription_id: Annotated[str, Field(description="Subscription id for the all-accounts mode (default DEFAULT_SUBSCRIPTION_ID, or the only subscription you can see)")] = "",
        days: Annotated[int, Field(description="Window in days (2-90)")] = 30,
        top_accounts: Annotated[int, Field(description="All-accounts mode: analyse at most this many accounts, highest cost first (1-25)")] = 10,
    ) -> dict[str, Any]:
        days = max(2, min(int(days), 90))
        end_day = _today() - dt.timedelta(days=1)  # cost data for today is incomplete
        start_day = end_day - dt.timedelta(days=days - 1)
        window = {"start": start_day.isoformat(), "end": end_day.isoformat(), "days": days}
        timespan = f"{start_day.isoformat()}T00:00:00Z/{(end_day + dt.timedelta(days=1)).isoformat()}T00:00:00Z"

        def metrics_call(rid: str):
            return runbook.call("arm", "GET", f"{rid}/providers/Microsoft.Insights/metrics", api_version=ARM_API_METRICS,
                                query={"metricnames": "ProcessedPromptTokens,GeneratedTokens,TokenTransaction", "interval": "P1D", "timespan": timespan,
                                       "aggregation": "Total", "$filter": "ModelDeploymentName eq '*'"}, max_chars=5_000_000)

        def deployments_call(rid: str):
            return runbook.call("arm", "GET", f"{rid}/deployments", api_version=ARM_API_COGNITIVE, max_chars=400_000)

        # ---------------------------------------------------------- one account
        if resource_id.strip():
            resource_id = resource_id.strip()
            sub_match = re.search(r"^/subscriptions/([^/]+)", resource_id, re.IGNORECASE)
            if not sub_match:
                return err("resource_id must be a full ARM id starting with /subscriptions/<id>/...")
            sub_scope = f"/subscriptions/{sub_match.group(1)}"
            cost_task = _cost_query(sub_scope, "Custom", start_day.isoformat(), end_day.isoformat(), ["Meter", "MeterSubCategory"],
                                    {"name": "ResourceId", "operator": "In", "values": [resource_id.lower()]})
            account_task = runbook.call("arm", "GET", resource_id, api_version=ARM_API_COGNITIVE, max_chars=50_000)
            tokens_r, deps_r, account_r, cost_q = await asyncio.gather(metrics_call(resource_id), deployments_call(resource_id), account_task, cost_task, return_exceptions=True)
            if isinstance(tokens_r, Exception) or not tokens_r.get("ok"):
                return tokens_r.result if isinstance(tokens_r, RunbookError) else (tokens_r if isinstance(tokens_r, dict) else err(str(tokens_r)))
            region = (account_r.get("body") or {}).get("location", "") if isinstance(account_r, dict) and account_r.get("ok") else ""
            cost_rows, currency = _rows(cost_q) if isinstance(cost_q, dict) else ([], "")
            cost_error = cost_q.result.get("message") if isinstance(cost_q, RunbookError) else (str(cost_q) if isinstance(cost_q, Exception) else "")
            a = _account_analysis(resource_id, region, days, tokens_r, deps_r, cost_rows, currency)
            if not cost_rows and not cost_error:
                a["recommendations"].append("No cost rows yet for this window. Cost Management lags by up to 24 hours; token metrics are near real time.")
            models = sorted({(d.get("model") or "") for d in a["deployments"] if d.get("model")})
            list_prices, price_note = await _list_prices_for([(m, region) for m in models[:6]])
            return {"ok": True, "mode": "account", "resource_id": resource_id, "region": region, "window": window,
                    "account_totals": a["totals"], "deployments": a["deployments"], "meters": a["meters"], "unassigned_meters": a["unassigned"],
                    "cost_error": cost_error, "list_prices": list_prices, "list_price_note": price_note, "recommendations": a["recommendations"],
                    "method": TOKENOMICS_METHOD}

        # ------------------------------------------------- every account in one subscription
        s, help_ = await _scope_or_help(subscription_id)
        if help_:
            return help_
        if not re.match(r"^/subscriptions/[^/]+$", s, re.IGNORECASE):
            return err("subscription_id must be a subscription id (or set DEFAULT_SUBSCRIPTION_ID); pass resource_id for a single account instead.")
        sub_id = s.split("/")[2]
        top_accounts = max(1, min(int(top_accounts), 25))
        arg_body = {"query": ("resources | where type =~ 'microsoft.cognitiveservices/accounts' "
                              "| project id, name, location, resourceGroup, kind | order by name asc"),
                    "subscriptions": [sub_id], "options": {"resultFormat": "objectArray", "$top": 1000}}
        cost_task = _cost_query(s, "Custom", start_day.isoformat(), end_day.isoformat(), ["ResourceId", "Meter", "MeterSubCategory"],
                                {"name": "ResourceType", "operator": "In", "values": ["microsoft.cognitiveservices/accounts"]})
        arg_task = runbook.call("arm", "POST", "/providers/Microsoft.ResourceGraph/resources", body=arg_body, api_version=ARM_API_ARG, max_chars=2_000_000)
        cost_q, arg_r = await asyncio.gather(cost_task, arg_task, return_exceptions=True)
        cost_rows, currency = _rows(cost_q) if isinstance(cost_q, dict) else ([], "")
        cost_error = cost_q.result.get("message") if isinstance(cost_q, RunbookError) else (str(cost_q) if isinstance(cost_q, Exception) else "")
        accounts: dict[str, dict[str, Any]] = {}
        inventory_error = ""
        if isinstance(arg_r, dict) and arg_r.get("ok"):
            for acc in (arg_r.get("body") or {}).get("data", []):
                accounts[str(acc.get("id", "")).lower()] = acc
        else:
            inventory_error = arg_r.get("message", "") if isinstance(arg_r, dict) else str(arg_r)
        cost_by_res: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for r in cost_rows:
            cost_by_res[str(r.get("ResourceId") or "").lower()].append(r)
        cost_total_by_res = {rid: sum(float(r.get("Cost") or 0) for r in rows) for rid, rows in cost_by_res.items()}
        # Only OpenAI / AI Services accounts have token metrics and model deployments. Translator, Speech,
        # Vision and the like still cost money, so they are listed separately rather than analysed.
        model_kinds = ("openai", "aiservices")
        model_accounts = {rid for rid, a in accounts.items() if str(a.get("kind") or "").lower() in model_kinds}
        if accounts:
            candidates = model_accounts | {rid for rid in cost_by_res if rid not in accounts}  # cost-only ids: deleted or inventory missed them
        else:
            candidates = set(cost_by_res)  # inventory failed: analyse whatever has cost
        other_accounts = [{"name": accounts[rid].get("name"), "kind": accounts[rid].get("kind"), "resource_id": accounts[rid].get("id"), "cost": round(cost_total_by_res.get(rid, 0.0), 4)}
                          for rid in accounts if rid not in model_accounts and cost_total_by_res.get(rid, 0.0)]
        other_accounts.sort(key=lambda x: -x["cost"])
        if not candidates and not other_accounts:
            return {"ok": True, "mode": "subscription", "subscription_id": sub_id, "window": window, "accounts_found": 0, "accounts": [],
                    "cost_error": cost_error, "inventory_error": inventory_error,
                    "recommendations": ["No Azure OpenAI / AI Services accounts and no AI cost in this subscription for the window."]}
        ranked = sorted(candidates, key=lambda rid: (-cost_total_by_res.get(rid, 0.0), rid))
        chosen = ranked[:top_accounts]
        sem = asyncio.Semaphore(6)

        async def analyse(rid_lower: str) -> dict[str, Any]:
            info = accounts.get(rid_lower) or {}
            rid = info.get("id") or rid_lower
            async with sem:
                tokens_r, deps_r = await asyncio.gather(metrics_call(rid), deployments_call(rid), return_exceptions=True)
            a = _account_analysis(rid, info.get("location", ""), days, tokens_r, deps_r, cost_by_res.get(rid_lower, []), currency)
            return {"resource_id": rid, "name": info.get("name") or rid.split("/")[-1], "region": info.get("location", ""), "resource_group": info.get("resourceGroup", ""),
                    "kind": info.get("kind", ""), "totals": a["totals"], "deployments": a["deployments"], "unassigned_meters": a["unassigned"],
                    "errors": a["errors"], "recommendations": [f"{info.get('name') or rid.split('/')[-1]}: {x}" for x in a["recommendations"]]}

        tasks = {asyncio.ensure_future(analyse(rid)): rid for rid in chosen}
        done, pending = await asyncio.wait(set(tasks), timeout=budget.fanout_timeout(80.0))
        for t in pending:
            t.cancel()
        analysed: list[dict[str, Any]] = []
        for t in done:
            try:
                analysed.append(t.result())
            except Exception as e:  # noqa: BLE001
                analysed.append({"resource_id": tasks[t], "name": tasks[t].split("/")[-1], "error": str(e)[:300], "totals": {"tokens": 0, "cost": round(cost_total_by_res.get(tasks[t], 0.0), 4)}})
        analysed.sort(key=lambda x: (-(x.get("totals") or {}).get("cost", 0), -(x.get("totals") or {}).get("tokens", 0)))
        skipped_for_time = [tasks[t].split("/")[-1] for t in pending]
        not_analysed = len(ranked) - len(chosen)

        grand_cost = sum(cost_total_by_res.values())
        other_cost = sum(x["cost"] for x in other_accounts)
        grand_tokens = sum(int((x.get("totals") or {}).get("tokens", 0)) for x in analysed)
        analysed_cost = sum(float((x.get("totals") or {}).get("cost", 0)) for x in analysed)
        recommendations: list[str] = []
        for x in analysed:
            recommendations.extend(x.get("recommendations", []))
        if other_accounts:
            recommendations.append(f"{len(other_accounts)} Cognitive Services account(s) of other kinds ({', '.join(sorted({str(x['kind']) for x in other_accounts}))}) cost {round(other_cost, 2)} {currency} in the window; see other_cognitive_services. Use finops_azure_ai_cost for their meters.")
        idle = [x["name"] for x in analysed if not (x.get("totals") or {}).get("tokens") and not (x.get("totals") or {}).get("cost") and x.get("deployments")]
        if idle:
            recommendations.append(f"{len(idle)} account(s) have deployments but no tokens and no cost in {days} days ({', '.join(idle[:8])}). Delete unused deployments to free quota.")
        if not_analysed > 0:
            recommendations.append(f"{not_analysed} further account(s) were not analysed (top_accounts={top_accounts}); the subscription totals still include their cost. Raise top_accounts or pass resource_id for one of them.")
        if skipped_for_time:
            recommendations.append(f"Ran out of time budget before analysing {', '.join(skipped_for_time[:8])}; call again with a smaller top_accounts or pass resource_id.")
        if not cost_rows and not cost_error:
            recommendations.append("No AI cost rows in this window. Cost Management lags by up to 24 hours; token metrics are near real time.")
        pairs: list[tuple[str, str]] = []
        for x in analysed:
            for d in x.get("deployments", []):
                if d.get("model") and (d.get("model"), x.get("region", "")) not in pairs:
                    pairs.append((d["model"], x.get("region", "")))
        list_prices, price_note = await _list_prices_for(pairs[:6])
        return {"ok": True, "mode": "subscription", "subscription_id": sub_id, "window": window, "currency": currency,
                "subscription_totals": {"cognitive_services_cost": round(grand_cost, 4), "model_accounts_cost": round(grand_cost - other_cost, 4),
                                        "other_cognitive_services_cost": round(other_cost, 4),
                                        "cost_of_analysed_accounts": round(analysed_cost, 4), "tokens_of_analysed_accounts": int(grand_tokens),
                                        "effective_price_per_1M_tokens_blended": round(analysed_cost / (grand_tokens / 1e6), 4) if grand_tokens and analysed_cost else None,
                                        "projected_30_day_cost": round(grand_cost / days * 30, 2)},
                "model_accounts_found": len(ranked), "accounts_analysed": len(analysed), "accounts_not_analysed": not_analysed, "skipped_for_time": skipped_for_time,
                "accounts": analysed, "other_cognitive_services": other_accounts[:25], "cost_error": cost_error, "inventory_error": inventory_error,
                "list_prices": list_prices, "list_price_note": price_note, "recommendations": recommendations, "method": TOKENOMICS_METHOD}

    @mcp.tool(structured_output=False, annotations=READ, description=(
        "Azure OpenAI quota and usage for a subscription and region: tokens-per-minute quota per model and deployment type, PTU limits, current usage and headroom."))
    async def finops_azure_ai_quota(
        location: Annotated[str, Field(description="Azure region, e.g. swedencentral, eastus2, uksouth")],
        subscription_id: Annotated[str, Field(description="Subscription id (default DEFAULT_SUBSCRIPTION_ID)")] = "",
        only_openai: Annotated[bool, Field(description="Only show OpenAI quota lines")] = True,
    ) -> dict[str, Any]:
        s, help_ = await _scope_or_help(subscription_id)
        if help_:
            return help_
        r = await runbook.call("arm", "GET", f"{s}/providers/Microsoft.CognitiveServices/locations/{location}/usages", api_version=ARM_API_COGNITIVE, max_chars=2_000_000)
        if not r.get("ok"):
            return r
        lines = []
        for u in (r.get("body") or {}).get("value", []):
            name = (u.get("name") or {}).get("value", "")
            if only_openai and "openai" not in name.lower():
                continue
            cur, lim = u.get("currentValue") or 0, u.get("limit") or 0
            lines.append({"quota": name, "display": (u.get("name") or {}).get("localizedValue"), "used": cur, "limit": lim, "unit": u.get("unit"),
                          "used_pct": _pct(cur, lim), "headroom": lim - cur})
        lines.sort(key=lambda x: -(x["used_pct"] or 0))
        return {"ok": True, "scope": s, "location": location, "count": len(lines), "quota": lines,
                "note": "Standard quota is in thousands of tokens per minute (TPM); ProvisionedManaged lines are PTUs. Request increases in the Foundry portal or via the Quota API."}

    @mcp.tool(structured_output=False, annotations=READ, description=(
        "Azure retail list prices for AI meters (Azure OpenAI and Foundry models): price per 1K/1M tokens by model, direction (input/output/cached), "
        "deployment type (global, data zone, regional) and region. Public data, no permissions needed."))
    async def finops_price_lookup(
        model_text: Annotated[str, Field(description="Model text, e.g. gpt-4o, gpt-4.1 mini, o3, gpt-5")],
        region: Annotated[str, Field(description="Optional armRegionName, e.g. swedencentral, eastus2")] = "",
        currency: Annotated[str, Field(description="Currency code, default USD")] = "USD",
        limit: Annotated[int, Field(description="Max items (1-200)")] = 60,
    ) -> dict[str, Any]:
        toks = _model_tokens(model_text)
        if not toks:
            return err("model_text is required")
        flt = _price_filter(model_text, region)
        raw, error = await _retail_prices(flt, currency, retry_wait_budget=20.0)
        if error and not raw:
            return err(f"Retail Prices API: {error}", filter=flt)
        items = [_price_item(it) for it in raw]
        for it in items:
            it.pop("model_query", None)
        rank = {"input": 0, "cached_input": 1, "output": 2, "cache_write": 3, "batch_input": 4, "batch_output": 5, "provisioned": 6}
        items.sort(key=lambda x: (rank.get(x["category"], 9), x["deployment_type"], x["price_per_1M"] or 0))
        return {"ok": True, "query": model_text, "filter": flt, "count": len(items), "items": items[: max(1, min(int(limit), 200))],
                "cache_note": error or None,
                "note": "Retail prices are list prices in the requested currency; your agreement may differ. Global < Data Zone < Regional."}

    # ---------------------------------------------------------------- Microsoft 365 Copilot
    @mcp.tool(structured_output=False, annotations=READ, description=(
        "Microsoft 365 Copilot licence position and usage: SKUs with Copilot in the name (purchased, assigned, available), and from the Copilot usage report "
        "how many licensed users were inactive for N days, with an estimated monthly waste at list price. Needs Reports.Read.All consented for the usage part."))
    async def finops_m365_copilot_licences(
        inactive_days: Annotated[int, Field(description="A user is inactive if their last Copilot activity is older than this (7-180)")] = 30,
        list_price_per_user_month: Annotated[float, Field(description="Price used for the waste estimate (default 30 USD)")] = 30.0,
    ) -> dict[str, Any]:
        skus_r = await runbook.call("graph", "GET", "/v1.0/subscribedSkus", query={"$select": "skuPartNumber,skuId,consumedUnits,prepaidUnits,capabilityStatus"}, max_chars=2_000_000)
        if not skus_r.get("ok"):
            return skus_r
        skus = []
        for s in (skus_r.get("body") or {}).get("value", []):
            part = s.get("skuPartNumber", "")
            if "COPILOT" in part.upper() or "AI" == part.upper():
                en = (s.get("prepaidUnits") or {}).get("enabled") or 0
                used = s.get("consumedUnits") or 0
                skus.append({"sku": part, "sku_id": s.get("skuId"), "purchased": en, "assigned": used, "unassigned": en - used, "status": s.get("capabilityStatus")})
        skus.sort(key=lambda x: -x["purchased"])
        usage: dict[str, Any] = {}
        period = "D90" if inactive_days > 30 else "D30"
        ur = await runbook.call("graph", "GET", f"/beta/copilot/reports/getMicrosoft365CopilotUsageUserDetail(period='{period}')", max_chars=20_000_000)
        if ur.get("ok"):
            rows = (ur.get("body") or {}).get("value", []) if isinstance(ur.get("body"), dict) else []
            cutoff = _today() - dt.timedelta(days=inactive_days)
            inactive, active, never = 0, 0, 0
            apps = defaultdict(int)
            for row in rows:
                last = row.get("lastActivityDate")
                if not last:
                    never += 1
                    inactive += 1
                    continue
                try:
                    d = dt.date.fromisoformat(str(last)[:10])
                except ValueError:
                    continue
                if d < cutoff:
                    inactive += 1
                else:
                    active += 1
                for k, v in row.items():
                    if k.endswith("LastActivityDate") and v and k != "lastActivityDate":
                        apps[k.replace("LastActivityDate", "")] += 1
            usage = {"report_period": period, "licensed_users_in_report": len(rows), "active_users": active, "inactive_users": inactive, "never_used": never,
                     "inactive_days_threshold": inactive_days, "app_activity_counts": dict(sorted(apps.items(), key=lambda kv: -kv[1])),
                     "estimated_monthly_waste": round(inactive * float(list_price_per_user_month), 2), "waste_currency": "USD (list price assumption)",
                     "report_truncated": ur.get("truncated", False)}
        else:
            usage = {"error": ur.get("message"), "status": ur.get("status"), "hints": ur.get("hints", []),
                     "fix": "Grant Reports.Read.All (delegated, admin consent) to the app registration and make sure the admin holds Reports Reader, Global Reader or AI Administrator."}
        recs: list[str] = []
        for s in skus:
            if s["unassigned"] > 0:
                recs.append(f"{s['sku']}: {s['unassigned']} licences purchased but not assigned.")
        if usage.get("inactive_users"):
            recs.append(f"{usage['inactive_users']} licensed users show no Copilot activity in {inactive_days} days (about {usage['estimated_monthly_waste']} per month at list price). Reassign or run adoption nudges before renewal.")
        return {"ok": True, "copilot_skus": skus, "usage": usage, "recommendations": recs}

    # ---------------------------------------------------------------- Budgets and alerts
    @mcp.tool(structured_output=False, annotations=WRITE, description=(
        "Create or update an Azure budget on a scope (subscription or resource group) with 80% actual and 100% forecasted alerts to the given emails. "
        "Use it to put a guard rail around AI spend. Preview unless confirm=true."))
    async def finops_create_budget(
        scope: Annotated[str, Field(description="Subscription id, /subscriptions/<id> or /subscriptions/<id>/resourceGroups/<rg>")],
        name: Annotated[str, Field(description="Budget name, e.g. ai-monthly-cap")],
        amount: Annotated[float, Field(description="Budget amount in the billing currency")],
        contact_emails: Annotated[str, Field(description="Comma separated email addresses for alerts")],
        time_grain: Annotated[str, Field(description="Monthly | Quarterly | Annually")] = "Monthly",
        confirm: Annotated[bool, Field(description="Set true to apply after the admin approved the preview")] = False,
    ) -> dict[str, Any]:
        s, help_ = await _scope_or_help(scope)
        if help_:
            return help_
        emails = [e.strip() for e in contact_emails.split(",") if e.strip()]
        if not emails:
            return err("contact_emails is required")
        first = _today().replace(day=1).isoformat()
        body = {"properties": {"category": "Cost", "amount": float(amount), "timeGrain": time_grain,
                               "timePeriod": {"startDate": f"{first}T00:00:00Z"},
                               "notifications": {
                                   "Actual_GreaterThan_80_Percent": {"enabled": True, "operator": "GreaterThan", "threshold": 80, "contactEmails": emails, "thresholdType": "Actual"},
                                   "Forecasted_GreaterThan_100_Percent": {"enabled": True, "operator": "GreaterThan", "threshold": 100, "contactEmails": emails, "thresholdType": "Forecasted"}}}}
        return await runbook.call("arm", "PUT", f"{s}/providers/Microsoft.Consumption/budgets/{quote(name)}", body=body, api_version=ARM_API_BUDGETS, confirm=confirm)

    @mcp.tool(structured_output=False, annotations=READ, description=(
        "Active Azure cost alerts (budget breaches, anomalies, credit) for a scope, plus existing budgets with their current spend."))
    async def finops_cost_alerts(
        scope: Annotated[str, Field(description="Subscription id, /subscriptions/<id> or resource group scope (default DEFAULT_SUBSCRIPTION_ID)")] = "",
    ) -> dict[str, Any]:
        s, help_ = await _scope_or_help(scope)
        if help_:
            return help_
        alerts_r, budgets_r = await asyncio.gather(
            runbook.call("arm", "GET", f"{s}/providers/Microsoft.CostManagement/alerts", api_version=ARM_API_BUDGETS, max_chars=500_000),
            runbook.call("arm", "GET", f"{s}/providers/Microsoft.Consumption/budgets", api_version=ARM_API_BUDGETS, max_chars=500_000))
        alerts = [{"id": a.get("id"), "type": (a.get("properties") or {}).get("alertType"), "status": (a.get("properties") or {}).get("status"),
                   "description": (a.get("properties") or {}).get("description"), "created": (a.get("properties") or {}).get("creationTime")}
                  for a in ((alerts_r.get("body") or {}).get("value", []) if alerts_r.get("ok") else [])]
        budgets = []
        for b in ((budgets_r.get("body") or {}).get("value", []) if budgets_r.get("ok") else []):
            p = b.get("properties") or {}
            cur = (p.get("currentSpend") or {})
            budgets.append({"name": b.get("name"), "amount": p.get("amount"), "time_grain": p.get("timeGrain"), "current_spend": cur.get("amount"), "unit": cur.get("unit"),
                            "spent_pct": _pct(float(cur.get("amount") or 0), float(p.get("amount") or 0)), "forecast": (p.get("forecastSpend") or {}).get("amount")})
        return {"ok": True, "scope": s, "alerts": alerts, "alerts_error": None if alerts_r.get("ok") else alerts_r.get("message"),
                "budgets": budgets, "budgets_error": None if budgets_r.get("ok") else budgets_r.get("message")}

    @mcp.tool(structured_output=False, annotations=READ, description="List Azure subscriptions visible to the signed-in admin (id, name, state). Use the id as scope for the cost tools.")
    async def finops_subscriptions() -> dict[str, Any]:
        try:
            subs = await _subscriptions()
        except RunbookError as e:
            return e.result
        return {"ok": True, "count": len(subs), "subscriptions": subs, "default_subscription_id": settings.default_subscription_id}
