import { Container, getContainer } from "@cloudflare/containers";

// Secrets come from `wrangler secret put`; the rest are `vars` in
// wrangler.jsonc. Either way they reach the Python server as plain env vars.
const PASSTHROUGH = [
  "XAI_API_KEY",
  "MCP_AUTH_TOKEN",
  "MCP_URL_SECRET",
  "MCP_OAUTH_SECRET",
  "MCP_OAUTH_PASSWORD",
  "R2_ACCOUNT_ID",
  "R2_ACCESS_KEY_ID",
  "R2_SECRET_ACCESS_KEY",
  "R2_BUCKET",
  "R2_PUBLIC_BASE",
  "R2_CHATS_BUCKET",
  "TELEGRAM_BOT_TOKEN",
  "TELEGRAM_CHAT_ID",
  "DOMAIN",
  "MCP_PATH",
] as const;

type Env = Cloudflare.Env & Partial<Record<(typeof PASSTHROUGH)[number], string>>;

// The whole MCP server (Python, ffmpeg, rembg) runs in this container. One
// instance serves everything: the server keeps no per-request state worth
// sharding, and video jobs are single long requests rather than sessions.
export class GrokMcp extends Container<Env> {
  defaultPort = 8000;
  // Outlives the longest single call (generate_video polls for up to ~10 min);
  // fetches renew the timer, so an idle instance still scales to zero.
  sleepAfter = "20m";
  envVars: Record<string, string>;

  constructor(...args: ConstructorParameters<typeof Container<Env>>) {
    super(...args);
    const env = args[1];
    this.envVars = { MCP_TRANSPORT: "http", MCP_HOST: "0.0.0.0", MCP_PORT: "8000" };
    for (const key of PASSTHROUGH) {
      const value = env[key];
      if (value) this.envVars[key] = value;
    }
  }
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const response = await getContainer(env.GROK_MCP).fetch(request);
    // claude.ai reads the authorization-server metadata and gives up before
    // registering unless it can use a client-metadata-document client id or
    // a secret-based token auth method. Advertise both, as deeporax.com's
    // working server does: authorize and token accept any client id, and the
    // token endpoint ignores a posted client_secret.
    const { pathname } = new URL(request.url);
    if (pathname.startsWith("/.well-known/oauth-authorization-server") && response.ok) {
      const metadata = (await response.json()) as Record<string, unknown>;
      const headers = new Headers(response.headers);
      headers.delete("content-length");
      return Response.json(
        {
          ...metadata,
          // Not client_secret_basic: the token endpoint reads client_id from the body only.
          token_endpoint_auth_methods_supported: ["none", "client_secret_post"],
          client_id_metadata_document_supported: true,
        },
        { headers },
      );
    }
    // claude.ai only starts OAuth when the MCP endpoint's 401 points at the
    // protected-resource metadata (RFC 9728). The server's bearer middleware
    // sends a bare `Bearer`, so name the metadata URL for the public host here.
    if (response.status !== 401 || response.headers.get("WWW-Authenticate") !== "Bearer") {
      return response;
    }
    const headers = new Headers(response.headers);
    const metadata = new URL("/.well-known/oauth-protected-resource", request.url);
    headers.set("WWW-Authenticate", `Bearer resource_metadata="${metadata}"`);
    return new Response(response.body, { status: 401, headers });
  },
} satisfies ExportedHandler<Env>;
