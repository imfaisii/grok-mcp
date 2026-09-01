import hmac
import os
import re
import sys

import uvicorn
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse

from mcp.server.transport_security import TransportSecuritySettings

import oauth
from files import FILES_DIR


def transport_security_settings():
    """Build the DNS-rebinding allowlist from env, for the FastMCP constructor."""
    port = os.getenv("MCP_PORT", "8000")
    https_port = os.getenv("HTTPS_PORT", "443")
    domain = os.getenv("DOMAIN", "")
    allowed_hosts = ["localhost", f"localhost:{port}", "127.0.0.1", f"127.0.0.1:{port}"]
    allowed_origins = []
    if domain:
        # The proxy forwards the original Host, port included. When HTTPS runs
        # on a non-standard port that port is part of the header, so allow it.
        allowed_hosts += [domain, f"{domain}:443", f"{domain}:80", f"{domain}:{https_port}"]
        allowed_origins += [
            f"https://{domain}",
            f"http://{domain}",
            f"https://{domain}:{https_port}",
        ]
    return TransportSecuritySettings(allowed_hosts=allowed_hosts, allowed_origins=allowed_origins)


def _consume_secret_prefix(scope, secret):
    """If the path starts with /<secret>, strip it and report a match.

    Lets clients that cannot send an Authorization header (claude.ai custom
    connectors have no field for one) authenticate by URL instead. Stripping
    it in place also keeps the secret out of uvicorn's access log, which
    formats the path after the request completes.
    """
    parts = scope.get("path", "").split("/", 2)
    if len(parts) < 2 or not hmac.compare_digest(parts[1], secret):
        return False
    rest = parts[2] if len(parts) > 2 else ""
    scope["path"] = "/" + rest
    if scope.get("raw_path"):
        scope["raw_path"] = ("/" + rest).encode()
    return True


class BearerAuthMiddleware:
    """Raw ASGI middleware, not BaseHTTPMiddleware: MCP streams Server-Sent
    Events, and BaseHTTPMiddleware buffers/breaks long-lived streams."""

    def __init__(self, app, token, url_secret="", mcp_path="/mcp"):
        self.app = app
        self.expected = f"Bearer {token}".encode()
        self.url_secret = url_secret
        self.mcp_path = mcp_path

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        if self.url_secret and _consume_secret_prefix(scope, self.url_secret):
            return await self.app(scope, receive, send)
        # Guard the MCP endpoint only; every other path falls through to the
        # app's own 404. /.well-known/*, /register and /oauth/* are oauth.py's
        # discovery and client-registration flow, which claude.ai connectors
        # probe before they ever hold a bearer token, so gating them here
        # would break the handshake instead of just the plain-bearer path.
        # /healthz and /files/* are meant to be public too.
        path = scope.get("path", "")
        if path != self.mcp_path and not path.startswith(self.mcp_path + "/"):
            return await self.app(scope, receive, send)
        provided = b""
        for name, value in scope["headers"]:
            if name == b"authorization":
                provided = value
                break
        # Compare bytes: a non-ASCII header would make the str form raise.
        if hmac.compare_digest(provided, self.expected):
            return await self.app(scope, receive, send)
        header = provided.decode("latin-1", "ignore")
        bearer = header[7:] if header.lower().startswith("bearer ") else ""
        if bearer and oauth.verify_access_token(bearer):
            return await self.app(scope, receive, send)
        response = JSONResponse(
            {"error": "unauthorized"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
        return await response(scope, receive, send)


async def healthz(request):
    return PlainTextResponse("ok")


async def get_file(request):
    name = request.path_params["name"]
    if not re.fullmatch(r"[0-9a-f-]{36}\.png", name):
        return PlainTextResponse("not found", status_code=404)
    path = FILES_DIR / name
    if not path.exists():
        return PlainTextResponse("not found", status_code=404)
    return FileResponse(path, media_type="image/png")


def build_app(mcp):
    # custom_route only takes effect if it runs before streamable_http_app()
    # reads mcp._custom_starlette_routes to build the Starlette app below.
    for path, methods, handler, name in oauth.ROUTES:
        mcp.custom_route(path, methods=methods, name=name)(handler)
    app = mcp.streamable_http_app()
    app.add_route("/healthz", healthz)
    app.add_route("/files/{name}", get_file)
    app.add_middleware(
        BearerAuthMiddleware,
        token=os.getenv("MCP_AUTH_TOKEN", ""),
        url_secret=os.getenv("MCP_URL_SECRET", ""),
        mcp_path=os.getenv("MCP_PATH", "/mcp"),
    )
    return app


def run_http(mcp):
    if not os.getenv("MCP_AUTH_TOKEN"):
        print("MCP_AUTH_TOKEN is required when MCP_TRANSPORT=http", file=sys.stderr)
        sys.exit(1)
    app = build_app(mcp)
    uvicorn.run(app, host=os.getenv("MCP_HOST", "0.0.0.0"), port=int(os.getenv("MCP_PORT", "8000")))
