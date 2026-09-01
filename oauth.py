"""Stateless OAuth 2.1 authorization server for this MCP resource server.

Ported from deeporax.com (src/lib/mcp/auth.ts). Authorization codes, access
tokens and refresh tokens are self-contained HS256 JWTs, so there is nothing to
store. The human gate on the consent screen is a single password
(MCP_OAUTH_PASSWORD, falling back to MCP_AUTH_TOKEN).
"""

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

# compose, the Caddyfile and src/http_app.py all set DOMAIN; MCP_DOMAIN is
# the name the upstream fork used, kept so an existing deploy keeps working.
DOMAIN = (os.getenv("DOMAIN") or os.getenv("MCP_DOMAIN") or "").strip().lower()
ISSUER = f"https://{DOMAIN}"
RESOURCE = f"https://{DOMAIN}/mcp"
SCOPES = ["mcp:tools"]

CODE_TTL = 5 * 60
ACCESS_TTL = 60 * 60
REFRESH_TTL = 30 * 24 * 60 * 60

CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "*",
    "Cache-Control": "no-store",
}


def _secret() -> str | None:
    explicit = (os.getenv("MCP_OAUTH_SECRET") or "").strip()
    if explicit:
        return explicit
    token = (os.getenv("MCP_AUTH_TOKEN") or "").strip()
    return f"oauth:{token}" if token else None


def _password() -> str:
    return (os.getenv("MCP_OAUTH_PASSWORD") or os.getenv("MCP_AUTH_TOKEN") or "").strip()


def configured() -> bool:
    return bool(DOMAIN and _secret())


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _b64d(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _sign(payload: dict, secret: str) -> str:
    header = _b64e(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64e(json.dumps(payload, separators=(",", ":")).encode())
    data = f"{header}.{body}"
    sig = _b64e(hmac.new(secret.encode(), data.encode(), hashlib.sha256).digest())
    return f"{data}.{sig}"


def _verify(token: str, secret: str) -> dict | None:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    header, body, sig = parts
    expected = hmac.new(secret.encode(), f"{header}.{body}".encode(), hashlib.sha256).digest()
    try:
        actual = _b64d(sig)
        payload = json.loads(_b64d(body))
    except Exception:
        return None
    if not hmac.compare_digest(expected, actual):
        return None
    if not isinstance(payload, dict):
        return None
    exp = payload.get("exp")
    if isinstance(exp, int) and exp < int(time.time()):
        return None
    return payload


def _pkce_s256(verifier: str) -> str:
    return _b64e(hashlib.sha256(verifier.encode()).digest())


def is_allowed_redirect_uri(uri: str) -> bool:
    """Redirect URIs the MCP hosts we support actually use."""
    try:
        parts = urlsplit(uri)
    except Exception:
        return False

    if parts.scheme in ("cursor", "vscode", "vscode-insiders"):
        return True
    if parts.scheme not in ("http", "https"):
        return False

    host = (parts.hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "::1"):
        return True
    if parts.scheme != "https":
        return False
    if host in ("claude.ai", "claude.com", "chatgpt.com"):
        return True
    if host.endswith((".claude.ai", ".claude.com", ".chatgpt.com")):
        return True
    return bool(DOMAIN) and host == DOMAIN


def verify_access_token(token: str) -> bool:
    secret = _secret()
    if not secret:
        return False
    payload = _verify(token, secret)
    return bool(payload and payload.get("typ") == "mcp_access")


def _issue_tokens(client_id: str, scope: str) -> dict:
    secret = _secret()
    now = int(time.time())
    common = {"cid": client_id, "scp": scope, "res": RESOURCE, "iat": now}
    access = _sign({**common, "typ": "mcp_access", "exp": now + ACCESS_TTL, "jti": secrets.token_hex(8)}, secret)
    refresh = _sign({**common, "typ": "mcp_refresh", "exp": now + REFRESH_TTL, "jti": secrets.token_hex(8)}, secret)
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": ACCESS_TTL,
        "refresh_token": refresh,
        "scope": scope,
    }


def _esc(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def _with_query(uri: str, extra: dict) -> str:
    parts = urlsplit(uri)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query.update(extra)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _page(title: str, body: str, status: int = 200) -> HTMLResponse:
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/><title>{_esc(title)}</title>
<style>
:root {{ color-scheme: dark; font-family: ui-sans-serif, system-ui, sans-serif; }}
body {{ margin:0; min-height:100vh; display:grid; place-items:center; background:#0a0a0a; color:#f5f5f5; }}
main {{ width:min(420px,92vw); border:1px solid #2a2a2a; border-radius:12px; padding:28px; background:#121212; }}
h1 {{ font-size:1.25rem; margin:0 0 8px; }}
p {{ margin:0 0 16px; color:#a3a3a3; line-height:1.5; font-size:.95rem; }}
input[type=password] {{ width:100%; box-sizing:border-box; padding:12px 14px; margin:0 0 12px; border-radius:8px;
  border:1px solid #2a2a2a; background:#0a0a0a; color:#f5f5f5; font-size:.95rem; }}
button, a.btn {{ display:flex; align-items:center; justify-content:center; width:100%; padding:12px 16px;
  border-radius:8px; border:none; font-weight:600; font-size:.95rem; cursor:pointer; text-decoration:none;
  box-sizing:border-box; }}
button.primary {{ background:#c8f542; color:#0a0a0a; }}
a.secondary {{ background:transparent; color:#a3a3a3; border:1px solid #2a2a2a; margin-top:10px; }}
.err {{ color:#fca5a5; }}
code {{ font-size:.85em; word-break:break-all; }}
</style></head><body><main>{body}</main></body></html>"""
    return HTMLResponse(html, status_code=status, headers={"Cache-Control": "no-store"})


def _parse_authorize(params) -> dict:
    client_id = params.get("client_id", "")
    redirect_uri = params.get("redirect_uri", "")
    challenge = params.get("code_challenge", "")
    method = params.get("code_challenge_method", "S256")

    if not client_id:
        return {"error": "client_id required"}
    if not redirect_uri:
        return {"error": "redirect_uri required"}
    if not is_allowed_redirect_uri(redirect_uri):
        return {"error": "redirect_uri not allowed"}
    if params.get("response_type", "") != "code":
        return {"error": "response_type must be code"}
    if not challenge:
        return {"error": "code_challenge required (PKCE)"}
    if method != "S256":
        return {"error": "code_challenge_method must be S256"}

    return {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": params.get("state") or "",
        "code_challenge": challenge,
        "scope": params.get("scope") or " ".join(SCOPES),
        "client_name": params.get("client_name") or client_id,
    }


def _consent(q: dict, error: str = "", status: int = 200) -> HTMLResponse:
    deny = {"error": "access_denied"}
    if q["state"]:
        deny["state"] = q["state"]
    hidden = "".join(
        f'<input type="hidden" name="{k}" value="{_esc(q[k])}"/>'
        for k in ("client_id", "redirect_uri", "state", "code_challenge", "scope")
    )
    warning = f'<p class="err">{_esc(error)}</p>' if error else ""
    return _page(
        "Authorize Grok MCP",
        f"""<h1>Connect Grok MCP</h1>
<p>Allow <strong>{_esc(q["client_name"])}</strong> to use the tools on <code>{_esc(RESOURCE)}</code>.</p>
{warning}
<form method="post" action="/oauth/authorize">
{hidden}
<input type="password" name="password" placeholder="Server password" autofocus required autocomplete="current-password"/>
<button class="primary" type="submit" name="decision" value="allow">Allow</button>
</form>
<a class="btn secondary" href="{_esc(_with_query(q["redirect_uri"], deny))}">Deny</a>""",
        status,
    )


def _preflight() -> Response:
    return Response(status_code=204, headers={**CORS, "Access-Control-Max-Age": "86400"})


def _unavailable() -> JSONResponse:
    return JSONResponse(
        {"error": "server_error", "error_description": "MCP OAuth is not configured"},
        status_code=503,
        headers=CORS,
    )


async def as_metadata(request: Request) -> Response:
    if request.method == "OPTIONS":
        return _preflight()
    if not configured():
        return _unavailable()
    return JSONResponse(
        {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/oauth/authorize",
            "token_endpoint": f"{ISSUER}/oauth/token",
            "registration_endpoint": f"{ISSUER}/oauth/register",
            "revocation_endpoint": f"{ISSUER}/oauth/revoke",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": SCOPES,
        },
        headers=CORS,
    )


async def pr_metadata(request: Request) -> Response:
    if request.method == "OPTIONS":
        return _preflight()
    if not configured():
        return _unavailable()
    return JSONResponse(
        {
            "resource": RESOURCE,
            "authorization_servers": [ISSUER],
            "bearer_methods_supported": ["header"],
            "scopes_supported": SCOPES,
        },
        headers=CORS,
    )


async def register(request: Request) -> Response:
    if request.method == "OPTIONS":
        return _preflight()
    if not configured():
        return _unavailable()

    try:
        body = await request.json()
    except Exception:
        body = None
    if not isinstance(body, dict):
        return JSONResponse(
            {"error": "invalid_client_metadata", "error_description": "Body must be JSON"},
            status_code=400,
            headers=CORS,
        )

    raw = body.get("redirect_uris")
    uris = [u for u in raw if isinstance(u, str)] if isinstance(raw, list) else []
    if not uris:
        return JSONResponse(
            {"error": "invalid_client_metadata", "error_description": "redirect_uris required"},
            status_code=400,
            headers=CORS,
        )
    if not all(is_allowed_redirect_uri(u) for u in uris):
        return JSONResponse(
            {"error": "invalid_client_metadata", "error_description": "One or more redirect_uris are not allowed"},
            status_code=400,
            headers=CORS,
        )

    return JSONResponse(
        {
            "client_id": f"mcp_{secrets.token_hex(16)}",
            "client_id_issued_at": int(time.time()),
            "client_secret_expires_at": 0,
            "redirect_uris": uris,
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "scope": " ".join(SCOPES),
        },
        status_code=201,
        headers=CORS,
    )


async def authorize(request: Request) -> Response:
    if request.method == "OPTIONS":
        return _preflight()
    if not configured():
        return _page("Unavailable", "<h1>MCP OAuth is not configured</h1>", 503)

    if request.method == "GET":
        parsed = _parse_authorize(request.query_params)
        if "error" in parsed:
            return _page("Authorization error", f'<h1>Cannot authorize</h1><p class="err">{_esc(parsed["error"])}</p>', 400)
        return _consent(parsed)

    form = await request.form()
    parsed = _parse_authorize({**dict(form), "response_type": "code", "code_challenge_method": "S256"})
    if "error" in parsed:
        return _page("Authorization error", f'<h1>Cannot authorize</h1><p class="err">{_esc(parsed["error"])}</p>', 400)

    if form.get("decision") != "allow":
        deny = {"error": "access_denied"}
        if parsed["state"]:
            deny["state"] = parsed["state"]
        return RedirectResponse(_with_query(parsed["redirect_uri"], deny), status_code=302)

    expected = _password()
    supplied = str(form.get("password") or "")
    if not expected or not hmac.compare_digest(supplied, expected):
        return _consent(parsed, "Wrong password.", 401)

    now = int(time.time())
    code = _sign(
        {
            "typ": "mcp_code",
            "cid": parsed["client_id"],
            "ru": parsed["redirect_uri"],
            "cc": parsed["code_challenge"],
            "scp": parsed["scope"],
            "res": RESOURCE,
            "iat": now,
            "exp": now + CODE_TTL,
            "jti": secrets.token_hex(8),
        },
        _secret(),
    )
    target = {"code": code}
    if parsed["state"]:
        target["state"] = parsed["state"]
    return RedirectResponse(_with_query(parsed["redirect_uri"], target), status_code=302)


async def token(request: Request) -> Response:
    if request.method == "OPTIONS":
        return _preflight()
    if not configured():
        return _unavailable()

    if "application/json" in request.headers.get("content-type", ""):
        try:
            body = await request.json()
        except Exception:
            body = {}
        params = {k: str(v) for k, v in body.items() if v is not None} if isinstance(body, dict) else {}
    else:
        params = dict(await request.form())

    def fail(error: str, description: str, status: int = 400) -> JSONResponse:
        return JSONResponse({"error": error, "error_description": description}, status_code=status, headers=CORS)

    grant = params.get("grant_type", "")
    client_id = params.get("client_id", "")
    if not client_id:
        return fail("invalid_client", "client_id required", 401)

    if grant == "authorization_code":
        code = params.get("code", "")
        redirect_uri = params.get("redirect_uri", "")
        verifier = params.get("code_verifier", "")
        if not code or not redirect_uri or not verifier:
            return fail("invalid_request", "code, redirect_uri and code_verifier required")

        payload = _verify(code, _secret())
        if not payload or payload.get("typ") != "mcp_code":
            return fail("invalid_grant", "Invalid or expired authorization code")
        if payload.get("cid") != client_id or payload.get("ru") != redirect_uri:
            return fail("invalid_grant", "Invalid or expired authorization code")
        if not hmac.compare_digest(_pkce_s256(verifier), str(payload.get("cc", ""))):
            return fail("invalid_grant", "Invalid or expired authorization code")

        return JSONResponse(_issue_tokens(client_id, str(payload.get("scp") or " ".join(SCOPES))), headers=CORS)

    if grant == "refresh_token":
        payload = _verify(params.get("refresh_token", ""), _secret())
        if not payload or payload.get("typ") != "mcp_refresh":
            return fail("invalid_grant", "Invalid or expired refresh token")
        return JSONResponse(
            _issue_tokens(str(payload.get("cid") or client_id), str(payload.get("scp") or " ".join(SCOPES))),
            headers=CORS,
        )

    return fail("unsupported_grant_type", "Use authorization_code or refresh_token")


async def revoke(request: Request) -> Response:
    if request.method == "OPTIONS":
        return _preflight()
    return Response(status_code=200, headers=CORS)


ROUTES = [
    ("/.well-known/oauth-authorization-server", ["GET", "OPTIONS"], as_metadata, "oauth_as_meta"),
    ("/.well-known/oauth-authorization-server/mcp", ["GET", "OPTIONS"], as_metadata, "oauth_as_meta_mcp"),
    ("/.well-known/oauth-protected-resource", ["GET", "OPTIONS"], pr_metadata, "oauth_pr_meta"),
    ("/.well-known/oauth-protected-resource/mcp", ["GET", "OPTIONS"], pr_metadata, "oauth_pr_meta_mcp"),
    ("/oauth/register", ["POST", "OPTIONS"], register, "oauth_register"),
    ("/register", ["POST", "OPTIONS"], register, "oauth_register_root"),
    ("/oauth/authorize", ["GET", "POST", "OPTIONS"], authorize, "oauth_authorize"),
    ("/oauth/token", ["POST", "OPTIONS"], token, "oauth_token"),
    ("/oauth/revoke", ["POST", "OPTIONS"], revoke, "oauth_revoke"),
]
