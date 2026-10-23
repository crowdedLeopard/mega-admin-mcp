"""ASGI application: auth middleware, health endpoints, connector definition, MCP mount.

Run locally:   python -m megamcp.app
App Service:   startup command  python -m megamcp.app   (listens on $PORT, default 8000)
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route

from mcp.server.transport_security import TransportSecuritySettings

from . import __version__
from .auth.entra import AuthError, principal_from_request_headers, protected_resource_metadata, set_principal
from .catalog.store import catalog
from .config import settings
from .runbook.invoke import runbook
from .server import create_server

logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO), format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("megamcp.app")

mcp = create_server()
mcp_asgi = mcp.streamable_http_app(
    streamable_http_path=settings.mcp_path,
    json_response=True,
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


class AuthMiddleware:
    """Validate the caller on every MCP request and expose the principal to tools via a contextvar."""

    def __init__(self, app):  # type: ignore[no-untyped-def]
        self.app = app

    async def __call__(self, scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] != "http" or not scope.get("path", "").startswith(settings.mcp_path):
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        try:
            principal = principal_from_request_headers(headers)
        except AuthError as e:
            body = json.dumps({"error": "unauthorized", "message": str(e), **e.detail}).encode()
            www = 'Bearer realm="mega-admin-mcp"'
            if settings.public_base_url:
                www += f', resource_metadata="{settings.public_base_url}/.well-known/oauth-protected-resource"'
            await send({"type": "http.response.start", "status": e.status, "headers": [
                (b"content-type", b"application/json"), (b"www-authenticate", www.encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        token = set_principal(principal)
        try:
            await self.app(scope, receive, send)
        finally:
            with contextlib.suppress(Exception):
                set_principal(None)
                del token


async def health(_: Request) -> Response:
    return JSONResponse({"status": "ok", "version": __version__, "time": int(time.time())})


async def ready(_: Request) -> Response:
    problems = settings.validate()
    ok = bool(catalog.ops) and not problems
    return JSONResponse({"status": "ready" if ok else "degraded", "catalog_operations": len(catalog.ops),
                         "catalog_loaded_from": catalog.source, "configuration_problems": problems,
                         "auth_mode": settings.auth_mode, "version": __version__}, status_code=200 if ok else 503)


async def prm(_: Request) -> Response:
    return JSONResponse(protected_resource_metadata())


async def connector_yaml(request: Request) -> Response:
    host = (settings.public_base_url or str(request.base_url).rstrip("/")).replace("https://", "").replace("http://", "")
    yaml_text = f"""swagger: '2.0'
info:
  title: Mega Admin MCP
  description: Any Microsoft admin REST call with FinOps and AI tokenomics built in. Self-updating, self-healing.
  version: '{__version__}'
host: {host}
basePath: /
schemes:
  - https
consumes:
  - application/json
produces:
  - application/json
paths:
  {settings.mcp_path}:
    post:
      summary: Mega Admin MCP server
      x-ms-agentic-protocol: mcp-streamable-1.0
      operationId: InvokeMCP
      responses:
        '200':
          description: Success
securityDefinitions:
  oauth2-auth:
    type: oauth2
    flow: accessCode
    authorizationUrl: https://login.microsoftonline.com/{settings.tenant_id or 'common'}/oauth2/v2.0/authorize
    tokenUrl: https://login.microsoftonline.com/{settings.tenant_id or 'common'}/oauth2/v2.0/token
    scopes:
      api://{settings.client_id or '<client-id>'}/.default: Access Mega Admin MCP as the signed-in admin
security:
  - oauth2-auth:
      - api://{settings.client_id or '<client-id>'}/.default
"""
    return PlainTextResponse(yaml_text, media_type="application/x-yaml")


async def index(_: Request) -> Response:
    return JSONResponse({
        "name": "Mega Admin MCP", "version": __version__, "mcp_endpoint": settings.mcp_path, "auth_mode": settings.auth_mode,
        "health": "/health", "ready": "/ready", "connector_definition": "/connector.yaml",
        "oauth_protected_resource": "/.well-known/oauth-protected-resource",
        "docs": "https://github.com/crowdedLeopard/mega-admin-mcp",
    })


@contextlib.asynccontextmanager
async def lifespan(app: Starlette):
    problems = settings.validate()
    for p in problems:
        log.error("configuration: %s", p)
    catalog.load_bundled()
    tasks: list[asyncio.Task[Any]] = []
    if settings.catalog_auto_refresh:
        tasks.append(asyncio.create_task(catalog.refresh_loop()))
    async with mcp.session_manager.run():
        log.info("Mega Admin MCP %s ready on %s (auth_mode=%s, %d catalog operations)", __version__, settings.mcp_path, settings.auth_mode, len(catalog.ops))
        try:
            yield
        finally:
            for t in tasks:
                t.cancel()
            await runbook.aclose()


app = Starlette(
    routes=[
        Route("/", index),
        Route("/health", health),
        Route("/ready", ready),
        Route("/.well-known/oauth-protected-resource", prm),
        Route("/.well-known/oauth-protected-resource" + settings.mcp_path, prm),
        Route("/connector.yaml", connector_yaml),
        Mount("/", app=AuthMiddleware(mcp_asgi)),
    ],
    lifespan=lifespan,
)


def main() -> None:
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level.lower(), proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
