# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Uses `uv` (not pip/poetry). Python >= 3.11.

```bash
uv sync                    # install deps from uv.lock
uv run python main.py      # run the server over stdio
mcp dev main.py            # MCP Inspector
docker compose up --build  # containerized stdio server
```

Checks the CI runs (`.github/workflows/ci.yml`, Python 3.11 and 3.12) — mirror these locally:

```bash
uv run python -m compileall -q src main.py
XAI_API_KEY=dummy-ci-key uv run python -c "import src.server"
```

There is no test suite, linter, or formatter in this repo. CI is byte-compile plus an import smoke test only.

## Architecture

Single-module MCP server. `main.py` checks for `XAI_API_KEY`, then calls `src.server.main()`, which branches on `MCP_TRANSPORT`: `stdio` (the default) or `http`. All 22 tools live in [src/server.py](src/server.py) as `@mcp.tool()`-decorated async functions on one `FastMCP` instance. [src/utils.py](src/utils.py) holds the shared helpers.

Every tool follows the same shape:

1. `client = Client(api_key=XAI_API_KEY)` — a fresh `xai_sdk.Client` per call, no module-level or pooled client.
2. Build a `params` dict, adding optional keys only when truthy (`build_params()` does this generically).
3. Call the SDK (`client.chat.create`, `client.image.sample_batch`, `client.video.generate`, `client.files.*`).
4. `client.close()`.
5. Return a **Markdown string**, never a dict. This is deliberate: a dict renders as unreadable single-line JSON in the Claude UI. There is a comment at the top of `src/server.py` saying so.
6. If `show_usage` is true, append `usage_footer(response)`.

Read-only tools carry `@mcp.tool(annotations=READONLY)`.

### Tool docstrings are the public API

MCP clients surface docstrings verbatim as the tool description, so the docstring is what makes Claude call the tool correctly. Keep the existing format: one summary line, a prose paragraph on behavior/constraints, `Args:` with every parameter, and `Returns:` describing the Markdown shape. The README deliberately does not duplicate them — it links to `src/server.py`.

### Cross-cutting conventions

- **Default model** is `grok-4.6` everywhere a `model` param exists. Change it in every tool signature at once, or not at all.
- **Local vs remote media**: tools take both `*_path` (local file, base64-encoded into a `data:` URI by `encode_image_to_base64` / `encode_video_to_base64`) and `*_url` (passed through). Local wins when both are given.
- **Session history** (`chat`, `grok_agent`, `chat_with_files`) is client-side JSON in `chats/{session}.json` via `load_history` / `save_history`. It is replayed by appending `user()` / `assistant()` messages before the new turn. Unrelated to `stateful_chat`, which uses xAI's server-side `response_id`.
- **Dates** in X-search params are `DD-MM-YYYY` strings, parsed with `datetime.strptime(..., "%d-%m-%Y")`.
- **Validation** is inline `raise ValueError` at the top of the tool (domain/handle list caps, mutually exclusive allow/deny lists). Follow that rather than adding a validation layer.
- `grok_agent` is the superset tool: it composes files + images + web search + X search + code execution. A change to how any of those is wired usually needs the same change in both the specialized tool and `grok_agent`.

## Gotchas

- `.env` is the real config file and is gitignored. `example.env` is the committed fallback and is **not** gitignored, so never put a real key there.
- Video generation polls synchronously and can block for up to xAI's 10-minute timeout. This is why the Caddy reverse proxy sets 900s timeouts and `flush_interval -1`.
- MCP streamable-http responses are Server-Sent Events. Do not put Starlette's `BaseHTTPMiddleware` in front of the app — it buffers and breaks long-lived streams. `src/http_app.py` uses raw ASGI middleware for exactly this reason.
- FastMCP's DNS-rebinding protection defaults to ON with an **empty** allowed-hosts list, which rejects every proxied request. `transport_security_settings()` populates it from `DOMAIN`; a wrong `Host` header returns 421.

## Deployment

`./deploy.sh` is the whole story: it writes `.env`, gates on DNS, and brings up compose.

Three compose profiles. `caddy` and `tunnel` are alternate ingresses; `sni` composes with `caddy` rather than replacing it, since Caddy still terminates TLS and does ACME renewal, nginx just routes 443 to it by hostname first.

```bash
docker compose --profile caddy up -d --build                # auto-TLS on DOMAIN, ports 80/HTTPS_PORT (443 by default)
docker compose --profile tunnel up -d --build               # cloudflared, no inbound ports
docker compose --profile caddy --profile sni up -d --build  # caddy + nginx SNI router sharing 443 with another service
```

`grok-mcp` publishes **no ports** by design — it is reachable only through Caddy or cloudflared on the internal compose network. Anything that needs to probe it directly must go through `docker compose exec`, not `localhost:8000`.

The container runs as non-root, so `/app/chats` is a **named volume**, not a bind mount. A bind mount would inherit the host directory's root ownership and every history write would fail.

Config flows one way: `deploy.sh` writes `.env` (mode 600) → compose reads it → the app and Caddyfile substitute from it. Adding a setting means adding it in all four places.

`HTTPS_PORT` (default 443) exists because 443 is sometimes already taken on the host by something unrelated. TLS-ALPN certificate issuance only works on 443, so the Caddyfile pins ACME to the HTTP-01 challenge instead, which needs port 80 free no matter what `HTTPS_PORT` is set to.

The Cloudflare-proxied (orange-cloud) path has a hard ceiling: Cloudflare's proxy cuts requests at ~100s. Measured: a 15s/720p `generate_video` call got HTTP 524 at 125s through the proxy, but HTTP 200 at 217s over a direct Caddy path with a DNS-only (grey-cloud) record. Don't route this server through an orange-clouded record or a tunnel by default — it silently breaks video generation and any other long-running tool call.

When `HTTPS_PORT` is non-default, the proxy forwards the Host header with that port included, so `src/http_app.py`'s DNS-rebinding allowlist (`transport_security_settings()`) must include `{domain}:{HTTPS_PORT}` or every request gets rejected with "Invalid Host header".

The third ingress option is `sni`: an nginx TLS SNI router that shares port 443 with an unrelated service already bound to it on the host. It composes with `caddy` rather than replacing it — nginx takes 443 and uses `ssl_preread` to read the hostname out of the TLS ClientHello, forwarding the raw connection to `127.0.0.1:HTTPS_PORT` (caddy) when it matches `DOMAIN`, or to `SNI_FALLBACK` otherwise, without terminating TLS itself. Caddy still does ACME renewal on port 80 and TLS termination for this app. `SNI_FALLBACK` is the on/off switch: unset, the profile does nothing useful; set to `host:port`, `deploy.sh` brings up `sni` alongside `caddy` and moves Caddy to a loopback `HTTPS_PORT`. Per the config-flow rule above, `SNI_FALLBACK` needs adding in all four places: `deploy.sh`, `.env`/`.env.example`, `docker-compose.yml`, and `sni/nginx.conf.template`.

**claude.ai custom connectors only ever connect on port 443.** This is a hard constraint, not a preference, and it fails silently: a connector URL on any other port produces no request at all, so there is nothing in the access log to debug. In the claude.ai UI it just shows "Connection issue — Couldn't connect to the server." If a deploy is stuck on an alternate `HTTPS_PORT` because something else already held 443, that is what `sni` is for.

`sni/nginx.conf.template` is rendered by the nginx image's built-in envsubst at container start, and needs `NGINX_ENVSUBST_FILTER` set to limit substitution to `DOMAIN`, `HTTPS_PORT`, `SNI_FALLBACK` — without it, envsubst would also try to expand nginx's own runtime variables, `$ssl_preread_server_name` and `$upstream`, which are not shell/compose variables and would come out empty. The router also sets `proxy_timeout 900s`, for the same reason the Caddy reverse proxy above sets 900s timeouts: `generate_video` can block for up to xAI's 10-minute timeout, and a shorter proxy timeout would cut it off mid-poll.

Two operational trade-offs come with this: the fallback service sees `127.0.0.1` as the peer address instead of real client IPs, since the router is now the TCP client it observes, unless it supports the PROXY protocol; and the router is a single point of failure for both services once enabled, since a routing bug or crash takes both down together.

`MCP_URL_SECRET` is optional. When set, `BearerAuthMiddleware` also accepts requests at `/<secret>/mcp` with no `Authorization` header, because claude.ai custom connectors cannot send one. The middleware strips the prefix from `scope["path"]` before the app or uvicorn's access log sees it, so the secret is not logged. The plain `/mcp` path still requires the bearer token.

`BearerAuthMiddleware` guards only `MCP_PATH`; every other path falls through to Starlette's 404. This is deliberate. A 401 carrying `WWW-Authenticate` is how the MCP spec signals that a server speaks OAuth, so answering the discovery probes (`/.well-known/oauth-protected-resource`, `/.well-known/oauth-authorization-server`, `/register`) with 401 makes claude.ai attempt dynamic client registration and fail with "Couldn't register with ... sign-in service". They must 404.
