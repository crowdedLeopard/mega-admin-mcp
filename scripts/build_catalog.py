#!/usr/bin/env python3
"""Build the operation catalog from Microsoft's own published API descriptions.

Sources
  graph         Microsoft Graph v1.0 and beta OpenAPI (microsoftgraph/msgraph-metadata) + permissions map
  azure         Azure REST API specs (Azure/azure-rest-api-specs) for the FinOps and admin resource providers
  powerplatform Power Platform API, read from Microsoft's Kiota-generated SDK on PyPI (powerplatform-management)
  fabric        Microsoft Fabric REST API specs (microsoft/fabric-rest-api-specs)
  curated       Hand-maintained operations for APIs with no public spec (Dataverse, BAP, Power BI admin, O365 Management)

Outputs (in --out, default src/megamcp/data)
  catalog.json.gz    the catalog the server loads
  catalog_meta.json  counts, versions, warnings
  changelog.json     rolling list of added / removed operations per build

Safety: if a source fails or shrinks by more than 30 percent, the previous
snapshot of that source is kept and a warning is recorded, so a flaky upstream
can never ship a broken catalog.

Usage
  python scripts/build_catalog.py [--sources graph,azure,powerplatform,fabric,curated] [--skip-beta] [--out DIR]
  GITHUB_TOKEN is optional but recommended (GitHub API rate limits).
"""
from __future__ import annotations

import argparse
import datetime as dt
import gzip
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DEFAULT_OUT = ROOT / "src" / "megamcp" / "data"
with open(("\\\\?\\" + os.path.abspath(HERE / "catalog_sources.json")) if (os.name == "nt" and len(str(HERE)) > 200) else str(HERE / "catalog_sources.json"), "r", encoding="utf-8") as _f:
    SOURCES = json.load(_f)
METHODS = ("get", "post", "put", "patch", "delete", "head")
UA = {"User-Agent": "mega-admin-mcp-catalog-builder"}
GH_TOKEN = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or ""
WARNINGS: list[str] = []


def log(msg: str) -> None:
    print(f"[{dt.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def longpath(p: Path | str) -> str:
    """Windows: allow paths longer than 260 characters."""
    s = str(p)
    if os.name == "nt" and len(s) > 240 and not s.startswith("\\\\?\\"):
        return "\\\\?\\" + os.path.abspath(s)
    return s


def read_text(p: Path) -> str:
    with open(longpath(p), "r", encoding="utf-8") as f:
        return f.read()


def write_text(p: Path, text: str) -> None:
    with open(longpath(p), "w", encoding="utf-8") as f:
        f.write(text)


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    log("WARNING: " + msg)


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------
def fetch(url: str, timeout: int = 120, github_api: bool = False, retries: int = 3) -> bytes:
    headers = dict(UA)
    if github_api and GH_TOKEN:
        headers["Authorization"] = f"token {GH_TOKEN}"
    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (403, 429) and github_api:
                reset = e.headers.get("x-ratelimit-reset")
                wait = max(5, min(int(reset) - int(time.time()), 120)) if reset else 15
                log(f"GitHub rate limited; waiting {wait}s")
                time.sleep(wait)
                continue
            if e.code == 404:
                raise
            time.sleep(2 * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"fetch failed for {url}: {last}")


def fetch_json(url: str, **kw: Any) -> Any:
    return json.loads(fetch(url, **kw).decode("utf-8-sig"))


def github_tree(repo: str, branch: str, path: str) -> list[dict[str, Any]]:
    url = f"https://api.github.com/repos/{repo}/git/trees/{branch}:{path}?recursive=1"
    data = fetch_json(url, github_api=True)
    if data.get("truncated"):
        warn(f"GitHub tree truncated for {repo}:{path}")
    return data.get("tree", [])


# ---------------------------------------------------------------------------
# record helpers
# ---------------------------------------------------------------------------
def clip(s: Any, n: int) -> str:
    if not s:
        return ""
    s = re.sub(r"\s+", " ", str(s)).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def ref_name(ref: str) -> str:
    return ref.split("/")[-1].split("#")[-1]


def resolve_param(p: Any, doc: dict[str, Any]) -> dict[str, Any]:
    if isinstance(p, dict) and "$ref" in p:
        ref = p["$ref"]
        target = None
        if ref.startswith("#/parameters/"):
            target = (doc.get("parameters") or {}).get(ref_name(ref))
        elif ref.startswith("#/components/parameters/"):
            target = ((doc.get("components") or {}).get("parameters") or {}).get(ref_name(ref))
        if target is None:
            name = ref_name(ref)
            guess = re.sub(r"Parameter$", "", name)
            guess = guess[:1].lower() + guess[1:] if guess else name
            if guess in ("apiVersion", "api-version"):
                return {"n": "api-version", "in": "query", "req": True, "t": "string", "d": "API version"}
            return {"n": guess, "in": "path" if guess not in ("top", "skip", "filter", "expand", "select") else "query", "req": True, "t": "string", "d": f"(from {name})"}
        p = target
    if not isinstance(p, dict):
        return {"n": str(p), "in": "", "req": False, "t": "", "d": ""}
    schema = p.get("schema") or {}
    t = p.get("type") or schema.get("type") or ("array" if schema.get("items") else "")
    if isinstance(t, list):
        t = "/".join(str(x) for x in t)
    return {"n": p.get("name", ""), "in": p.get("in", ""), "req": bool(p.get("required")), "t": t, "d": clip(p.get("description", ""), 140)}


def body_name(op: dict[str, Any]) -> str:
    rb = op.get("requestBody")
    if isinstance(rb, dict):
        content = rb.get("content") or {}
        for ctype in ("application/json", "*/*"):
            sch = (content.get(ctype) or {}).get("schema")
            if isinstance(sch, dict):
                return ref_name(sch.get("$ref", "")) or sch.get("type", "object")
        if "$ref" in rb:
            return ref_name(rb["$ref"])
        return "body"
    for p in op.get("parameters", []) or []:
        if isinstance(p, dict) and p.get("in") == "body":
            sch = p.get("schema") or {}
            return ref_name(sch.get("$ref", "")) or sch.get("type", "object") or p.get("name", "body")
    return ""


def make_op(svc: str, ver: str, method: str, path: str, op: dict[str, Any], doc: dict[str, Any], av: str = "", rp: str = "", docs: str = "") -> dict[str, Any]:
    params = []
    seen = set()
    for p in list(doc.get("_path_params", [])) + list(op.get("parameters", []) or []):
        rp_ = resolve_param(p, doc)
        if rp_["in"] == "body" or not rp_["n"] or rp_["n"] in seen:
            continue
        seen.add(rp_["n"])
        params.append(rp_)
    dep = bool(op.get("deprecated")) or "x-ms-deprecation" in op
    depnote = ""
    if "x-ms-deprecation" in op and isinstance(op["x-ms-deprecation"], dict):
        depnote = clip(op["x-ms-deprecation"].get("description") or op["x-ms-deprecation"].get("date") or "", 200)
    rec: dict[str, Any] = {
        "id": f"{svc}{('.' + ver) if ver else ''}:{method.upper()}:{path}",
        "svc": svc, "ver": ver, "m": method.upper(), "p": path,
        "s": clip(op.get("summary") or op.get("operationId") or "", 160),
        "d": clip(op.get("description") or "", 300),
        "tags": [str(t) for t in (op.get("tags") or [])][:4],
        "params": params[:40],
    }
    b = body_name(op)
    if b:
        rec["body"] = b
    if av:
        rec["av"] = av
    if rp:
        rec["rp"] = rp
    if dep:
        rec["dep"] = True
    if depnote:
        rec["depnote"] = depnote
    ext = op.get("externalDocs") or {}
    d = docs or (ext.get("url") if isinstance(ext, dict) else "")
    if d:
        rec["docs"] = d
    oid = op.get("operationId")
    if oid:
        rec["oid"] = str(oid)[:120]
    return rec


def iter_paths(doc: dict[str, Any]):
    for container in ("paths", "x-ms-paths"):
        for path, item in (doc.get(container) or {}).items():
            if not isinstance(item, dict):
                continue
            path_params = item.get("parameters") or []
            for method, op in item.items():
                if method.lower() in METHODS and isinstance(op, dict):
                    yield path, method, op, path_params


# ---------------------------------------------------------------------------
# Graph
# ---------------------------------------------------------------------------
def norm_graph_path(p: str) -> str:
    p = p.split("?")[0].lower().replace("'", "")
    p = re.sub(r"=@[a-z0-9_]+", "={}", p)
    p = re.sub(r"\{[^}]*\}", "{}", p)
    return p.rstrip("/")


def load_graph_permissions() -> dict[tuple[str, str], dict[str, set[str]]]:
    url = SOURCES["graph"]["permissions"]
    try:
        data = fetch_json(url)
    except Exception as e:  # noqa: BLE001
        warn(f"graph permissions map failed: {e}")
        return {}
    perms = data.get("permissions", data)
    out: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(lambda: {"del": set(), "app": set()})
    for name, info in perms.items():
        for ps in info.get("pathSets", []):
            schemes = ps.get("schemeKeys", [])
            kinds = set()
            if any(s.startswith("Delegated") for s in schemes):
                kinds.add("del")
            if "Application" in schemes:
                kinds.add("app")
            for method in ps.get("methods", []):
                for path in ps.get("paths", {}):
                    norm = norm_graph_path(path)
                    for key in {(method.upper(), norm), (method.upper(), re.sub(r"\(.*\)$", "", norm))}:
                        for k in kinds:
                            out[key][k].add(name)
    log(f"graph permissions map: {len(out)} path/method keys")
    return out


def lookup_perms(perms: dict, method: str, path: str) -> dict[str, set[str]] | None:
    norm = norm_graph_path(path)
    return perms.get((method.upper(), norm)) or perms.get((method.upper(), re.sub(r"\(.*\)$", "", norm)))


def build_graph(skip_beta: bool) -> list[dict[str, Any]]:
    import yaml  # type: ignore

    loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
    perms = load_graph_permissions()
    ops: list[dict[str, Any]] = []
    for ver, url in SOURCES["graph"]["specs"].items():
        if skip_beta and ver == "beta":
            continue
        log(f"graph {ver}: downloading {url}")
        raw = fetch(url, timeout=600)
        log(f"graph {ver}: parsing {len(raw) / 1e6:.1f} MB YAML")
        doc = yaml.load(io.BytesIO(raw), Loader=loader)
        del raw
        count = 0
        for path, method, op, path_params in iter_paths(doc):
            doc["_path_params"] = path_params
            rec = make_op("graph", ver, method, path, op, doc)
            pm = lookup_perms(perms, method, path)
            if pm:
                rec["perms"] = {"del": sorted(pm["del"])[:12], "app": sorted(pm["app"])[:12]}
            if not rec.get("docs") and rec.get("oid"):
                rec["docs"] = f"https://learn.microsoft.com/graph/api/resources/{rec['tags'][0].split('.')[-1] if rec.get('tags') else ''}?view=graph-rest-{ver.replace('v', '')}"
            ops.append(rec)
            count += 1
        log(f"graph {ver}: {count} operations")
        del doc
    return ops


# ---------------------------------------------------------------------------
# Azure
# ---------------------------------------------------------------------------
VERSION_RX = re.compile(r"^(stable|preview)/([0-9]{4}-[0-9]{2}-[0-9]{2}(?:-preview|-privatepreview|-alpha|-beta)?)/(.+\.json)$")


def azure_targets(service: str) -> list[tuple[str, str, str, list[str]]]:
    """Return [(rp, service_folder, version, [json file paths relative to resource-manager])]."""
    repo, branch = SOURCES["azure"]["repo"], SOURCES["azure"]["branch"]
    base = f"specification/{service}/resource-manager"
    try:
        tree = github_tree(repo, branch, base)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            warn(f"azure: {base} not found")
            return []
        raise
    groups: dict[tuple[str, str], dict[tuple[str, str], list[str]]] = defaultdict(lambda: defaultdict(list))
    for entry in tree:
        p = entry.get("path", "")
        if not p.endswith(".json") or "/examples/" in p or "/common-types/" in p.lower():
            continue
        parts = p.split("/")
        # new layout: <RP>/<Service>/stable/<ver>/file.json ; legacy: <RP>/stable/<ver>/file.json
        if len(parts) >= 5 and parts[2] in ("stable", "preview"):
            rp, svc_folder, rest = parts[0], parts[1], "/".join(parts[2:])
        elif len(parts) >= 4 and parts[1] in ("stable", "preview"):
            rp, svc_folder, rest = parts[0], "", "/".join(parts[1:])
        else:
            continue
        m = VERSION_RX.match(rest)
        if not m:
            continue
        kind, ver, _file = m.groups()
        groups[(rp, svc_folder)][(kind, ver)].append(p)
    targets = []
    for (rp, svc_folder), versions in groups.items():
        stable = sorted([k for k in versions if k[0] == "stable"], key=lambda k: k[1], reverse=True)
        preview = sorted([k for k in versions if k[0] == "preview"], key=lambda k: k[1], reverse=True)
        chosen = (stable or preview)[0]
        # include newest preview too when it is newer than stable (new features land there first)
        picks = [chosen]
        if stable and preview and preview[0][1][:10] > stable[0][1][:10]:
            picks.append(preview[0])
        for k in picks:
            targets.append((rp, svc_folder, k[1], versions[k]))
    return targets


def parse_swagger(doc: dict[str, Any], rp: str, av: str, source_path: str) -> list[dict[str, Any]]:
    ops = []
    base = doc.get("basePath") or ""
    if isinstance(doc.get("servers"), list) and doc["servers"]:
        u = doc["servers"][0].get("url", "")
        if u.startswith("/"):
            base = u
    for path, method, op, path_params in iter_paths(doc):
        doc["_path_params"] = path_params
        full = (base.rstrip("/") + path) if base and not path.startswith("http") else path
        rec = make_op("arm", "", method, full, op, doc, av=av, rp=rp, docs=f"https://github.com/Azure/azure-rest-api-specs/blob/main/{source_path}")
        ops.append(rec)
    return ops


def build_azure() -> list[dict[str, Any]]:
    repo, branch = SOURCES["azure"]["repo"], SOURCES["azure"]["branch"]
    services = [s for s in SOURCES["azure"]["services"] if not s.endswith("-skip")]
    all_targets: list[tuple[str, str, str, str, str]] = []  # (service, rp, svc_folder, version, path)
    for svc in services:
        try:
            for rp, svc_folder, ver, files in azure_targets(svc):
                for f in files:
                    all_targets.append((svc, rp, svc_folder, ver, f))
        except Exception as e:  # noqa: BLE001
            warn(f"azure: listing {svc} failed: {e}")
    log(f"azure: {len(all_targets)} spec files to fetch")
    ops: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    def work(t: tuple[str, str, str, str, str]) -> list[dict[str, Any]]:
        svc, rp, svc_folder, ver, f = t
        url = f"https://raw.githubusercontent.com/{repo}/{branch}/specification/{svc}/resource-manager/{f}"
        try:
            doc = fetch_json(url)
        except Exception as e:  # noqa: BLE001
            warn(f"azure: fetch failed {f}: {e}")
            return []
        if not isinstance(doc, dict) or not (doc.get("paths") or doc.get("x-ms-paths")):
            return []
        av = (doc.get("info") or {}).get("version") or ver
        return parse_swagger(doc, rp, av, f"specification/{svc}/resource-manager/{f}")

    with ThreadPoolExecutor(max_workers=8) as ex:
        for fut in as_completed([ex.submit(work, t) for t in all_targets]):
            for rec in fut.result():
                # prefer newest api-version when the same method+path appears in several files
                if rec["id"] in seen_ids:
                    continue
                seen_ids.add(rec["id"])
                ops.append(rec)
    log(f"azure: {len(ops)} operations across {len({o['rp'] for o in ops})} resource providers")
    return ops


# ---------------------------------------------------------------------------
# Power Platform API (from the Kiota SDK)
# ---------------------------------------------------------------------------
def download_pp_wheel(tmp: Path) -> Path | None:
    pkg = SOURCES["powerplatform"]["pypi_package"]
    try:
        meta = fetch_json(f"https://pypi.org/pypi/{pkg}/json")
        ver = meta["info"]["version"]
        wheel = next(u for u in meta["releases"][ver] if u["filename"].endswith(".whl"))
        try:
            data = fetch(wheel["url"], timeout=300)
            out = tmp / wheel["filename"]
            out.write_bytes(data)
            log(f"powerplatform: downloaded {wheel['filename']} from PyPI")
            return out
        except Exception as e:  # noqa: BLE001
            log(f"powerplatform: direct download failed ({e}); trying pip download")
    except Exception as e:  # noqa: BLE001
        log(f"powerplatform: PyPI metadata failed ({e}); trying pip download")
    try:
        subprocess.run([sys.executable, "-m", "pip", "download", "--no-deps", "-q", "-d", str(tmp), pkg], check=True, timeout=600)
        whl = next(tmp.glob("*.whl"), None)
        if whl:
            log(f"powerplatform: pip downloaded {whl.name}")
        return whl
    except Exception as e:  # noqa: BLE001
        warn(f"powerplatform: wheel download failed: {e}")
        return None


def parse_pp_template(template: str) -> tuple[str, list[str], set[str]]:
    """Split a Kiota URL template into (path, query parameter names, required query names).

    Required query parameters are written literally after api-version and optional ones in {&...}:
      /licensing/entitlements/{entitlementId}/users?api%2Dversion={api%2Dversion}&fromDate={fromDate}&toDate={toDate}{&pageSize*}
    """
    path = re.sub(r"\{[?&][^}]*\}", "", template)
    path = re.split(r"[?&]", path, maxsplit=1)[0]

    def clean(name: str) -> str:
        return urllib.request.unquote(name.replace("%2D", "-").replace("%24", "$"))

    required = [clean(x) for x in re.findall(r"[?&]([A-Za-z%0-9_]+)=\{", template)]
    optional: list[str] = []
    for qm in re.finditer(r"\{[?&]([^}]*)\}", template):
        optional += [clean(x.strip("*")) for x in qm.group(1).split(",")]
    names = [x for x in dict.fromkeys(required + optional) if x and "api" not in x.lower()]
    return path, names, {x for x in required if x in names}


def build_powerplatform(tmp: Path) -> list[dict[str, Any]]:
    whl = download_pp_wheel(tmp)
    if not whl:
        return []
    z = zipfile.ZipFile(whl)
    version = re.search(r"-(\d[\w.]*)-py3", whl.name)
    sdk_version = version.group(1) if version else ""
    builders = [n for n in z.namelist() if n.endswith("_request_builder.py")]
    ops: list[dict[str, Any]] = []
    av = SOURCES["powerplatform"]["api_version"]
    docs_base = SOURCES["powerplatform"]["docs_base"]
    for n in builders:
        src = z.read(n).decode("utf-8", "replace")
        m = re.search(r'"(\{\+baseurl\}[^"]*)"', src)
        if not m:
            continue
        template = m.group(1).replace("{+baseurl}", "")
        path, q_names, q_required = parse_pp_template(template)
        # descriptions from QueryParameters dataclass comments
        q_desc: dict[str, str] = {}
        for qm in re.finditer(r"#\s*(.+)\n\s+([a-z_0-9]+):\s*Optional\[", src):
            q_desc[qm.group(2)] = clip(qm.group(1), 140)
        path_params = [{"n": x, "in": "path", "req": True, "t": "string", "d": ""} for x in re.findall(r"\{([A-Za-z0-9_]+)\}", path)]
        ns = path.strip("/").split("/")[0]
        for mm in re.finditer(r"\n    async def (get|post|put|patch|delete)\(self(?:,\s*body:\s*([A-Za-z0-9_]+))?[^)]*\)[^:]*:\s*\n\s*\"\"\"\s*\n\s*(.*?)\n", src):
            method, body, summary = mm.group(1).upper(), mm.group(2), mm.group(3).strip()
            params = list(path_params)
            params.append({"n": "api-version", "in": "query", "req": True, "t": "string", "d": f"API version, default {av}"})
            for qn in q_names:
                key = qn.replace("$", "").replace("-", "_")
                params.append({"n": qn, "in": "query", "req": qn in q_required, "t": "string", "d": q_desc.get(key, q_desc.get(key.lower(), ""))})
            rec: dict[str, Any] = {
                "id": f"powerplatform:{method}:{path}", "svc": "powerplatform", "ver": "", "m": method, "p": path,
                "s": clip(summary, 160), "d": clip(summary, 300), "tags": [ns], "params": params, "av": av,
                "docs": f"{docs_base}{ns}", "rp": ns,
            }
            if body:
                rec["body"] = body
            if "deprecated" in summary.lower():
                rec["dep"] = True
                rec["depnote"] = "Marked deprecated in the Power Platform API SDK."
            ops.append(rec)
    log(f"powerplatform: {len(ops)} operations from SDK {sdk_version}")
    for o in ops:
        o["src"] = f"powerplatform-management {sdk_version}"
    return ops


# ---------------------------------------------------------------------------
# Fabric
# ---------------------------------------------------------------------------
def build_fabric() -> list[dict[str, Any]]:
    repo, branch = SOURCES["fabric"]["repo"], SOURCES["fabric"]["branch"]
    ops: list[dict[str, Any]] = []

    def work(folder: str) -> list[dict[str, Any]]:
        url = f"https://raw.githubusercontent.com/{repo}/{branch}/{folder}/swagger.json"
        try:
            doc = fetch_json(url)
        except Exception as e:  # noqa: BLE001
            warn(f"fabric: {folder} failed: {e}")
            return []
        base = doc.get("basePath") or ""
        out = []
        for path, method, op, path_params in iter_paths(doc):
            doc["_path_params"] = path_params
            rec = make_op("fabric", "", method, base.rstrip("/") + path, op, doc, docs=f"https://learn.microsoft.com/rest/api/fabric/{folder.lower()}")
            rec["tags"] = (rec.get("tags") or [])[:3] + [folder]
            out.append(rec)
        return out

    with ThreadPoolExecutor(max_workers=8) as ex:
        for fut in as_completed([ex.submit(work, f) for f in SOURCES["fabric"]["folders"]]):
            ops.extend(fut.result())
    log(f"fabric: {len(ops)} operations")
    return ops


# ---------------------------------------------------------------------------
# curated
# ---------------------------------------------------------------------------
def build_curated(out_dir: Path) -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = []
    for f in sorted((out_dir / "curated").glob("*.json")):
        try:
            data = json.loads(read_text(f))
            for op in data.get("operations", []):
                op.setdefault("ver", "")
                op.setdefault("tags", [])
                op.setdefault("params", [])
                op["id"] = op.get("id") or f"{op['svc']}:{op['m']}:{op['p']}"
                ops.append(op)
        except Exception as e:  # noqa: BLE001
            warn(f"curated {f.name}: {e}")
    log(f"curated: {len(ops)} operations")
    return ops


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def load_previous(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        with gzip.open(longpath(path), "rt", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:  # noqa: BLE001
        warn(f"previous catalog unreadable: {e}")
        return None


def source_of(op: dict[str, Any]) -> str:
    return "curated" if op.get("curated") else {"graph": "graph", "arm": "azure", "powerplatform": "powerplatform", "fabric": "fabric"}.get(op["svc"], "curated")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--sources", default="graph,azure,powerplatform,fabric,curated")
    ap.add_argument("--skip-beta", action="store_true")
    ap.add_argument("--previous", default="")
    args = ap.parse_args()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    wanted = {s.strip() for s in args.sources.split(",") if s.strip()}
    previous = load_previous(Path(args.previous) if args.previous else out_dir / "catalog.json.gz")
    prev_ops = previous.get("operations", []) if previous else []
    prev_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for op in prev_ops:
        prev_by_source[source_of(op)].append(op)

    started = time.time()
    results: dict[str, list[dict[str, Any]]] = {}
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        builders = {
            "graph": lambda: build_graph(args.skip_beta),
            "azure": build_azure,
            "powerplatform": lambda: build_powerplatform(tmp),
            "fabric": build_fabric,
            "curated": lambda: [dict(o, curated=True) for o in build_curated(out_dir)],
        }
        for name, fn in builders.items():
            if name not in wanted:
                if prev_by_source.get(name):
                    results[name] = prev_by_source[name]
                    log(f"{name}: skipped, kept {len(results[name])} previous operations")
                continue
            try:
                ops = fn()
            except Exception as e:  # noqa: BLE001
                warn(f"{name}: build failed: {e}")
                ops = []
            prev_n = len(prev_by_source.get(name, []))
            if not ops and prev_n:
                warn(f"{name}: no operations built; keeping previous {prev_n}")
                ops = prev_by_source[name]
            elif prev_n and len(ops) < prev_n * 0.7:
                warn(f"{name}: shrank from {prev_n} to {len(ops)} (>30%); keeping previous snapshot")
                ops = prev_by_source[name]
            if name == "graph" and args.skip_beta and prev_by_source.get(name):
                beta_prev = [o for o in prev_by_source[name] if o.get("ver") == "beta"]
                ops = [o for o in ops if o.get("ver") != "beta"] + beta_prev
                log(f"graph: kept {len(beta_prev)} previous beta operations")
            results[name] = ops

    merged: dict[str, dict[str, Any]] = {}
    for name in ("curated", "powerplatform", "fabric", "azure", "graph"):
        for op in results.get(name, []):
            merged.setdefault(op["id"], op)
    ops_all = sorted(merged.values(), key=lambda o: (o["svc"], o.get("ver", ""), o["p"], o["m"]))

    prev_ids = {o["id"]: o for o in prev_ops}
    new_ids = {o["id"]: o for o in ops_all}
    added = [{"id": i, "svc": o["svc"], "m": o["m"], "p": o["p"], "s": o.get("s", "")} for i, o in new_ids.items() if i not in prev_ids]
    removed = [{"id": i, "svc": o["svc"], "m": o["m"], "p": o["p"], "s": o.get("s", "")} for i, o in prev_ids.items() if i not in new_ids]
    generated = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    counts = Counter(o["svc"] for o in ops_all)
    sources_meta = {name: {"operations": len(ops)} for name, ops in results.items()}
    pp_src = next((o.get("src") for o in results.get("powerplatform", []) if o.get("src")), "")
    if pp_src:
        sources_meta["powerplatform"]["sdk"] = pp_src
    for o in ops_all:
        o.pop("src", None)
    meta = {"generated": generated, "builder_version": "1.0.0", "sources": sources_meta, "by_service": dict(counts), "warnings": WARNINGS,
            "added": len(added), "removed": len(removed), "duration_seconds": int(time.time() - started),
            "previous_generated": (previous or {}).get("generated", "")}

    catalog = {"generated": generated, "sources": sources_meta, "warnings": WARNINGS, "operations": ops_all}
    with gzip.open(longpath(out_dir / "catalog.json.gz"), "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump(catalog, f, separators=(",", ":"), ensure_ascii=False)
    write_text(out_dir / "catalog_meta.json", json.dumps(meta, indent=2))

    changelog_path = out_dir / "changelog.json"
    changelog = {"entries": []}
    if changelog_path.exists():
        try:
            changelog = json.loads(read_text(changelog_path))
        except Exception:  # noqa: BLE001
            changelog = {"entries": []}
    if previous is not None and (added or removed):
        changelog["entries"].insert(0, {"generated": generated, "previous": previous.get("generated", ""), "added_count": len(added), "removed_count": len(removed),
                                        "added": added[:2000], "removed": removed[:2000]})
    elif previous is None:
        changelog["entries"].insert(0, {"generated": generated, "previous": "", "added_count": len(added), "removed_count": 0, "added": [], "removed": [],
                                        "note": "initial build"})
    changelog["entries"] = changelog["entries"][:120]
    write_text(changelog_path, json.dumps(changelog, separators=(",", ":"), ensure_ascii=False))

    size = (out_dir / "catalog.json.gz").stat().st_size
    log(f"done: {len(ops_all)} operations ({dict(counts)}), +{len(added)} -{len(removed)}, {size / 1e6:.2f} MB gz, {int(time.time() - started)}s, {len(WARNINGS)} warnings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
