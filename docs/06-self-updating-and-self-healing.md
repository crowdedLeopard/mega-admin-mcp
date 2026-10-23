# Self-updating and self-healing

Microsoft ships API changes every month. Graph beta changes every week. A static list of operations would be wrong within days, and an MCP server that falls over when a token expires is worse than no server. These two properties were requirements from the start, so here is exactly how they work and where the limits are.

## Self-updating: the catalog pipeline

The server does not hard-code operations. It loads a catalog, and the catalog is rebuilt every day from Microsoft's own published descriptions:

| Source | What we read | How often it changes upstream |
| --- | --- | --- |
| Microsoft Graph | `openapi.yaml` for v1.0 and beta from microsoftgraph/msgraph-metadata, plus the permissions map from microsoft-graph-devx-content | weekly |
| Azure Resource Manager | Azure/azure-rest-api-specs: for each resource provider we care about, the newest stable (and newer preview) swagger files, discovered through the GitHub trees API | daily |
| Power Platform API | Microsoft's Kiota generated SDK on PyPI (`powerplatform-management`). Every request builder carries the URL template, methods, query parameters and descriptions, and Microsoft publishes it monthly, so it is the most reliable machine-readable description of that API | monthly |
| Microsoft Fabric | microsoft/fabric-rest-api-specs swagger per workload | monthly |
| Curated | `src/megamcp/data/curated/*.json` for APIs with no public spec: Dataverse Web API admin tables, the legacy BAP, Power Apps and Flow admin endpoints, Power BI admin, Office 365 Management Activity, Azure Retail Prices | when we edit them |

[scripts/build_catalog.py](../scripts/build_catalog.py) runs in GitHub Actions at 04:17 UTC ([.github/workflows/catalog-daily.yml](../.github/workflows/catalog-daily.yml)). It:

1. Downloads and parses each source (about two minutes end to end).
2. Diffs against the previous catalog and records added and removed operations in `changelog.json`.
3. Refuses to ship a shrunken source. If a source fails, or returns fewer than 70% of the operations it had yesterday, the previous snapshot of that source is kept and a warning is recorded. A flaky upstream can never break your server.
4. Runs the test suite against the new catalog.
5. Commits the catalog and publishes it as the latest GitHub release with `catalog.json.gz`, `changelog.json` and `catalog_meta.json` as assets.
6. Opens or updates a GitHub issue labelled `catalog-warning` when a source misbehaved.

At runtime the server starts from the catalog bundled in the package, then every `CATALOG_REFRESH_HOURS` (default 24) downloads the latest release (`CATALOG_URL`, with ETag so unchanged files cost nothing). It applies the same 30% shrink guard before swapping, and never replaces a newer catalog with an older one. `catalog_status` shows what is loaded, from where, and the last refresh result; `catalog_status refresh_now=true` pulls immediately; `catalog_changes days=7` lists what Microsoft added or removed.

What this does not do: it does not change the 32 tool definitions. Copilot Studio reads the tool list when you add the server and the tools are stable on purpose. New Microsoft operations arrive through `search_operations` and `invoke`, which is exactly where you want them.

If you fork the repository, change `CATALOG_URL` and `CHANGELOG_URL` to your fork's releases. If you would rather not pull from the internet at all, set `CATALOG_AUTO_REFRESH=false` and redeploy when you want a new catalog; the package always ships the latest one at build time.

## Self-healing: in the request path

Every call goes through one executor, so every tool gets the same behaviour:

- **Token refresh**: a 401 clears the cached token for that user and service and retries once.
- **Throttling**: 429, 503 and 504 are retried with the Retry-After the service asks for (capped at 60 seconds), with exponential backoff and jitter otherwise, up to `MAX_RETRIES`.
- **Transient server errors**: other 5xx are retried twice.
- **Api-version drift**: Azure and the Power Platform API both tell you which versions they support when you send a wrong one. The executor parses that message and retries with the newest supported version. When you leave api-version out for Azure, it is chosen from the catalog (newest stable for that path or resource provider).
- **Graph beta fallback**: a GET that fails on v1.0 for a path that the catalog says only exists in beta is retried on beta.
- **Circuit breaker**: five consecutive network failures to a host open a 30 second breaker so a dead endpoint fails fast instead of eating the agent's time.
- **Deprecation warnings**: operations the specs mark deprecated carry a warning in the response so the agent can tell you.
- **Explanations**: a failed call returns `hints` (what it means for this service and status), `suggested_operations` (closest catalog paths) and, for Graph 403s, the permissions the operation needs. A consent problem returns the admin consent URL.

## Self-healing: around the process

- `/health` is a liveness probe, `/ready` checks configuration and catalog. The Bicep template sets App Service's health check path to `/health`, so unhealthy instances are replaced, and turns on auto-heal: twenty 5xx in five minutes or thirty requests slower than two minutes recycle the process.
- The server is stateless (JSON responses, no sessions), so you can scale out or restart it any time. Token caches are rebuilt on demand.
- `doctor` runs live checks: configuration, a token per downstream API for the signed-in admin, a real call to Graph, Azure and the Power Platform API, catalog freshness, open circuit breakers and audit statistics, and ends with advice in plain words.
- The daily workflow runs the tests against the fresh catalog before publishing it, and CI smoke tests the MCP endpoint on every push.

## Limits, honestly

- The catalog describes what Microsoft publishes. If an API has no public spec and we have not curated it, `search_operations` will not know it. You can still call it with `invoke` by giving the path, as long as the host is one of the known services.
- The catalog cannot tell you whether an operation is enabled for your tenant or region. The Copilot Studio licensing report endpoint is a good example: documented and in the catalog, but returning 404 in my tenant in October 2026.
- Self-healing does not retry writes blindly. A write that failed with a 5xx is retried only when the service said nothing happened (it did not return 2xx). Check `audit_recent` if in doubt.
