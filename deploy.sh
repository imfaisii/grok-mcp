#!/usr/bin/env bash
# One-command deploy for grok-mcp: writes .env, brings up docker compose
# with the right profile (caddy or tunnel), and verifies the endpoint.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

ENV_FILE="$SCRIPT_DIR/.env"
MCP_HOST="0.0.0.0"
MCP_PORT="8000"
MCP_PATH="/mcp"
HTTPS_PORT="443"

if [[ -t 1 ]]; then
  RED=$'\033[31m'; GREEN=$'\033[32m'; YELLOW=$'\033[33m'; RESET=$'\033[0m'
else
  RED=""; GREEN=""; YELLOW=""; RESET=""
fi

info()  { printf '%s\n' "$*"; }
ok()    { printf '%s✓ %s%s\n' "$GREEN" "$*" "$RESET"; }
warn()  { printf '%s! %s%s\n' "$YELLOW" "$*" "$RESET"; }
err()   { printf '%s✗ %s%s\n' "$RED" "$*" "$RESET" >&2; }
die()   { err "$*"; exit 1; }

# ---------- preflight ----------

print_docker_install_hint() {
  if [[ "$(uname -s)" == "Darwin" ]]; then
    info "No Docker found. On macOS install Docker Desktop:"
    info "  brew install --cask docker"
    info "Full instructions: https://docs.docker.com/desktop/install/mac-install/"
    return
  fi
  local id="linux"
  if [[ -f /etc/os-release ]]; then
    id="$(. /etc/os-release && echo "$ID")"
  fi
  info "No Docker found for this distro ('$id')."
  info "Install it with Docker's official convenience script:"
  info "  curl -fsSL https://get.docker.com | sh"
  info "Full instructions: https://docs.docker.com/engine/install/"
}

preflight() {
  [[ -f "$SCRIPT_DIR/docker-compose.yml" ]] || die "docker-compose.yml not found next to this script. Run ./deploy.sh from inside a clone of grok-mcp."

  if ! command -v docker >/dev/null 2>&1; then
    print_docker_install_hint
    die "Docker is not installed."
  fi

  if ! docker compose version >/dev/null 2>&1; then
    print_docker_install_hint
    die "Docker Compose v2 (the 'docker compose' subcommand) is not available."
  fi

  if ! docker info >/dev/null 2>&1; then
    err "Cannot talk to the Docker daemon."
    info "Add your user to the docker group (sudo usermod -aG docker \$USER, then log out and back in) or re-run this script with sudo."
    exit 1
  fi
}

# ---------- existing .env ----------

env_get() {
  grep -E "^$1=" "$ENV_FILE" 2>/dev/null | tail -n1 | cut -d= -f2- || true
}

show_env_summary() {
  info "  DOMAIN=$(env_get DOMAIN)"
  info "  MCP_PORT=$(env_get MCP_PORT)"
  info "  MCP_TRANSPORT=$(env_get MCP_TRANSPORT)"
  local key
  for key in XAI_API_KEY MCP_AUTH_TOKEN TUNNEL_TOKEN; do
    if [[ -n "$(env_get "$key")" ]]; then
      info "  ${key}=<set>"
    else
      info "  ${key}=<not set>"
    fi
  done
}

handle_existing_env() {
  info "Found an existing .env:"
  show_env_summary
  read -rp "Reuse it as-is? [Y/n] " ans
  [[ "$ans" =~ ^[Nn] ]] && return 1
  return 0
}

# ---------- xAI key ----------

prompt_api_key() {
  if [[ -n "${XAI_API_KEY:-}" ]]; then
    info "Using XAI_API_KEY from the environment."
  else
    read -rsp "xAI API key (starts with xai-): " XAI_API_KEY
    echo
  fi
  if [[ -z "$XAI_API_KEY" || "$XAI_API_KEY" != xai-* ]]; then
    warn "That doesn't look like a typical xAI key (expected to start with 'xai-')."
    read -rp "Continue with it anyway? [y/N] " ans
    [[ "$ans" =~ ^[Yy] ]] || die "Aborted."
  fi
}

# ---------- domain ----------

prompt_domain() {
  read -rp "Domain (e.g. mcp.example.com): " DOMAIN
  DOMAIN="${DOMAIN#http://}"
  DOMAIN="${DOMAIN#https://}"
  DOMAIN="${DOMAIN%%/*}"
  DOMAIN="${DOMAIN%/}"
  [[ -n "$DOMAIN" ]] || die "A domain is required."

  read -rp "Email for Let's Encrypt expiry notices: " ACME_EMAIL
  [[ -n "$ACME_EMAIL" ]] || die "An email is required for ACME."
}

# ---------- DNS gate ----------

get_public_ip() {
  local ip url
  for url in https://api.ipify.org https://ifconfig.me https://icanhazip.com; do
    ip="$(curl -fsS --max-time 5 "$url" 2>/dev/null | tr -d '[:space:]')" || continue
    [[ -n "$ip" ]] && { printf '%s' "$ip"; return 0; }
  done
  return 1
}

# Prints resolved A/AAAA records, one per line. Return 2 = no resolver tool available.
resolve_domain() {
  local domain="$1" ips=""
  if command -v dig >/dev/null 2>&1; then
    ips="$( { dig +short A "$domain" @1.1.1.1 2>/dev/null; dig +short AAAA "$domain" @1.1.1.1 2>/dev/null; } || true)"
  elif command -v host >/dev/null 2>&1; then
    ips="$(host "$domain" 2>/dev/null | awk '/has address/{print $NF} /has IPv6 address/{print $NF}' || true)"
  elif command -v getent >/dev/null 2>&1; then
    ips="$(getent hosts "$domain" 2>/dev/null | awk '{print $1}' || true)"
  else
    return 2
  fi
  printf '%s\n' "$ips" | grep -v '^$' || true
}

# Cloudflare's published edge ranges. Fetched fresh; these are the fallback
# if that fetch fails.
CF_V4_FALLBACK="173.245.48.0/20
103.21.244.0/22
103.22.200.0/22
103.31.4.0/22
141.101.64.0/18
108.162.192.0/18
190.93.240.0/20
188.114.96.0/20
197.234.240.0/22
198.41.128.0/17
162.158.0.0/15
104.16.0.0/13
104.24.0.0/14
172.64.0.0/13
131.0.72.0/22"

CF_V6_FALLBACK="2400:cb00::/32
2606:4700::/32
2803:f800::/32
2405:b500::/32
2405:8100::/32
2a06:98c0::/29
2c0f:f248::/32"

ip_to_int() {
  local ip="$1" o1 o2 o3 o4
  IFS=. read -r o1 o2 o3 o4 <<< "$ip"
  [[ "$o1" =~ ^[0-9]+$ && "$o2" =~ ^[0-9]+$ && "$o3" =~ ^[0-9]+$ && "$o4" =~ ^[0-9]+$ ]] || return 1
  echo $(( (o1 << 24) + (o2 << 16) + (o3 << 8) + o4 ))
}

ip_in_cidr_v4() {
  local ip="$1" cidr="$2" base="${2%/*}" bits="${2#*/}" ip_i base_i mask
  ip_i="$(ip_to_int "$ip")" || return 1
  base_i="$(ip_to_int "$base")" || return 1
  if [[ "$bits" -eq 0 ]]; then
    mask=0
  else
    mask=$(( (0xFFFFFFFF << (32 - bits)) & 0xFFFFFFFF ))
  fi
  (( (ip_i & mask) == (base_i & mask) ))
}

is_cloudflare_ipv4() {
  local ip="$1" ranges cidr
  ranges="$(curl -fsS --max-time 5 https://www.cloudflare.com/ips-v4 2>/dev/null || true)"
  [[ -z "$ranges" ]] && ranges="$CF_V4_FALLBACK"
  while IFS= read -r cidr; do
    [[ -z "$cidr" ]] && continue
    ip_in_cidr_v4 "$ip" "$cidr" && return 0
  done <<< "$ranges"
  return 1
}

is_cloudflare_ipv6() {
  # ponytail: matches on the first two hextets (/32 granularity) since bash
  # has no 128-bit arithmetic for a real CIDR test. The one /29 range in the
  # fallback list is checked as /32, a slightly narrower match. Good enough
  # to flag the common case; upgrade with a real IPv6 library if this ever
  # produces a false negative in practice.
  local ip raw="$1" ranges line prefix
  ip="$(tr 'A-F' 'a-f' <<< "$raw")"
  ranges="$(curl -fsS --max-time 5 https://www.cloudflare.com/ips-v6 2>/dev/null || true)"
  [[ -z "$ranges" ]] && ranges="$CF_V6_FALLBACK"
  while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    prefix="$(cut -d/ -f1 <<< "$line" | tr 'A-F' 'a-f' | cut -d: -f1-2)"
    [[ "$ip" == "$prefix"* ]] && return 0
  done <<< "$ranges"
  return 1
}

is_cloudflare_ip() {
  local ip="$1"
  if [[ "$ip" == *:* ]]; then
    is_cloudflare_ipv6 "$ip"
  else
    is_cloudflare_ipv4 "$ip"
  fi
}

dns_failure_menu() {
  local resolved="$1" server_ip="$2"
  echo
  err "DNS check failed for $DOMAIN"
  if [[ -n "$resolved" ]]; then
    info "  Currently resolves to: $(tr '\n' ' ' <<< "$resolved")"
  else
    info "  Currently resolves to: no A/AAAA record"
  fi
  info "  This server's public IP: ${server_ip:-unknown}"
  echo
  info "Create this DNS record at your registrar/DNS provider:"
  info "  A    ${DOMAIN}    ${server_ip:-<this server IP>}    TTL 300"
  info "(Use '@' for the name instead of the full domain if your provider asks for just the host part of the apex.)"
  info "Propagation can take a few minutes."
  echo
  info "1) I have added the record — re-check now"
  info "2) Use a Cloudflare Tunnel instead (no DNS change needed, works behind NAT/firewall)"
  info "3) Abort"
  read -rp "Choose [1-3]: " choice
  case "$choice" in
    1) return 0 ;;
    2) setup_tunnel; return 1 ;;
    3) die "Aborted." ;;
    *) warn "Invalid choice."; dns_failure_menu "$resolved" "$server_ip" ;;
  esac
}

cloudflare_proxy_menu() {
  echo
  warn "$DOMAIN resolves to a Cloudflare edge IP (the DNS record is proxied / orange-clouded)."
  info "Caddy cannot complete an ACME HTTP challenge through a proxied record."
  echo
  info "1) I switched the record to 'DNS only' (grey cloud) in Cloudflare — re-check now"
  info "2) Use a Cloudflare Tunnel instead (works with the record proxied)"
  read -rp "Choose [1-2]: " choice
  case "$choice" in
    1) return 0 ;;
    2) setup_tunnel; return 1 ;;
    *) warn "Invalid choice."; cloudflare_proxy_menu ;;
  esac
}

setup_tunnel() {
  echo
  warn "Cloudflare's proxy cuts long requests at ~100s. Measured: a 15s/720p generate_video"
  warn "call got HTTP 524 at 125s through the tunnel, but HTTP 200 at 217s over a direct"
  warn "Caddy path. Chat and search are fine; video generation and long agent runs will fail"
  warn "here. Use the tunnel only when this box genuinely can't expose ports."
  echo
  info "Cloudflare Tunnel setup:"
  info "  1. Cloudflare Zero Trust dashboard -> Networks -> Tunnels -> Create a tunnel -> Cloudflared"
  info "  2. Copy the tunnel token"
  info "  3. Add a Public Hostname for $DOMAIN routing to http://grok-mcp:8000"
  read -rsp "Paste the tunnel token: " TUNNEL_TOKEN
  echo
  [[ -n "$TUNNEL_TOKEN" ]] || die "A tunnel token is required."
  PROFILE="tunnel"
}

dns_gate() {
  local server_ip=""
  server_ip="$(get_public_ip || true)"
  if [[ -z "$server_ip" ]]; then
    warn "Could not auto-detect this server's public IP."
    read -rp "Enter it manually, or leave blank to go straight to a Cloudflare Tunnel: " server_ip
    if [[ -z "$server_ip" ]]; then
      setup_tunnel
      return 0
    fi
  fi

  while true; do
    local resolved status=0
    resolved="$(resolve_domain "$DOMAIN")" || status=$?
    if [[ "$status" -eq 2 ]]; then
      err "No DNS resolver tool found (dig, host, or getent)."
      dns_failure_menu "" "$server_ip" || return 0
      continue
    fi

    if [[ -z "$resolved" ]]; then
      dns_failure_menu "" "$server_ip" || return 0
      continue
    fi

    local ip match=0 cf_hit=0
    while IFS= read -r ip; do
      [[ -z "$ip" ]] && continue
      [[ "$ip" == "$server_ip" ]] && match=1
      is_cloudflare_ip "$ip" && cf_hit=1
    done <<< "$resolved"

    if [[ "$match" -eq 1 ]]; then
      ok "$DOMAIN resolves to $server_ip. DNS check passed."
      PROFILE="caddy"
      return 0
    fi

    if [[ "$cf_hit" -eq 1 ]]; then
      cloudflare_proxy_menu || return 0
      continue
    fi

    dns_failure_menu "$resolved" "$server_ip" || return 0
  done
}

# ---------- port check ----------

# Prints the process holding $1/tcp and returns 0, or returns 1 if the port
# is free. Returns 2 if neither ss nor netstat is available. Inspects only,
# never kills anything.
port_holder() {
  local port="$1" line=""
  if command -v ss >/dev/null 2>&1; then
    line="$(ss -tlnp 2>/dev/null | awk -v p=":${port}\$" '$4 ~ p')"
  elif command -v netstat >/dev/null 2>&1; then
    line="$(netstat -tlnp 2>/dev/null | awk -v p=":${port}\$" '$4 ~ p')"
  else
    return 2
  fi
  [[ -z "$line" ]] && return 1
  grep -oE '"[^"]+"' <<< "$line" | head -n1 | tr -d '"'
  return 0
}

alt_https_port() {
  local port
  while true; do
    read -rp "Alternate HTTPS port [8443]: " port
    port="${port:-8443}"
    if [[ ! "$port" =~ ^[0-9]+$ ]] || (( port < 1024 || port > 65535 )); then
      warn "Enter a number between 1024 and 65535."
      continue
    fi
    if port_holder "$port" >/dev/null; then
      warn "Port $port is also in use. Pick another."
      continue
    fi
    HTTPS_PORT="$port"
    return 0
  done
}

port_gate() {
  local holder status=0

  holder="$(port_holder 80)" || status=$?
  if [[ "$status" -eq 2 ]]; then
    warn "No 'ss' or 'netstat' found; skipping the port check."
    return 0
  fi
  if [[ "$status" -eq 0 ]]; then
    echo
    err "Port 80 is already in use (by: ${holder:-an unknown process})."
    info "Caddy needs port 80 free for the ACME HTTP-01 challenge — an alternate HTTPS port doesn't fix this one."
    echo
    info "1) I freed it up — re-check now"
    info "2) Use a Cloudflare Tunnel instead (no ports needed on this host)"
    info "3) Abort"
    read -rp "Choose [1-3]: " choice
    case "$choice" in
      1) port_gate; return ;;
      2) setup_tunnel; return ;;
      3) die "Aborted." ;;
      *) warn "Invalid choice."; port_gate; return ;;
    esac
  fi

  status=0
  holder="$(port_holder 443)" || status=$?
  if [[ "$status" -eq 0 ]]; then
    echo
    warn "Port 443 is already in use (by: ${holder:-an unknown process}). Caddy cannot bind it."
    echo
    info "1) Share port 443 with it (recommended — needed for claude.ai connectors)"
    info "2) Use an alternate HTTPS port (claude.ai connectors will NOT work)"
    info "3) Abort"
    read -rp "Choose [1-3]: " choice
    case "$choice" in
      1) sni_setup ;;
      2) alt_https_port ;;
      3) die "Aborted." ;;
      *) warn "Invalid choice."; port_gate; return ;;
    esac
    return
  fi

  HTTPS_PORT="443"
}

# ---------- SNI router ----------

# Sets HTTPS_PORT (Caddy's internal port) and SNI_FALLBACK (host:port of the
# service that used to be on 443) once the other service has actually moved.
sni_setup() {
  echo
  info "This puts an nginx TLS SNI router on port 443. It reads the server"
  info "name in the TLS handshake and forwards the raw connection: $DOMAIN"
  info "goes to Caddy, everything else goes to whatever is on 443 right now."
  info "Neither service loses its own TLS — nginx never terminates it."
  info "The service currently on 443 has to move to a different port first."
  info "This script cannot do that move for you."
  echo

  local fallback_port
  while true; do
    read -rp "Port the OTHER service will listen on [8444]: " fallback_port
    fallback_port="${fallback_port:-8444}"
    if [[ ! "$fallback_port" =~ ^[0-9]+$ ]] || (( fallback_port < 1024 || fallback_port > 65535 )); then
      warn "Enter a number between 1024 and 65535."
      continue
    fi
    if port_holder "$fallback_port" >/dev/null; then
      warn "Port $fallback_port is already in use by something else. Pick another."
      continue
    fi
    break
  done

  local caddy_port
  while true; do
    read -rp "Port Caddy should use internally [8443]: " caddy_port
    caddy_port="${caddy_port:-8443}"
    if [[ ! "$caddy_port" =~ ^[0-9]+$ ]] || (( caddy_port < 1024 || caddy_port > 65535 )); then
      warn "Enter a number between 1024 and 65535."
      continue
    fi
    if [[ "$caddy_port" == "443" ]]; then
      warn "443 is what nginx is about to take. Pick another port for Caddy."
      continue
    fi
    if [[ "$caddy_port" == "$fallback_port" ]]; then
      warn "That's the same port you gave the other service. Pick a different one."
      continue
    fi
    if port_holder "$caddy_port" >/dev/null; then
      warn "Port $caddy_port is already in use by something else. Pick another."
      continue
    fi
    break
  done

  local ans
  while true; do
    echo
    info "Now move the other service to port $fallback_port and confirm it's listening there."
    read -rp "Press Enter once it's moved (or type 'abort'): " ans
    [[ "$ans" == "abort" ]] && die "Aborted."
    if port_holder 443 >/dev/null; then
      warn "Port 443 is still in use. nginx needs it free before it can bind."
      continue
    fi
    break
  done

  HTTPS_PORT="$caddy_port"
  SNI_FALLBACK="127.0.0.1:$fallback_port"
  ok "SNI router configured: $DOMAIN -> caddy on $HTTPS_PORT, everything else -> $SNI_FALLBACK"
}

# ---------- token ----------

generate_token() {
  if command -v openssl >/dev/null 2>&1; then
    MCP_AUTH_TOKEN="$(openssl rand -hex 32)"
  else
    MCP_AUTH_TOKEN="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  fi
}

# ---------- connector secret ----------

# claude.ai custom connectors can't send an Authorization header, so
# src/http_app.py also accepts the token as a URL segment. That URL is then
# a password, so this is opt-in.
prompt_connector_secret() {
  info "claude.ai custom connectors can't send an Authorization header, so"
  info "the token would have to live in the URL instead — anyone with that"
  info "URL could then use the server, same as anyone with the token."
  local ans
  read -rp "Enable a claude.ai connector URL? [Y/n] " ans
  if [[ "$ans" =~ ^[Nn] ]]; then
    MCP_URL_SECRET=""
    return
  fi
  if command -v openssl >/dev/null 2>&1; then
    MCP_URL_SECRET="$(openssl rand -hex 32)"
  else
    MCP_URL_SECRET="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
  fi
}

# ---------- write .env ----------

write_env() {
  umask 077
  cat > "$ENV_FILE" <<EOF
XAI_API_KEY=$XAI_API_KEY
MCP_TRANSPORT=http
MCP_HOST=$MCP_HOST
MCP_PORT=$MCP_PORT
MCP_PATH=$MCP_PATH
MCP_AUTH_TOKEN=$MCP_AUTH_TOKEN
MCP_URL_SECRET=${MCP_URL_SECRET:-}
DOMAIN=$DOMAIN
ACME_EMAIL=$ACME_EMAIL
HTTPS_PORT=$HTTPS_PORT
SNI_FALLBACK=${SNI_FALLBACK:-}
TUNNEL_TOKEN=${TUNNEL_TOKEN:-}
EOF
  chmod 600 "$ENV_FILE"
  ok ".env written (permissions 600)."
}

# ---------- deploy ----------

# https://$DOMAIN, or https://$DOMAIN:$HTTPS_PORT when it isn't the default.
# With the SNI router active the public port is always 443 regardless of
# HTTPS_PORT (that's Caddy's *internal* port), so no port goes in the URL.
base_url() {
  if [[ -n "${SNI_FALLBACK:-}" || "$HTTPS_PORT" == "443" ]]; then
    printf 'https://%s' "$DOMAIN"
  else
    printf 'https://%s:%s' "$DOMAIN" "$HTTPS_PORT"
  fi
}

deploy() {
  local -a flags=(--profile "$PROFILE")
  local label="$PROFILE"
  if [[ -n "${SNI_FALLBACK:-}" ]]; then
    flags+=(--profile sni)
    label="$PROFILE+sni"
  fi
  info "Deploying with the '$label' profile(s)..."
  docker compose "${flags[@]}" up -d --build
}

wait_for_200() {
  # No -k: an invalid certificate must fail this check, not silently pass it.
  local url="$1" timeout="$2" waited=0 code=""
  while (( waited < timeout )); do
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url" 2>/dev/null || true)"
    [[ "$code" == "200" ]] && { printf '%s' "$code"; return 0; }
    sleep 3
    waited=$((waited + 3))
  done
  printf '%s' "$code"
  return 1
}

# The app container publishes no ports, so it is not reachable from the host.
# Probe it from the inside instead.
wait_for_container() {
  local timeout="$1" waited=0
  while (( waited < timeout )); do
    if docker compose --profile "$PROFILE" exec -T grok-mcp \
         curl -fsS -o /dev/null "http://127.0.0.1:${MCP_PORT}/healthz" 2>/dev/null; then
      return 0
    fi
    sleep 3
    waited=$((waited + 3))
  done
  return 1
}

verify() {
  local timeout=90 code url
  url="$(base_url)"

  if [[ "$PROFILE" == "tunnel" ]]; then
    info "Probing /healthz inside the container (up to ${timeout}s)..."
    if wait_for_container "$timeout"; then
      ok "Container is healthy."
    else
      err "Container did not become healthy within ${timeout}s."
      info "Check logs with: docker compose --profile $PROFILE logs -f"
      return 1
    fi
    info "Checking $url/healthz (only works once the Public Hostname is mapped in Cloudflare)..."
  else
    info "Waiting for $url/healthz (up to ${timeout}s — first cert issuance can be slow)..."
  fi

  if code="$(wait_for_200 "$url/healthz" "$timeout")"; then
    ok "$url/healthz is healthy."
  else
    err "$url/healthz did not return 200 within ${timeout}s (last status: ${code:-none})."
    info "Check logs with: docker compose --profile $PROFILE logs -f"
    return 1
  fi

  local auth_code
  auth_code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$url$MCP_PATH" 2>/dev/null || true)"
  if [[ "$auth_code" == "401" ]]; then
    ok "Auth check passed: $MCP_PATH rejects unauthenticated requests (401)."
  elif [[ "$auth_code" == "200" ]]; then
    err "SECURITY: $url$MCP_PATH returned 200 to an unauthenticated request. It should return 401."
  else
    warn "Could not confirm the auth check (got HTTP ${auth_code:-none} from $MCP_PATH)."
  fi
}

# ---------- final output ----------

final_output() {
  local url
  url="$(base_url)"
  echo
  ok "Deployed."
  info "Endpoint: $url$MCP_PATH"
  echo
  info "Add it to a client:"
  info "  claude mcp add --transport http grok $url$MCP_PATH --header \"Authorization: Bearer $MCP_AUTH_TOKEN\""
  echo
  warn "That token is a password. It's stored in $ENV_FILE (mode 600) — treat it the same way."

  if [[ -n "${MCP_URL_SECRET:-}" ]]; then
    echo
    info "claude.ai connector URL: $url/$MCP_URL_SECRET$MCP_PATH"
    warn "That URL is a password — anyone who has it can use the server."
  fi

  if [[ "$HTTPS_PORT" != "443" && -z "${SNI_FALLBACK:-}" ]]; then
    echo
    warn "HTTPS_PORT is $HTTPS_PORT, not 443. claude.ai custom connectors only"
    warn "ever connect on port 443, so they will NOT be able to reach this URL."
  fi
}

# ---------- main ----------

main() {
  preflight

  local reuse=1
  if [[ -f "$ENV_FILE" ]] && handle_existing_env; then
    reuse=0
  fi

  if [[ "$reuse" -eq 0 ]]; then
    DOMAIN="$(env_get DOMAIN)"
    MCP_AUTH_TOKEN="$(env_get MCP_AUTH_TOKEN)"
    MCP_URL_SECRET="$(env_get MCP_URL_SECRET)"
    HTTPS_PORT="$(env_get HTTPS_PORT)"
    HTTPS_PORT="${HTTPS_PORT:-443}"
    SNI_FALLBACK="$(env_get SNI_FALLBACK)"
    PROFILE="caddy"
    [[ -n "$(env_get TUNNEL_TOKEN)" ]] && PROFILE="tunnel"
    local profile_label="$PROFILE"
    [[ -n "$SNI_FALLBACK" ]] && profile_label="$PROFILE+sni"
    info "Reusing existing .env (profile: $profile_label)."
  else
    XAI_API_KEY="${XAI_API_KEY:-}"
    TUNNEL_TOKEN=""
    SNI_FALLBACK=""
    prompt_api_key
    prompt_domain
    dns_gate
    [[ "$PROFILE" == "caddy" ]] && port_gate
    generate_token
    prompt_connector_secret
    write_env
  fi

  deploy
  verify
  final_output
}

main "$@"
