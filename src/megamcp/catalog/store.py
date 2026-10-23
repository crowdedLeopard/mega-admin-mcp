"""The operation catalog: every REST operation the runbook knows about.

The catalog is built daily by scripts/build_catalog.py from Microsoft's own
published OpenAPI documents and SDKs, shipped inside the package as a
snapshot, and refreshed at runtime from the latest GitHub release so a
deployed server keeps up with Microsoft without a redeploy.

Compact record keys (to keep the file small):
  id    stable identifier          svc   service name        ver   API version / flavour
  m     HTTP method                p     path template       s     summary
  d     description (short)        tags  list                av    api-version (ARM / PP)
  params [{n,in,req,t,d}]          body  request body name   dep   deprecated flag
  docs  documentation URL          perms {del:[], app:[]}    rp    ARM resource provider
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import math
import os
import re
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx

from ..config import settings

log = logging.getLogger("megamcp.catalog")

_WORD = re.compile(r"[A-Za-z0-9]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
STOP = {"the", "a", "an", "of", "for", "to", "in", "and", "or", "get", "list", "by", "with", "on", "is", "this", "that", "api", "all", "id", "me", "my", "our", "do", "we", "i", "want", "show", "find", "please"}
_SUFFIXES = ("ations", "ation", "ated", "ates", "ate", "ities", "ity", "ies", "ing", "ied", "ers", "ed", "es", "s", "y")

# Admin and FinOps vocabulary -> words that appear in Microsoft's API descriptions.
SYNONYMS: dict[str, list[str]] = {
    "credit": ["mcsmessages", "entitlement", "allocation", "licensing", "capacity", "currency"],
    "credits": ["mcsmessages", "entitlement", "allocation", "licensing", "capacity", "currency"],
    "copilot": ["copilotstudio", "bot", "agent", "mcsmessages"],
    "agent": ["bot", "copilotstudio"],
    "agents": ["bot", "copilotstudio"],
    "bot": ["agent"],
    "openai": ["cognitive", "cognitiveservices", "deployment", "account", "foundry", "model"],
    "aoai": ["cognitive", "cognitiveservices", "openai", "deployment"],
    "foundry": ["cognitive", "cognitiveservices", "openai", "machinelearning", "project"],
    "ptu": ["provisioned", "capacity", "quota", "usages", "commitment"],
    "quota": ["usages", "limit", "capacity"],
    "token": ["metric", "usages", "deployment"],
    "tokens": ["metric", "usages", "deployment"],
    "cost": ["costmanagement", "consumption", "billing", "usage", "price"],
    "costs": ["costmanagement", "consumption", "billing", "usage", "price"],
    "spend": ["cost", "costmanagement", "budget", "consumption"],
    "spending": ["cost", "costmanagement", "budget", "consumption"],
    "finops": ["cost", "costmanagement", "licensing", "entitlement", "budget"],
    "licence": ["license", "licensing", "subscribedskus", "sku", "entitlement"],
    "licences": ["license", "licensing", "subscribedskus", "sku", "entitlement"],
    "license": ["licensing", "subscribedskus", "sku", "entitlement"],
    "licenses": ["licensing", "subscribedskus", "sku", "entitlement"],
    "payg": ["billingpolicy", "billingpolicies", "paygo", "billing"],
    "pay": ["billing", "billingpolicies", "paygo"],
    "plan": ["billingpolicy", "billingpolicies", "policy"],
    "dlp": ["governance", "policies", "connector"],
    "environment": ["environmentmanagement"],
    "environments": ["environmentmanagement"],
    "user": ["users", "systemuser"],
    "users": ["systemuser"],
    "group": ["groups"],
    "team": ["teams"],
    "flow": ["workflow", "powerautomate"],
    "flows": ["workflow", "powerautomate"],
    "app": ["application", "powerapps", "canvasapp"],
    "apps": ["application", "powerapps", "canvasapp"],
    "report": ["reports", "getmicrosoft365copilotusage", "usage"],
    "usage": ["report", "reports", "usages", "consumption"],
    "budget": ["budgets", "consumption", "costmanagement"],
    "alert": ["alerts"],
    "price": ["retail", "prices", "pricesheet", "meter"],
    "prices": ["retail", "pricesheet", "meter"],
    "pricing": ["retail", "prices", "pricesheet", "meter"],
    "settings": ["setting", "tenantsettings"],
    "quarantine": ["botquarantine"],
    "conditional": ["conditionalaccess"],
    "mfa": ["authentication", "methods", "conditionalaccess"],
    "intune": ["devicemanagement", "device"],
    "device": ["devicemanagement"],
    "sharepoint": ["sites", "site"],
    "exchange": ["mail", "mailbox"],
    "entra": ["directory", "users", "groups", "applications"],
    "aad": ["directory", "users", "groups", "applications"],
    "subscription": ["subscriptions"],
    "deployment": ["deployments", "model"],
    "model": ["deployments", "models"],
    "health": ["servicehealth", "healthoverviews", "issues"],
}


def stem(t: str) -> str:
    for suf in _SUFFIXES:
        if t.endswith(suf) and len(t) - len(suf) >= 3:
            return t[: -len(suf)]
    return t


def tokenize(text: str) -> list[str]:
    out: list[str] = []
    for w in _WORD.findall(text or ""):
        for part in _CAMEL.split(w):
            p = part.lower()
            if len(p) > 1 and p not in STOP:
                out.append(stem(p))
    return out


def longpath(p: str | Path) -> str:
    """Windows: allow paths longer than 260 characters."""
    s = str(p)
    if os.name == "nt" and len(s) > 240 and not s.startswith("\\\\?\\"):
        return "\\\\?\\" + os.path.abspath(s)
    return s


_MULTI_SEGMENT = re.compile(r"scope|uri|resourceid|resourcepath|parentresourcepath|resourcename$|\+", re.IGNORECASE)


def _path_regex(template: str) -> re.Pattern:
    # /subscriptions/{subscriptionId}/x -> ^/subscriptions/[^/]+/x$ ; {scope}/{resourceUri} match several segments
    parts = re.split(r"(\{[^}]+\})", template)
    out = []
    for p in parts:
        if p.startswith("{"):
            name = p.strip("{}")
            out.append(r".+?" if _MULTI_SEGMENT.search(name) else r"[^/]+")
        else:
            out.append(re.escape(p))
    return re.compile("^" + "".join(out) + "/?$", re.IGNORECASE)


class CatalogStore:
    def __init__(self) -> None:
        self.ops: list[dict[str, Any]] = []
        self.meta: dict[str, Any] = {}
        self.changelog: dict[str, Any] = {}
        self.source: str = "none"
        self.loaded_at: float = 0.0
        self.last_refresh_attempt: float = 0.0
        self.last_refresh_result: str = "not attempted"
        self._etag: str = ""
        self._by_id: dict[str, int] = {}
        self._index: dict[str, set[int]] = defaultdict(set)
        self._df: dict[str, int] = {}
        self._path_cache: dict[tuple[str, str], list[tuple[re.Pattern, int]]] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------ loading
    def load_bundled(self) -> None:
        p = Path(settings.catalog_path)
        if not p.exists():
            log.warning("No bundled catalog at %s. Run scripts/build_catalog.py. Starting with curated-only catalog.", p)
            self._load_curated_only()
            return
        with gzip.open(longpath(p), "rt", encoding="utf-8") as f:
            data = json.load(f)
        # curated operations live beside the catalog; merge them if the build missed them
        curated = self._read_curated(p.parent / "curated")
        have = {o["id"] for o in data.get("operations", [])}
        extra = [o for o in curated if o["id"] not in have]
        if extra:
            data["operations"] = data.get("operations", []) + extra
        self._install(data, source=f"bundled:{p.name}")
        cl = p.parent / "changelog.json"
        if cl.exists():
            try:
                with open(longpath(cl), "r", encoding="utf-8") as f:
                    self.changelog = json.load(f)
            except Exception:  # noqa: BLE001
                self.changelog = {}

    def _read_curated(self, cur: Path) -> list[dict[str, Any]]:
        ops: list[dict[str, Any]] = []
        if cur.exists():
            for f in sorted(cur.glob("*.json")):
                try:
                    with open(longpath(f), "r", encoding="utf-8") as fh:
                        for op in json.load(fh).get("operations", []):
                            op.setdefault("ver", "")
                            op.setdefault("tags", [])
                            op.setdefault("params", [])
                            op["id"] = op.get("id") or f"{op['svc']}:{op['m']}:{op['p']}"
                            ops.append(op)
                except Exception as e:  # noqa: BLE001
                    log.warning("curated file %s failed: %s", f, e)
        return ops

    def _load_curated_only(self) -> None:
        ops = self._read_curated(Path(settings.catalog_path).parent / "curated")
        self._install({"generated": "", "sources": {"curated": {"operations": len(ops)}}, "operations": ops}, source="curated-only")

    def _install(self, data: dict[str, Any], source: str) -> None:
        ops = data.get("operations", [])
        by_id: dict[str, int] = {}
        index: dict[str, set[int]] = defaultdict(set)
        for i, op in enumerate(ops):
            by_id[op["id"]] = i
            text = " ".join([
                op.get("p", ""), op.get("s", ""), op.get("d", "")[:200], " ".join(op.get("tags", [])),
                op.get("svc", ""), op.get("rp", "") or "", op.get("m", ""), op.get("ver", "") or "", op.get("oid", "") or "",
            ])
            for t in set(tokenize(text)):
                index[t].add(i)
        with self._lock:
            self.ops = ops
            self.meta = {k: v for k, v in data.items() if k != "operations"}
            self._by_id = by_id
            self._index = index
            self._df = {t: len(s) for t, s in index.items()}
            self._path_cache.clear()
            self.source = source
            self.loaded_at = time.time()
        log.info("catalog loaded: %d operations from %s", len(ops), source)

    # ------------------------------------------------------------ refresh
    async def refresh_from_remote(self, force: bool = False) -> str:
        """Download the latest catalog release. Never replaces a good catalog with a bad one."""
        if not settings.catalog_url:
            return "no CATALOG_URL configured"
        self.last_refresh_attempt = time.time()
        headers = {"User-Agent": "mega-admin-mcp"}
        if self._etag and not force:
            headers["If-None-Match"] = self._etag
        try:
            async with httpx.AsyncClient(timeout=60, follow_redirects=True) as c:
                r = await c.get(settings.catalog_url, headers=headers)
                if r.status_code == 304:
                    self.last_refresh_result = "unchanged (304)"
                    return self.last_refresh_result
                if r.status_code != 200:
                    self.last_refresh_result = f"HTTP {r.status_code} from catalog URL"
                    return self.last_refresh_result
                data = json.loads(gzip.decompress(r.content).decode("utf-8"))
                n_new, n_old = len(data.get("operations", [])), len(self.ops)
                if n_old and n_new < n_old * 0.7:
                    self.last_refresh_result = f"rejected: remote has {n_new} operations vs {n_old} local (shrunk > 30%)"
                    log.warning(self.last_refresh_result)
                    return self.last_refresh_result
                if data.get("generated") and self.meta.get("generated") and data["generated"] <= self.meta["generated"] and not force:
                    self.last_refresh_result = f"remote catalog {data['generated']} is not newer than local {self.meta['generated']}"
                    return self.last_refresh_result
                curated = self._read_curated(Path(settings.catalog_path).parent / "curated")
                have = {o["id"] for o in data.get("operations", [])}
                data["operations"] = data.get("operations", []) + [o for o in curated if o["id"] not in have]
                self._install(data, source=f"remote:{settings.catalog_url}")
                self._etag = r.headers.get("etag", "")
                if settings.changelog_url:
                    try:
                        cr = await c.get(settings.changelog_url, headers={"User-Agent": "mega-admin-mcp"})
                        if cr.status_code == 200:
                            self.changelog = cr.json()
                    except Exception as e:  # noqa: BLE001
                        log.info("changelog fetch failed: %s", e)
                self.last_refresh_result = f"updated to {data.get('generated', '?')} ({n_new} operations)"
                return self.last_refresh_result
        except Exception as e:  # noqa: BLE001
            self.last_refresh_result = f"failed: {type(e).__name__}: {str(e)[:200]}"
            log.warning("catalog refresh failed: %s", e)
            return self.last_refresh_result

    async def refresh_loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                await self.refresh_from_remote()
            except Exception as e:  # noqa: BLE001
                log.warning("refresh loop error: %s", e)
            await asyncio.sleep(max(1, settings.catalog_refresh_hours) * 3600)

    # ------------------------------------------------------------ queries
    def get(self, op_id: str) -> dict[str, Any] | None:
        i = self._by_id.get(op_id)
        return self.ops[i] if i is not None else None

    def search(self, query: str, service: str = "", method: str = "", version: str = "", limit: int = 15) -> list[dict[str, Any]]:
        raw_words = [w.lower() for w in _WORD.findall(query or "")]
        tokens = tokenize(query)
        expanded: dict[str, float] = {t: 1.0 for t in tokens}
        for w in raw_words:
            for syn in SYNONYMS.get(w, []):
                for st in tokenize(syn):
                    expanded.setdefault(st, 0.6)
        n = max(1, len(self.ops))
        scores: dict[int, float] = defaultdict(float)
        q_lower = (query or "").lower().strip()
        for t, weight in expanded.items():
            hits = self._index.get(t)
            if not hits:
                if len(t) >= 4:
                    for key, s in self._index.items():
                        if key.startswith(t) and len(s) < n * 0.2:
                            idf = math.log(1 + n / (1 + len(s)))
                            for i in s:
                                scores[i] += idf * 0.5 * weight
                continue
            idf = math.log(1 + n / (1 + len(hits)))
            for i in hits:
                scores[i] += idf * weight
        results = []
        svc = service.lower().strip()
        meth = method.upper().strip()
        for i, sc in scores.items():
            op = self.ops[i]
            if svc and op.get("svc") != svc:
                continue
            if meth and op.get("m") != meth:
                continue
            if version and (op.get("ver") or "") != version:
                continue
            p = op.get("p", "").lower()
            if q_lower and q_lower in p:
                sc += 3
            if q_lower and q_lower in (op.get("s", "").lower()):
                sc += 2
            if op.get("dep"):
                sc -= 1.5
            if op.get("ver") == "beta":
                sc -= 0.4  # prefer v1.0 when both exist
            if op.get("m") == "GET":
                sc += 0.15
            sc -= 0.002 * len(p)
            results.append((sc, i))
        results.sort(key=lambda x: -x[0])
        out = []
        for sc, i in results[:limit]:
            op = self.ops[i]
            out.append({
                "id": op["id"], "service": op["svc"], "version": op.get("ver", ""), "method": op["m"], "path": op["p"],
                "summary": op.get("s", ""), "api_version": op.get("av", ""), "deprecated": bool(op.get("dep")), "score": round(sc, 2),
            })
        return out

    def describe(self, op_id: str) -> dict[str, Any] | None:
        op = self.get(op_id)
        if not op:
            return None
        return {
            "id": op["id"], "service": op["svc"], "version": op.get("ver", ""), "method": op["m"], "path": op["p"],
            "summary": op.get("s", ""), "description": op.get("d", ""), "tags": op.get("tags", []), "operation_id": op.get("oid", ""),
            "api_version": op.get("av", ""), "parameters": op.get("params", []), "request_body": op.get("body", ""),
            "deprecated": bool(op.get("dep")), "deprecation_note": op.get("depnote", ""), "docs": op.get("docs", ""),
            "permissions": op.get("perms", {}), "resource_provider": op.get("rp", ""),
            "how_to_call": self._how_to_call(op),
        }

    def _how_to_call(self, op: dict[str, Any]) -> dict[str, Any]:
        svc = op["svc"]
        ex: dict[str, Any] = {"tool": "invoke", "service": svc, "method": op["m"], "path": op["p"]}
        if svc == "graph":
            ex["path"] = f"/{op.get('ver') or 'v1.0'}{op['p']}"
        if op.get("av"):
            ex["api_version"] = op["av"]
        if svc == "dataverse":
            ex["dataverse_org_url"] = "https://<org>.crm.dynamics.com"
        if op["m"] in ("POST", "PUT", "PATCH", "DELETE"):
            ex["confirm"] = True
            ex["note"] = "Write operation: call once with confirm=false to preview, then confirm=true to execute."
        return ex

    def match_path(self, service: str, method: str, path: str, version: str = "") -> list[dict[str, Any]]:
        """Find catalog operations whose path template matches a concrete path."""
        key = (service, method)
        with self._lock:
            compiled = self._path_cache.get(key)
            if compiled is None:
                compiled = []
                for i, op in enumerate(self.ops):
                    if op["svc"] == service and op["m"] == method:
                        try:
                            compiled.append((_path_regex(op["p"]), i))
                        except re.error:
                            continue
                self._path_cache[key] = compiled
        clean = path.split("?")[0]
        out = []
        for rx, i in compiled:
            if rx.match(clean):
                op = self.ops[i]
                if version and (op.get("ver") or "") != version:
                    continue
                out.append(op)
        return out

    def latest_api_version(self, method: str, path: str, resource_provider: str = "") -> str:
        """Best api-version for an ARM call: match the exact path, else the provider's newest stable."""
        matches = self.match_path("arm", method, path)
        versions = sorted({m.get("av", "") for m in matches if m.get("av")}, reverse=True)
        if versions:
            stable = [v for v in versions if "preview" not in v]
            return (stable or versions)[0]
        rp = resource_provider or _provider_from_path(path)
        if rp:
            vs = sorted({op.get("av", "") for op in self.ops if op["svc"] == "arm" and (op.get("rp") or "").lower() == rp.lower() and op.get("av")}, reverse=True)
            stable = [v for v in vs if "preview" not in v]
            if vs:
                return (stable or vs)[0]
        return ""

    def similar_paths(self, service: str, path: str, limit: int = 5) -> list[dict[str, Any]]:
        toks = set(tokenize(path))
        scored = []
        for op in self.ops:
            if op["svc"] != service:
                continue
            ot = set(tokenize(op["p"]))
            if not ot:
                continue
            j = len(toks & ot) / len(toks | ot)
            if j > 0.3:
                scored.append((j, op))
        scored.sort(key=lambda x: -x[0])
        return [{"id": o["id"], "method": o["m"], "path": o["p"], "version": o.get("ver", ""), "summary": o.get("s", "")} for _, o in scored[:limit]]

    def status(self) -> dict[str, Any]:
        by_svc: dict[str, int] = defaultdict(int)
        for op in self.ops:
            by_svc[op["svc"]] += 1
        age_h = (time.time() - self.loaded_at) / 3600 if self.loaded_at else None
        return {
            "operations": len(self.ops),
            "by_service": dict(sorted(by_svc.items())),
            "generated": self.meta.get("generated", ""),
            "sources": self.meta.get("sources", {}),
            "loaded_from": self.source,
            "loaded_hours_ago": round(age_h, 2) if age_h is not None else None,
            "auto_refresh": settings.catalog_auto_refresh,
            "refresh_every_hours": settings.catalog_refresh_hours,
            "catalog_url": settings.catalog_url,
            "last_refresh_attempt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.last_refresh_attempt)) if self.last_refresh_attempt else None,
            "last_refresh_result": self.last_refresh_result,
            "warnings": self.meta.get("warnings", []),
        }

    def changes(self, days: int = 7, service: str = "") -> dict[str, Any]:
        entries = self.changelog.get("entries", []) if isinstance(self.changelog, dict) else []
        cutoff = time.time() - days * 86400
        out = []
        for e in entries:
            try:
                ts = time.mktime(time.strptime(e.get("generated", "")[:19], "%Y-%m-%dT%H:%M:%S"))
            except Exception:  # noqa: BLE001
                ts = time.time()
            if ts < cutoff:
                continue
            added = [a for a in e.get("added", []) if not service or a.get("svc") == service]
            removed = [r for r in e.get("removed", []) if not service or r.get("svc") == service]
            if added or removed or e.get("note"):
                out.append({"generated": e.get("generated"), "added": added[:200], "removed": removed[:200],
                            "added_count": e.get("added_count", len(added)) if not service else len(added),
                            "removed_count": e.get("removed_count", len(removed)) if not service else len(removed), "note": e.get("note", "")})
        return {"days": days, "service": service or "all", "entries": out,
                "note": "Added = operations Microsoft published since the previous build. Removed = operations that disappeared from Microsoft's specs (often renamed or retired)."}


def _provider_from_path(path: str) -> str:
    m = re.search(r"/providers/(Microsoft\.[A-Za-z0-9]+)", path, re.IGNORECASE)
    return m.group(1) if m else ""


catalog = CatalogStore()
