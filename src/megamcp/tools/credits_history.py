"""Copilot Credits spend per agent over any period up to 13 months (6 and 12 months are the usual asks).

Where the numbers come from (Power Platform API, entitlement MCSMessages = Copilot Credits):
  GET /licensing/entitlements/MCSMessages/users?fromDate&toDate
      one row per consuming user for the period: billed credits ("consumed"), non-billable credits, resource count
  GET /licensing/entitlements/MCSMessages/users/{userId}/resources?fromDate&toDate
      that user's rows per agent (resourceId = agent id, metadata.ResourceName = agent name) per feature, with environmentId

The tenant-wide per-agent operation (/licensing/entitlements/MCSMessages/resources) would be one call, but it answers 403
for Power Platform Administrators in delegated mode, so per-agent totals are built from the per-user rows. They reconcile:
each user's resource rows add up to that user's billed total. History goes back as far as the tenant has usage (tested
to 12 months). Because it fans out over every consuming user it runs as a background job; results are cached per caller
and period for six hours so regrouping (environment, creator, user, feature) is instant.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import time
from collections import defaultdict
from typing import Annotated, Any
from urllib.parse import quote

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..auth.entra import current_principal
from ..auth.tokens import broker
from ..config import settings
from ..runbook import budget
from ..runbook.invoke import runbook
from ..runbook.jobs import Job, jobs
from .common import READ, err
from .inventory import AGENT, GUID, ZERO_GUID, Query, kql_str, resolve_users, run_query

ENTITLEMENT = "MCSMessages"
BASE = f"/licensing/entitlements/{ENTITLEMENT}"
CACHE_SECONDS = 6 * 3600
GROUPS = ("agent", "environment", "creator", "user", "feature")
_cache: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}
_running: dict[tuple[str, str, str], str] = {}

NOTES = [
    "Billed credits are what counts against prepaid Copilot Credits or pay-as-you-go. Non-billable credits are usage that was not charged "
    "(for example covered by Microsoft 365 Copilot licences, or test and maker usage).",
    "Source: Power Platform licensing API (entitlement MCSMessages), the same data as Power Platform admin centre > Licensing > Copilot Studio.",
    "Agents without a creator are first-party agents (Dynamics 365, Microsoft 365) or agents that have since been deleted.",
]


# ---------------------------------------------------------------------------
# period
# ---------------------------------------------------------------------------
def _months_back(d: dt.date, months: int) -> dt.date:
    y, m = divmod(d.year * 12 + (d.month - 1) - months, 12)
    m += 1
    for day in (d.day, 30, 29, 28):
        try:
            return dt.date(y, m, day)
        except ValueError:
            continue
    return dt.date(y, m, 28)


def period(months: int, from_date: str, to_date: str, today: dt.date | None = None) -> tuple[str, str]:
    """(from, to) as YYYY-MM-DD. Explicit dates win; otherwise the last N months up to today (rolling)."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    if from_date.strip() or to_date.strip():
        try:
            f = dt.date.fromisoformat(from_date.strip()[:10]) if from_date.strip() else _months_back(today, 6)
            t = dt.date.fromisoformat(to_date.strip()[:10]) if to_date.strip() else today
        except ValueError as e:
            raise ValueError("from_date and to_date must be dates like 2026-05-01") from e
    else:
        n = int(months)
        if not 1 <= n <= 24:
            raise ValueError("months must be between 1 and 24")
        f, t = _months_back(today, n), today
    if f > t:
        raise ValueError("from_date is after to_date")
    if (t - f).days > 740:
        raise ValueError("The period can be at most about 24 months")
    return f.isoformat(), t.isoformat()


def month_windows(frm: str, to: str) -> list[tuple[str, str, str]]:
    """Calendar months covering the period: (label, from, to); the first and last may be partial."""
    f, t = dt.date.fromisoformat(frm), dt.date.fromisoformat(to)
    out, cur = [], f
    while cur <= t:
        nxt = _months_back(dt.date(cur.year, cur.month, 1), -1)
        end = min(t, nxt - dt.timedelta(days=1))
        out.append((cur.strftime("%Y-%m"), cur.isoformat(), end.isoformat()))
        cur = nxt
    return out


# ---------------------------------------------------------------------------
# aggregation (pure, unit tested)
# ---------------------------------------------------------------------------
def _num(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def new_aggregate() -> dict[str, Any]:
    return {"agents": {}, "features": defaultdict(lambda: [0.0, 0.0]), "users": {}, "failed_users": [], "users_billed_total": 0.0,
            "users_non_billable_total": 0.0}


def add_users(agg: dict[str, Any], users: list[dict[str, Any]]) -> None:
    for u in users:
        uid = str(u.get("userId") or "")
        md = u.get("metadata") or {}
        rec = agg["users"].setdefault(uid, {"billed": 0.0, "non_billable": 0.0, "agents": 0, "last": ""})
        rec["billed"] += _num(u.get("consumed"))
        rec["non_billable"] += _num(md.get("NonBillableQuantity"))
        rec["agents"] = max(rec["agents"], int(_num(md.get("Resources"))))
        rec["last"] = max(rec["last"], str(u.get("asOfDate") or ""))
        agg["users_billed_total"] += _num(u.get("consumed"))
        agg["users_non_billable_total"] += _num(md.get("NonBillableQuantity"))


def add_user_resources(agg: dict[str, Any], user_id: str, rows: list[dict[str, Any]]) -> None:
    for r in rows:
        rid = str(r.get("resourceId") or "(unknown)")
        env = str(r.get("environmentId") or "")
        md = r.get("metadata") or {}
        billed, free = _num(r.get("consumed")), _num(md.get("NonBillableQuantity"))
        # First-party agents use their name as resourceId ("Sales Opportunity Agent") and exist in many
        # environments, so an agent is keyed by environment and id.
        a = agg["agents"].setdefault(f"{env.lower()}|{rid}", {"resource_id": rid, "name": None, "environment_id": env or None, "billed": 0.0,
                                                             "non_billable": 0.0, "users": set(), "features": defaultdict(float), "last": ""})
        a["name"] = md.get("ResourceName") or a["name"]
        a["billed"] += billed
        a["non_billable"] += free
        a["users"].add(user_id)
        feat = md.get("FeatureName") or "(not specified)"
        a["features"][feat] += billed + free
        agg["features"][feat][0] += billed
        agg["features"][feat][1] += free
        a["last"] = max(a["last"], str(r.get("asOfDate") or ""))


def _r(x: float) -> float:
    return round(x, 2)


def render(agg: dict[str, Any], group_by: str, top: int, environment: str = "", name_contains: str = "",
           include_non_billable: bool = True) -> dict[str, Any]:
    """One view of a finished aggregate. Names for environments, creators and users come from agg['names']."""
    names = agg.get("names") or {}
    env_names: dict[str, str] = names.get("environments", {})
    creators: dict[str, dict[str, Any]] = names.get("agents", {})
    people: dict[str, dict[str, Any]] = names.get("users", {})
    env_f, name_f = environment.strip().lower(), name_contains.strip().lower()

    def env_label(eid: str | None) -> str:
        return env_names.get(str(eid or "").lower()) or (eid or "(unknown)")

    agents = []
    for k, a in agg["agents"].items():
        rid = a.get("resource_id") or k
        if env_f and env_f not in str(a.get("environment_id") or "").lower() and env_f not in env_label(a.get("environment_id")).lower():
            continue
        if name_f and name_f not in str(a.get("name") or rid).lower():
            continue
        if a["billed"] <= 0 and (a["non_billable"] <= 0 or not include_non_billable):
            continue
        agents.append((rid, a))
    total_billed = sum(a["billed"] for _, a in agents)
    total_free = sum(a["non_billable"] for _, a in agents)
    key = lambda x: (-x[1]["billed"], -x[1]["non_billable"])  # noqa: E731

    def person(uid: str) -> str:
        if uid == ZERO_GUID:
            return "(autonomous / no signed-in user)"
        p = people.get(uid.lower()) or {}
        return f"{p['name']} <{p['upn']}>" if p.get("name") and p.get("upn") else (p.get("name") or uid)

    rows: list[dict[str, Any]] = []
    if group_by == "agent":
        for rid, a in sorted(agents, key=key)[:top]:
            inv = creators.get(rid.lower()) or {}
            row = {"agent": a.get("name") or rid, "agent_id": rid, "environment": env_label(a.get("environment_id")),
                   "environment_id": a.get("environment_id"), "creator": inv.get("creator_name") or inv.get("created_by") or None,
                   "billed_credits": _r(a["billed"])}
            if include_non_billable:
                row["non_billable_credits"] = _r(a["non_billable"])
            row.update(users=len(a["users"]), top_features=[f for f, _ in sorted(a["features"].items(), key=lambda kv: -kv[1])[:3]],
                       last_activity=(a.get("last") or "")[:10] or None)
            if inv.get("created_in"):
                row["created_in"] = inv["created_in"]
            rows.append({k: v for k, v in row.items() if v not in (None, "", [])})
    elif group_by in ("environment", "creator"):
        groups: dict[str, dict[str, Any]] = {}
        for rid, a in agents:
            if group_by == "environment":
                k, label = str(a.get("environment_id") or "(unknown)"), env_label(a.get("environment_id"))
            else:
                inv = creators.get(rid.lower()) or {}
                k = inv.get("created_by") or "(first-party or deleted agent)"
                label = inv.get("creator_name") or k
            g = groups.setdefault(k, {group_by: label, "billed_credits": 0.0, "non_billable_credits": 0.0, "agents_billed": 0, "agents_any_usage": 0, "_top": []})
            g["billed_credits"] += a["billed"]
            g["non_billable_credits"] += a["non_billable"]
            g["agents_billed"] += 1 if a["billed"] > 0 else 0
            g["agents_any_usage"] += 1
            g["_top"].append((a["billed"], a.get("name") or rid))
        for k, g in sorted(groups.items(), key=lambda kv: (-kv[1]["billed_credits"], -kv[1]["non_billable_credits"]))[:top]:
            g["top_agents"] = [n for _, n in sorted(g.pop("_top"), key=lambda x: -x[0])[:3]]
            g["billed_credits"], g["non_billable_credits"] = _r(g["billed_credits"]), _r(g["non_billable_credits"])
            if group_by == "environment":
                g["environment_id"] = k
            if not include_non_billable:
                g.pop("non_billable_credits")
                g.pop("agents_any_usage")
            rows.append(g)
    elif group_by == "user":
        for uid, u in sorted(agg["users"].items(), key=lambda kv: (-kv[1]["billed"], -kv[1]["non_billable"]))[:top]:
            row = {"user": person(uid), "user_id": uid, "billed_credits": _r(u["billed"])}
            if include_non_billable:
                row["non_billable_credits"] = _r(u["non_billable"])
            row.update(agents_used=u["agents"], last_activity=(u.get("last") or "")[:10] or None)
            rows.append(row)
    elif group_by == "feature":
        for f, (b, n) in sorted(agg["features"].items(), key=lambda kv: (-kv[1][0], -kv[1][1]))[:top]:
            row = {"feature": f, "billed_credits": _r(b)}
            if include_non_billable:
                row["non_billable_credits"] = _r(n)
            rows.append(row)

    attributed = sum(a["billed"] for a in agg["agents"].values())
    out: dict[str, Any] = {
        "totals": {"billed_credits": _r(total_billed), **({"non_billable_credits": _r(total_free)} if include_non_billable else {}),
                   "agents_with_billed_usage": sum(1 for _, a in agents if a["billed"] > 0),
                   **({"agents_with_any_usage": len(agents)} if include_non_billable else {}),
                   "users_with_usage": sum(1 for u in agg["users"].values() if u["billed"] > 0 or u["non_billable"] > 0)},
        "group_by": group_by, "rows": rows, "rows_shown": len(rows),
    }
    if env_f or name_f:
        out["filters"] = {k: v for k, v in {"environment": environment, "name_contains": name_contains}.items() if v}
    ub = agg.get("users_billed_total", 0.0)
    if ub:
        out["coverage"] = {"tenant_billed_credits": _r(ub), "attributed_to_agents": _r(attributed),
                           "attributed_percent": round(100 * attributed / ub, 1)}
        if agg.get("failed_users"):
            out["coverage"]["not_broken_down"] = [{"user_id": f["user_id"], "billed_credits": _r(f.get("billed", 0.0)), "reason": f.get("reason")}
                                                  for f in agg["failed_users"][:10]]
    return out


# ---------------------------------------------------------------------------
# fetching
# ---------------------------------------------------------------------------
async def _paged(path: str, key: str, frm: str, to: str, principal: Any, max_pages: int = 50) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    rows: list[dict[str, Any]] = []
    token = ""
    for _ in range(max_pages):
        q: dict[str, Any] = {"fromDate": frm, "toDate": to, "pageSize": 1000}
        if token:
            q["continuationToken"] = token
        r = await runbook.call("powerplatform", "GET", path, query=q, api_version=settings.powerplatform_api_version,
                               principal=principal, max_chars=20_000_000, retry_wait_budget=60)
        if not r.get("ok"):
            return rows, r
        body = r.get("body") or {}
        for v in body.get("value", []) if isinstance(body, dict) else []:
            rows += v.get(key, []) if isinstance(v, dict) else []
        token = (body.get("continuationToken") or body.get("continuationtoken") or "") if isinstance(body, dict) else ""
        if not token:
            break
    return rows, None


async def _names(agg: dict[str, Any], principal: Any) -> dict[str, Any]:
    """Creator and environment names from the inventory, people from Graph. Best effort."""
    ranked = sorted(agg["agents"].items(), key=lambda kv: -(kv[1]["billed"] * 1000 + kv[1]["non_billable"]))
    ids = list(dict.fromkeys(a["resource_id"].lower() for _, a in ranked if GUID.match(a["resource_id"]) and (a["billed"] > 0 or a["non_billable"] > 0)))[:1500]
    agents: dict[str, dict[str, Any]] = {}
    envs: dict[str, str] = {}
    notes: list[str] = []
    for i in range(0, len(ids), 300):
        chunk = ", ".join(kql_str(x) for x in ids[i:i + 300])
        q = (Query().where(f"type == '{AGENT}'").where(f"tolower(name) in ({chunk})").join_environments()
             .project("agent_id = tolower(name)", "created_by = tostring(properties.createdBy)", "created_in = tostring(properties.createdIn)",
                      "environment_id = tolower(tostring(properties.environmentId))", "environment = environmentName"))
        q.kql_only = True
        r = await run_query(q, top=1000)
        if not r.get("ok"):
            notes.append("Creator names unavailable: the inventory query failed (needs Power Platform Administrator, Global Reader or AI Administrator).")
            break
        for row in r.get("rows", []):
            agents[str(row.get("agent_id"))] = row
            if row.get("environment_id") and row.get("environment"):
                envs[str(row["environment_id"])] = row["environment"]
    env_ids = sorted({str(a.get("environment_id") or "").lower() for a in agg["agents"].values() if a.get("environment_id")} - set(envs))
    for i in range(0, len(env_ids), 300):
        chunk = ", ".join(kql_str(x) for x in env_ids[i:i + 300])
        q = Query().where("type == 'microsoft.powerplatform/environments'").where(f"tolower(name) in ({chunk})").project(
            "environment_id = tolower(name)", "environment = tostring(properties.displayName)")
        q.kql_only = True
        r = await run_query(q, top=1000)
        if r.get("ok"):
            envs.update({str(x.get("environment_id")): x.get("environment") for x in r.get("rows", []) if x.get("environment")})
    creator_ids = {a.get("created_by") for a in agents.values() if a.get("created_by")}
    top_users = [uid for uid, _ in sorted(agg["users"].items(), key=lambda kv: -(kv[1]["billed"] * 1000 + kv[1]["non_billable"]))[:200]]
    people, perr = await resolve_users(set(creator_ids) | set(top_users))
    if perr:
        notes.append(f"Some names could not be resolved from Microsoft Graph: {perr[:120]}")
    for a in agents.values():
        p = people.get(str(a.get("created_by") or "").lower()) or {}
        if p.get("name"):
            a["creator_name"] = f"{p['name']} <{p['upn']}>" if p.get("upn") else p["name"]
        elif a.get("created_by") == ZERO_GUID:
            a["creator_name"] = "(system)"
    return {"agents": agents, "environments": envs, "users": people, "notes": notes}


def _token_problem(prob: dict[str, Any]) -> bool:
    text = " ".join(str(prob.get(k) or "") for k in ("error", "message", "aad_error", "description"))
    return bool(prob.get("user_token_expired")) or "token_acquisition_failed" in text or "On-behalf-of" in text


async def _wait_for_fresh_token(principal: Any, seconds: float = 240.0) -> bool:
    """Wait until a request from the same admin has brought a user token valid for at least two more minutes."""
    deadline = time.time() + seconds
    while time.time() < deadline:
        if broker.assertion_valid_for(principal) > 120:
            return True
        await asyncio.sleep(5)
    return broker.assertion_valid_for(principal) > 0


def _cache_get(key: tuple[str, str, str]) -> dict[str, Any] | None:
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    return None


def register(mcp: MCPServer) -> None:
    @mcp.tool(structured_output=False, annotations=READ, description=(
        "Copilot Credits spend over a period of up to 24 months (for example the last 6 or 12 months) per agent, with agent name, creator, "
        "environment, users, top features, billed and non-billable credits, plus a month-by-month tenant trend. group_by agent | environment | "
        "creator | user | feature. Use this for any spend or consumption question longer than the current month (finops_agent_limits is month to "
        "date only). The first run for a period scans every consuming user in the background and can take several minutes: if it returns a job_id, "
        "call job_status with it. Later calls for the same period (any group_by or filter) answer instantly from a six-hour cache."))
    async def finops_agent_credits_history(
        months: Annotated[int, Field(description="Last N months up to today (1-24), e.g. 6 or 12. Ignored when from_date or to_date is set")] = 6,
        from_date: Annotated[str, Field(description="Optional start date YYYY-MM-DD")] = "",
        to_date: Annotated[str, Field(description="Optional end date YYYY-MM-DD (default today)")] = "",
        group_by: Annotated[str, Field(description="agent | environment | creator | user | feature")] = "agent",
        top: Annotated[int, Field(description="Rows to return, highest billed credits first (1-200)")] = 25,
        environment: Annotated[str, Field(description="Only agents in environments whose name or id contains this text")] = "",
        name_contains: Annotated[str, Field(description="Only agents whose name contains this text")] = "",
        include_non_billable: Annotated[bool, Field(description="Also show non-billable credits (usage not charged)")] = True,
        include_monthly_trend: Annotated[bool, Field(description="Add tenant-wide billed credits per calendar month")] = True,
        refresh: Annotated[bool, Field(description="Ignore the six-hour cache and scan again")] = False,
        wait_seconds: Annotated[int, Field(description="How long to wait for a new scan before returning a job_id (0-80)")] = 75,
    ) -> dict[str, Any]:
        g = (group_by or "agent").strip().lower()
        if g not in GROUPS:
            return err(f"group_by must be one of {', '.join(GROUPS)}")
        try:
            frm, to = period(months, from_date, to_date)
        except ValueError as e:
            return err(str(e))
        top_n = max(1, min(int(top), 200))
        principal = current_principal()
        key = (principal.cache_key, frm, to)

        def view(agg: dict[str, Any]) -> dict[str, Any]:
            out = {"ok": True, "period": {"from": frm, "to": to, "days": (dt.date.fromisoformat(to) - dt.date.fromisoformat(frm)).days + 1},
                   **render(agg, g, top_n, environment, name_contains, include_non_billable)}
            if include_monthly_trend and agg.get("monthly"):
                out["monthly_trend"] = agg["monthly"]
            out["scanned_at"] = agg.get("scanned_at")
            out["notes"] = NOTES + (agg.get("names") or {}).get("notes", []) + agg.get("notes", [])
            out["next_steps"] = [f"Same period, other views (instant): group_by environment | creator | user | feature, or filter with environment / name_contains."]
            return out

        if not refresh:
            cached = _cache_get(key)
            if cached:
                return {**view(cached), "from_cache": True}
        running = _running.get(key)
        job = jobs.get(running, principal.cache_key) if running else None

        if job is None or job.done:
            async def work(job: Job) -> dict[str, Any]:
                try:
                    return await scan(job)
                finally:
                    _running.pop(key, None)

            async def scan(job: Job) -> dict[str, Any]:
                started = time.time()
                agg = new_aggregate()
                users, problem = await _paged(f"{BASE}/users", "users", frm, to, principal)
                if problem:
                    return problem  # the access layer explains a refusal
                add_users(agg, users)
                active = [u for u in users if _num(u.get("consumed")) > 0 or _num((u.get("metadata") or {}).get("NonBillableQuantity")) > 0]
                job.progress["total"] = len(active)
                sem = asyncio.Semaphore(max(4, min(16, settings.fanout_concurrency + 4)))

                async def one(u: dict[str, Any]) -> None:
                    uid = str(u.get("userId") or "")
                    if job.cancel_requested:
                        return
                    rem = budget.remaining()
                    if rem is not None and rem < 20:
                        agg["failed_users"].append({"user_id": uid, "billed": _num(u.get("consumed")), "reason": "job time budget ran out"})
                        return
                    path = f"{BASE}/users/{quote(uid, safe='')}/resources"
                    async with sem:
                        rows, prob = await _paged(path, "resources", frm, to, principal)
                    if prob and _token_problem(prob):
                        # The admin's token from Copilot Studio expired mid-scan. Their next request (job_status) brings a
                        # fresh one; wait for it rather than losing this user's breakdown.
                        agg["token_waits"] = agg.get("token_waits", 0) + 1
                        if await _wait_for_fresh_token(principal):
                            async with sem:
                                rows, prob = await _paged(path, "resources", frm, to, principal)
                    job.progress["done"] = job.progress.get("done", 0) + 1
                    if prob:
                        job.progress["failed"] = job.progress.get("failed", 0) + 1
                        agg["failed_users"].append({"user_id": uid, "billed": _num(u.get("consumed")),
                                                    "reason": f"{prob.get('status')} {str(prob.get('message') or prob.get('error') or '')[:80]}".strip()})
                        return
                    add_user_resources(agg, uid, rows)

                async def trend() -> None:
                    sem_m = asyncio.Semaphore(4)

                    async def month(label: str, f: str, t: str) -> dict[str, Any]:
                        async with sem_m:
                            rows, prob = await _paged(f"{BASE}/users", "users", f, t, principal)
                        if prob:
                            return {"month": label, "error": prob.get("status")}
                        return {"month": label, "from": f, "to": t, "billed_credits": _r(sum(_num(u.get("consumed")) for u in rows)),
                                "non_billable_credits": _r(sum(_num((u.get("metadata") or {}).get("NonBillableQuantity")) for u in rows)),
                                "active_users": len(rows)}
                    agg["monthly"] = list(await asyncio.gather(*(month(*w) for w in month_windows(frm, to))))

                # The trend is always collected (cheap next to the fan-out) so any later call for this period can show it from the cache.
                await asyncio.gather(trend(), *(one(u) for u in active))
                if broker.assertion_valid_for(principal) < 120:
                    await _wait_for_fresh_token(principal)
                agg["names"] = await _names(agg, principal)
                agg["scanned_at"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                agg["notes"] = [f"Scanned {len(active)} consuming users in {round(time.time() - started)} s."]
                if agg.get("token_waits"):
                    agg["notes"].append("Your sign-in token from Copilot Studio expired during the scan; the job waited for a fresh one and carried on.")
                transient = any(any(s in str(f.get("reason")) for s in ("budget", "On-behalf-of", "expired", "token")) for f in agg["failed_users"])
                if not job.cancel_requested and not transient:
                    _cache[key] = (time.time(), agg)
                elif transient:
                    agg["notes"].append("Some users could not be broken down because of a sign-in or time problem, so this result is not cached; "
                                        "run it again with refresh=true for a complete answer.")
                return view(agg)

            try:
                job = jobs.start(principal, "credits_history", f"Copilot Credits per agent {frm} to {to}", work)
            except RuntimeError as e:
                return err(str(e))
            _running[key] = job.id
        rem = budget.remaining()
        wait = max(0.0, min(float(wait_seconds), 80.0, (rem - 12.0) if rem is not None else 80.0))
        finished = await jobs.wait(job, wait)
        if finished and job.result:
            cached = _cache_get(key)  # render for THIS call's view; the job may have been started by another call
            if cached and job.result.get("ok"):
                return {**view(cached), "job_id": job.id}
            return {**job.result, "job_id": job.id}
        out = job.summary()
        out["next_step"] = (f"Scanning every user's Copilot Credit usage for {frm} to {to} in the background (it can take a few minutes). "
                            f"Call job_status with job_id={job.id} in about a minute; then call this tool again with the same months for other views.")
        return out
