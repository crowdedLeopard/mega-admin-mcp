"""The universal REST executor behind every tool.

One code path handles authentication, versioning, safety policy, retries,
throttling, circuit breaking, self-healing, response shaping and auditing, so
the curated FinOps tools and the generic `invoke` tool behave identically.
"""
from __future__ import annotations

import asyncio
import csv
import gzip
import io
import json
import logging
import random
import re
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

from ..auth.entra import Principal, current_principal
from ..auth.tokens import TokenError, broker
from ..catalog.services import Service, resolve_service, scope_for, service_for_url
from ..catalog.store import catalog
from ..config import settings
from . import healing
from .policy import audit, policy

log = logging.getLogger("megamcp.runbook")

USER_AGENT = "mega-admin-mcp/1.0 (+https://github.com/crowdedLeopard/mega-admin-mcp)"
REDACT_HEADERS = {"authorization", "x-api-key", "ocp-apim-subscription-key", "api-key"}


class RunbookError(Exception):
    def __init__(self, result: dict[str, Any]):
        super().__init__(result.get("message") or f"HTTP {result.get('status')}")
        self.result = result


class _Breaker:
    def __init__(self) -> None:
        self.failures = 0
        self.open_until = 0.0

    def allow(self) -> bool:
        return time.time() >= self.open_until

    def ok(self) -> None:
        self.failures = 0
        self.open_until = 0.0

    def fail(self) -> None:
        self.failures += 1
        if self.failures >= 5:
            self.open_until = time.time() + 30
            self.failures = 0


class Runbook:
    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._breakers: dict[str, _Breaker] = {}

    async def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(settings.request_timeout_seconds, connect=20),
                follow_redirects=True,
                max_redirects=5,
                headers={"User-Agent": USER_AGENT},
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def breaker_status(self) -> dict[str, Any]:
        now = time.time()
        return {h: {"open_for_seconds": round(b.open_until - now, 1)} for h, b in self._breakers.items() if b.open_until > now}

    # ------------------------------------------------------------------ URL
    def build_url(self, service: Service, path: str, query: dict[str, Any] | None, api_version: str, dataverse_org_url: str) -> tuple[str, str]:
        """Return (url, version_used)."""
        query = dict(query or {})
        version_used = ""
        if path.lower().startswith(("http://", "https://")):
            url = path
        else:
            if not path.startswith("/"):
                path = "/" + path
            if service.name == "dataverse":
                base = (dataverse_org_url or settings.default_dataverse_url).rstrip("/")
                if not base:
                    raise RunbookError({"status": 0, "error": "missing_dataverse_org_url",
                                        "message": "Dataverse calls need dataverse_org_url (https://<org>.crm.dynamics.com). Use pp_environments to find it."})
                if not path.startswith("/api/"):
                    path = "/api/data/v9.2" + path
                url = base + path
            elif service.name == "graph":
                if not re.match(r"^/(v1\.0|beta)(/|$)", path):
                    path = "/v1.0" + path
                version_used = path.split("/")[1]
                url = service.base_url + path
            else:
                url = service.base_url + path

        parsed = urlparse(url)
        existing = dict(parse_qsl(parsed.query, keep_blank_values=True))
        for k, v in query.items():
            if v is None or v == "":
                continue
            existing[k] = v if isinstance(v, str) else json.dumps(v) if isinstance(v, (dict, list)) else str(v).lower() if isinstance(v, bool) else str(v)

        if service.versioning == "query":
            param = service.api_version_param
            if api_version:
                existing[param] = api_version
            elif param not in existing:
                if service.name == "arm":
                    av = catalog.latest_api_version("GET", parsed.path) if settings.arm_auto_api_version else ""
                    if not av:
                        raise RunbookError({"status": 0, "error": "missing_api_version",
                                            "message": "Azure calls need api_version. The catalog has no match for this path; pass api_version explicitly (describe_operation shows it)."})
                    existing[param] = av
                elif service.name == "powerplatform":
                    existing[param] = settings.powerplatform_api_version
                else:
                    existing[param] = service.default_version
            version_used = existing.get(param, version_used)

        new_query = urlencode(existing, safe="$',():/*", quote_via=_quote)
        url = urlunparse(parsed._replace(query=new_query))
        return url, version_used

    # ----------------------------------------------------------------- call
    async def call(
        self,
        service: str,
        method: str,
        path: str,
        query: dict[str, Any] | None = None,
        body: Any = None,
        headers: dict[str, str] | None = None,
        api_version: str = "",
        confirm: bool = False,
        dataverse_org_url: str = "",
        principal: Principal | None = None,
        raise_on_error: bool = False,
        max_chars: int | None = None,
    ) -> dict[str, Any]:
        principal = principal or current_principal()
        method = (method or "GET").upper()
        svc: Service
        if path.lower().startswith(("http://", "https://")):
            by_host = service_for_url(path)
            if by_host is None:
                res = {"status": 0, "error": "unknown_host", "message": f"The host in {path} is not a Microsoft API this runbook knows. Known services: {', '.join(sorted(k for k in __import__('megamcp.catalog.services', fromlist=['SERVICES']).SERVICES))}."}
                return self._fail(res, raise_on_error)
            svc = by_host
            if svc.name == "dataverse":
                p = urlparse(path)
                dataverse_org_url = f"{p.scheme}://{p.hostname}"
        else:
            svc = resolve_service(service)

        try:
            url, version_used = self.build_url(svc, path, query, api_version, dataverse_org_url)
        except RunbookError as e:
            return self._fail(e.result, raise_on_error)

        decision = policy.evaluate(method, url, confirm)
        if not decision.allowed:
            preview = {
                "status": "preview" if decision.get("needs_confirm") else "blocked",
                "allowed": False,
                "needs_confirm": bool(decision.get("needs_confirm")),
                "reason": decision.get("reason"),
                "request": {"service": svc.name, "method": method, "url": url, "headers": self._redact(headers or {}), "body": body},
            }
            if decision.get("needs_confirm"):
                preview["next_step"] = "Show this preview to the admin. If they approve, call the same tool again with confirm=true."
            audit.record({"user": principal.upn or principal.name, "method": method, "url": url, "status": preview["status"], "write": True})
            return preview

        req_headers = {"Accept": "application/json"}
        if svc.name == "dataverse":
            req_headers.update({"OData-MaxVersion": "4.0", "OData-Version": "4.0", "Prefer": 'odata.include-annotations="OData.Community.Display.V1.FormattedValue"'})
        if svc.name == "graph" and ("$count" in url or "$search" in url or "$orderby" in url and "$filter" in url):
            req_headers["ConsistencyLevel"] = "eventual"
        if headers:
            req_headers.update({k: v for k, v in headers.items() if k.lower() not in ("authorization", "host", "content-length")})
        content = None
        if body is not None and method in ("POST", "PUT", "PATCH", "DELETE"):
            if isinstance(body, (dict, list)):
                content = json.dumps(body).encode("utf-8")
                req_headers.setdefault("Content-Type", "application/json")
            elif isinstance(body, str):
                content = body.encode("utf-8")
                req_headers.setdefault("Content-Type", "application/json")
            else:
                content = body

        scope = scope_for(svc, url) if svc.scope or svc.name == "dataverse" else None
        host = urlparse(url).hostname or svc.name
        breaker = self._breakers.setdefault(host, _Breaker())
        if not breaker.allow():
            res = {"status": 0, "error": "circuit_open", "message": f"{host} failed repeatedly; the circuit breaker is open for {round(breaker.open_until - time.time())}s. Try again shortly.", "url": url}
            return self._fail(res, raise_on_error)

        client = await self.client()
        healed: list[str] = []
        warnings: list[str] = []
        attempt = 0
        refreshed = False
        api_version_healed = False
        beta_healed = False
        started = time.time()
        resp: httpx.Response | None = None
        error: dict[str, Any] | None = None

        while True:
            try:
                if scope:
                    token = await broker.get_token(scope, principal, force_refresh=refreshed and attempt == 0)
                    req_headers["Authorization"] = f"Bearer {token}"
            except TokenError as e:
                error = {"status": 0, **e.to_dict(), "url": url}
                break
            try:
                resp = await client.request(method, url, headers=req_headers, content=content)
                breaker.ok()
            except httpx.HTTPError as e:
                breaker.fail()
                if attempt < 2:
                    attempt += 1
                    wait = 1.5 * attempt + random.random()
                    healed.append(f"network error {type(e).__name__}; retried after {wait:.1f}s")
                    await asyncio.sleep(wait)
                    continue
                error = {"status": 0, "error": "network_error", "message": f"{type(e).__name__}: {str(e)[:300]}", "url": url}
                break

            status = resp.status_code
            if status == 401 and not refreshed and scope:
                refreshed = True
                broker.invalidate(scope, principal)
                healed.append("401 from service; token refreshed and request retried")
                continue
            if status in (429, 503, 504) and attempt < settings.max_retries:
                attempt += 1
                wait = _retry_after(resp) or min(2 ** attempt + random.random(), 30)
                healed.append(f"HTTP {status}; waited {wait:.1f}s and retried (attempt {attempt})")
                await asyncio.sleep(wait)
                continue
            if 500 <= status < 600 and status not in (503, 504) and attempt < 2:
                attempt += 1
                await asyncio.sleep(1 + attempt)
                healed.append(f"HTTP {status}; retried (attempt {attempt})")
                continue
            if status in (400, 404) and svc.name == "arm" and settings.arm_auto_api_version and not api_version_healed:
                versions = healing.arm_supported_versions(_safe_json(resp))
                current = dict(parse_qsl(urlparse(url).query)).get("api-version", "")
                if versions and versions[0] != current:
                    api_version_healed = True
                    url = _replace_query_param(url, "api-version", versions[0])
                    healed.append(f"api-version {current or '(none)'} not supported; retried with {versions[0]}")
                    continue
            if status in (400, 404) and svc.name == "graph" and method == "GET" and settings.graph_auto_beta_fallback and not beta_healed and "/v1.0/" in url:
                if healing.graph_beta_candidate(method, urlparse(url).path):
                    beta_healed = True
                    url = url.replace("/v1.0/", "/beta/", 1)
                    healed.append("path exists only in Graph beta; retried on /beta")
                    continue
            if status == 400 and svc.name in ("powerplatform", "bap", "powerapps", "flow") and not api_version_healed and "api version" in resp.text.lower().replace("-", " "):
                current = dict(parse_qsl(urlparse(url).query)).get("api-version", "")
                supported = healing.supported_versions_from_text(resp.text)
                candidates = [v for v in supported if v != current] or [v for v in ("2024-10-01", "2022-03-01-preview") if v != current]
                if candidates:
                    api_version_healed = True
                    url = _replace_query_param(url, "api-version", candidates[0])
                    healed.append(f"api-version {current} rejected; retried with {candidates[0]} (service reports supported: {', '.join(supported[:6]) or 'unknown'})")
                    continue
            break

        elapsed_ms = int((time.time() - started) * 1000)
        if error is not None:
            error.update({"service": svc.name, "method": method, "elapsed_ms": elapsed_ms, "healing": healed})
            audit.record({"user": principal.upn or principal.name, "method": method, "url": url, "status": 0, "error": error.get("error"), "ms": elapsed_ms, "write": decision.get("write"), "healing": healed})
            return self._fail(error, raise_on_error)

        assert resp is not None
        shaped = self._shape(resp, max_chars or settings.max_response_chars)
        result: dict[str, Any] = {
            "status": resp.status_code,
            "ok": 200 <= resp.status_code < 300,
            "service": svc.name,
            "method": method,
            "url": url,
            "version": version_used,
            "elapsed_ms": elapsed_ms,
            "response_headers": {k: v for k, v in resp.headers.items() if k.lower() in (
                "request-id", "client-request-id", "x-ms-request-id", "x-ms-correlation-request-id", "retry-after",
                "x-ms-ratelimit-remaining-subscription-reads", "x-ms-ratelimit-microsoft.costmanagement-qpu-remaining",
                "location", "azure-asyncoperation", "operation-location", "content-type", "ratelimit-remaining")},
            **shaped,
        }
        if healed:
            result["healing"] = healed
        if warnings:
            result["warnings"] = warnings
        if resp.status_code == 202 or (resp.status_code in (200, 201) and resp.headers.get("Azure-AsyncOperation")):
            poll = resp.headers.get("Azure-AsyncOperation") or resp.headers.get("Operation-Location") or resp.headers.get("Location")
            if poll:
                result["async_operation"] = {"poll_url": poll, "retry_after_seconds": _retry_after(resp) or 5,
                                            "next_step": "Call poll_operation with this poll_url until it reports Succeeded or Failed."}
        if not result["ok"]:
            result.update(healing.explain(svc.name, resp.status_code, shaped.get("body"), method, url, urlparse(url).path))
            result["message"] = _message_from_body(shaped.get("body"), resp.status_code)
        dep = self._deprecation_note(svc.name, method, urlparse(url).path, version_used)
        if dep:
            result.setdefault("warnings", []).append(dep)

        audit.record({"user": principal.upn or principal.name, "method": method, "url": url, "status": resp.status_code, "ms": elapsed_ms,
                      "write": decision.get("write"), "healing": healed or None})
        if not result["ok"] and raise_on_error:
            raise RunbookError(result)
        return result

    # ---------------------------------------------------------- conveniences
    async def get_json(self, service: str, path: str, query: dict[str, Any] | None = None, api_version: str = "", dataverse_org_url: str = "", headers: dict[str, str] | None = None) -> Any:
        r = await self.call(service, "GET", path, query=query, api_version=api_version, dataverse_org_url=dataverse_org_url, headers=headers, raise_on_error=True)
        return r.get("body")

    async def paged(self, service: str, path: str, query: dict[str, Any] | None = None, api_version: str = "", max_pages: int = 10, dataverse_org_url: str = "") -> list[Any]:
        items: list[Any] = []
        r = await self.call(service, "GET", path, query=query, api_version=api_version, dataverse_org_url=dataverse_org_url, raise_on_error=True, max_chars=10_000_000)
        pages = 1
        while True:
            body = r.get("body")
            if isinstance(body, dict) and isinstance(body.get("value"), list):
                items.extend(body["value"])
            elif isinstance(body, list):
                items.extend(body)
            else:
                items.append(body)
            nxt = r.get("next_link")
            if not nxt or pages >= max_pages:
                break
            pages += 1
            r = await self.call(service, "GET", nxt, raise_on_error=True, max_chars=10_000_000)
        return items

    # --------------------------------------------------------------- helpers
    @staticmethod
    def _fail(res: dict[str, Any], raise_on_error: bool) -> dict[str, Any]:
        res.setdefault("ok", False)
        if raise_on_error:
            raise RunbookError(res)
        return res

    @staticmethod
    def _redact(h: dict[str, str]) -> dict[str, str]:
        return {k: ("<redacted>" if k.lower() in REDACT_HEADERS else v) for k, v in h.items()}

    @staticmethod
    def _deprecation_note(service: str, method: str, path: str, version: str) -> str:
        try:
            clean = re.sub(r"^/(v1\.0|beta)", "", path) if service == "graph" else path
            for op in catalog.match_path(service, method, clean, version=version if service == "graph" else ""):
                if op.get("dep"):
                    note = op.get("depnote") or ""
                    return f"Deprecated operation ({op['id']}). {note}".strip()
        except Exception:  # noqa: BLE001
            return ""
        return ""

    def _shape(self, resp: httpx.Response, max_chars: int) -> dict[str, Any]:
        ctype = (resp.headers.get("content-type") or "").lower()
        raw = resp.content or b""
        out: dict[str, Any] = {}
        if not raw:
            out["body"] = None
            return out
        if "gzip" in ctype or raw[:2] == b"\x1f\x8b":
            try:
                raw = gzip.decompress(raw)
                ctype = "text/csv" if b"," in raw[:200] else "text/plain"
            except Exception:  # noqa: BLE001
                pass
        if "json" in ctype:
            try:
                body = json.loads(raw.decode("utf-8-sig"))
            except Exception:  # noqa: BLE001
                body = raw.decode("utf-8", "replace")[:max_chars]
            out["next_link"] = _next_link(body)
            shrunk, truncated, total = _shrink(body, max_chars)
            out["body"] = shrunk
            if truncated:
                out["truncated"] = True
                out["items_total_in_page"] = total
                out["hint"] = "Response truncated to fit. Use $select/$top (Graph, Dataverse), smaller date ranges, or follow next_link with invoke_url."
            return out
        if "csv" in ctype or ctype.startswith("text/"):
            text = raw.decode("utf-8-sig", "replace")
            if "csv" in ctype or (text.count(",") > 5 and "\n" in text):
                rows = list(csv.DictReader(io.StringIO(text)))
                out["body"] = {"rows": rows[:500], "row_count": len(rows), "columns": list(rows[0].keys()) if rows else []}
                if len(rows) > 500:
                    out["truncated"] = True
                return out
            out["body"] = text[:max_chars]
            out["truncated"] = len(text) > max_chars
            return out
        out["body"] = {"content_type": ctype, "bytes": len(raw), "note": "binary content not shown"}
        return out


def _quote(s, safe="", encoding=None, errors=None):
    from urllib.parse import quote

    return quote(s, safe=safe)


def _retry_after(resp: httpx.Response) -> float:
    ra = resp.headers.get("retry-after") or resp.headers.get("x-ms-ratelimit-microsoft.costmanagement-qpu-retry-after")
    if not ra:
        return 0.0
    try:
        return min(float(ra), 60.0)
    except ValueError:
        return 5.0


def _replace_query_param(url: str, name: str, value: str) -> str:
    p = urlparse(url)
    q = dict(parse_qsl(p.query, keep_blank_values=True))
    q[name] = value
    return urlunparse(p._replace(query=urlencode(q, safe="$',():/*", quote_via=_quote)))


def _safe_json(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        return resp.text


def _next_link(body: Any) -> str | None:
    if not isinstance(body, dict):
        return None
    for k in ("@odata.nextLink", "nextLink", "odata.nextLink", "@nextLink", "continuationUri"):
        if body.get(k):
            return str(body[k])
    props = body.get("properties")
    if isinstance(props, dict) and props.get("nextLink"):
        return str(props["nextLink"])
    if body.get("continuationToken"):
        return None  # token-based paging is service specific; surfaced in body
    return None


def _shrink(body: Any, max_chars: int) -> tuple[Any, bool, int]:
    text = json.dumps(body, default=str)
    if len(text) <= max_chars:
        return body, False, len(body.get("value", [])) if isinstance(body, dict) and isinstance(body.get("value"), list) else (len(body) if isinstance(body, list) else 1)
    if isinstance(body, dict) and isinstance(body.get("value"), list):
        total = len(body["value"])
        items = body["value"]
        keep = max(1, int(len(items) * max_chars / max(len(text), 1)))
        shrunk = dict(body)
        shrunk["value"] = items[:keep]
        while len(json.dumps(shrunk, default=str)) > max_chars and keep > 1:
            keep = max(1, keep // 2)
            shrunk["value"] = items[:keep]
        shrunk["_kept"] = keep
        return shrunk, True, total
    if isinstance(body, list):
        total = len(body)
        keep = max(1, int(total * max_chars / max(len(text), 1)))
        return body[:keep], True, total
    return {"_truncated_text": text[:max_chars]}, True, 1


def _message_from_body(body: Any, status: int) -> str:
    if isinstance(body, dict):
        err = body.get("error") or body
        if isinstance(err, dict):
            msg = err.get("message") or err.get("Message") or err.get("error_description") or ""
            if isinstance(msg, str) and msg:
                return msg[:500]
        if isinstance(err, str):
            return err[:500]
        if body.get("message"):
            return str(body["message"])[:500]
    if isinstance(body, str):
        return body[:500]
    return f"HTTP {status}"


runbook = Runbook()
