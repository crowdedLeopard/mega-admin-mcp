"""Run one Dataverse query in every environment, as a background job.

The inventory answers questions about agents, apps and flows. Anything that
lives only inside Dataverse (classic bots, conversation transcripts, security
roles, solutions, table row counts) needs a call per environment. With hundreds
of environments that does not fit in Copilot Studio's 120 second tool limit, so
the work runs as a job: the tool waits up to `wait_seconds`, returns the answer
if it finished, otherwise a job id for `job_status`.

Reads only: the method is always GET.
"""
from __future__ import annotations

import asyncio
import time
from typing import Annotated, Any

from mcp.server.mcpserver import MCPServer
from pydantic import Field

from ..auth.entra import current_principal
from ..config import settings
from ..runbook import budget
from ..runbook.invoke import RunbookError, runbook
from ..runbook.jobs import STARTED, Job, jobs
from .common import READ, err, parse_json_arg

ENV_TYPES = {"production", "sandbox", "developer", "default", "trial", "teams", "subscriptionbasedtrial", "notspecified"}


def _classify(r: dict[str, Any]) -> str:
    status = r.get("status")
    text = f"{r.get('message', '')} {r.get('error', '')} {r.get('aad_error', '')}".lower()
    if status == 0 and ("token" in text or r.get("error") == "token_acquisition_failed"):
        return "token_failed"
    if status in (401, 403):
        return "no_access" if "not a member" not in text else "not_a_member"
    if status == 404:
        return "path_not_found"
    if status == 400:
        return "bad_query"
    if status == 429:
        return "throttled"
    if status == 0 and r.get("error") == "circuit_open":
        return "circuit_open"
    if status == 0:
        return "network_or_timeout"
    return f"http_{status}"


FAILURE_HINTS = {
    "not_a_member": "You are not a user in these environments. Power Platform admins can add themselves (admin centre > environment > Users) or use the inventory tools instead.",
    "no_access": "Your security role in these environments cannot read this table.",
    "path_not_found": "The entity set does not exist in these environments (feature not installed). Check the name with dataverse_describe_table.",
    "bad_query": "Dataverse rejected the OData options. Check $select / $filter column names with dataverse_describe_table.",
    "token_failed": "Could not get a Dataverse token for these environments. Run doctor with one of their URLs.",
    "throttled": "Dataverse service protection limits; run again later or lower FANOUT_CONCURRENCY.",
}


def _trim_result(result: dict[str, Any], max_items: int) -> dict[str, Any]:
    out = dict(result)
    per_env = out.get("by_environment") or []
    if len(per_env) > max_items:
        out["by_environment"] = per_env[:max_items]
        out["by_environment_note"] = f"Showing {max_items} of {len(per_env)} environments with results (highest count first). Raise max_items to see more."
    rows_budget = 30_000
    used = 0
    for e in out.get("by_environment") or []:
        rows = e.get("rows")
        if not rows:
            continue
        kept = []
        for r in rows:
            size = len(str(r))
            if used + size > rows_budget:
                break
            kept.append(r)
            used += size
        if len(kept) < len(rows):
            e["rows"] = kept
            e["rows_trimmed"] = True
    return out


def register(mcp: MCPServer) -> None:
    @mcp.tool(structured_output=False, annotations=READ, description=(
        "Run ONE read-only Dataverse query in EVERY environment (or a filtered set) as a background job, e.g. count classic bots (/bots), "
        "conversation transcripts, solutions or system users per environment. Returns the answer if it finishes within wait_seconds, otherwise a job_id "
        "to poll with job_status. For agents, apps and flows prefer pp_agents_tenant / pp_inventory, which answer in one inventory query."))
    async def pp_query_all_environments(
        dataverse_path: Annotated[str, Field(description="Entity set path, e.g. /bots, /conversationtranscripts, /solutions, /systemusers")],
        query_json: Annotated[str, Field(description="OData options as a JSON string, e.g. {\"$select\": \"name,createdon\", \"$filter\": \"createdon ge 2026-10-22\"}")] = "",
        count_only: Annotated[bool, Field(description="Return only the number of matching rows per environment (counts above 5000 show as 5000+)")] = False,
        environment_filter: Annotated[str, Field(description="Only environments whose name contains this text")] = "",
        environment_type: Annotated[str, Field(description="any | Production | Sandbox | Developer | Default | Trial | Teams")] = "any",
        max_environments: Annotated[int, Field(description="Safety cap on environments to query (1-2000)")] = 1000,
        max_rows_per_environment: Annotated[int, Field(description="Rows to keep per environment when count_only is false (1-200)")] = 20,
        wait_seconds: Annotated[int, Field(description="How long to wait for the job before returning a job_id (0-80)")] = 70,
    ) -> dict[str, Any]:
        path = (dataverse_path or "").strip()
        if not path or path.lower().startswith(("http://", "https://")):
            return err("dataverse_path must be an entity set path such as /bots")
        if not path.startswith("/"):
            path = "/" + path
        try:
            q = parse_json_arg(query_json, "query_json") or {}
        except ValueError as e:
            return err(str(e))
        if not isinstance(q, dict):
            return err("query_json must be a JSON object of OData options")
        etype = (environment_type or "any").strip().lower()
        if etype not in ("", "any") and etype not in ENV_TYPES:
            return err(f"environment_type must be any or one of {', '.join(sorted(ENV_TYPES))}")
        try:
            envs = await runbook.paged("powerplatform", "/environmentmanagement/environments", api_version=settings.powerplatform_api_version, max_pages=40)
        except RunbookError as e:
            return e.result
        matching = [e for e in envs if isinstance(e, dict)]
        if environment_filter.strip():
            matching = [e for e in matching if environment_filter.strip().lower() in (e.get("displayName") or "").lower()]
        if etype not in ("", "any"):
            matching = [e for e in matching if str(e.get("type") or "").lower() == etype]
        targets = [e for e in matching if e.get("url")]
        no_dataverse = len(matching) - len(targets)  # matching environments that have no Dataverse database
        cap = max(1, min(int(max_environments), 2000))
        capped = max(0, len(targets) - cap)
        targets = targets[:cap]
        if not targets:
            return err("No environments with Dataverse matched the filters", environments_without_dataverse=no_dataverse)

        query = dict(q)
        headers: dict[str, str] = {}
        if count_only:
            query["$count"] = "true"
            headers["Prefer"] = "odata.maxpagesize=1"
        else:
            query.setdefault("$top", max(1, min(int(max_rows_per_environment), 200)))
        principal = current_principal()
        title = f"GET {path} in {len(targets)} environments" + (" (count)" if count_only else "")

        async def work(job: Job) -> dict[str, Any]:
            sem = asyncio.Semaphore(max(1, settings.fanout_concurrency))
            per_env: list[dict[str, Any]] = []
            failures: dict[str, list[dict[str, Any]]] = {}
            started = time.time()

            async def one(env: dict[str, Any]) -> None:
                if job.cancel_requested:
                    return
                rem = budget.remaining()
                if rem is not None and rem < 10:
                    failures.setdefault("skipped_time_budget", []).append({"environment": env.get("displayName")})
                    return
                async with sem:
                    r = await runbook.call("dataverse", "GET", path, query=query, headers=headers or None, dataverse_org_url=env["url"],
                                           principal=principal, max_chars=400_000, retry_wait_budget=20)
                job.progress["done"] = job.progress.get("done", 0) + 1
                base = {"environment": env.get("displayName"), "environment_id": env.get("id"), "type": env.get("type"), "url": env.get("url")}
                if r.get("ok"):
                    body = r.get("body") or {}
                    entry = dict(base)
                    if count_only:
                        c = body.get("@odata.count")
                        entry["count"] = c if c is not None else len(body.get("value", []))
                        if isinstance(c, int) and c >= 5000:
                            entry["count_note"] = "5000+ (Dataverse caps $count at 5000)"
                    else:
                        rows = [{k: v for k, v in row.items() if not k.startswith("@odata.etag")} for row in body.get("value", [])]
                        entry["count"] = len(rows)
                        entry["more_available"] = bool(r.get("next_link"))
                        entry["rows"] = rows
                    per_env.append(entry)
                else:
                    job.progress["failed"] = job.progress.get("failed", 0) + 1
                    failures.setdefault(_classify(r), []).append({**base, "status": r.get("status"), "message": str(r.get("message") or r.get("error") or "")[:200]})

            await asyncio.gather(*(one(e) for e in targets))
            per_env.sort(key=lambda e: -(e.get("count") or 0))
            total = sum(int(e.get("count") or 0) for e in per_env)
            nonzero = sum(1 for e in per_env if e.get("count"))
            return {
                "ok": True, "path": path, "query": query, "count_only": count_only,
                "environments_targeted": len(targets), "environments_succeeded": len(per_env), "environments_with_rows": nonzero,
                "environments_failed": sum(len(v) for v in failures.values()), "environments_without_dataverse": no_dataverse,
                "environments_not_scanned_cap": capped,
                ("total_count" if count_only else "rows_collected"): total,
                "by_environment": [e for e in per_env if e.get("count")],
                "failures": {k: {"count": len(v), "hint": FAILURE_HINTS.get(k, ""), "examples": v[:8]} for k, v in failures.items()},
                "elapsed_seconds": round(time.time() - started, 1),
                "notes": ["Environments where the query returned nothing are counted in environments_succeeded but not listed.",
                          "Counts in delegated mode reflect what your own security role can read in each environment."],
            }

        try:
            job = jobs.start(principal, "dataverse_fanout", title, work, total=len(targets))
        except RuntimeError as e:
            return err(str(e))
        rem = budget.remaining()
        wait = max(0.0, min(float(wait_seconds), 80.0, (rem - 12.0) if rem is not None else 80.0))
        finished = await jobs.wait(job, wait)
        out: dict[str, Any] = job.summary()
        if finished and job.result:
            out["result"] = _trim_result(job.result, 100)
        elif not finished:
            out["next_step"] = f"Still running in the background. Call job_status with job_id={job.id} in about a minute; it returns the result when done."
        return out

    @mcp.tool(structured_output=False, annotations=READ, description=(
        "Status and results of background jobs started by tools such as pp_query_all_environments. With no job_id, lists your recent jobs. "
        "Waits briefly for a running job before answering. Set cancel=true to stop a running job."))
    async def job_status(
        job_id: Annotated[str, Field(description="Job id returned by the tool that started the job; empty lists your jobs")] = "",
        cancel: Annotated[bool, Field(description="Cancel the job")] = False,
        max_items: Annotated[int, Field(description="Maximum environments to include in the result (10-1000)")] = 100,
        wait_seconds: Annotated[int, Field(description="Wait up to this long for a running job to finish (0-60)")] = 30,
    ) -> dict[str, Any]:
        p = current_principal()
        if not job_id.strip():
            mine = jobs.list_for(p.cache_key)
            return {"ok": True, "count": len(mine), "jobs": [j.summary() for j in mine[:20]]}
        job = jobs.get(job_id, p.cache_key)
        if job is None:
            return err(f"Unknown job {job_id}. Jobs are kept in memory for {settings.job_retention_hours} hours on the server instance that ran them; "
                       "a restart or a scaled-out instance will not know it. Start the job again.",
                       server_restarted_minutes_ago=round((time.time() - STARTED) / 60), next_step="Run the original request again. Do not tell the admin it failed for good.")
        if cancel and not job.done:
            jobs.cancel(job)
            await jobs.wait(job, 5)
        elif not job.done:
            rem = budget.remaining()
            await jobs.wait(job, max(0.0, min(float(wait_seconds), 60.0, (rem - 10.0) if rem is not None else 60.0)))
        out = job.summary()
        if job.done and job.result:
            out["result"] = _trim_result(job.result, max(10, min(int(max_items), 1000)))
        elif not job.done:
            out["next_step"] = "Still running. Call job_status again in a minute."
        return out
