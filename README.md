# Grok-MCP
MCP server for xAI’s Grok API with Web/X search, vision, image/video generation and file support.

<a href="https://glama.ai/mcp/servers/@merterbak/Grok-MCP">
  <img width="380" height="200" src="https://glama.ai/mcp/servers/@merterbak/Grok-MCP/badge" />
</a>

## Features

- **Agentic Tool Calling**: Web search, X search, and code execution with multi-step reasoning
- **Multiple Grok Models**: Access to latest models such as grok-4.6, grok-4.5, grok-build-0.1 and more
- **Image and Video Generation**: Create images and videos using Grok Imagine
- **Vision Capabilities**: Analyze images with Grok's vision models
- **Files API**: Upload, manage, and chat with documents 
- **Stateful Conversations**: Maintain conversation context as id across multiple requests
- **Local Chat History**: Option to save persistent client side chat history as JSON files in chats/
- **One-command server deploy**: `./deploy.sh` sets up HTTPS on your domain with a bearer-token protected endpoint, or a Cloudflare Tunnel when DNS cannot point at the box

## Deploy to a server

Clone, run one script, answer the prompts. You get an HTTPS MCP endpoint on your own domain.

```bash
git clone https://github.com/merterbak/Grok-MCP.git
cd Grok-MCP
./deploy.sh
```

The script asks for your xAI API key (never echoed), your domain, and an email for Let's Encrypt. Then it checks whether the domain actually points at this server.

**If DNS does not point here, it stops before deploying** and shows you the exact record to create:

```
A    mcp    203.0.113.10    TTL 300
```

You then pick: re-check after adding the record, switch to a Cloudflare Tunnel, or abort.

It also handles the orange-cloud case. If your domain resolves to a Cloudflare edge IP, that is not a plain mismatch — Caddy cannot complete an ACME challenge through a proxied record. The script says so and offers the two real options: set the record to "DNS only", or use a tunnel.

It also checks whether port 443 is already taken on this host. If something else is already bound to it, the script offers two ways forward: share port 443 with the existing service via an nginx SNI router (see [Sharing port 443 with another service](#sharing-port-443-with-another-service)), or fall back to an alternate HTTPS port (default `8443`), with the client URL carrying that port, e.g. `https://your-domain.com:8443/mcp`. The alternate port works fine for Claude Code but breaks claude.ai custom connectors, which only ever connect on 443 — see [Adding it to claude.ai connectors](#adding-it-to-claudeai-connectors). If port 80 is taken too, that blocks Let's Encrypt entirely, since certificate issuance needs it regardless of which HTTPS port you pick — the script explains this and offers the Cloudflare Tunnel instead.

### Cloudflare Tunnel

Choose this when the server is behind NAT, has no public IP, or you cannot change DNS — nothing needs to be exposed. Get a token from the Zero Trust dashboard under Networks → Tunnels, then add a Public Hostname routing your domain to `http://grok-mcp:8000`.

The trade-off: Cloudflare's proxy cuts requests at around 100 seconds. Measured on this project: a 15s/720p `generate_video` call returned HTTP 524 at 125s through the tunnel, while the same call over a direct Caddy path succeeded with HTTP 200 in 217s. Chat and search are unaffected, but video generation and other long tool calls will fail behind the tunnel.

Prefer the direct Caddy path with a DNS-only (grey-cloud) record whenever this host can expose ports. Use the tunnel only when it genuinely can't.

### Sharing port 443 with another service

Use this when port 443 on the host is already taken by something else — the case this was built for was a Telegram MTProto proxy on the same box. An alternate `HTTPS_PORT` (`:8443`) works around that for `curl` and Claude Code, but it silently breaks claude.ai custom connectors, which only ever connect on 443. Rather than move Grok-MCP off 443, an nginx TLS SNI router takes 443 and splits traffic by hostname before TLS is even terminated:

```
:443  nginx (sni-router)
        ├── SNI == DOMAIN   →  127.0.0.1:HTTPS_PORT   (caddy → grok-mcp)
        └── anything else   →  SNI_FALLBACK           (the other service)
:80   caddy (ACME HTTP-01 renewal only)
```

This works as long as the two services present different TLS server names. `ssl_preread` reads the hostname out of the TLS ClientHello and forwards the raw TCP connection without terminating it, so each backend still presents its own certificate.

Set `SNI_FALLBACK` to `host:port` — wherever the other service now listens, e.g. `127.0.0.1:8444`. You have to move that service off 443 yourself first; `deploy.sh` prompts for its new port and won't continue until 443 is actually free. Once it is, the script brings up the `sni` profile alongside `caddy`: Caddy moves to a loopback `HTTPS_PORT` (default `8443`) and nginx takes 443, so the public client URL ends up as a plain `https://your-domain.com` with no port, which is what claude.ai connectors need. The router config is in `sni/nginx.conf.template`.

Two trade-offs to know about:
- The router becomes the TCP peer for the fallback service, so it sees `127.0.0.1` instead of real client IPs, unless that service supports the PROXY protocol.
- The router is a single point of failure for both services once enabled.

### After it finishes

The script prints a ready-to-paste client config:

```bash
claude mcp add --transport http grok https://your-domain.com/mcp --header "Authorization: Bearer <token>"
```

The endpoint requires that bearer token on every request. It is generated for you, stored in `.env` with mode 600, and it is a password — treat it like one.

### Adding it to claude.ai connectors

**claude.ai custom connectors only ever connect on port 443.** A URL on any
other port (`:8443`, for instance) works fine in `curl` and in Claude Code,
but claude.ai never sends it a single packet — the UI just shows "Connection
issue — Couldn't connect to the server," with nothing to see on the server
side. If your deployment is on an alternate `HTTPS_PORT` because something
else already held 443, see
[Sharing port 443 with another service](#sharing-port-443-with-another-service)
above.

claude.ai custom connectors also have no field for an `Authorization` header, so
the bearer-protected `/mcp` path cannot be used there. `deploy.sh` now asks
(default yes) whether to set this up: it generates a 32-byte hex secret, writes
`MCP_URL_SECRET` to `.env`, and prints the connector URL at the end. The server
answers at `/<secret>/mcp` with no header required:

```
https://your-domain.com/<secret>/mcp
```

Paste that into Settings, Connectors, Add custom connector. Leave the OAuth
fields empty. The URL is the credential, so treat it like a password. Claude Code
keeps using the bearer header on the plain `/mcp` path.

Anthropic connects from its own cloud rather than your browser, so the domain
has to be reachable from the public internet.

### Managing the deployment

```bash
docker compose --profile caddy logs -f     # or --profile tunnel
docker compose --profile caddy restart
docker compose --profile caddy down
```

When `SNI_FALLBACK` is set, add `--profile sni` to each of those, otherwise the
router is left out and `logs` shows nothing for it while `down` leaves it running:

```bash
docker compose --profile caddy --profile sni logs -f
```

The app container publishes no ports. It is reachable only through Caddy or cloudflared, never directly from the internet.

## Prerequisites

- Python 3.11 or higher
- xAI API key ([Get one here](https://console.x.ai))
- [Astral UV](https://docs.astral.sh/uv/getting-started/installation/)

## Installation

1. Clone the repository:
```bash
git clone https://github.com/merterbak/Grok-MCP.git
cd Grok-MCP
```

2. Create a venv environment:
```bash
uv venv
source .venv/bin/activate # macOS/Linux or .venv\Scripts\activate on Windows
```

3. Install dependencies:

```bash
uv sync
```


## Configuration

### Claude Desktop Integration

Add this to your Claude Desktop configuration file:

```json
{
  "mcpServers": {
    "grok": {
      "command": "uv",
      "args": [
        "--directory",
        "/path/to/Grok-MCP",
        "run",
        "python",
        "main.py"
      ],
      "env": {
        "XAI_API_KEY": "your_api_key_here"
      }
    }
  }
}
```

### Claude Code Integration

Run this command from inside the project directory:

```bash
claude mcp add grok-mcp -e XAI_API_KEY=your_api_key_here -- uv run --directory /path/to/Grok-MCP python main.py
```

Or if you have a `.env` file with your key:

```bash
 claude mcp add grok-mcp -- uv run --directory /path/to/Grok-MCP python main.py
```

Verify it's registered:

```bash
claude mcp list
```

### Filesystem MCP (Optional)

Claude Desktop can't send uploaded images in the chat to an MCP tool.
The easiest way to give access to files directly from your computer is official Filesystem MCP server.
After setting it up you’ll be able to just write the image’s file path (such as /Users/mert/Desktop/image.png) in chat and Claude can use it with any vision chat tool.

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": [
        "-y",
        "@modelcontextprotocol/server-filesystem",
        "/Users/<your-username>/Desktop",
        "/Users/<your-username>/Downloads"
      ]
    }
  }
}

```

---

For stdio:

```bash
uv run python main.py
```

Mcp Inspector:

```bash
mcp dev main.py
```

For a server deployment, see [Deploy to a server](#deploy-to-a-server).


# Available Tools

Each tool has a full docstring in [src/server.py](src/server.py) with its arguments and return format. MCP client surfaces those directly, so this list is just a quick map of what's available.

Note: For using images and files, you must provide paths to chat. See [Filesystem MCP (Optional)](#filesystem-mcp-optional) for setup.

### Chat and reasoning
- `chat` — standard chat completion with optional persistent history and multi-agent support.
- `chat_with_vision` — analyze local or remote images with a Grok vision model.
- `chat_with_files` — chat grounded on previously uploaded documents.
- `stateful_chat` — continue a server-side stored conversation via `response_id`.
- `retrieve_stateful_response` — fetch a stored response by ID.
- `delete_stateful_response` — delete a stored response by ID.

### Agentic tools
- `web_search` — autonomous web research with domain filters and citations.
- `x_search` — autonomous search over X (Twitter) posts, with handle and date filters.
- `code_executor` — solve tasks by running Python in a sandbox.
- `grok_agent` — unified agent that mixes files, images, web search, X search, and code execution.

### Image and video
- `generate_image` — create or edit images with Grok Imagine (multi-reference editing supported).
- `generate_video` — text-to-video, image-to-video, or video editing with Grok Imagine.
- `extend_video` — extend an existing generated video with a follow-up prompt.

### Files
- `upload_file` — upload a local document.
- `list_files` — list uploaded files with sorting.
- `get_file` — fetch file metadata by ID.
- `get_file_content` — download file content as text.
- `delete_file` — delete a file by ID.

### Local chat history
- `list_chat_sessions` — list saved sessions in `chats/`.
- `get_chat_history` — get a session's full transcript.
- `clear_chat_history` — delete a session's local history file.

### Models
- `list_models` — list all Grok language and image models with live pricing.

  
## License

This project is open source and available under the MIT License.
