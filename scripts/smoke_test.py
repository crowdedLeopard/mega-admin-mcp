#!/usr/bin/env python3
"""Smoke test a running Mega Admin MCP endpoint over Streamable HTTP (JSON mode).

  python scripts/smoke_test.py --url http://127.0.0.1:8000/mcp
  python scripts/smoke_test.py --url https://<app>.azurewebsites.net/mcp --bearer <token>
  python scripts/smoke_test.py --url https://<app>.azurewebsites.net/mcp --api-key <key>
  add --live to also call tools that hit Microsoft APIs (whoami works offline, doctor and finops_subscriptions need credentials)
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request


def rpc(url: str, method: str, params: dict | None, headers: dict[str, str], rid: int = 1) -> dict:
    body = json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, **({"params": params} if params is not None else {})}).encode()
    req = urllib.request.Request(url, data=body, method="POST", headers={"Content-Type": "application/json", "Accept": "application/json, text/event-stream", **headers})
    with urllib.request.urlopen(req, timeout=180) as r:
        raw = r.read().decode("utf-8")
    if raw.startswith("event:") or "\ndata:" in raw:
        data_lines = [l[5:].strip() for l in raw.splitlines() if l.startswith("data:")]
        raw = data_lines[-1] if data_lines else raw
    return json.loads(raw)


def call_tool(url: str, name: str, args: dict, headers: dict[str, str]) -> dict:
    res = rpc(url, "tools/call", {"name": name, "arguments": args}, headers, rid=int(time.time() * 1000) % 100000)
    if "error" in res:
        return {"_rpc_error": res["error"]}
    content = res["result"].get("content", [])
    text = content[0].get("text", "") if content else ""
    try:
        return json.loads(text)
    except Exception:  # noqa: BLE001
        return {"_text": text, "is_error": res["result"].get("isError")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000/mcp")
    ap.add_argument("--bearer", default="")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--api-key-header", default="x-api-key")
    ap.add_argument("--live", action="store_true")
    a = ap.parse_args()
    headers: dict[str, str] = {}
    if a.bearer:
        headers["Authorization"] = f"Bearer {a.bearer}"
    if a.api_key:
        headers[a.api_key_header] = a.api_key

    failures = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        print(f"[{'PASS' if ok else 'FAIL'}] {label} {detail}")
        if not ok:
            failures += 1

    init = rpc(a.url, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "smoke", "version": "1"}}, headers)
    check("initialize", "result" in init, json.dumps(init.get("result", init.get("error")))[:160])
    tools = rpc(a.url, "tools/list", None, headers, rid=2)
    names = [t["name"] for t in tools.get("result", {}).get("tools", [])]
    check("tools/list", len(names) >= 20, f"{len(names)} tools")
    bad = []
    for t in tools.get("result", {}).get("tools", []):
        s = json.dumps(t.get("inputSchema", {}))
        if "$ref" in s or "anyOf" in s or "oneOf" in s:
            bad.append(t["name"])
    check("Copilot Studio schema constraints", not bad, f"offending: {bad}" if bad else "no $ref/anyOf/oneOf")

    who = call_tool(a.url, "whoami", {}, headers)
    check("whoami", who.get("ok") is True, str(who.get("caller", who))[:160])
    srch = call_tool(a.url, "search_operations", {"query": "copilot credits allocation", "limit": 5}, headers)
    check("search_operations", srch.get("ok") is True and srch.get("count", 0) > 0, f"{srch.get('count')} results, top: {srch.get('results', [{}])[0].get('id') if srch.get('results') else None}")
    if srch.get("results"):
        d = call_tool(a.url, "describe_operation", {"operation_id": srch["results"][0]["id"]}, headers)
        check("describe_operation", d.get("ok") is True, d.get("path", ""))
    st = call_tool(a.url, "catalog_status", {}, headers)
    check("catalog_status", st.get("ok") is True, f"{st.get('operations')} operations from {st.get('loaded_from')}")
    prev = call_tool(a.url, "invoke", {"service": "graph", "method": "DELETE", "path": "/v1.0/users/00000000-0000-0000-0000-000000000000"}, headers)
    check("write preview without confirm", prev.get("status") == "preview" and prev.get("needs_confirm") is True, prev.get("reason", ""))

    if a.live:
        doc = call_tool(a.url, "doctor", {}, headers)
        check("doctor (live)", "token_checks" in doc, json.dumps(doc.get("advice", doc))[:200])
        subs = call_tool(a.url, "finops_subscriptions", {}, headers)
        check("finops_subscriptions (live)", subs.get("ok") is True, f"{subs.get('count')} subscriptions")
        cc = call_tool(a.url, "finops_copilot_credits_overview", {}, headers)
        check("finops_copilot_credits_overview (live)", cc.get("ok") is True, json.dumps(cc.get("copilot_credits"))[:200])

    print(f"\n{'ALL PASSED' if not failures else f'{failures} FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
